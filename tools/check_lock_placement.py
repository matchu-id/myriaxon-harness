"""Verify the compaction-lock placement in all three providers.

The check that matters is not "the marker exists" but "the marker is inside compact(), and BEFORE the summariser
call". A marker in the wrong method, or after the call, would compile, run, and detect nothing.
"""

from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\myriaxon")
FILES = ("agent.py", "openai_agent.py", "gemini_agent.py")

ok_all = True
for name in FILES:
    src = (ROOT / name).read_text(encoding="utf-8")

    compact_at = src.find("    def compact(")
    start_at = src.find("record_compaction_start")
    applied_at = src.find("_mark_compaction_end(applied=True")
    refused_at = src.find("_mark_compaction_end(applied=False")
    record_at = src.find("record_compaction(old, summary,")

    # The summariser call: the first client call AFTER the start marker.
    call_at = -1
    for needle in (".create(", ".generate_content(", ".interactions.create("):
        found = src.find(needle, start_at) if start_at > 0 else -1
        if found > 0 and (call_at < 0 or found < call_at):
            call_at = found
    # For Gemini the call is nested one level in; search from the try that follows the marker.
    if call_at < 0:
        call_at = src.find(".create(", start_at)

    checks = {
        "compact() found": compact_at > 0,
        "start inside compact()": start_at > compact_at > 0,
        "start BEFORE the summariser call": 0 < start_at < call_at,
        "end-on-applied before record_compaction": 0 < applied_at < record_at,
        "end-on-refusal before the return": 0 < refused_at,
        "exactly one start": src.count("record_compaction_start") == 1,
        "exactly one end implementation": src.count("record_compaction_end") == 1,
        "two end call sites": src.count("_mark_compaction_end(applied") == 2,
        "helpers defined once": src.count("def _mark_compaction_end") == 1,
    }
    good = all(checks.values())
    ok_all = ok_all and good
    print(f"  {name:<20} {'OK' if good else 'FAIL'}")
    for label, passed in checks.items():
        print(f"      [{'ok' if passed else 'XX'}] {label}")
    if not good:
        print(f"      offsets: compact={compact_at} start={start_at} call={call_at} "
              f"applied={applied_at} refused={refused_at} record={record_at}")
    print()

print("  ALL THREE CORRECT" if ok_all else "  PROBLEMS FOUND")
raise SystemExit(0 if ok_all else 1)
