import hashlib
import json
import os
import time

from . import jsonl_log
from .constants import AUDIT_LOG_FILE

_REDACT_KEYS = {"api_key", "apikey", "password", "token", "secret", "authorization"}
_STRING_TRUNCATE_LEN = 500


def _redact(value):
    if isinstance(value, dict):
        return {k: ("<redacted>" if k.lower() in _REDACT_KEYS else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str) and len(value) > _STRING_TRUNCATE_LEN:
        return value[:_STRING_TRUNCATE_LEN] + "...(truncated)"
    return value


def _identity(ctx) -> dict:
    """Who acted: the run, the surface, and the agent.

    Added because an audit trail that cannot name the actor is not an audit trail. The previous
    entries recorded `ts/cwd/tool/args/is_error` and nothing else, and `mission_control` said so in
    as many words -- "there is no run to attribute these rows to" -- which made the log useless for
    the one question an audit log exists to answer: what did THIS agent do?

    Read defensively from the context, because the same function serves a session turn, a Locus
    wake-up and a subagent, and a field missing on one of them must degrade to an empty string
    rather than break the tool call being recorded.
    """
    if ctx is None:
        return {}
    out: dict = {}
    try:
        capability = getattr(ctx, "run_capability", None)
        if capability is not None:
            run_id = str(getattr(capability, "run_id", "") or "")
            if run_id:
                out["run"] = run_id
            kind = str(getattr(capability, "name", "") or "")
            if kind:
                out["run_kind"] = kind
        # The Locus identity, when this turn belongs to one -- the same marker the completion-authority
        # gate reads, so the log and the gate agree about who is acting.
        source = str(getattr(ctx, "locus_run_source", "") or "")
        if source:
            out["source"] = source
        locus_id = str(getattr(ctx, "locus_id", "") or "")
        if locus_id:
            out["locus"] = locus_id
        # Subagents are marked on their own context; recording it separates "the agent did this" from
        # "a delegate of the agent did this", which is the difference between two very different reviews.
        if getattr(ctx, "_in_subagent", False):
            out["delegate"] = True
    except Exception:  # noqa: BLE001
        # Identity is best-effort. A context whose attributes raise must still produce a log entry --
        # losing the record of what happened is worse than losing the name of who did it, and this
        # function sits inside the tool-dispatch path where an exception would fail the tool call it
        # is supposed to be recording.
        return out
    return out


def _tail_state() -> tuple[str, int]:
    """The previous entry's chain hash, and the byte offset of the last COMPLETE line.

    Delegates to `jsonl_log` so this module and `usage.py` cannot drift: the SAME torn-tail bug was
    found in both, on the same day, by the same test shape -- a crash mid-write leaves a partial line
    with no trailing newline, and the next append concatenates onto the fragment, producing two JSON
    objects on one line that every reader then skips. The entry after the torn line was lost entirely.

    A rule that must be remembered in two places is a rule that will be forgotten in one of them.
    """
    try:
        previous, offset = jsonl_log.tail_state(AUDIT_LOG_FILE)
    except Exception:  # noqa: BLE001
        return "", 0
    chain = (previous or {}).get("chain")
    return (chain if isinstance(chain, str) else ""), offset


def log_tool_call(name: str, tool_input: dict, is_error: bool, ctx=None) -> None:
    """Best-effort JSONL audit trail of every tool call -- when, where, WHO, what, and the outcome.

    Never raises: an audit-log failure must never block the tool call it is recording. This is a
    lightweight governance/audit log, not a full OpenTelemetry trace -- there is no collector in this
    deployment, but a JSONL file under ~/.myriaxon/ is enough for after-the-fact review.

    Each entry carries a `chain` hash over its own content and the previous entry's hash, which makes
    retrospective edits detectable: changing or removing a row breaks every hash after it. That is
    tamper-EVIDENT rather than tamper-proof -- anyone with write access can rebuild the chain -- and
    it is worth having because it turns a silent edit into an obvious one.
    """
    try:
        AUDIT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.time(),
            "cwd": os.getcwd(),
            "tool": name,
            "args": _redact(tool_input),
            "is_error": is_error,
        }
        entry.update(_identity(ctx))
        previous, safe_offset = _tail_state()
        entry["prev"] = previous
        entry["chain"] = hashlib.sha256(
            json.dumps({**entry, "chain": None}, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:32]
        mode = "r+" if AUDIT_LOG_FILE.exists() else "w+"
        with AUDIT_LOG_FILE.open(mode, encoding="utf-8") as f:
            # Rewind over any torn tail so this entry starts on its own line. See `_tail_state`.
            f.seek(safe_offset)
            f.truncate()
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:  # noqa: BLE001
        # Deliberately broad. This runs inside tool dispatch, so ANY failure here would surface as a
        # failed tool call -- an audit log that breaks the work it records is worse than no audit log.
        # Measured: an `OSError`-only guard let a context whose attribute raises escape and abort the
        # call, which is exactly the failure this must never cause.
        pass
