import hashlib
import json
from dataclasses import dataclass

SUMMARY_SYSTEM_PROMPT = (
    "Summarize the following coding session concisely, using these exact section headers so "
    "the structure survives compaction:\n"
    "## Key facts & decisions\n"
    "## Files touched (and how)\n"
    "## Outstanding tasks\n"
    "Keep each section terse -- bullet points, not prose. Omit a section entirely if it has "
    "nothing to report rather than writing 'none'."
)

_PRUNE_MARKER = "[pruned:"


def _audit():
    """The active compaction recorder, or None.

    Imported here rather than at module scope because `compaction_audit` pulls in `constants`, and this module
    is imported on the CLI's startup path -- the same eager-import mistake that made the MCP SDK cost 700 ms
    per invocation.
    """
    try:
        from . import compaction_audit

        return compaction_audit.active()
    except Exception:
        return None


def _record_prune(original: str, *, tool: str = "", age: int | None = None, path: str = "") -> str:
    """Record a tool result that is about to be replaced, and return the handle that retrieves it.

    Called *before* the replacement, because the replacement destroys the only copy. This is the whole point of
    the audit: the placeholder keeps the size, and only the store keeps the content.

    Returns the digest so the placeholder can name it. Without that the placeholder could only tell the model to
    **re-run the tool**, which is the expensive option: the content is already on disk, keyed by a digest this
    function just computed, so re-running the tool to recover it spends a whole call to fetch what the harness
    already has. The earlier version computed the digest, stored the content under it, and then threw the digest
    away.
    """
    recorder = _audit()
    if recorder is None:
        return ""
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    recorder.record_prune(original, tool=tool, path=path, age_turns=age)
    return digest


def _prune_placeholder(age: int, original_len: int, digest: str = "") -> str:
    """The text that replaces a pruned tool result.

    When a `digest` is given the model is told to **recall** it, which costs one cheap tool call against content
    the harness already holds. Only when there is no digest -- auditing disabled, or the store write failed -- does
    it fall back to telling the model to re-run the tool, which is honest about the only remaining option rather
    than naming a handle that would not resolve.
    """
    head = (
        f"{_PRUNE_MARKER} tool result from {age} turn(s) ago, {original_len:,} chars removed "
        "to save context -- "
    )
    if digest:
        return head + f"call recall with handle {digest!r} to read the original]"
    return head + "re-run the tool if you need this again]"


def _is_tool_result_user_message(msg: dict) -> bool:
    """Anthropic's convention: tool results ride back as role=='user' messages containing a
    list of {type: 'tool_result', ...} blocks -- indistinguishable from a genuine new user turn
    by role alone, so the content shape must be inspected."""
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _is_turn_start(msg: dict) -> bool:
    """True only for a genuine new user turn -- never a tool-result continuation. A "turn"
    spans from one real user message through all its tool-call follow-ups until the next real
    user message, so splitting/pruning at turn boundaries never separates an assistant's
    tool-call from its result."""
    if msg.get("type") == "user_input":  # Gemini
        return True
    if msg.get("role") == "user":
        return not _is_tool_result_user_message(msg)
    return False


def _prune_message_tool_results(msg: dict, age: int, min_chars: int) -> int:
    pruned = 0
    # OpenAI-compatible: {"role": "tool", "content": "<string>"}
    if msg.get("role") == "tool" and isinstance(msg.get("content"), str):
        content = msg["content"]
        if len(content) > min_chars and not content.startswith(_PRUNE_MARKER):
            handle = _record_prune(content, tool=str(msg.get("name") or ""), age=age)
            msg["content"] = _prune_placeholder(age, len(content), handle)
            pruned += 1
    # Gemini: {"type": "function_result", "result": "<string>", ...}
    elif msg.get("type") == "function_result" and isinstance(msg.get("result"), str):
        result = msg["result"]
        if len(result) > min_chars and not result.startswith(_PRUNE_MARKER):
            handle = _record_prune(result, tool=str(msg.get("name") or ""), age=age)
            msg["result"] = _prune_placeholder(age, len(result), handle)
            pruned += 1
    # Anthropic: {"role": "user", "content": [{"type": "tool_result", "content": "<string>", ...}]}
    elif _is_tool_result_user_message(msg):
        for block in msg["content"]:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            content = block.get("content")
            if isinstance(content, str) and len(content) > min_chars and not content.startswith(_PRUNE_MARKER):
                handle = _record_prune(content, tool=str(block.get("tool_use_id") or ""), age=age)
                block["content"] = _prune_placeholder(age, len(content), handle)
                pruned += 1
    return pruned


