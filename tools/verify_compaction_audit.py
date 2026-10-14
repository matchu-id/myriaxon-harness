#!/usr/bin/env python3
"""Verify compaction auditability by exercising every discard site.

The gap being closed: `compaction.py` dropped information in three ways and recorded none of them, so an audit
could not establish what the agent had seen, and compaction could not be evaluated because the before/after
pair did not exist.

Each check below drives a real code path with real input, then asserts the audit contains the discarded bytes
and that the digest still verifies. Nothing is asserted from reading the source.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import compaction, compaction_audit  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        recorder = compaction_audit.Recorder(session="verify", enabled=True, root=root)
        compaction_audit.set_active(recorder)

        # ---------------------------------------------------------------- 1. prune sites
        print("=== 1. every prune path records the content it destroys ===")
        big = "X" * 5000
        openai_msg = {"role": "tool", "name": "read_file", "content": big}
        gemini_msg = {"type": "function_result", "name": "read_file", "result": "G" * 4000}
        anthropic_msg = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "A" * 3000}],
        }

        # Age them past the threshold so pruning actually fires. Age is measured in *turns before the current
        # one*, so a stale payload has to sit near the START of the history with real turns after it. The first
        # version of this test appended them at the end, which made them age 0 and pruned nothing -- the test
        # was wrong, not the code.
        history: list[dict] = [
            {"role": "user", "content": "start"},
            openai_msg,
            gemini_msg,
            anthropic_msg,
        ]
        for _ in range(6):
            history.append({"role": "assistant", "content": "ok"})
            history.append({"role": "user", "content": "next"})

        pruned = compaction.prune_stale_tool_results(history, max_age_turns=2, min_chars=100)
        check("pruning fired on all three message shapes", pruned == 3, f"pruned={pruned}")

        entries = recorder.read_log()
        prunes = [e for e in entries if e["kind"] == "prune"]
        check("three prune records written", len(prunes) == 3, f"got {len(prunes)}")

        # The whole point: the destroyed bytes must be recoverable and the digest must verify.
        recovered = {}
        for entry in prunes:
            content = recorder.retrieve(entry["digest"])
            recovered[entry["chars"]] = content
        check("openai payload recovered", recovered.get(5000) == big)
        check("gemini payload recovered", recovered.get(4000) == "G" * 4000)
        check("anthropic payload recovered", recovered.get(3000) == "A" * 3000)

        mismatches = [
            e["digest"] for e in prunes
            if compaction_audit._digest(recovered.get(e["chars"], "") or "") != e["digest"]
        ]
        check("digests verify against the stored bytes", not mismatches, f"mismatched: {mismatches}")

        # The placeholder must survive untouched -- the audit is additive, not a behaviour change.
        check("placeholder text unchanged",
              str(openai_msg["content"]).startswith("[pruned:") and "5,000 chars removed" in openai_msg["content"],
              str(openai_msg["content"])[:80])

        # ---------------------------------------------------------------- 2. transcript omissions
        print()
        print("=== 2. the transcript records what the summariser was denied ===")
        before = len(recorder.read_log())
        convo = [
            {"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": "ok"},
            {"role": "tool", "name": "read_file", "content": "TOOL OUTPUT " * 50},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_9", "content": "ANTHROPIC RESULT"},
                {"type": "image", "source": {"data": "base64stuff"}},
            ]},
        ]
        transcript = compaction.render_transcript(convo, "anthropic")
        after = recorder.read_log()[before:]
        omissions = [e for e in after if e["kind"] == "omission"]

        check("transcript still renders", "do the thing" in transcript and "ok" in transcript)
        check("omissions recorded", len(omissions) >= 3, f"got {len(omissions)}")
        kinds = {e["extra"].get("omission") for e in omissions}
        check("tool_result and attachment both captured",
              {"tool_result", "attachment"} <= kinds, f"kinds={kinds}")
        check("the denied tool output is recoverable",
              any(recorder.retrieve(e["digest"]) == "TOOL OUTPUT " * 50 for e in omissions))
        check("the anthropic result is recoverable",
              any(recorder.retrieve(e["digest"]) == "ANTHROPIC RESULT" for e in omissions))
        check("placeholder remains in the transcript",
              "[output omitted from summary]" in transcript)

        # ---------------------------------------------------------------- 3. summary pair
        print()
        print("=== 3. a summary is recorded so it can be judged against its input ===")
        inp = "the full old history text"
        out = "## Key facts\n- something"
        recorder.record_transcript(inp)
        recorder.record_summary(out)
        tail = recorder.read_log()[-2:]
        check("transcript input recorded", any(e["kind"] == "transcript" for e in tail))
        check("summary recorded", any(e["kind"] == "summary" for e in tail))
        check("both recoverable",
              recorder.retrieve([e for e in tail if e["kind"] == "summary"][0]["digest"]) == out)

        # ---------------------------------------------------------------- 3b. whole-compaction recording
        print()
        print("=== 3b. a successful compaction records what left the conversation ===")
        # This is the path every agent backend takes: `old` messages are replaced by a summary, and before this
        # the only record of what was summarised was the summary the model chose to write. A failed compaction
        # is already safe in all three backends -- each returns before replacing self.messages -- so this
        # covers the successful case, which is the one that actually discards history.
        old_messages = [
            {"role": "user", "content": "the original question about the bug"},
            {"role": "assistant", "content": "the diagnosis I gave"},
            {"role": "tool", "name": "grep_search", "content": "MATCH LINE " * 30},
        ]
        summary_text = "## Key facts\n- the bug was in the parser"
        before_compaction = len(recorder.read_log())
        compaction.record_compaction(
            old_messages, summary_text, "anthropic", kept_turns=3
        )
        new_entries = recorder.read_log()[before_compaction:]
        kinds_in = {e["kind"] for e in new_entries}
        check("compaction records both sides",
              {"transcript", "summary"} <= kinds_in, f"kinds={kinds_in}")

        summary_entry = next((e for e in new_entries if e["kind"] == "summary"), None)
        check("the summary is recoverable",
              summary_entry is not None and recorder.retrieve(summary_entry["digest"]) == summary_text)

        # The transcript recorded must be the real rendering of the old messages, not a placeholder -- otherwise
        # there is still no way to tell a summary that dropped something from an input that lacked it.
        transcript_entry = next((e for e in new_entries if e["kind"] == "transcript"), None)
        recorded_transcript = recorder.retrieve(transcript_entry["digest"]) if transcript_entry else ""
        check("the summariser's input is recoverable verbatim",
              "the original question about the bug" in recorded_transcript
              and "the diagnosis I gave" in recorded_transcript,
              repr(recorded_transcript[:80]))
        check("the recorded transcript matches render_transcript",
              recorded_transcript == compaction.render_transcript(old_messages, "anthropic"))

        # Recording must never break a compaction that already succeeded.
        compaction_audit.set_active(compaction_audit.Recorder(session="", enabled=True, root=Path("/nonexistent/x")))
        try:
            compaction.record_compaction(old_messages, summary_text, "anthropic", kept_turns=3)
            check("recording failure does not raise into the caller", True)
        except Exception as exc:  # noqa: BLE001
            check("recording failure does not raise into the caller", False, repr(exc))
        compaction_audit.set_active(recorder)

        # ---------------------------------------------------------------- 4. safety properties
        print()
        print("=== 4. the audit cannot break a session ===")
        check("no recorder failures", recorder.failures == 0, f"failures={recorder.failures}")

        disabled = compaction_audit.Recorder(session="off", enabled=False, root=root)
        compaction_audit.set_active(disabled)
        msg = {"role": "tool", "name": "t", "content": "Z" * 5000}
        hist = [{"role": "user", "content": "a"}, msg] + [
            {"role": "user", "content": "x"} for _ in range(8)
        ]
        compaction.prune_stale_tool_results(hist, max_age_turns=1, min_chars=100)
        check("disabled recorder writes nothing", disabled.read_log() == [])
        check("pruning still works when auditing is off", str(msg["content"]).startswith("[pruned:"))

        compaction_audit.set_active(None)
        msg2 = {"role": "tool", "name": "t", "content": "Y" * 5000}
        hist2 = [{"role": "user", "content": "a"}, msg2] + [
            {"role": "user", "content": "x"} for _ in range(8)
        ]
        try:
            compaction.prune_stale_tool_results(hist2, max_age_turns=1, min_chars=100)
            check("no recorder installed does not raise", str(msg2["content"]).startswith("[pruned:"))
        except Exception as exc:  # noqa: BLE001
            check("no recorder installed does not raise", False, repr(exc))

        print()
        print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
        for label in FAIL:
            print(f"  FAILED: {label}")
        return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
