"""Context budget: estimate what is in the window, and act *before* the provider rejects the request.

Why this exists: on a small-window model, the failure mode is not "the answer is worse" -- it is a
hard API error mid-task, after the user has already paid for the tokens leading up to it. The
existing pruning only ran by *age* (tool results older than N turns), which does nothing when a
single `ls -R` fills the window on turn one.

Actions are graduated, because each one costs the user something different:

| usage | action | cost |
|---|---|---|
| >= warn_at | print a one-line notice | attention |
| >= prune_at | replace old tool results with placeholders | loses detail, keeps structure |
| >= compact_at | summarize older turns | loses detail, costs one model call |
| >= refuse_at | refuse and explain, instead of sending | the user must act |

Estimation is deliberately honest about being an estimate. A real tokenizer is used when one is
importable; otherwise a character heuristic is used and `confidence` says so, so a report can state
that it was working from an approximation rather than implying precision it does not have.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from .model_profiles import CHARS_PER_TOKEN_CJK, CHARS_PER_TOKEN_TEXT, ModelProfile

Action = Literal["ok", "warn", "prune", "compact", "refuse"]

_CJK_RE = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")


@dataclass
class TokenEstimate:
    tokens: int
    confidence: str  # "tokenizer" | "heuristic"
    method: str = ""


@dataclass
class BudgetState:
    """What one evaluation of the window found, and what should happen about it."""

    used_tokens: int
    window: int
    used_fraction: float
    action: Action
    headroom_tokens: int
    confidence: str
    breakdown: dict[str, int] = field(default_factory=dict)
    reason: str = ""

    @property
    def percent(self) -> int:
        return int(round(self.used_fraction * 100))

    def meter(self, width: int = 12) -> str:
        filled = max(0, min(width, int(round(self.used_fraction * width))))
        return "\u2588" * filled + "\u2591" * (width - filled)

    def to_dict(self) -> dict:
        return {
            "used_tokens": self.used_tokens,
            "window": self.window,
            "used_fraction": round(self.used_fraction, 4),
            "percent": self.percent,
            "action": self.action,
            "headroom_tokens": self.headroom_tokens,
            "confidence": self.confidence,
            "breakdown": self.breakdown,
            "reason": self.reason,
        }


def estimate_tokens(text: str) -> TokenEstimate:
    """Token count for a string, using a real tokenizer when one is available.

    `tiktoken` is not a dependency of this project and is not imported unless present; the
    character heuristic is the documented fallback and is accurate to roughly +/-15% on ordinary
    code and prose, which is more than sufficient for threshold decisions that fire at 60/75/85%.
    """
    if not text:
        return TokenEstimate(0, "heuristic", "empty")
    try:  # pragma: no cover - optional dependency, exercised only when installed
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        return TokenEstimate(len(encoding.encode(text, disallowed_special=())), "tokenizer", "tiktoken/cl100k_base")
    except Exception:  # noqa: BLE001 -- absence of the optional dependency is the normal path
        pass
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    tokens = cjk / CHARS_PER_TOKEN_CJK + other / CHARS_PER_TOKEN_TEXT
    return TokenEstimate(int(tokens) + 1, "heuristic", f"chars/{CHARS_PER_TOKEN_TEXT}")


def _content_text(content) -> str:
    """Flatten a message's content, including tool_use blocks, so nothing is missed in the count."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return json.dumps(content, default=str)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                # Tool arguments and results are the expensive part; keeping their full text here is
                # what makes the estimate useful rather than flattering.
                parts.append(json.dumps(item, default=str))
            else:
                block_type = getattr(item, "type", None)
                if block_type == "text":
                    parts.append(getattr(item, "text", "") or "")
                elif block_type == "tool_use":
                    parts.append(json.dumps(getattr(item, "input", {}) or {}, default=str))
                else:
                    parts.append(json.dumps(item, default=str))
        return "\n".join(parts)
    return str(content)


def estimate_messages(messages: list[dict]) -> tuple[TokenEstimate, dict[str, int]]:
    """Estimate the whole conversation plus a per-role breakdown.

    The breakdown is what makes `/context` actionable: "12k tokens, 9k of which is tool output" tells
    the user to narrow a search, while "12k tokens" alone tells them nothing.
    """
    buckets: dict[str, int] = {}
    total = 0
    confidence = "heuristic"
    methods = set()
    for message in messages or []:
        role = message.get("role") or ("tool" if message.get("tool_call_id") else "other")
        estimate = estimate_tokens(_content_text(message.get("content")))
        # Tool-call arguments live on the assistant message, not in `content`.
        if message.get("tool_calls"):
            estimate = TokenEstimate(
                estimate.tokens + estimate_tokens(json.dumps(message["tool_calls"], default=str)).tokens,
                estimate.confidence,
                estimate.method,
            )
        buckets[role] = buckets.get(role, 0) + estimate.tokens
        total += estimate.tokens
        confidence = estimate.confidence
        methods.add(estimate.method)
    return TokenEstimate(total, confidence, ",".join(sorted(methods))), buckets


