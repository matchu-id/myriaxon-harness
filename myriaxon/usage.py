import json
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    request_count: int = 0
    # Prompt-cache accounting. Both providers report it and neither was being read, so a session
    # with a 99% cache hit rate looked identical to one paying full price for every token -- the
    # single most useful number for judging whether a long session is economical.
    #
    # Anthropic: usage.cache_read_input_tokens / cache_creation_input_tokens.
    # OpenAI-compatible: usage.prompt_tokens_details.cached_tokens, which is a *read* count; those
    # APIs do not bill cache writes separately, so cache_write stays 0 there rather than being
    # invented.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
    ) -> None:
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0
        self.cache_read_tokens += cache_read_tokens or 0
        self.cache_write_tokens += cache_write_tokens or 0
        self.request_count += 1

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        """Share of the prompt served from cache, as a fraction.

        Measured against input + cache reads, which is the denominator that makes the number mean
        "of everything that could have been cached, how much was". A rate against input alone can
        exceed 100% and is then meaningless.
        """
        cacheable = self.input_tokens + self.cache_read_tokens
        if cacheable <= 0:
            return 0.0
        return self.cache_read_tokens / cacheable


def cache_fields(usage: object) -> tuple[int, int]:
    """Cache read/write counts from a provider's usage object, or (0, 0).

    Reads every provider shape and tolerates any being absent, because a missing cache field means
    "this provider does not report it", not "no tokens were cached" -- and a status line that silently
    displayed 0% would be a wrong number rather than a missing one.

    Four shapes, because the providers disagree and each was verified rather than assumed:

        Anthropic    cache_read_input_tokens / cache_creation_input_tokens
        OpenAI       prompt_tokens_details.cached_tokens
        Gemini       cached_content_token_count        (google-genai, snake_case attributes)
        Gemini, REST cachedContentTokenCount           (the JSON field, if a proxy forwards it verbatim)

    Gemini was added after MEASURING that its absence was not harmless. `cachedContents` responds 200 on
    the Vertex project in use, so context caching is a real, available feature -- and it was being
    reported as a permanent 0% cache-hit rate. A number that is always zero for a working feature is
    worse than no number, because it reads as "caching does not help here" and steers the user away from
    a real saving.

    Only READS are reported for Gemini: it has no separate write counter, since creating an explicit
    cache is its own API call rather than a side effect of a normal request, so there is nothing to
    attribute to a turn.
    """
    read = getattr(usage, "cache_read_input_tokens", None)
    write = getattr(usage, "cache_creation_input_tokens", None)
    if read is None and write is None:
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            read = getattr(details, "cached_tokens", None)
    if read is None:
        # Both spellings checked: the SDK exposes snake_case attributes while the REST payload uses
        # camelCase, and a caller may hand over either depending on which transport produced it.
        read = getattr(usage, "cached_content_token_count", None)
        if read is None:
            read = getattr(usage, "cachedContentTokenCount", None)
        if read is None and isinstance(usage, dict):
            read = usage.get("cachedContentTokenCount") or usage.get("cached_content_token_count")
    try:
        return int(read or 0), int(write or 0)
    except (TypeError, ValueError):
        return 0, 0


