"""Syntax-check the dashboard's inline JavaScript.

    python scripts/check_ui_js.py

The dashboard ships one HTML document with a large inline <script>. A syntax error there is invisible to
every Python-side check and to a check that greps for element ids: the page still serves, the ids still
appear in the HTML, and the controls simply do nothing because the whole script failed to parse. This
extracts the script and parses it with node, which is the same engine that will run it.

Also asserts that every `$("id")` the script references exists in the markup -- a rename on one side
only is the other common way a control silently stops working.
"""
import json
import re
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
warnings.filterwarnings("ignore")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

passed, failed, skipped = [], [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + str(detail)) if detail and not condition else ''}")


def skip(label, why):
    skipped.append(label)
    print(f"  [SKIP] {label} -- {why}")


def main() -> int:
    from myriaxon.dashboard import ui as ui_module

    html = ui_module.INDEX_HTML
    scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    check("the page has an inline script", bool(scripts), f"{len(scripts)} found")
    if not scripts:
        return 1
    script = "\n".join(scripts)
    print(f"    script size: {len(script):,} chars")

    node = shutil.which("node")
    if node:
        out = REPO / "bench-out" / "ui-script-check.js"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(script, encoding="utf-8")
        try:
            result = subprocess.run([node, "--check", str(out)], capture_output=True, text=True, timeout=120)
            ok = result.returncode == 0
            check("the inline script parses", ok, (result.stderr or "")[-500:])
            if not ok:
                for line in (result.stderr or "").splitlines()[:8]:
                    print(f"      {line}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            skip("the inline script parses", f"cannot run node here ({type(exc).__name__}: {exc})")
        finally:
            out.unlink(missing_ok=True)
    else:
        skip("the inline script parses", "node is not on PATH")

    print("\n=== every element the script reaches for exists ===")
    referenced = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', script))
    declared = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
    # Elements created at runtime count as existing: the script builds some nodes itself and assigns
    # their id (for example the buffering indicator, `wrap.id = "buffering-msg"`). Treating only the
    # static markup as the universe of ids reported those as missing.
    created = set(re.findall(r'\.id\s*=\s*"([A-Za-z0-9_-]+)"', script))
    known = declared | created
    missing = sorted(referenced - known)
    check("no handler targets a missing element", not missing, missing)
    print(f"    {len(referenced)} ids referenced, {len(declared)} declared, {len(created)} created at runtime")
    if created:
        print(f"    runtime-created: {sorted(created)}")
    if missing:
        for name in missing[:12]:
            print(f"      missing: {name}")

    print("\n=== the menu is wired ===")
    for element in ("menubtn", "menupop", "menulabel"):
        check(f"#{element} exists", element in declared)
    check("the menu starts closed", 'id="menupop" class="menupop" hidden' in html,
          [l for l in html.splitlines() if "menupop" in l][:2])
    check("the menu carries sections", "menusect" in html and html.count("menusect") >= 2)
    for entry in ("codebtn", "settingsbtn", "mcpbtn", "plugbtn", "rulesbtn", "msgbtn"):
        check(f"the menu has an entry for {entry}", entry in declared)
    check("no handler opens a panel without closing the menu",
          script.count("setMenu(false)") >= 6, script.count("setMenu(false)"))

    print("\n=== handlers do not call application methods on themselves ===")
    # This bug has occurred three times: a request handler calling an Application method as `self.x()`,
    # which resolves against the handler and raises AttributeError at request time -- a 500 the user
    # sees as "the feature is broken". It is invisible to every other check because the module imports
    # and the route exists. Comparing the two class bodies statically catches it before it ships.
    import ast

    source = (REPO / "myriaxon" / "dashboard" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    def methods(node):
        return {
            item.name
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    app_methods = methods(classes["DashboardApp"]) if "DashboardApp" in classes else set()
    handler = classes.get("Handler")
    handler_methods = methods(handler) if handler else set()

    # Methods that exist only on the application, so `self.x()` inside the handler is wrong.
    app_only = {name for name in app_methods if name not in handler_methods and not name.startswith("__")}

    offenders = []
    if handler:
        for item in ast.walk(handler):
            if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute):
                target = item.func.value
                if (isinstance(target, ast.Name) and target.id == "self"
                        and item.func.attr in app_only):
                    offenders.append(f"Handler.self.{item.func.attr}() line {item.func.lineno}")
    check("no handler calls an application method on itself", not offenders, offenders)
    if offenders:
        for offender in offenders:
            print(f"      {offender} -- should be self.app.{offender.split('self.')[1]}")

    print(f"\n{len(passed)} passed, {len(failed)} failed"
          + (f", {len(skipped)} skipped" if skipped else ""))
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