def evaluate(
    messages: list[dict],
    profile: ModelProfile,
    *,
    system_prompt: str = "",
    tools_chars: int = 0,
    reserve_output_tokens: int | None = None,
) -> BudgetState:
    """Decide what should happen right now, given everything that will be sent in the next request.

    The system prompt and tool schemas are counted because they are re-sent on every request and are
    a real fraction of a small window -- omitting them is how a "60% full" reading still overflows.
    """
    convo, breakdown = estimate_messages(messages)
    sys_est = estimate_tokens(system_prompt)
    tools_tokens = int(tools_chars / CHARS_PER_TOKEN_TEXT) + 1 if tools_chars else 0
    reserve = reserve_output_tokens if reserve_output_tokens is not None else min(profile.max_output_tokens, 2_048)

    used = convo.tokens + sys_est.tokens + tools_tokens
    breakdown["system_prompt"] = sys_est.tokens
    if tools_tokens:
        breakdown["tool_schemas"] = tools_tokens
    breakdown["reserved_for_reply"] = reserve

    window = max(1, profile.context_window)
    effective = used + reserve
    fraction = effective / window
    headroom = window - effective

    if fraction >= profile.refuse_at:
        action, reason = "refuse", (
            f"context is {fraction:.0%} full including the {reserve:,}-token reply reserve; "
            "sending this request would fail"
        )
    elif fraction >= profile.compact_at:
        action, reason = "compact", f"context is {fraction:.0%} full; older turns must be summarized"
    elif fraction >= profile.prune_at:
        action, reason = "prune", f"context is {fraction:.0%} full; old tool results should be dropped"
    elif fraction >= profile.warn_at:
        action, reason = "warn", f"context is {fraction:.0%} full"
    else:
        action, reason = "ok", ""

    return BudgetState(
        used_tokens=used,
        window=window,
        used_fraction=fraction,
        action=action,
        headroom_tokens=headroom,
        confidence=convo.confidence,
        breakdown=breakdown,
        reason=reason,
    )


def shrink_tool_result(text: str, limit: int, *, kind: str = "") -> tuple[str, bool]:
    """Truncate one tool result, keeping both ends and saying so in the text.

    Head-and-tail rather than head-only: for a stack trace, a test summary or a directory listing,
    the decisive line is as often at the end as at the start. The marker is part of the returned
    text so the model knows it is looking at a window rather than the whole output -- a silently
    truncated result makes a model assert things about data it never saw.
    """
    if text is None:
        return "", False
    if limit <= 0 or len(text) <= limit:
        return text, False
    # The marker is counted against the limit, so the returned text never exceeds it -- a truncation
    # that overshoots the budget it was called with is a slow leak of exactly the resource it exists
    # to protect. The marker is bounded to a fifth of the budget so a small limit still leaves room
    # for real content.
    label = f" {kind}" if kind else ""
    # Short, because this marker is re-sent on every later request in the turn, once per truncated
    # result. The long form ran to 201 characters and carried a paragraph of advice ("narrow the
    # command, e.g. git status, a specific file, a grep pattern") that the model reads once and pays
    # for forever -- measured at 50 tokens per truncated result per request, so 502 tokens across ten
    # requests for guidance it has already acted on. The advice moved into the `read_file` and
    # `run_bash` descriptions, where it is charged once rather than once per result.
    marker = f"\n... [{len(text):,} chars omitted{label} -- re-run with a range if you need them.]\n"
    if len(marker) >= limit:
        # Budget too small for the full explanation: keep the shortest honest signal instead.
        marker = f"\n... [{label.strip() or 'output'} truncated]\n"
    keep = max(0, limit - len(marker))
    head = keep * 2 // 3
    tail = keep - head
    return text[:head] + marker + text[-tail:], True


def plan_prune(messages: list[dict], min_chars: int = 400) -> int:
    """How many tool results would be replaced with placeholders. Used for the notice text."""
    count = 0
    for message in messages or []:
        text = _content_text(message.get("content"))
        if len(text) >= min_chars and (message.get("role") == "tool" or message.get("tool_call_id")):
            count += 1
    return count


