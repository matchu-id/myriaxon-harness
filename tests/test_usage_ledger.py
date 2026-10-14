"""Usage must survive the process, and must not double-count.

Found by an audit: every token count lived in memory only. `session_store` persisted messages and no
usage field, and grepping for `save_usage` or `usage.json` returned nothing. So totals reset on exit,
which means there was no cost history at all -- and a spend limit you cannot measure is not a limit.

The subtle part, and the reason for the delta tests: recording the agent's RUNNING TOTAL once per turn
counts the same tokens again every turn. The error grows with the square of the turn count, so a cost
report is most wrong exactly when the spend is largest.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from myriaxon import usage  # noqa: E402


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(usage, "ledger_path", lambda: path)
    return path


class FakeAgent:
    model = "deepseek-v4-flash"

    def __init__(self):
        self.usage_totals = usage.UsageTotals()


def test_a_turn_is_recorded_with_tokens_and_cost(ledger):
    agent = FakeAgent()
    before = copy.copy(agent.usage_totals)
    agent.usage_totals.add(1000, 200)
    assert usage.record_turn_delta(agent, before, provider="deepseek")
    row = usage.read_ledger()[-1]
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 200
    assert row["provider"] == "deepseek"
    assert isinstance(row["cost_usd"], float)


def test_the_delta_is_recorded_not_the_running_total(ledger):
    """THE regression. Three turns of 1000 tokens must total 3000, not 6000.

    Recording the total each turn yields 1000 + 2000 + 3000 = 6000: the same tokens billed again on
    every turn, growing with the square of the turn count.
    """
    agent = FakeAgent()
    for _ in range(3):
        before = copy.copy(agent.usage_totals)
        agent.usage_totals.add(1000, 200)
        usage.record_turn_delta(agent, before, provider="deepseek")

    assert len(usage.read_ledger()) == 3
    summary = usage.ledger_summary()
    assert summary["input_tokens"] == 3000, "the running total was recorded instead of the delta"
    assert summary["output_tokens"] == 600
    assert summary["requests"] == 3


def test_a_turn_that_spent_nothing_writes_nothing(ledger):
    """A refused turn (budget exhausted before any request) must not inflate the turn count."""
    agent = FakeAgent()
    before = copy.copy(agent.usage_totals)
    assert not usage.record_turn_delta(agent, before)
    assert usage.read_ledger() == []


def test_an_unpriced_model_is_not_reported_as_free(ledger):
    """An unmeasured cost must not look like a zero one -- the rule bench/prices.py already follows."""
    agent = FakeAgent()
    agent.model = "some-brand-new-model-nobody-has-priced"
    before = copy.copy(agent.usage_totals)
    agent.usage_totals.add(5000, 1000)
    usage.record_turn_delta(agent, before)
    assert usage.read_ledger()[-1]["cost_usd"] is None
    summary = usage.ledger_summary()
    assert summary["unpriced_turns"] == 1
    text = usage.format_ledger_summary(summary)
    assert "at least" in text, "a partial cost was presented as a complete one"


def test_an_agent_without_usage_totals_is_tolerated(ledger):
    class Bare:
        model = "x"

    assert not usage.record_turn_delta(Bare(), usage.UsageTotals())


def test_a_corrupt_ledger_line_is_skipped_not_fatal(ledger):
    """A crash mid-write leaves a partial line; the rest of the history must survive."""
    agent = FakeAgent()
    before = copy.copy(agent.usage_totals)
    agent.usage_totals.add(100, 10)
    usage.record_turn_delta(agent, before)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write('{"ts": 1, "input_tok')      # torn
    agent.usage_totals.add(100, 10)
    usage.record_turn_delta(agent, before)
    entries = usage.read_ledger()
    assert len(entries) == 2, f"a torn line cost us a good row: {entries}"


def test_the_ledger_is_append_only_across_calls(ledger):
    agent = FakeAgent()
    for _ in range(5):
        before = copy.copy(agent.usage_totals)
        agent.usage_totals.add(10, 1)
        usage.record_turn_delta(agent, before)
    lines = [line for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 5
    for line in lines:
        json.loads(line)      # every line must be independently parseable


def test_identity_fields_can_be_joined_to_the_audit_trail(ledger):
    """The audit log names run/locus/delegate; a cost must be attributable to the same actor, or
    "which agent spent this" is unanswerable."""
    agent = FakeAgent()
    before = copy.copy(agent.usage_totals)
    agent.usage_totals.add(50, 5)
    usage.record_turn_delta(agent, before, source="locus", agent_name="cowork", run_id="df721f52")
    row = usage.read_ledger()[-1]
    assert row["source"] == "locus"
    assert row["agent"] == "cowork"
    assert row["run_id"] == "df721f52"


def test_record_turn_never_raises_when_the_path_is_unwritable(tmp_path, monkeypatch):
    """Accounting must never break a turn."""
    blocked = tmp_path / "nope" / "usage.jsonl"

    def refuse():
        raise OSError("cannot write")

    monkeypatch.setattr(usage, "ledger_path", refuse)
    agent = FakeAgent()
    before = copy.copy(agent.usage_totals)
    agent.usage_totals.add(10, 1)
    assert usage.record_turn_delta(agent, before) is False