def prune_stale_tool_results(messages: list[dict], max_age_turns: int, min_chars: int) -> int:
    """Replaces large tool-call results older than max_age_turns with a short placeholder, in
    place. Returns the count of entries pruned. Cheaper and more surgical than a full /compact
    -- oversized tool output (file reads, grep dumps), not conversational text, is almost
    always what actually fills context, so this alone often delays or avoids the need for a
    full compaction. Idempotent -- already-pruned placeholders are skipped."""
    turn_index = 0
    turn_indices: list[int] = []
    for msg in messages:
        if _is_turn_start(msg):
            turn_index += 1
        turn_indices.append(turn_index)
    current_turn = turn_index
    if current_turn <= max_age_turns:
        return 0

    pruned = 0
    for msg, idx in zip(messages, turn_indices):
        age = current_turn - idx
        if age < max_age_turns:
            continue
        pruned += _prune_message_tool_results(msg, age, min_chars)
    return pruned


def split_at_turn_boundary(messages: list[dict], keep_last_n_turns: int) -> tuple[list[dict], list[dict]]:
    """Splits messages into (old, recent) at a genuine turn boundary -- 'recent' holds the last
    keep_last_n_turns full turns verbatim, 'old' is everything before that. The split point is
    always between two genuine user turns (see _is_turn_start), so it can never separate an
    assistant's tool-call from its result. Used by rolling-window compaction: summarize only
    'old' instead of the entire history, preserving recent detail verbatim."""
    turn_starts = [i for i, msg in enumerate(messages) if _is_turn_start(msg)]
    if len(turn_starts) <= keep_last_n_turns:
        return [], list(messages)
    boundary = turn_starts[-keep_last_n_turns]
    return messages[:boundary], messages[boundary:]


def sanitize_resumed_messages(messages: list[dict]) -> list[dict]:
    """Repairs a message history loaded from a saved session that may have been corrupted by a
    since-fixed bug (or an interrupt that raced with an older version of this code) -- a
    trailing assistant turn that called a tool with no matching result ever appended. The API
    rejects that outright on the very next request, which otherwise permanently blocks that
    session.json from ever being resumed again (the run_turn-level rollback fix only prevents
    *new* corruption; it can't retroactively fix history that was already broken when it was
    loaded from disk). Strips just the dangling tool-call metadata from the last message rather
    than deleting it outright, so any real text the model produced alongside the tool call
    survives; only removes the message entirely if nothing would be left."""
    if not messages:
        return messages
    messages = list(messages)
    last = messages[-1]

    if last.get("role") == "assistant" and last.get("tool_calls"):
        repaired = {k: v for k, v in last.items() if k != "tool_calls"}
        if repaired.get("content"):
            messages[-1] = repaired
        else:
            messages.pop()
    elif last.get("type") == "function_call":
        # Gemini: a function_call step with no matching function_result ever appended.
        messages.pop()
    elif last.get("role") == "assistant" and isinstance(last.get("content"), list) and last["content"]:
        if _block_type(last["content"][-1]) == "tool_use":
            trimmed_content = last["content"][:-1]
            if trimmed_content:
                messages[-1] = {**last, "content": trimmed_content}
            else:
                messages.pop()

    return messages


def _block_type(item) -> str | None:
    if isinstance(item, dict):
        return item.get("type")
    return getattr(item, "type", None)


def _block_text(item) -> str | None:
    if isinstance(item, dict):
        return item.get("text")
    return getattr(item, "text", None)


def _block_name(item) -> str | None:
    if isinstance(item, dict):
        return item.get("name")
    return getattr(item, "name", None)


