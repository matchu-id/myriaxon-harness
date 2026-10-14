"""Check the dashboard's markdown renderer against the shapes models actually emit.

    python scripts/check_markdown_render.py

Extracts the md()/inline() functions out of the UI's script and runs them under Node, so the
renderer is tested directly rather than through a browser. The input is the kind of answer that was
previously shown with its `**` and `###` marks visible.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SAMPLE = """I am **Myriaxon**, an interactive AI coding assistant.

### 1. **Codebase Exploration & Analysis**
- **Search & Navigation:** find files (`glob_search`) and read source
- **Architecture Mapping:** analyze structure

---

| Feature | Status | Notes |
|---|---|---|
| Search | ready | fast |
| Tables | new | aligned |

```python
def hello():
    return "world"
```

> a quoted note

See [the docs](https://example.com) and `inline_code` and *italics*.
"""

def main() -> int:
    node = shutil.which("node")
    if not node:
        print("node not available; cannot execute the renderer")
        return 2

    ui = (REPO / "myriaxon" / "dashboard" / "ui.py").read_text(encoding="utf-8")
    # Pull the three functions out of the page's script block.
    def grab(name, pattern):
        match = re.search(pattern, ui, re.S)
        if not match:
            raise SystemExit(f"could not find {name} in ui.py")
        return match.group(0)

    esc = grab("esc", r"const esc = \(s\) =>.*?;\n")
    inline = grab("inline", r"  function inline\(text\) \{.*?\n  \}\n")
    split_row = grab("splitRow", r"  function splitRow\(line\) \{.*?\n  \}\n")
    md = grab("md", r"  function md\(src\) \{.*?\n  \}\n")

    harness = f"""
{esc}
{inline}
{split_row}
{md}
const sample = {json.dumps(SAMPLE)};
const out = md(sample);
const checks = [
  ["bold rendered", out.includes("<strong>Myriaxon</strong>")],
  ["no literal bold marks", !out.includes("**Myriaxon**")],
  ["h3 rendered", out.includes("<h3>")],
  ["no literal hashes", !/^### /m.test(out) && !out.includes("### 1.")],
  ["list rendered", out.includes("<ul>") && out.includes("<li>")],
  ["bold inside list item", out.includes("<strong>Search &amp; Navigation:</strong>")],
  ["horizontal rule rendered", out.includes("<hr>")],
  ["table rendered", out.includes("<table>") && out.includes("<th>Feature</th>")],
  ["table rows", out.includes("<td>Search</td>") && out.includes("<td>fast</td>")],
  ["table separator not shown", !out.includes("|---|") && !out.includes("<p>|---")],
  ["fenced code rendered", out.includes("<pre><code>") && out.includes("def hello()")],
  ["fence markers gone", !out.includes("```")],
  ["blockquote rendered", out.includes("<blockquote>")],
  ["inline code rendered", out.includes("<code>inline_code</code>")],
  ["italics rendered", out.includes("<em>italics</em>")],
  ["link rendered", out.includes('<a href="https://example.com"')],
  ["link text rendered", out.includes(">the docs</a>")],
  ["no literal link syntax", !out.includes("[the docs]")],
  ["html escaped", out.includes("&amp;")],
];
const failed = checks.filter(([, ok]) => !ok).map(([name]) => name);
console.log(JSON.stringify({{ failed, passed: checks.length - failed.length, total: checks.length }}));
console.log("---- rendered preview ----");
console.log(out.slice(0, 700));
"""
    # Written into the workspace rather than a temp directory: tempfile.TemporaryDirectory is
    # denied in this environment (and on the managed machine generally), which made the check fail
    # for reasons unrelated to markdown.
    scratch = REPO / "bench-out" / "mdcheck"
    scratch.mkdir(parents=True, exist_ok=True)
    script = scratch / "render.js"
    script.write_text(harness, encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)

    if result.returncode != 0:
        print("node failed:", result.stderr[:800])
        return 1
    first, _, rest = result.stdout.partition("\n")
    outcome = json.loads(first)
    print(f"markdown checks: {outcome['passed']}/{outcome['total']} passed")
    if outcome["failed"]:
        print("FAILED:", outcome["failed"])
    print(rest)
    return 0 if not outcome["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
