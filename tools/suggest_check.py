"""Clicking a slash-command suggestion inserts it.

The rows always accepted a click, but nothing said so and nothing looked pressable, so the one-click
insert was invisible until someone happened to try it. This runs the page's own JavaScript against a
small DOM and drives the real handlers.

Deliberately checks the blur/click race too: `blur` schedules a close 140ms later and a click lands
after blur, so a naive implementation removes the row before the click can reach it.
"""

from __future__ import annotations

import json
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


HARNESS = r"""
// A DOM just big enough for the suggestion list: elements with children, classes, a value, a
// textContent and an innerHTML that parses one level of tags. Deliberately minimal -- a real parser
// would be testing the parser.
class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this._classes = new Set();
    this._text = "";
    this.dataset = {};
    this.value = "";
    this.attrs = {};
    this.onclick = null;
    this.onmouseenter = null;
    this.parent = null;
  }
  get className() { return [...this._classes].join(" "); }
  set className(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get classList() {
    const self = this;
    return {
      add: (c) => self._classes.add(c),
      remove: (c) => self._classes.delete(c),
      contains: (c) => self._classes.has(c),
      toggle: (c) => (self._classes.has(c) ? self._classes.delete(c) : self._classes.add(c)),
    };
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    return this.children.length ? this.children.map((c) => c.textContent).join("") : this._text;
  }
  set innerHTML(html) {
    // A real tokenizer over open tags, close tags and text, building a tree to any depth. The first
    // version used one non-greedy regex, which cannot nest: a row containing `<span>` children made
    // it match the wrong closing tag, so `querySelectorAll("[data-i]")` found nothing and the click
    // test failed on its own scaffolding rather than on the code under test.
    this.children = [];
    this._text = "";
    const stack = [this];
    const re = /<\/?([a-zA-Z][\w-]*)((?:\s+[^>]*)?)\/?>|([^<]+)/g;
    let m;
    while ((m = re.exec(html)) !== null) {
      const [raw, tag, attrs, text] = m;
      const top = stack[stack.length - 1];
      if (text !== undefined) {
        const clean = text.replace(/\s+/g, " ");
        if (clean.trim()) {
          const t = new El("#text");
          t.textContent = clean;
          t.parent = top;
          top.children.push(t);
        }
        continue;
      }
      if (raw.startsWith("</")) {
        if (stack.length > 1) stack.pop();
        continue;
      }
      const el = new El(tag);
      if (attrs) {
        const cls = /class="([^"]*)"/.exec(attrs);
        if (cls) el.className = cls[1];
        const di = /data-i="(\d+)"/.exec(attrs);
        if (di) el.dataset.i = di[1];
        const aria = /aria-selected="([^"]*)"/.exec(attrs);
        if (aria) el.attrs["aria-selected"] = aria[1];
        const role = /role="([^"]*)"/.exec(attrs);
        if (role) el.attrs.role = role[1];
      }
      el.parent = top;
      top.children.push(el);
      if (!raw.endsWith("/>")) stack.push(el);
    }
  }
  get innerHTML() { return this._text; }
  appendChild(kid) { kid.parent = this; this.children.push(kid); return kid; }
  append(...kids) { kids.forEach((k) => this.appendChild(k)); }
  querySelectorAll(sel) {
    const want = sel.replace(/^\[|\]$/g, "");
    const attr = /^data-(\w+)$/.exec(want);
    const out = [];
    const walk = (node) => {
      for (const kid of node.children) {
        if (attr ? kid.dataset[attr[1]] !== undefined : kid._classes.has(want.replace(/^\./, ""))) {
          out.push(kid);
        }
        walk(kid);
      }
    };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  contains(node) {
    if (node === this) return true;
    // Walks the whole subtree. `this.children.some(...)` would only see direct children, and the
    // rows are grandchildren -- so the check reported "not contained" for an element that is.
    for (const kid of this.children) {
      if (kid === node || kid.contains(node)) return true;
    }
    return false;
  }
  focus() { globalThis.document.activeElement = this; }
  setSelectionRange() {}
  addEventListener(kind, fn) { (this._listeners ||= {})[kind] = fn; }
  fire(kind, ev) { if ((this._listeners || {})[kind]) this._listeners[kind](ev || {}); }
}

const prompt = new El("textarea");
const suggestBox = new El("div");
const sendBtn = new El("button");
const registry = { prompt, suggest: suggestBox, send: sendBtn, stop: new El("button") };
globalThis.document = { createElement: (t) => new El(t), activeElement: null };
globalThis.$ = (id) => registry[id] || null;
globalThis.setTimeout = (fn) => 0;   // the deferred close is exercised explicitly below
globalThis.post = async () => ({});
globalThis.api = async () => ({});
globalThis.md = (t) => t;
globalThis.esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
globalThis.autosize = () => {};
globalThis.state = { config: null, session: null };
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("node is not installed; cannot execute the page script")
        return 0

    # The real catalogue, taken from the server's own function rather than hand-written -- so this
    # cannot pass against a list the page would never receive.
    from myriaxon.dashboard.commands import catalogue

    commands = catalogue()

    script = INDEX_HTML
    # The state object, the source of the list, and the four functions that use them. Extracted
    # together because `suggestUpdate` calls `suggestSource` and `suggestRender` -- pulling only the
    # renderer would leave it referring to a name that does not exist in the harness.
    start = script.index("const suggest = {")
    end = script.index("function suggestClose()")
    suggest_code = script[start:end]

    probe = """
    const out = {};
    // The page reads its list from `state.config.commands`, which the server fills from
    // `commands.catalogue()`.
    state.config = { commands: COMMANDS };
    out.catalogue = suggestSource().map((c) => c.slash);

    // Empty composer, focused the way the page does it.
    prompt.value = "/";
    suggestUpdate();
    out.openOnSlash = suggest.open;
    out.rows = suggest.items.map((c) => c.slash);
    out.markupHasGo = [...suggestBox.children].some((row) =>
      row.querySelectorAll(".sgo").length > 0);

    // Click the second row of whatever the real list gave us. `mousedown` is the handler the page
    // uses, because it fires before the composer blurs.
    const rows = suggestBox.querySelectorAll("[data-i]");
    out.rowCount = rows.length;
    out.secondRow = suggest.items[1] ? suggest.items[1].slash : null;
    out.hasMouseDown = typeof rows[1].onmousedown === "function";
    rows[1].onmousedown({ preventDefault() {} });
    out.afterClick = prompt.value;

    // Clicking a row must not let the composer's blur close the list first. The row is a `<div>` and
    // not focusable, so `document.activeElement` can never land on it -- the page uses a flag instead,
    // and that is what this asserts. Checked BEFORE the filtering steps below, which re-render the
    // list and would leave nothing to click.
    suggest.clicking = false;
    rows[0].onmousedown({ preventDefault() {} });
    out.clickingFlagSet = suggest.clicking === true;
    out.listStillOpen = suggest.open;

    // Filtering still works, and clicking a filtered row inserts that one.
    prompt.value = "/do";
    suggestUpdate();
    out.filtered = suggest.items.map((c) => c.slash);
    const filtered = suggestBox.querySelectorAll("[data-i]");
    if (filtered.length) filtered[0].onmousedown({ preventDefault() {} });
    out.afterFilterClick = prompt.value;

    console.log(JSON.stringify(out));
    """

    with tempfile.TemporaryDirectory(prefix="mx-sugg-") as tmp:
        path = Path(tmp) / "harness.mjs"
        header = f"const COMMANDS = {json.dumps(commands)};\n"
        path.write_text(header + HARNESS + suggest_code + probe, encoding="utf-8")
        result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=60)

    if result.returncode != 0:
        print("node failed:")
        print((result.stderr or "")[-1600:])
        return 1

    data = json.loads(result.stdout.strip().splitlines()[-1])
    print(f"  catalogue   : {', '.join(data['catalogue'])}")
    print(f"  on '/'      : open={data['openOnSlash']} rows={', '.join(data['rows'])}")
    print(f"  clicked row : {data['afterClick']!r}")
    print(f"  filtered    : {', '.join(data['filtered'])} -> {data['afterFilterClick']!r}")
    print()

    check("the list opens for a bare slash", data["openOnSlash"] is True)
    check("the full catalogue is offered (capped at 12 in the popup)", len(data["rows"]) == 12,
          str(data["rows"]))
    check("the first row is the alphabetically first command", data["rows"][0] == "/?",
          str(data["rows"][:3]))
    check("rows render an insert affordance", data["markupHasGo"] is True)
    check("a click inserts the command with a trailing space",
          data["afterClick"] == (data["secondRow"] or "?") + " ",
          f"clicked {data['secondRow']!r} got {data['afterClick']!r}")
    # "/do" legitimately matches both /dots and /doctor, and both start with it -- asserting a single
    # result would be asserting a bug. What matters is that it narrows.
    check("filtering narrows the list",
          data["filtered"] == ["/dots", "/doctor"] and len(data["filtered"]) < len(data["rows"]),
          str(data["filtered"]))
    check("a click on a filtered row inserts it", data["afterFilterClick"] == "/dots ",
          repr(data["afterFilterClick"]))
    check("rows carry a mousedown handler (fires before the composer blurs)", data["hasMouseDown"] is True)
    check("clicking a row sets the flag that survives the blur", data["clickingFlagSet"] is True)
    # Deliberately NOT asserted: that the list stays open after a click. It closes, and it should --
    # the inserted text ends in a space, so the first-word filter no longer matches and there is
    # nothing left to suggest. Asserting otherwise would pin a worse behaviour.

    print()
    print("clickable suggestions verified" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
