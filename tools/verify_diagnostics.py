#!/usr/bin/env python3
"""Verify post-edit diagnostics.

## The gap this closes

Opencode queries a language server after every `edit`/`write`/`apply_patch` and appends the error block to the tool
result. Myriaxon had nothing, so **a change that compiles nowhere looked exactly like a change that works** — the
only signal was whatever test the agent chose to run next, and it had no reason to run one.

## What these checks are mostly about: NOT crying wolf

Building this produced a defect worth pinning. The first version selected pyright's `dist/pyright.js` entry point,
which on this host reported **three false errors on correct code** — `"int" is not defined` — because it could not
resolve a Python environment. A checker that is wrong about correct code is **worse than no checker**, because the
model learns to ignore the diagnostic block entirely.

So the central checks are that clean code produces nothing, that a real defect produces something, and that a
checker which fails either test is **refused rather than used**.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import diagnostics as D  # noqa: E402
from myriaxon import tools  # noqa: E402  (for the write-path check)

PASS: list[str] = []
FAIL: list[str] = []

CLEAN = "def add(a: int, b: int) -> int:\n    return a + b\n\n\ndef total(xs: list[int]) -> int:\n    return sum(xs)\n"
TYPE_ERROR = 'def add(a: int, b: int) -> int:\n    return a + b\n\n\nx: int = add("a", "b")\n'
SYNTAX_ERROR = "def f(:\n    pass\n"
ATTR_ERROR = "class T:\n    pass\n\n\nt = T()\nt.missing_attribute()\n"


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def workspace() -> Path:
    return Path(tempfile.mkdtemp())


def write(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def main() -> int:
    root = workspace()

    print("=== 1. tier 1 ALWAYS works, with no dependencies ===")
    # The stdlib tier is what runs on a bare host, so it must never depend on anything being installed.
    bad_syntax = write(root, "bad.py", SYNTAX_ERROR)
    report = D.check([bad_syntax], root, deep=False)
    check("a syntax error is caught with no external tool", bool(report.errors), report.render()[:100])
    check("  the tier is named", any("syntax" in t for t in report.tiers), str(report.tiers))
    check("  the line is reported", any(d.line > 0 for d in report.diagnostics), str(report.diagnostics))
    check("  and the column", any(d.column > 0 for d in report.diagnostics), str(report.diagnostics))

    clean = write(root, "clean.py", CLEAN)
    shallow = D.check([clean], root, deep=False)
    check("clean Python produces nothing in tier 1", shallow.clean, shallow.render()[:120])

    bad_json = write(root, "bad.json", '{"a": 1,}')
    json_report = D.check([bad_json], root, deep=False)
    check("invalid JSON is caught", bool(json_report.errors), json_report.render()[:100])
    good_json = write(root, "good.json", '{"a": 1}')
    check("valid JSON is clean", D.check([good_json], root, deep=False).clean)

    bad_toml = write(root, "bad.toml", "a = = 1\n")
    check("invalid TOML is caught or the tier is reported absent",
          bool(D.check([bad_toml], root, deep=False).errors)
          or "no checker for .toml" in " ".join(D.check([bad_toml], root, deep=False).unavailable),
          str(D.check([bad_toml], root, deep=False).unavailable))

    print()
    print("=== 2. THE DEFECT: no false errors on correct code ===")
    # This is the check that matters. The bundled pyright entry point failed it, and the module must refuse such a
    # checker rather than using it.
    deep = D.check([clean], root)
    check("correct, well-typed code produces NO errors", not deep.errors,
          f"{len(deep.errors)} error(s): " + "; ".join(d.message[:40] for d in deep.errors[:3]))
    check("  and it does not claim a problem it cannot see", not deep.diagnostics or all(
        d.severity != D.ERROR for d in deep.diagnostics), deep.render()[:160])

    print()
    print("=== 3. a checker that is wrong about correct code is REFUSED ===")
    # `_probe_checker` runs every candidate against known-good code and rejects any that reports errors. Without
    # this, a bad entry point silently poisons every diagnostic the model ever sees.
    usable, entry, note = D._probe_checker("pyright")
    if usable:
        check("the probed pyright entry is a working one", "dist" not in entry.replace("\\", "/").split("/package/")[-1][:5],
              entry)
        check("  and the probe agreed it is clean", note == "", note)
        # The rejected candidate is still discoverable, and must NOT be what find_checker returns.
        candidates = D._checker_candidates("pyright")
        nested = [c for c in candidates if "dist" in c]
        if nested:
            check("  the bundled CLI was tried and is not selected",
                  D.find_checker("pyright") != nested[0], f"candidates: {candidates}")
    else:
        check("pyright absent on this host, and reported as such",
              "not" in note.lower() or "present but" in note, note)

    print()
    print("=== 4. a REAL type error IS caught (when the tier is available) ===")
    typed = write(root, "typed.py", TYPE_ERROR)
    report = D.check([typed], root)
    if D.find_checker("pyright"):
        check("pyright catches a wrong argument type", bool(report.errors), report.render()[:140])
        check("  the message names the parameter", any("parameter" in d.message or "int" in d.message
                                                       for d in report.diagnostics),
              "; ".join(d.message[:50] for d in report.diagnostics[:2]))
    else:
        check("pyright unavailable, so types were reported as NOT checked",
              any("NOT checked" in n or "not installed" in n for n in report.unavailable),
              str(report.unavailable))

    attr = write(root, "attr.py", ATTR_ERROR)
    attr_report = D.check([attr], root)
    if D.find_checker("pyright"):
        check("pyright catches a missing attribute", bool(attr_report.errors),
              attr_report.render()[:120])

    print()
    print("=== 5. an unsupported file is REPORTED, never silently clean ===")
    # The dangerous failure: "no problems found" when nothing was examined.
    other = write(root, "thing.zzz", "not code")
    report = D.check([other], root)
    check("an unchecked suffix produces a note", bool(report.unavailable), str(report.unavailable))
    check("  the note says it was NOT checked", any("not checked" in n for n in report.unavailable),
          str(report.unavailable))
    check("  and the render includes it", "note:" in report.render(), report.render()[:140])

    print()
    print("=== 6. shallow mode does not claim more than it did ===")
    shallow = D.check([clean], root, deep=False)
    check("shallow mode names only tier 1", all("pyright" not in t for t in shallow.tiers),
          str(shallow.tiers))
    check("  and says nothing was type-checked", "pyright" not in shallow.render(), shallow.render()[:120])

    print()
    print("=== 7. it never raises, whatever it is handed ===")
    # Every caller is on a write path. A diagnostic that can fail a write is worse than no diagnostic.
    for label, arg in (
        ("an empty list", []),
        ("a missing file", [root / "nope.py"]),
        ("a directory", [root]),
        ("a path of None-like text", ["\x00bad"]),
    ):
        try:
            result = D.check(arg, root)
            check(f"handles {label}", isinstance(result, D.DiagnosticReport),
                  f"{len(result.diagnostics)} finding(s)")
        except Exception as exc:  # noqa: BLE001
            check(f"handles {label}", False, f"{type(exc).__name__}: {exc}")

    print()
    print("=== 8. it is cheap enough for every edit ===")
    import time

    several = [write(root, f"f{i}.py", CLEAN) for i in range(3)]
    D.check(several, root)  # warm the entry probe
    start = time.perf_counter()
    D.check([clean], root, deep=False)
    shallow_time = time.perf_counter() - start
    check("a shallow check is fast", shallow_time < 0.25, f"{shallow_time * 1000:.0f} ms")

    print()
    print("=== 9. WIRED: the write path actually reports it ===")
    # A diagnostic module nothing calls is a library. This is the check that would catch it being unwired.
    import io

    from rich.console import Console

    from myriaxon.tool_context import ToolContext

    stage = workspace()
    ctx = ToolContext(client=None, model="m", max_tokens=1, console=Console(file=io.StringIO()),
                      yolo=True, tools=[])
    import os

    here = os.getcwd()
    try:
        os.chdir(stage)
        text, failed = tools._write_file({"path": "typed.py", "content": TYPE_ERROR}, ctx)
        check("the write still SUCCEEDS", not failed, text[:80])
        check("  and the result carries diagnostics", "Diagnostics" in text, text[:160])
        check("  and it names the tier that ran", "syntax" in text, text[:200])

        clean_text, clean_failed = tools._write_file({"path": "ok.py", "content": CLEAN}, ctx)
        check("a clean write is clean", not clean_failed and "no problems" in clean_text,
              clean_text[:160])
        check("  and produces no ERROR line", "[error]" not in clean_text, clean_text[:160])
    finally:
        os.chdir(here)

    check("the helper is wired into write_file", "_diagnostic_suffix(path)" in
          (REPO / "myriaxon" / "tools.py").read_text(encoding="utf-8"))
    source = (REPO / "myriaxon" / "tools.py").read_text(encoding="utf-8")
    check("  and into edit_file too", source.count("_diagnostic_suffix(path)") >= 2,
          f"{source.count('_diagnostic_suffix(path)')} call site(s)")
    # The slice must reach past the whole function body. The first version took 1400 characters, which ended while
    # still inside the docstring, so the check failed against correct code -- the `except` is real but sits below it.
    body = source[source.index("def _diagnostic_suffix"):]
    body = body[:body.index("\ndef ", 1)] if "\ndef " in body[1:] else body
    check("  and it can never fail the write", "except Exception" in body,
          "a post-edit convenience that can break an edit is worse than none")

    print()
    print("=== 10. available_tiers is honest about this host ===")
    tiers = D.available_tiers()
    check("the always-available tiers are listed", any("syntax" in t for t in tiers), str(tiers))
    if D.find_checker("pyright"):
        check("  pyright is listed when it works", any("pyright" in t for t in tiers), str(tiers))
    else:
        check("  pyright is NOT listed when it does not", not any("pyright" in t for t in tiers),
              str(tiers))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