# ---------------------------------------------------------------------------------------------
# Level 5.8: the agent choosing, rather than the harness acting on its behalf
# ---------------------------------------------------------------------------------------------
#
# Everything above this line DECIDES FOR the agent: it evaluates the window and picks an action. That is a
# good mechanism and it is not the requirement. Requirement 5.8 is that the agent "manages its own context
# budget deliberately", and an agent cannot decide about a number it is never shown -- so these functions
# expose the same evaluation, plus one capability the automatic path does not have.
#
# The capability is SPILLING. The automatic path can prune, which DISCARDS an old tool result, and compact,
# which spends a model call to summarise. Neither lets the agent say "I will need this later". Moving it to a
# file does, and the distinction is the whole value: pruning loses information, spilling relocates it.
#
# The numbers come from `evaluate` rather than being recomputed. A second calculation would eventually
# disagree with the first, and the agent would then be deciding on a number the harness does not use.

# Below this many tokens remaining there is no room to think, so the status says so rather than reporting a
# small number and leaving the reader to work out that it is a problem.
TIGHT_HEADROOM_TOKENS = 8_000

# A spill is only worth doing above this size: a file write plus a re-read costs more than it saves for a
# small result, which makes the tool feel like overhead rather than relief.
MIN_SPILL_CHARS = 4_000


@dataclass
class ContextStatus:
    """The numbers an agent needs to decide about its own context."""

    window: int = 0
    used: int = 0
    headroom: int = 0
    percent: int = 0
    # What the harness WOULD do, from the same evaluation that would do it.
    harness_action: str = ""
    confidence: str = ""
    messages: int = 0
    verdict: str = ""

    @property
    def tight(self) -> bool:
        return 0 < self.headroom < TIGHT_HEADROOM_TOKENS

    def to_dict(self) -> dict:
        return {"window": self.window, "used": self.used, "headroom": self.headroom,
                "percent": self.percent, "harness_action": self.harness_action,
                "confidence": self.confidence, "messages": self.messages,
                "verdict": self.verdict, "tight": self.tight}


def status_for_agent(agent, *, system_prompt: str = "", tools_chars: int = 0) -> ContextStatus:
    """Read this agent's real context budget. Never raises -- an unknown budget reports zeros.

    The status is ADVICE, and advice that can crash the thing it advises is worse than no advice, so every
    failure path returns a usable object with a verdict saying the budget is unknown.
    """
    status = ContextStatus()
    try:
        profile = getattr(agent, "profile", None)
        if profile is None:
            from .model_profiles import detect_profile

            # `detect_profile`, not a lookup by window: it is the same resolver the agent used to get its own
            # profile, so the budget reported here is the budget the harness will enforce. A second
            # calculation would eventually disagree with the first, and the agent would then be deciding on a
            # number nobody else uses.
            profile = detect_profile(
                str(getattr(agent, "model", "") or ""),
                max_input_tokens=getattr(agent, "max_input_tokens", None),
            )
    except Exception:  # noqa: BLE001
        profile = None

    if profile is None:
        status.verdict = "Context budget unknown for this model; the harness will manage it."
        return status

    prompt = system_prompt
    if not prompt:
        try:
            prompt = agent._system_prompt()
        except Exception:  # noqa: BLE001
            prompt = ""

    try:
        state = evaluate(getattr(agent, "messages", []) or [], profile,
                         system_prompt=prompt or "", tools_chars=tools_chars)
        status.window = int(state.window)
        status.used = int(state.used_tokens)
        status.headroom = int(state.headroom_tokens)
        status.percent = int(state.percent)
        status.harness_action = str(state.action)
        status.confidence = str(state.confidence)
    except Exception:  # noqa: BLE001
        pass

    try:
        status.messages = len(getattr(agent, "messages", []) or [])
    except Exception:  # noqa: BLE001
        status.messages = 0

    status.verdict = _verdict(status)
    return status


def _verdict(status: ContextStatus) -> str:
    """One line an agent can act on, rather than a number it has to interpret."""
    if not status.window:
        return "Context budget unknown for this model; the harness will manage it."
    if status.harness_action == "refuse":
        return ("There is NO room left: the next request would be refused. Spill something you will not need "
                "on the next step, or summarise and continue.")
    if status.harness_action in ("compact", "prune"):
        return (f"Context is {status.percent}% used and the harness is about to {status.harness_action} "
                f"automatically. Acting first keeps what matters -- spilling relocates content, while "
                f"{status.harness_action} either discards it or spends a model call to summarise it.")
    if status.tight:
        return (f"{status.headroom:,} tokens remain, less than one large file. Prefer targeted reads over "
                "whole files, and spill anything you will not need again.")
    return f"{status.percent}% used; {status.headroom:,} tokens remain. No action needed."


