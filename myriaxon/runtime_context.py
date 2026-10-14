"""Runtime context: volatile per-step facts as a sourced message, so the cached prefix stays stable.

## The gap this closes

Myriaxon puts some volatile facts in the **system prompt** — the working directory, the mode, the date. Anything
volatile in the system prompt is expensive out of proportion to its size: the system prompt is the head of the
request, so changing one character of it invalidates the provider's cached prefix for the whole conversation. A
session that ran for an hour re-bills every token because the clock moved.

DeepSeek's `interaction/user-approval` and its base mount handle this properly, and their snapshot test asserts
the exact property worth copying (`sdk.snapshot.ts`, `runtimeContext: {includes, excludes}`):

* volatile per-step facts — the time, the sandbox mode, the approval policy, the terminal — are a **sourced user
  message appended after the retained history**, never written into the request header;
* on a change, a **new full snapshot** is appended rather than the earlier one being rewritten, so the stable
  prefix stays byte-identical and remains cacheable;
* the snapshot test asserts **exactly one such message** exists, that it contains the clauses, and that **the same
  clauses do NOT appear in the system prompt**. That last assertion is the one that keeps the design honest: it is
  easy to add a message and forget to remove the copy from the prompt.

## Why appending rather than mutating

A message list is append-only in practice: the provider caches a prefix of it, and any rewrite before the end
discards that cache. Appending a fresh snapshot costs one small message; rewriting the previous snapshot costs the
whole conversation at full price. That asymmetry is the entire reason for the design, and it is why the class here
never edits a snapshot it has already emitted.

## What is dropped

Their tmux-pane and terminal-state fields, the client/server split, and the policy-change event plumbing. What is
kept is the shape: a small set of named clauses, a version, a sourced message, append-on-change.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

# Where a snapshot message says it came from. Their rule: a sourced message, so a reader can tell harness-supplied
# context from something the user or a tool said.
SOURCE = "runtime-context"
SOURCE_MARKER = "[runtime context]"

# A closed set of clause names. Free-form keys would let the snapshot grow without bound and make the test that
# asserts "the same clauses do not appear in the system prompt" impossible to write.
CLAUSE_TIME = "time"
CLAUSE_MODE = "mode"
CLAUSE_WORKSPACE = "workspace"
CLAUSE_SANDBOX = "sandbox"
CLAUSE_APPROVAL = "approval"
CLAUSE_BUDGET = "budget"
CLAUSES = (CLAUSE_TIME, CLAUSE_MODE, CLAUSE_WORKSPACE, CLAUSE_SANDBOX, CLAUSE_APPROVAL, CLAUSE_BUDGET)


@dataclass
class RuntimeContext:
    """The volatile facts for right now.

    `time` is quantised deliberately. A per-second timestamp would change the message on every step and re-send
    the snapshot constantly for no benefit; a per-minute one changes once a minute, which is the resolution a
    model actually reasons about.
    """

    mode: str = ""
    workspace: str = ""
    sandbox: str = ""
    approval: str = ""
    budget: str = ""
    when: float = field(default_factory=time.time)

    def clauses(self) -> dict[str, str]:
        """The non-empty clauses, in the fixed order. Empty ones are omitted rather than rendered blank: a
        snapshot full of "mode: (unknown)" trains the reader to skim it."""
        values = {
            CLAUSE_TIME: time.strftime("%Y-%m-%d %H:%M", time.localtime(self.when)),
            CLAUSE_MODE: self.mode,
            CLAUSE_WORKSPACE: self.workspace,
            CLAUSE_SANDBOX: self.sandbox,
            CLAUSE_APPROVAL: self.approval,
            CLAUSE_BUDGET: self.budget,
        }
        return {name: value for name, value in values.items() if value}

    def render(self) -> str:
        """The message body. Every clause on its own line, prefixed with a stable marker."""
        parts = [SOURCE_MARKER]
        for name, value in self.clauses().items():
            parts.append(f"{name}: {value}")
        return "\n".join(parts)

    def body(self) -> str:
        return self.render()


class RuntimeContextTracker:
    """Emits a snapshot when something changed, and stays silent when nothing did.

    The silence is the point. A tracker that appended on every step would grow the history without bound and
    re-send the same facts; one that appends only on a change costs a handful of small messages per session.
    """

    def __init__(self, *, time_bucket_seconds: int = 60) -> None:
        self.time_bucket_seconds = max(1, int(time_bucket_seconds))
        self._last_rendered: str = ""
        self.appended = 0
        self.skipped = 0

    def _bucket(self, when: float) -> float:
        return float(int(when // self.time_bucket_seconds) * self.time_bucket_seconds)

    def observe(self, context: RuntimeContext) -> str | None:
        """The message text to append, or None when nothing that matters has changed.

        The time is quantised before comparison, so two observations a second apart in the same minute are the
        same observation. Without that the tracker would append every step, which is the failure it exists to
        avoid.
        """
        quantised = RuntimeContext(
            mode=context.mode,
            workspace=context.workspace,
            sandbox=context.sandbox,
            approval=context.approval,
            budget=context.budget,
            when=self._bucket(context.when),
        )
        rendered = quantised.render()
        if rendered == self._last_rendered:
            self.skipped += 1
            return None
        self._last_rendered = rendered
        self.appended += 1
        return rendered

    def message(self, context: RuntimeContext) -> dict | None:
        """The snapshot as a sourced message, or None when nothing changed.

        Anthropic-shaped on purpose: this is appended to the transcript, and the two other providers convert that
        shape at their own boundary rather than each having a snapshot format of its own.
        """
        body = self.observe(context)
        if body is None:
            return None
        return {"role": "user", "content": body, "source": SOURCE}

    def reset(self) -> None:
        """Forget what was last sent, so the next observation appends regardless.

        For a genuine discontinuity -- a resumed session, a compaction that rewrote the history. Without it the
        tracker would believe the facts were already communicated when the message carrying them is gone.
        """
        self._last_rendered = ""


def is_runtime_context_message(message: Any) -> bool:
    """Whether a transcript entry is a runtime-context snapshot.

    Checked by the source field first and the marker second, because a message that has been through a provider
    conversion may have lost its extra keys but not its text.
    """
    if not isinstance(message, dict):
        return False
    if message.get("source") == SOURCE:
        return True
    content = message.get("content")
    return isinstance(content, str) and content.startswith(SOURCE_MARKER)


def count_snapshots(messages: list) -> int:
    """How many snapshots a transcript holds. Their snapshot test asserts exactly one after a single change."""
    return sum(1 for message in messages if is_runtime_context_message(message))


def last_snapshot(messages: list) -> str:
    """The most recent snapshot's text, or "". For a test and for a status line."""
    for message in reversed(messages or []):
        if is_runtime_context_message(message):
            content = message.get("content")
            return content if isinstance(content, str) else ""
    return ""


