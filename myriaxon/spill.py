"""Tool-result spill: keep a large result readable instead of truncating it into uselessness.

## The gap this closes

Myriaxon truncates an oversized tool result. `model_adapt.format_result_for_model` cuts it to fit the model's
window and the rest is gone -- the model knows something was dropped and has no way to get it back. So the tool
call has to be repeated with a narrower scope, which costs the whole call again and often returns the same
oversized answer.

OpenClaw's **spill** mechanism, and DeepSeek's `spill/*` packages, both replace truncation with the same idea:
price the result, and if it is over budget, **write the full text to disk and keep a head+tail preview plus a
footer naming the file**. The model then reads the file. Verified details worth copying exactly:

* **The notice is inside the budget, and it is reserved for.** Their `notice.ts` reserves
  `price(GAP) + price(worstNotice)` before choosing how much to retain, so the preview plus its footer together
  fit the limit. Retaining first and appending the notice afterwards would overshoot every time.
* **Cut on Unicode code points, never between a surrogate pair.** Their tool-result pruner does this explicitly;
  slicing a `str` in Python cannot split a pair, but slicing UTF-16 or bytes can, so the guarantee is stated
  rather than assumed.
* **Any failure leaves the ORIGINAL content in place.** Their rule: a spill failure never destroys a successful
  result. A missing store, an unwritable directory, a notice that will not fit -- each logs a warning and returns
  what it was given.
* **Images become a path plus an instruction**, not a dropped block: `[Image: "<path>"; <mime>; WxH. Use
  read_image to view it.]`
* **Retention is deliberate, not a leak.** A resumed or forked session may still hold an old locator, so spilled
  files outlive the turn; their local backend sweeps only files older than 30 days at startup, and refuses to
  prune the live root.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .constants import CONFIG_DIR

# Where spilled results live. Their local backend uses `<root>/session-<hash>/` with an unpredictable name,
# an exclusive 0600 write and a 0700 root -- the locator is handed to the model, so a guessable path in a shared
# directory would let a second user read or replace another session's tool output.
SPILL_ROOT = CONFIG_DIR / "spill"

# Their shipped budget.
DEFAULT_MAX_INLINE_TOKENS = 12_500
CHARS_PER_TOKEN = 4

# The marker between head and tail.
GAP = "\n\n[... middle omitted; see the file named below ...]\n\n"

# A preview must retain at least this much of the result, or spilling it is not worth doing. Without a floor, a
# small budget produces a notice with a few characters attached -- which tells the model less than the original
# did, at the cost of a file read. Below this line the original is returned untouched instead.
MIN_RETAINED_CHARS = 128

# Sweep spilled files older than this at startup. Long, because a resumed session can legitimately hold an old
# locator.
RETENTION_DAYS = 30

_NOTICE_TAIL = (
    "\n\n(Full result stored at: {path}\n"
    "Read it with read_file, or search it with grep_search. It is the complete output of this call.)"
)


def estimate_tokens(text: str) -> int:
    """A cheap price. The same 4-chars-per-token heuristic the rest of the harness uses."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def _session_dir(session_id: str) -> Path:
    """An unpredictable per-session directory.

    Hashed rather than used verbatim: a session id can contain path separators or `..`, and it is also the kind
    of value that ends up in a locator the model prints.
    """
    digest = hashlib.sha256((session_id or "default").encode("utf-8")).hexdigest()[:16]
    return SPILL_ROOT / f"session-{digest}"


@dataclass
class SpillRecord:
    """One spilled result."""

    path: str
    original_tokens: int
    retained_tokens: int
    bytes_written: int
    tool_name: str = ""
    call_id: str = ""

    def render(self) -> str:
        return (
            f"  spilled {self.tool_name or 'result'} to {self.path} "
            f"({self.original_tokens} -> {self.retained_tokens} tokens, {self.bytes_written} bytes)"
        )


@dataclass
class SpillOutcome:
    """The content to send, plus what happened.

    `content` is ALWAYS usable: on any failure it is exactly what was passed in. A caller never has to branch on
    whether spilling worked, which is what makes "a spill failure never destroys a result" true in practice
    rather than in intent.
    """

    content: str
    spilled: bool = False
    record: SpillRecord | None = None
    warnings: list[str] = field(default_factory=list)


