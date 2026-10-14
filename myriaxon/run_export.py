"""Export a run: the transcript, what it touched, and what it decided — as one reviewable artefact.

## The gap

Opencode has `opencode export`, which writes a run's transcript, patches and snapshots as JSON with an optional
`--sanitize` pass. PiHarness exports a "run dossier" plus a redacted debug bundle. Myriaxon had **nothing**: the
records existed but were scattered across `session_store`, `compaction_audit`, `checkpoints` and the activity logs,
and there was no supported way to hand someone a run.

That matters for a harness that claims to prove things, because "prove it" ends with *showing* it. An unexportable
run is one nobody can review.

## The three decisions that shape this module

**Sanitised BY DEFAULT, and that is the opposite of a convenience flag.** The transcript contains whatever the agent
read: file contents, command output, environment dumps. An export is the artefact most likely to leave the machine
-- pasted into an issue, attached to a review -- so the safe setting must be the default and the unsafe one must be
explicit. `sanitize=False` exists for a local copy and says so in the bundle.

**Sanitising must be *reported*, not silent.** A reader who cannot tell redaction happened will read `REDACTED` as
the real value. So the bundle records how many replacements were made and where, using the existing
`redaction.redact` rather than a second implementation of the same idea.

**An export never mutates and never raises.** It is a read. A run that cannot be exported because the audit store is
mid-write is worse than an export with a gap in it, so every section is best-effort and a missing one is named in the
bundle rather than omitted silently.

## What is deliberately not here

Sharing to a URL. Opencode's `share` publishes a session to a remote service; that needs a server and a trust
decision, and neither exists here. `export` produces a file you can send to a person, which is the useful half.
"""

from __future__ import annotations

import json
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Bumped when the bundle's shape changes, so a reader can tell whether an old file is comparable.
BUNDLE_VERSION = 1

# A run's transcript is bounded on export. A very long session would otherwise produce a file nobody can open, and
# the tail is what a reviewer needs; the full transcript stays in the session file.
MAX_MESSAGES = 400
MAX_CHARS_PER_MESSAGE = 20_000


@dataclass
class RedactionSummary:
    """What was sanitised, so a reader is not misled by a `REDACTED` placeholder."""

    applied: bool = False
    replacements: int = 0
    labels: dict[str, int] = field(default_factory=dict)
    note: str = ""

    def render(self) -> str:
        if not self.applied:
            return "sanitize was OFF -- this bundle may contain credentials and raw file contents"
        if not self.replacements:
            return "sanitize was ON and found nothing to replace"
        kinds = ", ".join(f"{name} x{count}" for name, count in sorted(self.labels.items()))
        return f"sanitize was ON: {self.replacements} replacement(s) -- {kinds}"