def format_usage_summary(totals: UsageTotals, provider: str, model: str) -> str:
    if totals.request_count == 0:
        return "No usage recorded yet this session."

    lines = [
        f"Token usage this session ({provider}, {model}):",
        f"  Requests: {totals.request_count}",
        f"  Input tokens:  {totals.input_tokens:,}",
        f"  Output tokens: {totals.output_tokens:,}",
        f"  Total tokens:  {totals.total_tokens:,}",
    ]
    if totals.cache_read_tokens or totals.cache_write_tokens:
        lines.append(
            f"  Cache: {totals.cache_read_tokens:,} read / {totals.cache_write_tokens:,} written "
            f"({totals.cache_hit_rate:.1%} of prompts served from cache)"
        )
    lines.append(
        "(Token counts only -- not a dollar cost estimate, since per-model pricing changes "
        "over time and isn't tracked here. Resets on /reauth, /provider switch, or session resume.)"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------
# The ledger: usage that SURVIVES the process
# ---------------------------------------------------------------------------------------
#
# Found by an audit: every count above lived in memory only. `session_store` persisted messages and
# no usage field at all, and grepping for `save_usage` or `usage.json` returned nothing. So the
# totals reset on every exit, which means there was no cost history -- and a spend limit you cannot
# measure is not a spend limit, it is a hope.
#
# Appended as JSONL, one line per TURN rather than per request: a turn is the unit a person reasons
# about ("that question cost 4 cents") and the unit a budget is set on, while a request is an
# implementation detail that varies with how many tools the model chose to call.
#
# Failure to write is never fatal. A ledger that can abort a turn would be worse than no ledger, and
# this sits on the path of every completed turn.

def ledger_path() -> Path:
    """Where the ledger lives. Resolved late so a test can redirect CONFIG_DIR."""
    from .constants import USAGE_LEDGER_FILE

    return USAGE_LEDGER_FILE


def estimate_cost(model: str, totals: UsageTotals) -> float | None:
    """Dollars for this usage, or None when the model has no declared price.

    Reuses `bench/prices.py` rather than keeping a second price table: two tables would drift, and
    the one that drifts silently is the one nobody is looking at.

    None rather than 0.0 for an unpriced model -- an unmeasured cost must not look like a free one,
    which is the same rule `estimate_cost_usd` already follows.
    """
    try:
        from .bench.prices import estimate_cost_usd

        return estimate_cost_usd(model, totals.input_tokens, totals.output_tokens)
    except Exception:  # noqa: BLE001 - pricing is optional; usage is not
        return None


def record_turn(
    totals: UsageTotals,
    *,
    provider: str = "",
    model: str = "",
    source: str = "",
    agent: str = "",
    run_id: str = "",
    label: str = "",
) -> bool:
    """Append one turn's usage. Returns whether it was written; never raises.

    `source`/`agent`/`run_id` mirror the audit trail's identity fields so a cost can be attributed to
    the same actor the audit log names -- otherwise the two logs cannot be joined, and "which agent
    spent this" is unanswerable.
    """
    try:
        from .jsonl_log import append_jsonl

        path = ledger_path()
        entry = {
            "ts": time.time(),
            "provider": provider,
            "model": model,
            "input_tokens": totals.input_tokens,
            "output_tokens": totals.output_tokens,
            "cache_read_tokens": totals.cache_read_tokens,
            "cache_write_tokens": totals.cache_write_tokens,
            "requests": totals.request_count,
            "cost_usd": estimate_cost(model, totals),
        }
        for key, value in (("source", source), ("agent", agent), ("run_id", run_id), ("label", label)):
            if value:
                entry[key] = value
        # `append_jsonl` repairs a torn tail before writing. Without it, a crash mid-write leaves a
        # fragment with no newline and the NEXT entry is concatenated onto it -- two JSON objects on
        # one line, which every reader then skips. Measured: the entry after the torn line was lost
        # entirely. Same bug, same day, in audit.py; hence the shared helper.
        return append_jsonl(path, entry)
    except Exception:  # noqa: BLE001
        return False


def read_ledger(limit: int = 0) -> list[dict]:
    """The recorded turns, oldest first. Unreadable or malformed lines are skipped, not fatal."""
    try:
        from .jsonl_log import read_jsonl

        return read_jsonl(ledger_path(), limit=limit)
    except Exception:  # noqa: BLE001
        return []


def ledger_summary(entries: list[dict] | None = None) -> dict:
    """Totals across the ledger, with unpriced usage counted separately rather than as zero.

    `unpriced_turns` exists so a report can say "12 turns, 3 of which have no known price" instead of
    quietly producing a dollars figure that is missing a quarter of the usage.
    """
    rows = read_ledger() if entries is None else entries
    summary = {
        "turns": 0, "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "requests": 0, "cost_usd": 0.0, "unpriced_turns": 0,
    }
    for row in rows:
        summary["turns"] += 1
        for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_write_tokens", "requests"):
            try:
                summary[field] += int(row.get(field) or 0)
            except (TypeError, ValueError):
                pass
        cost = row.get("cost_usd")
        if cost is None:
            summary["unpriced_turns"] += 1
        else:
            try:
                summary["cost_usd"] += float(cost)
            except (TypeError, ValueError):
                summary["unpriced_turns"] += 1
    return summary


def record_turn_delta(
    agent: object,
    before: "UsageTotals",
    *,
    provider: str = "",
    source: str = "",
    agent_name: str = "",
    run_id: str = "",
) -> bool:
    """Record what THIS turn spent, given the totals before it started.

    The delta rather than the running total, because the total belongs to the agent's whole life and
    would count the same tokens again on every turn. Measured consequence of getting that wrong: a
    ten-turn session would report roughly 55 turns' worth of usage, and the error grows with the
    square of the turn count -- a cost report that is most wrong exactly when the spend is largest.

    Reads the agent defensively: a caller may hand over anything with or without `usage_totals`.
    """
    try:
        totals = getattr(agent, "usage_totals", None)
        if totals is None:
            return False
        delta = UsageTotals(
            input_tokens=max(0, int(getattr(totals, "input_tokens", 0) or 0) - before.input_tokens),
            output_tokens=max(0, int(getattr(totals, "output_tokens", 0) or 0) - before.output_tokens),
            request_count=max(0, int(getattr(totals, "request_count", 0) or 0) - before.request_count),
            cache_read_tokens=max(
                0, int(getattr(totals, "cache_read_tokens", 0) or 0) - before.cache_read_tokens
            ),
            cache_write_tokens=max(
                0, int(getattr(totals, "cache_write_tokens", 0) or 0) - before.cache_write_tokens
            ),
        )
        if delta.request_count == 0:
            # A turn that made no request (refused by the budget, or failed before the first call)
            # has nothing to bill and would otherwise add a zero row that inflates the turn count.
            return False
        return record_turn(
            delta,
            provider=provider,
            model=str(getattr(agent, "model", "") or ""),
            source=source,
            agent=agent_name,
            run_id=run_id,
            label="turn",
        )
    except Exception:  # noqa: BLE001 - never let accounting break a turn
        return False


def format_ledger_summary(summary: dict, *, table_date: str = "") -> str:
    """A report that never presents a partial dollars figure as a complete one."""
    if not summary.get("turns"):
        return "No usage recorded yet. The ledger fills as turns complete."
    lines = [
        f"Usage across {summary['turns']} recorded turn(s):",
        f"  Input tokens:  {summary['input_tokens']:,}",
        f"  Output tokens: {summary['output_tokens']:,}",
        f"  Requests:      {summary['requests']:,}",
    ]
    if summary.get("cache_read_tokens"):
        lines.append(f"  Cache reads:   {summary['cache_read_tokens']:,}")
    unpriced = summary.get("unpriced_turns", 0)
    if unpriced:
        lines.append(
            f"  Cost:          at least ${summary['cost_usd']:.4f} -- {unpriced} of "
            f"{summary['turns']} turn(s) have no declared price, so the true figure is higher"
        )
    else:
        lines.append(f"  Cost:          ${summary['cost_usd']:.4f}")
    if table_date:
        lines.append(f"  (prices dated {table_date}; older usage is reported at today's rates)")
    return "\n".join(lines)