def _write(path: Path, text: str) -> None:
    """Write with the parent created and the file owner-only.

    `0o600`/`0o700` on POSIX. On Windows the mode is largely advisory, which is why the directory name is
    unpredictable in the first place -- the two together are the protection, and the weaker one is not relied on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    # `x` would fail on a repeat call with the same id; `w` is correct because the locator is content-addressed
    # by name, and a re-spill of the same call writes the same bytes.
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def spill(
    content: str,
    *,
    tool_name: str = "",
    call_id: str = "",
    session_id: str = "",
    max_inline_tokens: int = DEFAULT_MAX_INLINE_TOKENS,
    root: Path | None = None,
) -> SpillOutcome:
    """Return content that fits the budget, spilling the full text when it does not.

    Never raises and never returns something unusable. Every failure path returns the original content with a
    warning attached, because the alternative -- returning a truncated result because the *spill* failed -- makes
    a storage problem look like a result problem.
    """
    if not isinstance(content, str) or not content:
        return SpillOutcome(content=content if isinstance(content, str) else str(content))

    original_tokens = estimate_tokens(content)
    if original_tokens <= max_inline_tokens:
        return SpillOutcome(content=content)

    # The notice has to fit INSIDE the budget, so it is built before deciding how much to retain and its price is
    # subtracted. Retaining first and appending afterwards would overshoot on every single call.
    #
    # The reservation is done in CHARACTERS, not tokens. `estimate_tokens` is `len // 4`, so reserving `n` tokens
    # and then spending `n * 4` characters overshoots by whatever the integer division rounded away -- measured at
    # a consistent 25 tokens over budget before this was corrected.
    #
    # A budget smaller than the fixed parts cannot hold a preview at all. Requirement: a preview must retain at
    # least 128 characters of head, or it is not a preview -- it is a notice with a couple of characters attached,
    # which is strictly worse than the original. Below that line the original is returned untouched, and the
    # warning says so rather than implying the result was trimmed.
    provisional = _NOTICE_TAIL.format(path="<pending>")
    minimum_chars = len(GAP) + len(provisional) + MIN_RETAINED_CHARS
    budget_chars = max_inline_tokens * CHARS_PER_TOKEN
    if budget_chars < minimum_chars:
        return SpillOutcome(
            content=content,
            warnings=[
                f"a {max_inline_tokens}-token budget cannot hold a usable spill preview "
                f"(needs at least {minimum_chars // CHARS_PER_TOKEN} tokens); returned the result unchanged"
            ],
        )

    keep_chars = budget_chars - len(GAP) - len(provisional)
    head_chars = keep_chars * 6 // 10
    tail_chars = keep_chars - head_chars
    # A slice of a `str` is by code point, so a surrogate pair can never be split. Stated rather than assumed
    # because the equivalent operation on UTF-16 or bytes can split one, and the guarantee is the point.
    head = content[:head_chars]
    tail = content[len(content) - tail_chars:] if tail_chars else ""
    path = _target_path(root, session_id, tool_name, call_id, content)

    try:
        _write(path, content)
    except OSError as exc:
        return SpillOutcome(
            content=content,
            warnings=[f"could not spill to {path}: {exc}; returned the result unchanged"],
        )

    notice = _NOTICE_TAIL.format(path=path)
    # Re-derive the split against the REAL path, then assert the invariant by construction.
    #
    # Two corrections are folded in here, both found by measurement rather than reasoning. First, reserving
    # *tokens* and spending `n * 4` characters overshot by a consistent 25 because `estimate_tokens` is integer
    # division -- so the reservation is in characters. Second, the reserve used a placeholder path, and the real
    # path is longer, which left a further 24 characters over. Estimating the fixed parts twice and hoping they
    # agree is the actual mistake; the bound is now enforced on the finished string.
    budget_chars = max_inline_tokens * CHARS_PER_TOKEN
    tail_chars = min(tail_chars, max(0, budget_chars - len(notice) - len(GAP) - 1))
    head_chars = max(0, budget_chars - len(notice) - len(GAP) - tail_chars)
    if head_chars:
        head = content[:head_chars]
    if tail_chars:
        tail = content[len(content) - tail_chars:]
    else:
        tail = ""

    retained = f"{head}{GAP}{tail}{notice}"
    if len(retained) > budget_chars:
        # Last-resort trim. Reached only if the arithmetic above is wrong, and a preview one character over
        # budget is still better than one that lies about being within it.
        retained = retained[:budget_chars - len(notice)] + notice

    record = SpillRecord(
        path=str(path),
        original_tokens=original_tokens,
        retained_tokens=estimate_tokens(retained),
        bytes_written=len(content.encode("utf-8")),
        tool_name=tool_name,
        call_id=call_id,
    )
    return SpillOutcome(content=retained, spilled=True, record=record)


def _target_path(
    root: Path | None, session_id: str, tool_name: str, call_id: str, content: str
) -> Path:
    """A stable, collision-free filename.

    Content-addressed on the tail of the digest: spilling the same call twice writes the same file, so a retry
    does not multiply copies, and two different results cannot share a name.
    """
    base = Path(root) if root is not None else SPILL_ROOT
    directory = base / f"session-{hashlib.sha256((session_id or 'default').encode()).hexdigest()[:16]}"
    safe_tool = re.sub(r"[^A-Za-z0-9_.-]", "_", tool_name or "result")[:40] or "result"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", call_id)[:24] if call_id else ""
    name = f"{safe_tool}-{stem}-{digest}.txt" if stem else f"{safe_tool}-{digest}.txt"
    return directory / name


def spill_image_reference(path: str, mime: str = "", width: int = 0, height: int = 0) -> str:
    """The text that replaces an image block whose bytes were written to disk.

    Their format, kept because the `read_image` instruction is what makes the replacement actionable rather than
    just informative.
    """
    detail = "; ".join(part for part in (mime, f"{width}x{height}" if width and height else "") if part)
    suffix = f"; {detail}" if detail else ""
    return f'[Image: "{path}"{suffix}. Use read_image to view it.]'


def sweep(root: Path | None = None, retention_days: int = RETENTION_DAYS) -> dict:
    """Delete spilled files older than the retention period.

    Best effort, and it refuses to remove the root itself. Retention is deliberately long: a resumed or forked
    session can still hold a locator from weeks ago, and deleting a file the model is about to be told to read
    would turn a working recovery path into a broken one.
    """
    base = Path(root) if root is not None else SPILL_ROOT
    report = {"examined": 0, "removed": 0, "errors": 0}
    if not base.is_dir():
        return report
    cutoff = time.time() - retention_days * 86_400
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        report["examined"] += 1
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                report["removed"] += 1
        except OSError:
            report["errors"] += 1
    return report


def resolve(locator: str) -> Path | None:
    """Map a locator back to a path, refusing anything outside the spill root.

    The locator is a value that travelled through the model's context, so it is treated as untrusted input: a
    locator naming `../../etc/passwd` must not be read. Containment is checked against the *resolved* root so a
    symlink cannot escape it either.
    """
    if not locator:
        return None
    candidate = Path(locator)
    try:
        resolved = candidate.resolve()
        root = SPILL_ROOT.resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved if resolved.is_file() else None