def clauses_present(text: str, clauses: list[str] | None = None) -> dict[str, bool]:
    """Which clauses a piece of text carries *as clauses*.

    Matched only in clause position -- at the start of a line, or right after the marker -- and never as a bare
    substring. A substring check produced a false positive on live text: the system prompt contains the sentence
    "Work one step at a time: inspect what you need", and `"time:" in prompt` matched the `time:` inside it. A
    detector that reports a leak where there is none is worse than no detector, because the next real leak is
    dismissed as the same false alarm.
    """
    wanted = clauses or list(CLAUSES)
    body = text or ""
    found: dict[str, bool] = {}
    for name in wanted:
        # `^name:` on any line, or `[runtime context]` followed by it. `re.MULTILINE` so every line is a
        # candidate, which is how a rendered snapshot writes them.
        pattern = re.compile(rf"^(?:{re.escape(SOURCE_MARKER)}\s*)?{re.escape(name)}:", re.MULTILINE)
        found[name] = bool(pattern.search(body))
    return found


def volatile_clauses_in_system_prompt(prompt: str) -> list[str]:
    """Any volatile clause present in a system prompt. Their test asserts this list is empty.

    Returns the names rather than a bool so a failure names what leaked, which is the difference between "the
    prompt is unstable" and "the prompt contains `workspace:`".
    """
    return [name for name, present in clauses_present(prompt).items() if present]


def append_if_changed(
    messages: list,
    context: RuntimeContext,
    tracker: RuntimeContextTracker | None = None,
) -> bool:
    """Append a snapshot to `messages` when it changed. Returns whether one was appended.

    Appends rather than edits: a message list is append-only in practice because the provider caches a prefix of
    it, so rewriting an earlier snapshot discards that cache for the whole conversation. The cost of appending is
    one small message; the cost of rewriting is every token re-billed.
    """
    tracker = tracker or RuntimeContextTracker()
    message = tracker.message(context)
    if message is None:
        return False
    messages.append(message)
    return True