def spill_dir(workspace=None):
    """Where spilled content goes: the workspace when there is one, the config directory otherwise.

    The workspace by preference, because a spilled file the agent cannot read back is not a spill -- it is a
    deletion with extra steps.
    """
    from pathlib import Path as _Path

    candidates = []
    if workspace:
        candidates.append(_Path(workspace))
    try:
        from .constants import CONFIG_DIR

        candidates.append(_Path(CONFIG_DIR))
    except Exception:  # noqa: BLE001
        pass
    if not candidates:
        candidates.append(_Path.cwd())

    for base in candidates:
        path = base / "spill"
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except OSError:
            continue
    return candidates[-1]


def spill(text: str, name: str = "", *, workspace=None) -> tuple[str, str]:
    """Move content to a file and return (pointer, problem).

    The pointer is what goes back into the conversation: a path and a size, not the content. That is the
    difference between "something was dropped for you" and "you chose to put it there and can read it again".
    """
    import time as _time

    from .atomic_io import atomic_write_text

    body = text or ""
    if len(body) < MIN_SPILL_CHARS:
        return "", (f"nothing spilled: {len(body)} characters is below the {MIN_SPILL_CHARS}-character "
                    "threshold, and a file write plus a re-read costs more than it saves")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (name or "spill"))[:60] or "spill"
    target = spill_dir(workspace) / f"{int(_time.time())}-{safe}.txt"
    try:
        atomic_write_text(target, body)
    except OSError as exc:
        return "", f"could not spill to disk ({exc}); keep the content in context instead"

    lines = body.count("\n") + 1
    return (f"Spilled {len(body):,} characters ({lines:,} lines) to {target}\n"
            f"Read it back with read_file when you need it. Nothing was lost -- it moved out of the "
            f"conversation and onto disk."), ""


def propose_action(status: ContextStatus, *, large_result_chars: int = 0) -> str:
    """What the agent could do about its context, offered rather than applied.

    A mechanism that acts on the agent's behalf is what already exists; the point of this half is the agent
    choosing, so this returns a recommendation and performs nothing.
    """
    if large_result_chars >= MIN_SPILL_CHARS and status.percent > 60:
        return (f"Spill this {large_result_chars:,}-character result rather than adding it: context is "
                f"{status.percent}% used.")
    if status.harness_action in ("compact", "prune"):
        return f"The harness will {status.harness_action} on the next step. Finish the current step first."
    if status.tight:
        return "Read only what you need next; the remaining budget is smaller than a large file."
    return ""


# --- the tools ---------------------------------------------------------------------------------

CHECK_CONTEXT_TOOL_DEF = {
    "name": "check_context",
    "description": (
        "See how much context budget is left, and what the harness is about to do about it. Worth calling "
        "before reading a large file or running a command whose output could be huge: acting first means "
        "keeping what matters, rather than having something dropped for you."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

SPILL_TOOL_DEF = {
    "name": "spill",
    "description": (
        "Move a large result out of the conversation and onto disk, keeping only a pointer. Use this for an "
        "intermediate result you will need later but not on the next step -- a long file dump, a build log, a "
        "large API response. The content is not lost: read it back with read_file. Prefer this to letting the "
        "harness prune it, because pruning discards and spilling relocates."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The content to move out of context."},
            "name": {"type": "string", "description": "A short label for the file, e.g. 'build-log'."},
        },
        "required": ["text"],
    },
}

CONTEXT_TOOL_DEFS = (CHECK_CONTEXT_TOOL_DEF, SPILL_TOOL_DEF)


def execute_check_context(agent=None) -> tuple[str, bool]:
    """Report the budget as a readable block rather than raw JSON."""
    if agent is None:
        return "No agent context available.", True
    status = status_for_agent(agent)
    lines = [
        (f"Context: {status.used:,} / {status.window:,} tokens ({status.percent}%)"
         if status.window else "Context: budget unknown for this model"),
        f"  remaining    : {status.headroom:,} tokens",
        f"  messages     : {status.messages}",
        f"  harness would: {status.harness_action or 'do nothing'}",
    ]
    if status.confidence == "heuristic":
        # Stated because a report from an approximation should not imply precision it does not have.
        lines.append("  note         : counted from a character estimate, not a real tokenizer")
    lines.append(f"  {status.verdict}")
    action = propose_action(status)
    if action:
        lines.append(f"  suggested    : {action}")
    return "\n".join(lines), False


def execute_spill(tool_input: dict, *, workspace=None) -> tuple[str, bool]:
    pointer, problem = spill(str(tool_input.get("text") or ""),
                             str(tool_input.get("name") or ""), workspace=workspace)
    if problem:
        return problem, True
    return pointer, False