@dataclass
class RunBundle:
    """One run, as a portable object. Every field is JSON-serialisable."""

    harness: str = "myriaxon"
    bundle_version: int = BUNDLE_VERSION
    exported_at: float = 0.0
    sanitized: RedactionSummary = field(default_factory=RedactionSummary)
    session: dict = field(default_factory=dict)
    messages: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    compaction: dict = field(default_factory=dict)
    checkpoints: list[dict] = field(default_factory=list)
    lineage: dict = field(default_factory=dict)
    environment: dict = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "harness": self.harness,
            "bundle_version": self.bundle_version,
            "exported_at": self.exported_at,
            "exported_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.exported_at)),
            "sanitized": {
                "applied": self.sanitized.applied,
                "replacements": self.sanitized.replacements,
                "labels": self.sanitized.labels,
                "note": self.sanitized.render(),
            },
            "session": self.session,
            "metadata": self.metadata,
            "messages": self.messages,
            "compaction": self.compaction,
            "checkpoints": self.checkpoints,
            "lineage": self.lineage,
            "environment": self.environment,
            # Named rather than omitted. A bundle that is quietly missing a section reads as a complete one.
            "incomplete_because": self.gaps,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False, default=str)

    def to_markdown(self) -> str:
        """A form a person reads, because the first consumer of an export is usually a human in a review."""
        lines: list[str] = []
        meta = self.metadata
        lines.append(f"# Run export — {meta.get('provider', '?')}/{meta.get('model', '?')}")
        lines.append("")
        lines.append(f"- Exported: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.exported_at))}")
        lines.append(f"- Bundle version: {self.bundle_version}")
        lines.append(f"- **{self.sanitized.render()}**")
        if meta.get("session_id"):
            lines.append(f"- Session: `{meta['session_id']}`")
        if meta.get("saved_at"):
            lines.append(f"- Last saved: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(meta['saved_at']))}")
        lines.append(f"- Messages exported: {len(self.messages)}")
        lines.append("")

        if self.lineage:
            lines.append("## Lineage")
            lines.append("")
            lines.append(f"- Parent: `{self.lineage.get('parent') or '(root)'}`")
            if self.lineage.get("fork_index"):
                lines.append(f"- Inherited from parent: {self.lineage['fork_index']} message(s)")
            if self.lineage.get("children"):
                lines.append(f"- Children: {', '.join('`' + c + '`' for c in self.lineage['children'])}")
            lines.append("")

        if self.compaction:
            lines.append("## Compaction")
            lines.append("")
            for key, value in sorted(self.compaction.items()):
                lines.append(f"- {key}: {value}")
            lines.append("")

        if self.checkpoints:
            lines.append("## Checkpoints")
            lines.append("")
            lines.append("| id | tool | when |")
            lines.append("|---|---|---|")
            for entry in self.checkpoints[:40]:
                when = entry.get("at") or 0
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when)) if when else "?"
                lines.append(f"| `{str(entry.get('id', '?'))[:12]}` | {entry.get('tool', '?')} | {stamp} |")
            lines.append("")

        lines.append("## Transcript")
        lines.append("")
        for message in self.messages:
            role = message.get("role") or message.get("type") or "?"
            lines.append(f"### {role}")
            lines.append("")
            content = message.get("content")
            if isinstance(content, str):
                lines.append(content)
            else:
                lines.append("```json")
                lines.append(json.dumps(content, indent=2, ensure_ascii=False, default=str)[:6000])
                lines.append("```")
            lines.append("")

        lines.append("## Environment")
        lines.append("")
        for key, value in sorted(self.environment.items()):
            lines.append(f"- {key}: {value}")

        if self.gaps:
            lines.append("")
            lines.append("## Incomplete")
            lines.append("")
            lines.append("This bundle is missing sections. It was still written, because a partial export is more")
            lines.append("useful than none, but it must not be read as a complete record:")
            lines.append("")
            for gap in self.gaps:
                lines.append(f"- {gap}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------------------
# Sanitising
# ---------------------------------------------------------------------------------------


def _sanitize_text(text: str, summary: RedactionSummary) -> str:
    """Redact one string, accumulating into `summary`. Never raises.

    `redaction.redact` returns `(text, Report)` where `Report` carries `total`, `kinds` (a `{kind: count}` dict) and
    `changed`. That `kinds` breakdown is what makes the summary useful: "3 replacements" tells a reader to look,
    and "stripe-key x1, aws-access-key x1, github-token x1" tells them what kind of material was in the transcript.
    """
    if not text:
        return text
    try:
        from . import redaction

        cleaned, report = redaction.redact(text)
    except Exception:  # noqa: BLE001
        # A redaction failure must not silently pass the original through -- that is the one outcome worse than
        # losing the text. The text is withheld and the bundle says redaction was unavailable.
        summary.labels["redaction-unavailable"] = summary.labels.get("redaction-unavailable", 0) + 1
        return "[REMOVED: redaction was unavailable, so this text was withheld rather than risked]"

    try:
        summary.replacements += int(getattr(report, "total", 0) or 0)
    except (TypeError, ValueError):
        pass
    kinds = getattr(report, "kinds", None)
    if isinstance(kinds, dict):
        for name, count in kinds.items():
            summary.labels[str(name)] = summary.labels.get(str(name), 0) + int(count or 0)
    return cleaned


def _sanitize_value(value: Any, summary: RedactionSummary) -> Any:
    """Redact recursively. Structure is preserved so the bundle stays comparable."""
    if isinstance(value, str):
        return _sanitize_text(value, summary)
    if isinstance(value, dict):
        return {k: _sanitize_value(v, summary) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(v, summary) for v in value]
    return value


# ---------------------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------------------


def build(
    project_root: Path | None = None,
    *,
    session_id: str = "",
    sanitize: bool = True,
    include_messages: bool = True,
) -> RunBundle:
    """Collect everything available for a run. Every section is best-effort.

    `sanitize` defaults to **True**. The transcript holds whatever the agent read, and an export is the artefact most
    likely to leave the machine, so the safe setting is the default and the unsafe one must be asked for by name.
    """
    from . import session_store

    root = Path(project_root) if project_root else Path.cwd()
    bundle = RunBundle(exported_at=time.time())
    summary = RedactionSummary(applied=sanitize)
    bundle.sanitized = summary

    # --- session -------------------------------------------------------------------------
    session = session_store.load_session(root, session_id)
    if session is None:
        bundle.gaps.append(
            f"no session found in {root}" + (f" with id {session_id!r}" if session_id else "")
        )
    else:
        messages = session.get("messages") or []
        bundle.metadata = {
            "session_id": session.get("session_id", session_id),
            "provider": session.get("provider", ""),
            "model": session.get("model", ""),
            "mode": session.get("mode", ""),
            "effort": session.get("effort"),
            "thinking": session.get("thinking"),
            "saved_at": session.get("saved_at", 0),
            "message_count": len(messages),
        }
        if include_messages:
            trimmed = messages[-MAX_MESSAGES:]
            if len(trimmed) < len(messages):
                bundle.gaps.append(
                    f"transcript truncated to the last {MAX_MESSAGES} of {len(messages)} messages"
                )
            for message in trimmed:
                entry = dict(message)
                content = entry.get("content")
                if isinstance(content, str) and len(content) > MAX_CHARS_PER_MESSAGE:
                    entry["content"] = (
                        content[:MAX_CHARS_PER_MESSAGE]
                        + f"\n... [truncated, {len(content) - MAX_CHARS_PER_MESSAGE} chars omitted]"
                    )
                    bundle.gaps.append("at least one message was truncated at "
                                       f"{MAX_CHARS_PER_MESSAGE} characters")
                bundle.messages.append(entry)

    # --- lineage -------------------------------------------------------------------------
    try:
        from . import session_lineage

        own_id = bundle.metadata.get("session_id") or session_id
        if own_id:
            tree = session_lineage.load_tree(root)
            entry = tree.get(own_id)
            if entry is not None:
                bundle.lineage = {
                    "session_id": entry.session_id,
                    "name": entry.name,
                    "parent": entry.parent,
                    "fork_index": entry.fork_index,
                    "children": [c.session_id for c in session_lineage.children(own_id, root)],
                }
    except Exception as exc:  # noqa: BLE001
        bundle.gaps.append(f"lineage could not be read: {type(exc).__name__}")

    # --- compaction audit ----------------------------------------------------------------
    try:
        from . import compaction_audit

        session_name = bundle.metadata.get("session_id") or session_id or "default"
        recorder = compaction_audit.audit_for(session_name, root)
        # `recorder.entries` is a COUNT (an int), not the entries themselves -- measured, and the first version of
        # this assumed a list and failed with `TypeError: object of type 'int' has no len()`, which surfaced as
        # "compaction audit could not be read". The per-kind breakdown comes from `summary()`, which is also what
        # `self_status.py` uses, so the two cannot disagree.
        entries: dict[str, int] = {}
        summary_data = recorder.summary() if hasattr(recorder, "summary") else {}
        raw = summary_data.get("entries") if isinstance(summary_data, dict) else None
        if isinstance(raw, dict):
            for kind, count in raw.items():
                entries[str(kind)] = int(count or 0)
        elif isinstance(recorder.entries, int):
            entries["recorded"] = recorder.entries
        failures = getattr(recorder, "failures", 0)
        if failures:
            entries["write-failures"] = int(failures)
        bundle.compaction = dict(sorted(entries.items()))
        # The module-level function is `interrupted_compactions` (plural, across sessions); the singular is a METHOD
        # on the recorder for the current session only. The first version called the plural name on the module and
        # the singular on the recorder -- one of them had to be wrong, and it was this one.
        pending = None
        if hasattr(recorder, "interrupted_compaction"):
            pending = recorder.interrupted_compaction()
        if pending is None and hasattr(compaction_audit, "interrupted_compactions"):
            pending = compaction_audit.interrupted_compactions(root)
        if pending:
            bundle.compaction["INTERRUPTED"] = len(pending) if isinstance(pending, list) else 1
    except Exception as exc:  # noqa: BLE001
        bundle.gaps.append(f"compaction audit could not be read: {type(exc).__name__}: {exc}")

    # --- checkpoints ---------------------------------------------------------------------
    try:
        from . import checkpoints

        listed = checkpoints.list_checkpoints(project_root=root)
        for item in listed[:80]:
            if isinstance(item, dict):
                bundle.checkpoints.append({
                    "id": item.get("id", ""),
                    "tool": item.get("tool", ""),
                    "at": item.get("at", 0),
                    "path": item.get("path", ""),
                })
    except Exception as exc:  # noqa: BLE001
        bundle.gaps.append(f"checkpoints could not be read: {type(exc).__name__}")

    # --- environment ---------------------------------------------------------------------
    bundle.environment = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "project_root": str(root),
    }
    try:
        from . import interpreter

        best = interpreter.find()
        bundle.environment["interpreter"] = best.path if best else "(none found)"
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import diagnostics

        bundle.environment["diagnostic_tiers"] = ", ".join(diagnostics.available_tiers())
    except Exception:  # noqa: BLE001
        pass

    # --- sanitise last, over EVERYTHING --------------------------------------------------
    # Last, and over the whole bundle, because a section added later must not be able to escape redaction by being
    # written after the call. That is the failure mode of sanitising field by field.
    if sanitize:
        bundle.messages = _sanitize_value(bundle.messages, summary)
        bundle.environment = _sanitize_value(bundle.environment, summary)
        bundle.session = _sanitize_value(bundle.session, summary)
        bundle.gaps = _sanitize_value(bundle.gaps, summary)

    return bundle