def _omitted_spans(messages: list[dict]) -> list[tuple[str, str, str]]:
    """Every span `render_transcript` will omit, as `(kind, description, content)`.

    Computed separately from the rendering rather than recorded inline, because the renderer *replaces* each
    span with a terse marker as it goes -- by the time a line is appended the original is out of reach. Walking
    the messages once more is cheap (this runs once per compaction, not once per turn) and keeps the renderer's
    output byte-identical to before, which matters: the summary is generated from it.

    The point of collecting these is that `render_transcript`'s docstring says tool payloads are omitted because
    they "rarely need verbatim preservation". That is a judgement, and until now it was an untestable one --
    nobody could ask what a summary had been denied, because the answer was never written down.
    """
    omitted: list[tuple[str, str, str]] = []

    for index, msg in enumerate(messages):
        msg_type = msg.get("type")

        if msg_type == "function_result":
            result = msg.get("result")
            if isinstance(result, str) and result:
                omitted.append(("tool_result", f"message {index} gemini function_result", result))
            continue

        if msg_type == "model_output":
            for item in msg.get("content") or []:
                if not isinstance(item, dict):
                    continue
                # Non-text blocks in a model_output (thoughts, attachments) never reach the transcript.
                if item.get("type") not in ("text", None) and item.get("type") != "text":
                    omitted.append(
                        ("model_block", f"message {index} block type {item.get('type')!r}",
                         json.dumps(item, ensure_ascii=False, default=str))
                    )
            continue

        if msg.get("role") == "tool":
            content = msg.get("content")
            if isinstance(content, str) and content:
                omitted.append(("tool_result", f"message {index} openai tool message", content))
            continue

        content = msg.get("content")
        if isinstance(content, list):
            for item in content:
                item_type = _block_type(item)
                if item_type == "tool_result":
                    raw = item.get("content") if isinstance(item, dict) else None
                    if raw is None:
                        raw = getattr(item, "content", None)
                    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
                    omitted.append(("tool_result", f"message {index} anthropic tool_result", text or ""))
                elif item_type in ("image", "image_url", "document", "file"):
                    omitted.append(
                        ("attachment", f"message {index} attached {item_type}",
                         json.dumps(item, ensure_ascii=False, default=str))
                    )

    return omitted


@dataclass
class SummaryCheck:
    """Whether a summary is worth applying, with the numbers behind the verdict.

    Returns the figures rather than only a bool so a refusal can say *how much* longer the summary was, which is
    the difference between "compaction refused" and "compaction refused: the summary was 1.4x the history it
    replaced".
    """

    ok: bool
    reason: str = ""
    summary_chars: int = 0
    replaced_chars: int = 0
    retained_chars: int = 0

    @property
    def ratio(self) -> float:
        return self.summary_chars / self.replaced_chars if self.replaced_chars else 0.0

    def render(self) -> str:
        return (
            f"summary {self.summary_chars:,} chars vs {self.replaced_chars:,} replaced "
            f"({self.ratio:.2f}x)"
        )


# The replacement adds two framing messages on top of the summary, and the recent turns are carried verbatim
# either way, so the comparison is against the span actually being dropped rather than the whole transcript.
_FRAMING_CHARS = 120
_SUMMARY_OPENING = "[Conversation compacted. Summary of earlier context:]\n"


