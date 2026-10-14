"""Two-step patching: authorise one exact span, then replace it.

## The gap this closes

`edit_file` takes `old_string` and `new_string` and replaces the first match. That is fine for a human who has
just read the file and is looking at it. For a model working from context, it has three failure modes that all
end the same way — a wrong edit that looks successful:

* **the span matches more than once.** The wrong occurrence is replaced.
* **the span does not match at all**, because the model is working from a slightly different version. Depending
  on the tool, that is either an error (fine) or a silent no-op (not fine).
* **the span is empty**, which some tools treat as "append" and others as "replace everything".

OpenClaw's `skill_workshop` splits this into two tool calls, verified in `skill-workshop-tool-schema.ts`:
`prepare_patch` "authorizes **one exact non-empty span** and returns bounded context", with "only one prepared
span active per skill", and `patch` requires a prior prepare. The two-step shape is what makes the guarantees
possible:

* `prepare` refuses a span that does not match **exactly once**, so ambiguity is caught while the model can still
  see the file and fix it, rather than after a write.
* `prepare` returns **bounded context** around the match — enough for the model to confirm it found the right
  place, not the whole file, which would defeat the point of a targeted edit.
* `patch` re-verifies against the content the prepare was made from. If the file changed in between (another
  process, another agent, a formatter), the patch is refused rather than applied to text that has moved.

The revision check is the same compare-and-swap idea as the skill proposal's `expected_revision_hash`, applied
per-file.

## What is dropped

Their `prepare_patch`/`patch` are actions of one large `skill_workshop` tool with a policy-selected action subset
and approval hooks. Here they are two functions, because Myriaxon's tool surface is small enough that one tool per
verb is clearer than one tool with a discriminator and a policy table.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

# How much context a prepare returns around the match. Enough to confirm the right occurrence, small enough that
# a prepare is not a way to read the whole file.
CONTEXT_CHARS = 240


@dataclass
class PreparedPatch:
    """One authorised span, awaiting a replacement."""

    path: str
    old_string: str
    occurrences: int
    before_context: str
    after_context: str
    file_hash: str
    prepared_at: float

    def render(self) -> str:
        lines = [
            f"  prepared: {self.path}",
            f"      matched exactly once",
            f"      ...{self.before_context}[[SPAN]]{self.after_context}...",
            f"      file revision {self.file_hash[:12]}",
        ]
        return "\n".join(lines)


@dataclass
class PatchResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    prepared: PreparedPatch | None = None

    def render(self) -> str:
        lines: list[str] = []
        if self.prepared is not None:
            lines.append(self.prepared.render())
        for note in self.notes:
            lines.append(f"      {note}")
        for error in self.errors:
            lines.append(f"      error: {error}")
        return "\n".join(lines)


def file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path: Path) -> tuple[str, str]:
    try:
        return path.read_text(encoding="utf-8"), ""
    except OSError as exc:
        return "", f"cannot read {path}: {exc}"
    except UnicodeDecodeError:
        return "", f"{path} is not UTF-8 text"


def prepare(path: str | Path, old_string: str) -> PatchResult:
    """Authorise one exact occurrence of `old_string`, or explain why it cannot be.

    Refuses four cases, each with a message that says what to do instead — a prepare that fails without telling
    the model how to fix it produces another attempt with the same defect.
    """
    result = PatchResult(ok=False)
    target = Path(path)

    if not old_string:
        result.errors.append(
            "old_string is empty. An empty search string matches everywhere, so it cannot identify a span. "
            "Supply the exact text you want to replace."
        )
        return result

    if not target.is_file():
        result.errors.append(f"{target} is not a file")
        return result

    text, problem = _read(target)
    if problem:
        result.errors.append(problem)
        return result

    occurrences = text.count(old_string)
    if occurrences == 0:
        # The most useful thing to say here is *why* it did not match, because a near-miss is usually whitespace.
        hint = ""
        collapsed = old_string.strip()
        if collapsed and collapsed in text:
            hint = " The text matches once surrounding whitespace is ignored -- check leading indentation."
        elif old_string.strip() in (line.strip() for line in text.splitlines()):
            hint = " A line matches once stripped -- check indentation and trailing spaces."
        result.errors.append(
            f"old_string does not appear in {target}.{hint} Re-read the file and use the exact text."
        )
        return result
    if occurrences > 1:
        result.errors.append(
            f"old_string appears {occurrences} times in {target}. It must match exactly once, or the wrong "
            "occurrence would be replaced. Include more surrounding text to make it unique."
        )
        return result

    index = text.index(old_string)
    result.ok = True
    result.prepared = PreparedPatch(
        path=str(target),
        old_string=old_string,
        occurrences=occurrences,
        before_context=text[max(0, index - CONTEXT_CHARS):index],
        after_context=text[index + len(old_string):index + len(old_string) + CONTEXT_CHARS],
        file_hash=file_hash(text),
        prepared_at=__import__("time").time(),
    )
    result.notes.append("call patch with this exact old_string to apply it")
    return result


def patch(prepared: PreparedPatch, new_string: str, *, allow_drift: bool = False) -> PatchResult:
    """Replace the prepared span, refusing if the file moved underneath it.

    The revision check is the point of the two-step shape. Between `prepare` and `patch` the file may have been
    edited by another process -- or by another agent working the same repository -- and applying the patch anyway
    would replace text the author never saw.
    """
    result = PatchResult(ok=False, prepared=prepared)
    target = Path(prepared.path)

    if not target.is_file():
        result.errors.append(f"{target} no longer exists")
        return result

    text, problem = _read(target)
    if problem:
        result.errors.append(problem)
        return result

    if file_hash(text) != prepared.file_hash and not allow_drift:
        result.errors.append(
            f"{target} changed after the patch was prepared. Re-read it and prepare again, so the replacement "
            "is made against text you have seen."
        )
        return result

    occurrences = text.count(prepared.old_string)
    if occurrences != 1:
        result.errors.append(
            f"the prepared span now appears {occurrences} time(s) in {target}; it must appear exactly once"
        )
        return result

    from .atomic_io import atomic_write_text

    updated = text.replace(prepared.old_string, new_string, 1)
    try:
        atomic_write_text(target, updated)
    except OSError as exc:
        result.errors.append(f"could not write {target}: {exc}")
        return result

    result.ok = True
    result.notes.append(f"replaced {len(prepared.old_string)} chars with {len(new_string)} in {target}")
    if allow_drift:
        result.notes.append("applied despite the file having changed since it was prepared")
    return result


def edit(path: str | Path, old_string: str, new_string: str, *, allow_drift: bool = False) -> PatchResult:
    """`prepare` then `patch` in one call, for a caller that does not need the confirmation step.

    Provided because most edits do not benefit from the two-step form, and forcing it everywhere would make the
    safe path the tedious one. The guarantees still hold: exactly-once or refuse, non-empty or refuse.
    """
    prepared_result = prepare(path, old_string)
    if not prepared_result.ok or prepared_result.prepared is None:
        return prepared_result
    return patch(prepared_result.prepared, new_string, allow_drift=allow_drift)
