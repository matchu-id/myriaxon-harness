#!/usr/bin/env python3
"""Verify the skill-generated tool gate by exercising it, including the cases that must FAIL.

A gate that has only ever seen good input is not a gate. Half these checks feed it code that must be refused,
and the test only passes if the refusal actually happens and the file stays out of the plugin directory.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import skill_tools  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


GOOD = '''
PLUGIN = {"name": "slugify", "version": "1.0", "description": "Turn text into a slug."}


def slugify(text):
    out = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


def register(api):
    api.add_tool(
        "slugify",
        "Convert a string into a URL-safe slug.",
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        lambda tool_input, ctx: slugify(tool_input.get("text", "")),
    )
'''

RAISES = '''
def register(api):
    def boom(tool_input, ctx):
        raise ValueError("this tool is broken")
    api.add_tool("boom", "Always raises.", {"type": "object", "properties": {}}, boom)
'''

NO_REGISTER = '''
PLUGIN = {"name": "nothing"}
'''

BROKEN_SYNTAX = '''
def register(api)
    pass
'''

SUBPROCESS = '''
import subprocess


def register(api):
    api.add_tool("run", "Runs a command.", {"type": "object", "properties": {}},
                 lambda ti, ctx: subprocess.run(["whoami"], capture_output=True))
'''

EVAL = '''
def register(api):
    api.add_tool("ev", "Evaluates.", {"type": "object", "properties": {}},
                 lambda ti, ctx: eval("1+1"))
'''

NETWORK = '''
import urllib.request


def register(api):
    api.add_tool("get", "Fetches.", {"type": "object", "properties": {}},
                 lambda ti, ctx: urllib.request.urlopen("http://example.com"))
'''

MUTATING = '''
def register(api):
    api.add_tool("wipe", "Declared mutating.", {"type": "object", "properties": {}},
                 lambda ti, ctx: "done", mutating=True)
'''


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        print("=== 1. a well-formed generated tool is verified and promoted ===")
        result = skill_tools.stage_and_verify("slugify", GOOD, project_root=root)
        check("good tool passes", result.ok, "; ".join(result.errors) or "no errors")
        check("it was smoke-tested (handler ran)", any("handler ran" in c for c in result.checks),
              "; ".join(result.checks))
        check("it was promoted out of quarantine",
              Path(result.promoted_path).is_file() if result.promoted_path else False)
        check("nothing left in quarantine", skill_tools.quarantined(root) == [],
              str(skill_tools.quarantined(root)))

        # The promoted file must be where the real loader looks, and the quarantine dir must be skipped by it.
        plugins_dir = skill_tools.promoted_dir(root)
        discovered = [p.stem for p in plugins_dir.glob("*.py")]
        check("the loader would find it", "slugify" in discovered, str(discovered))

        print()
        print("=== 2. a raising handler is caught by the smoke test ===")
        result = skill_tools.stage_and_verify("boom", RAISES, project_root=root)
        check("raising handler FAILS", not result.ok)
        check("the error names the handler", any("handler raised" in e for e in result.errors),
              "; ".join(result.errors))
        check("it stays in quarantine", not (plugins_dir / "boom.py").exists())
        check("it is listed as quarantined", "boom" in skill_tools.quarantined(root))

        print()
        print("=== 3. structurally broken tools never reach the plugins directory ===")
        for label, code in (("no register()", NO_REGISTER), ("syntax error", BROKEN_SYNTAX)):
            result = skill_tools.stage_and_verify(label.replace(" ", "-"), code, project_root=root)
            check(f"{label} FAILS", not result.ok, "; ".join(result.errors[:1]))
            check(f"{label} not promoted", not (plugins_dir / f"{label.replace(' ', '-')}.py").exists())

        print()
        print("=== 4. code that reaches outside the process is refused outright ===")
        for label, code in (("subprocess", SUBPROCESS), ("eval", EVAL), ("network", NETWORK)):
            result = skill_tools.stage_and_verify(label, code, project_root=root)
            check(f"{label} refused", not result.ok)
            check(f"{label} refusal says why", any("refused:" in e for e in result.errors),
                  "; ".join(result.errors[:1]))
            check(f"{label} not promoted", not (plugins_dir / f"{label}.py").exists())

        print()
        print("=== 5. a mutating tool is structurally verified but NOT invoked ===")
        result = skill_tools.stage_and_verify("wipe", MUTATING, project_root=root)
        check("mutating tool passes structurally", result.ok, "; ".join(result.errors))
        check("its handler was not called",
              any("not invoked" in w for w in result.warnings), "; ".join(result.warnings))

        print()
        print("=== 6. rejections are recorded so a failure can be diagnosed ===")
        recorded = skill_tools.rejections(root)
        check("the ledger has entries", bool(recorded), str(list(recorded))[:80])
        check("the raising tool's reason is stored",
              "boom" in recorded and recorded["boom"]["errors"])

        print()
        print("=== 7. quarantined code is inert to the real plugin loader ===")
        # The guarantee rests on the loader skipping the `_quarantine` directory itself, so this must call
        # `_discover` on the *plugins* directory -- the one `plugin_dirs()` actually iterates -- and assert that
        # the quarantined files are not among the results. The first version of this check passed the quarantine
        # directory, which asked a different question and would have reported a failure even though the real
        # loader is correct.
        from myriaxon import plugins as plugins_module

        found = plugins_module.PluginRegistry._discover(skill_tools.promoted_dir(root))
        leaked = [p.name for p in found if p.name in {f"{n}.py" for n in skill_tools.quarantined(root)}]
        check("the loader does not see quarantined files", leaked == [],
              f"loader found: {[p.name for p in found]}; leaked: {leaked}")
        check("but it does see promoted ones", "slugify.py" in {p.name for p in found})
        check("quarantine dirname starts with _ (what makes it inert)",
              skill_tools.QUARANTINE_DIRNAME.startswith("_"))

        print()
        print("=== 8. the reviewed escape hatch works only on already-staged files ===")
        result = skill_tools.promote_after_review("nonexistent-tool", project_root=root)
        check("cannot promote something never staged", not result.ok)

        result = skill_tools.promote_after_review("subprocess", project_root=root)
        check("can promote a reviewed staged file", result.ok, "; ".join(result.errors))
        check("it says the refusal list was waived",
              any("waived" in w for w in result.warnings), "; ".join(result.warnings))

        print()
        print("=== 9. promotion does not create a duplicate or leave residue ===")
        check("promoted file exists", (plugins_dir / "subprocess.py").is_file())
        check("and is no longer in quarantine", "subprocess" not in skill_tools.quarantined(root))

        print()
        print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
        for label in FAIL:
            print(f"  FAILED: {label}")
        return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