def check_summary(
    summary: str,
    *,
    transcript: str,
    replaced_chars: int = 0,
    max_ratio: float = 1.0,
) -> SummaryCheck:
    """Refuse a summary that is not smaller than the history it would replace.

    ## Why this exists

    Compaction's entire purpose is to make the conversation smaller. A summariser that returns something as long
    as, or longer than, what it replaced has made the request *more* expensive while also destroying the original
    — the worst of both outcomes, and one that silently gets worse on every subsequent compaction because each one
    is summarising the previous summary.

    DeepSeek's harness prices both sides and rejects the summary (verified in `compaction-basic/src/region.ts`).
    Myriaxon had no such check: a model that echoed its input, or padded a short history with headings, would have
    its output installed as the new context.

    ## What is compared

    The summary (plus its framing) against `replaced_chars` — the size of the span leaving the conversation. When
    the caller does not supply that, the rendered transcript stands in for it: it is a flattening of the same
    history, so it is a *lower* bound, and using a lower bound makes the check stricter rather than looser.

    `max_ratio` is 1.0 by default — strictly smaller, not merely not-larger, because a summary of exactly the same
    size has cost the original and bought nothing.
    """
    summary_chars = len(summary or "")
    replaced = int(replaced_chars or 0)
    if replaced <= 0:
        replaced = len(transcript or "")

    check = SummaryCheck(
        ok=True,
        summary_chars=summary_chars,
        replaced_chars=replaced,
    )
    if replaced <= 0:
        # Nothing to compare against. Refusing would block a legitimate compaction of a trivially small history,
        # so this allows it and says the comparison was not made.
        check.reason = "no measurable history to compare against; the check was not applied"
        return check

    budget = int(replaced * max_ratio)
    check.retained_chars = max(0, replaced - summary_chars)
    if summary_chars + _FRAMING_CHARS > budget:
        check.ok = False
        check.reason = (
            f"the summary is {summary_chars:,} chars against {replaced:,} replaced "
            f"({check.ratio:.2f}x). Compaction exists to shrink the conversation, so applying this would make "
            "the next request larger while destroying the original. The history was kept and compaction was "
            "skipped."
        )
    return check


def record_compaction(old: list[dict], summary: str, provider: str, *, kept_turns: int) -> None:
    """Record one compaction: what was summarised away, and what replaced it.

    Called by each agent at the point of a *successful* compaction, immediately before `self.messages` is
    replaced. Two things are recorded, and both are needed to judge the result:

    * the **transcript** the summariser was given, so a summary that dropped something is distinguishable from
      a transcript that never contained it;
    * the **summary** that replaced the history, so the discarded span has a recoverable representation.

    Worth stating what this does *not* claim. Each agent's `compact()` returns before replacing `self.messages`
    when summarisation fails, so a failed compaction loses nothing — verified by reading all three backends. The
    exposure this closes is the *successful* one: `old` leaves the conversation permanently, and before this
    the only record of it was inside the summary the model chose to write.

    Never raises. A compaction that succeeded must not be undone by an audit failure, so this is best-effort by
    construction; `Recorder.failures` counts anything that went wrong rather than hiding it.
    """
    recorder = _audit()
    if recorder is None:
        return
    try:
        recorder.record_transcript(
            render_transcript(old, provider),
            reason=f"compaction input ({len(old)} messages, kept last {kept_turns} turns verbatim)",
        )
        recorder.record_summary(
            summary,
            reason=f"compaction output ({len(old)} messages replaced)",
        )
    except Exception:
        # Best-effort: see the docstring. The compaction itself has already succeeded.
        pass


def render_transcript(messages: list[dict], provider: str) -> str:
    """Best-effort flatten of a conversation into readable text for a summarization prompt.
    Tool calls are noted tersely; raw tool_result/tool-message payloads are omitted, since
    they're usually the bulk of the token weight and rarely need verbatim preservation.

    Every omitted span is recorded to the compaction audit first, so that judgement is checkable after the
    fact instead of resting on the docstring above.
    """
    recorder = _audit()
    if recorder is not None:
        for kind, description, content in _omitted_spans(messages):
            recorder.record_omission(content, omission=kind, description=description)

    lines: list[str] = []

    for msg in messages:
        # Gemini messages are "type"-keyed (user_input/model_output/function_result), not
        # "role"-keyed like Anthropic/OpenAI -- handle that shape first, then fall through.
        msg_type = msg.get("type")
        if msg_type == "user_input":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                lines.append(f"user: {content}")
            continue
        if msg_type == "model_output":
            for item in msg.get("content") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and item.get("text"):
                    lines.append(f"assistant: {item['text']}")
            continue
        if msg_type == "function_call":
            lines.append(f"assistant: [called tool {msg.get('name')}]")
            continue
        if msg_type == "function_result":
            lines.append("tool result: [output omitted from summary]")
            continue

        role = msg.get("role")
        if role == "system":
            continue
        if role == "tool":
            lines.append("tool result: [output omitted from summary]")
            continue

        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                lines.append(f"{role}: {content}")
        elif isinstance(content, list):
            for item in content:
                item_type = _block_type(item)
                if item_type == "text":
                    text = _block_text(item)
                    if text:
                        lines.append(f"{role}: {text}")
                elif item_type == "tool_use":
                    lines.append(f"{role}: [called tool {_block_name(item)}]")
                elif item_type == "tool_result":
                    lines.append("tool result: [output omitted from summary]")
                elif item_type in ("image", "image_url", "document", "file"):
                    lines.append(f"{role}: [attached file]")
                # thinking / compaction / other block types: skip silently

        for call in msg.get("tool_calls") or []:
            fn = call.get("function", {})
            lines.append(f"{role}: [called tool {fn.get('name')}]")

    return "\n".join(lines)


