"""Render the stat cluster the way the browser will, and check the structure.

Chrome cannot be launched in this environment, so the DOM cannot be inspected directly. This runs the
page's own JavaScript in Node against a minimal DOM shim and inspects the result -- which checks the
part that matters (does the cluster get built, with which labels and values) without needing a
browser.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon.dashboard.ui import INDEX_HTML  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail and not ok else ''}")


def extract(script: str) -> str:
    start = script.index("function statCell(")
    end = script.index("function handle(e)")
    return script[start:end]


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("node is not installed; cannot execute the page script")
        return 0

    script = extract(INDEX_HTML)

    harness = """
    // A DOM small enough for the three functions under test: create elements, append them, and let
    // the shim be queried afterwards. Deliberately not a general DOM -- a big shim would be testing
    // the shim.
    class El {
      constructor(tag) { this.tagName = tag; this.children = []; this.className = ""; this._text = ""; this.title = ""; }
      set textContent(v) { this._text = String(v); this.children = []; }
      get textContent() { return this.children.length ? this.children.map(c => c.textContent).join("") : this._text; }
      set innerHTML(v) { this.children = []; this._text = v === "" ? "" : String(v); }
      append(...kids) { this.children.push(...kids); }
      appendChild(kid) { this.children.push(kid); return kid; }
    }
    const host = new El("div");
    const registry = { turninfo: host };
    globalThis.document = { createElement: (t) => new El(t) };
    globalThis.$ = (id) => registry[id] || null;
    """ + script + """
    function dump(usage, seconds) {
      renderTurnInfo(usage, seconds);
      return host.children.map((c) => {
        const k = c.children.find((x) => x.className === "k");
        const v = c.children.find((x) => String(x.className).startsWith("v"));
        return { key: k ? k.textContent : null, value: v ? v.textContent : null, tone: v ? v.className : null };
      });
    }
    const cases = {
      rich: dump({ steps: 1540, output_tokens: 28700, total_tokens: 640_000_000, cache_read_tokens: 5, cache_hit_rate: 0.998, request_count: 15, turn_count: 15, input_tokens: 900, cache_write_tokens: 10 }, 100),
      fresh: dump({ request_count: 1, output_tokens: 15, total_tokens: 4895, input_tokens: 4880, steps: 0, cache_read_tokens: 0, cache_hit_rate: 0 }, 3.7),
      empty: dump({}, 0),
      lowcache: dump({ request_count: 4, total_tokens: 200000, cache_read_tokens: 100, cache_hit_rate: 0.12, steps: 3 }, 12),
      one: dump({ request_count: 1, steps: 1, total_tokens: 100 }, 2),
    };
    console.log(JSON.stringify({ cases, title: host.title }));
    """

    with tempfile.TemporaryDirectory(prefix="mx-ui-") as tmp:
        path = Path(tmp) / "harness.mjs"
        path.write_text(harness, encoding="utf-8")
        result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=60)

    if result.returncode != 0:
        print("node failed:")
        print((result.stderr or "")[-1500:])
        return 1

    payload = json.loads(result.stdout.strip().splitlines()[-1])
    cases = payload["cases"]

    print("rendered clusters")
    for name, cells in cases.items():
        rendered = "  ".join(f"{c['key']}={c['value']}" for c in cells)
        print(f"  {name:9} {rendered or '(empty)'}")

    print()
    rich = {c["key"]: c for c in cases["rich"]}
    check("rich case has a Steps cell", "Steps" in rich)
    check("rich case has a Requests cell", "Requests" in rich)
    check("rich case has a Tokens cell", "Tokens" in rich)
    check("640M renders as 640M", rich.get("Tokens", {}).get("value") == "640.0M",
          str(rich.get("Tokens")))
    check("cache 99.8% keeps one decimal", rich.get("Cache hit", {}).get("value") == "99.8%",
          str(rich.get("Cache hit")))
    check("a high cache rate is marked good", "good" in (rich.get("Cache hit", {}).get("tone") or ""),
          str(rich.get("Cache hit", {}).get("tone")))

    low = {c["key"]: c for c in cases["lowcache"]}
    check("a low cache rate is marked warn", "warn" in (low.get("Cache hit", {}).get("tone") or ""),
          str(low.get("Cache hit", {})))

    fresh = {c["key"]: c for c in cases["fresh"]}
    check("4,895 tokens renders as 4.9k", fresh.get("Tokens", {}).get("value") == "4.9k",
          str(fresh.get("Tokens")))
    check("a single request is singular", "Request" in fresh and "Requests" not in fresh,
          str(list(fresh)))

    one = {c["key"]: c for c in cases["one"]}
    check("one step is singular", one.get("Steps", {}).get("value") == "1")

    check("an empty usage renders no cells", cases["empty"] == [], str(cases["empty"]))
    check("the hover title lists the exact figures", "input:" in payload["title"], payload["title"][:80])

    print()
    print("stat cluster verified" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
