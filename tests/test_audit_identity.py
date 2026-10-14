"""The audit log must name the actor and be tamper-evident.

Found by an audit against a platform spec. The entries recorded `ts/cwd/tool/args/is_error` and
nothing else, and `mission_control` said so in as many words -- "there is no run to attribute these
rows to" -- which made the log unable to answer the one question an audit trail exists for: what did
THIS agent do?

Two things are pinned here: identity, and a hash chain that makes a retrospective edit obvious.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from myriaxon import audit  # noqa: E402


@pytest.fixture()
def log(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "AUDIT_LOG_FILE", path)
    return path


def read(path: Path) -> list[dict]:
    """Only COMPLETE lines -- a torn final line from a crash mid-write is expected, not a failure."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def ctx_with(**kw):
    return SimpleNamespace(**kw)


def test_the_entry_names_the_run_and_its_kind(log):
    capability = SimpleNamespace(run_id="run-42", name="agent-turn")
    audit.log_tool_call("read_file", {"path": "x"}, False, ctx_with(run_capability=capability))
    entry = read(log)[0]
    assert entry["run"] == "run-42"
    assert entry["run_kind"] == "agent-turn"


def test_a_locus_turn_is_attributed_to_its_locus(log):
    """The marker the completion-authority gate reads is the same one the log records, so the two
    cannot disagree about who was acting."""
    audit.log_tool_call(
        "write_file", {"path": "x"}, False,
        ctx_with(locus_run_source="locus", locus_id="df721f52"),
    )
    entry = read(log)[0]
    assert entry["source"] == "locus"
    assert entry["locus"] == "df721f52"


def test_a_delegate_is_distinguishable_from_its_parent(log):
    audit.log_tool_call("read_file", {}, False, ctx_with(_in_subagent=True))
    assert read(log)[0]["delegate"] is True


def test_a_missing_context_still_logs(log):
    """Backward compatibility: an older caller passing no context must not lose its entry."""
    audit.log_tool_call("read_file", {"path": "x"}, False)
    entry = read(log)[0]
    assert entry["tool"] == "read_file"
    assert "run" not in entry


def test_a_hostile_context_does_not_break_the_log(log):
    """The recorder is best-effort: an attribute that raises must not kill the tool call."""
    class Hostile:
        @property
        def run_capability(self):
            raise RuntimeError("boom")

    audit.log_tool_call("read_file", {}, False, Hostile())
    # The entry must still exist, possibly without identity -- losing the record is worse.
    assert len(read(log)) >= 1


def test_entries_are_chained(log):
    for index in range(3):
        audit.log_tool_call("tool", {"n": index}, False)
    entries = read(log)
    assert len(entries) == 3
    assert entries[0]["prev"] == ""
    assert entries[1]["prev"] == entries[0]["chain"]
    assert entries[2]["prev"] == entries[1]["chain"]


def test_editing_a_row_is_detectable(log):
    """Tamper-EVIDENT: changing a logged action breaks every hash after it."""
    for index in range(3):
        audit.log_tool_call("tool", {"n": index}, False)
    lines = log.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["tool"] = "something-else"        # the edit an attacker would want to make
    lines[1] = json.dumps(tampered)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    entries = read(log)
    # The row's own chain no longer matches its content...
    recomputed = audit.hashlib.sha256(
        json.dumps({**entries[1], "chain": None}, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]
    assert recomputed != entries[1]["chain"]
    # ...and the next row still points at the OLD hash, so the break is visible from either side.
    assert entries[2]["prev"] == entries[1]["chain"]


def test_a_torn_final_line_does_not_lose_the_chain(log):
    """A crash mid-write leaves a partial line; the chain must resume from the last good one."""
    audit.log_tool_call("a", {}, False)
    audit.log_tool_call("b", {}, False)
    good = read(log)[-1]["chain"]
    with log.open("a", encoding="utf-8") as f:
        f.write('{"ts": 1, "tool": "torn"')      # no newline, invalid JSON
    audit.log_tool_call("c", {}, False)
    last = read(log)[-1]
    assert last["prev"] == good, "the chain did not resume from the last complete entry"


def test_secrets_in_arguments_are_still_redacted(log):
    """The new fields must not have weakened the existing redaction."""
    audit.log_tool_call("call", {"api_key": "sk-secret-value", "path": "ok"}, False)
    entry = read(log)[0]
    assert entry["args"]["api_key"] == "<redacted>"
    assert entry["args"]["path"] == "ok"
