"""End-to-end: does a guardrail rule refuse a real dispatch_tool call?

Written as a file rather than an inline command because the pattern contains backslashes, and PowerShell's
handling of them in a here-string silently produced a pattern that could not match -- which made the earlier
check report "allowed" for a rule that works.
"""

import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
from rich.console import Console

import myriaxon.guardrails as G
from myriaxon import tool_registry as TR

scratch = Path(tempfile.mkdtemp())

BLOCK = """---
name: no-force-push
event: PreToolUse
action: block
conditions:
  - field: command
    operator: regex_match
    pattern: 'git\\s+push\\s+.*--force'
---

Force-pushing rewrites published history.
"""

WARN = """---
name: warn-env
event: PreToolUse
action: warn
conditions:
  - field: path
    operator: contains
    pattern: .env
---

Reading .env may put secrets in the transcript.
"""

(scratch / "block.md").write_text(BLOCK, encoding="utf-8", newline="\n")
(scratch / "warn.md").write_text(WARN, encoding="utf-8", newline="\n")

# Point the loader at the scratch directory and force a re-read.
G.USER_RULES_DIR = scratch
G.PROJECT_RULES_SUBDIR = "no-such-project-dir"
TR.reset_guardrail_cache()

print("  rules loaded:", [r.name for r in TR._guardrail_rules()])
print("  problems    :", TR.guardrail_problems() or "none")
print()


class Ctx:
    session_id = "probe"
    guardrails_ignore_project = False

    def __init__(self):
        self.console = Console(file=io.StringIO())


ctx = Ctx()

cases = [
    ("git push origin main --force", "BLOCKED"),
    ("git push origin main", "allowed"),
    ("git push --force-with-lease", "BLOCKED"),
    ("ls -la", "allowed"),
]
for command, expected in cases:
    result = TR._check_guardrails("run_bash", {"command": command}, ctx)
    blocked = result is not None and result[1]
    got = "BLOCKED" if blocked else "allowed"
    mark = "OK " if got == expected else "XX "
    print(f"  [{mark}] {command!r:<32} -> {got} (expected {expected})")
    if blocked:
        print(f"          {result[0].splitlines()[0][:70]}")

print()
ctx2 = Ctx()
result = TR._check_guardrails("read_file", {"path": "/app/.env"}, ctx2)
print("  .env read blocked:", bool(result and result[1]), "(warn-only rule, so expected False)")
print("  warning printed  :", "guardrail" in ctx2.console.file.getvalue())
print("  warning text     :", ctx2.console.file.getvalue().strip()[:90])