def analyze_context_usage(messages: list[dict]) -> dict[str, int]:
    """Rough character-count breakdown of the message list, bucketed by category -- character
    counts, not tokens, same discipline as the existing ~4-chars/token usage estimate. Meant to
    show *where* context is going (tool results vs. conversation text), not an exact accounting.
    Reuses _block_type/_block_text since they already handle both plain dicts and SDK objects
    (Anthropic's assistant content blocks are real SDK objects, not dicts)."""
    buckets = {"conversation_text": 0, "tool_results": 0, "other": 0}
    for msg in messages:
        msg_type = msg.get("type")
        if msg_type == "user_input":
            buckets["conversation_text"] += len(msg.get("content") or "")
            continue
        if msg_type == "model_output":
            for item in msg.get("content") or []:
                if _block_type(item) == "text":
                    buckets["conversation_text"] += len(_block_text(item) or "")
            continue
        if msg_type == "function_result":
            result = msg.get("result")
            if isinstance(result, str):
                buckets["tool_results"] += len(result)
            continue

        role = msg.get("role")
        if role == "system":
            continue  # counted separately by the caller -- Anthropic passes it outside messages
        if role == "tool":
            content = msg.get("content")
            if isinstance(content, str):
                buckets["tool_results"] += len(content)
            continue

        content = msg.get("content")
        if isinstance(content, str):
            buckets["conversation_text"] += len(content)
        elif isinstance(content, list):
            for item in content:
                item_type = _block_type(item)
                if item_type == "text":
                    buckets["conversation_text"] += len(_block_text(item) or "")
                elif item_type == "tool_result":
                    tr_content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
                    if isinstance(tr_content, str):
                        buckets["tool_results"] += len(tr_content)
                else:
                    buckets["other"] += len(str(item))
        for call in msg.get("tool_calls") or []:
            buckets["other"] += len(str(call))
    return buckets


def format_context_breakdown(
    buckets: dict[str, int], system_prompt_len: int, tools_len: int, max_input_tokens: int | None
) -> str:
    total_chars = system_prompt_len + tools_len + sum(buckets.values())

    def pct(n: int) -> str:
        return f"{(n / total_chars * 100):.0f}%" if total_chars else "0%"

    lines = [
        "Context usage breakdown (rough char counts, ~4 chars/token):",
        f"  System prompt + skills/subagent listings: {system_prompt_len:,} chars ({pct(system_prompt_len)})",
        f"  Tool definitions:                         {tools_len:,} chars ({pct(tools_len)})",
        f"  Tool results:                              {buckets['tool_results']:,} chars ({pct(buckets['tool_results'])})",
        f"  Conversation text:                         {buckets['conversation_text']:,} chars ({pct(buckets['conversation_text'])})",
    ]
    if buckets["other"]:
        lines.append(f"  Other (tool calls, misc):                  {buckets['other']:,} chars ({pct(buckets['other'])})")
    estimated_tokens = total_chars // 4
    lines.append(f"  Total: ~{estimated_tokens:,} tokens estimated")
    if max_input_tokens:
        lines.append(f"  ({round(estimated_tokens / max_input_tokens * 100)}% of the {max_input_tokens:,}-token context window)")
    return "\n".join(lines)