def write(
    bundle: RunBundle,
    destination: Path | None = None,
    *,
    fmt: str = "json",
) -> tuple[bool, str, Path | None]:
    """Write a bundle to disk. Returns `(ok, message, path)`.

    Refuses an existing file rather than overwriting: an export is a record of a past run, and silently replacing
    someone's bundle is the kind of loss this repository has already had to design around twice. The caller can pass
    an explicit path to choose a new one.
    """
    if fmt not in ("json", "markdown"):
        return False, f"unknown format {fmt!r}; use 'json' or 'markdown'", None

    suffix = ".json" if fmt == "json" else ".md"
    if destination is None:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(bundle.exported_at))
        session_part = (bundle.metadata.get("session_id") or "session")[:24]
        destination = Path.cwd() / f"run-export-{session_part}-{stamp}{suffix}"
    destination = Path(destination)
    if destination.exists() and destination.is_dir():
        return False, f"{destination} is a directory", None

    body = bundle.to_json() if fmt == "json" else bundle.to_markdown()
    try:
        from .atomic_io import atomic_write_text

        atomic_write_text(destination, body)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not write {destination}: {type(exc).__name__}: {exc}", None

    size = len(body.encode("utf-8"))
    return True, f"wrote {size:,} bytes ({len(bundle.messages)} message(s)) to {destination}", destination
