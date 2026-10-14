#!/usr/bin/env python3
"""Verify the instruction layer: nested discovery, defined precedence, visible cost, no silent truncation.

Each check builds a real directory tree and loads it, because the defects being fixed were all about *which
files get found* -- something a mocked filesystem would not exercise.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import instructions as I  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pkg = root / "packages" / "api"
        pkg.mkdir(parents=True)

        print("=== 1. an empty project reports every candidate and loads nothing ===")
        result = I.load_instructions(project_root=root, cwd=root)
        check("no text assembled", result.text == "")
        check("no sources marked loaded", result.loaded == [])
        check("candidates are still reported", len(result.sources) >= 3,
              f"{len(result.sources)} sources")

        print()
        print("=== 2. the root file loads and is reported with its size ===")
        (root / "AGENTS.md").write_text("Root rule: always run the tests.\n", encoding="utf-8")
        result = I.load_instructions(project_root=root, cwd=root)
        check("root AGENTS.md loaded", any(s.kind == "agents" and s.loaded for s in result.sources))
        check("its text reached the prompt", "always run the tests" in result.text)
        check("its size is reported", result.total_chars > 0, f"{result.total_chars} chars")
        check("cost per turn is estimated", result.est_tokens >= 0, f"~{result.est_tokens} tokens")

        print()
        print("=== 3. THE FIX: a nested AGENTS.md is now found ===")
        (pkg / "AGENTS.md").write_text("API package rule: use frozen dataclasses here.\n", encoding="utf-8")
        result = I.load_instructions(project_root=root, cwd=pkg)
        nested = [s for s in result.sources if s.kind == "nested" and s.loaded]
        check("the nested file was discovered", len(nested) == 1,
              str([str(s.path) for s in result.sources if s.kind == "nested"]))
        check("it is named in the report", any("packages" in str(s.path) for s in nested),
              str([str(s.path) for s in nested]))
        check("its content reached the prompt", "frozen dataclasses" in result.text)
        check("the root file is still loaded too", "always run the tests" in result.text)

        print()
        print("=== 4. precedence is DEFINED and stated, innermost last ===")
        check("the prompt says the later source wins",
              "more specific" in result.text and "wins" in result.text,
              result.text.splitlines()[0][:90] if result.text else "")
        root_at = result.text.find("always run the tests")
        nested_at = result.text.find("frozen dataclasses")
        check("root text appears before nested text", 0 <= root_at < nested_at,
              f"root@{root_at} nested@{nested_at}")
        check("the nested heading names its directory", "packages" in result.text)

        print()
        print("=== 5. deeper files come after shallower ones ===")
        deep = pkg / "src" / "handlers"
        deep.mkdir(parents=True)
        (pkg / "src" / "AGENTS.md").write_text("src rule: no prints.\n", encoding="utf-8")
        (deep / "AGENTS.md").write_text("handler rule: validate input first.\n", encoding="utf-8")
        result = I.load_instructions(project_root=root, cwd=deep)
        positions = {
            key: result.text.find(key)
            for key in ("always run the tests", "frozen dataclasses", "no prints", "validate input first")
        }
        ordered = all(v >= 0 for v in positions.values()) and list(positions.values()) == sorted(positions.values())
        check("all four levels loaded in outermost-to-innermost order", ordered, str(positions))
        check("three nested files were found",
              len([s for s in result.sources if s.kind == "nested" and s.loaded]) == 3)

        print()
        print("=== 6. a file that exists but is empty does not masquerade as loaded ===")
        (root / "MYRIAXON.md").write_text("   \n", encoding="utf-8")
        result = I.load_instructions(project_root=root, cwd=root)
        project = [s for s in result.sources if s.kind == "project"]
        check("the empty project file is reported", len(project) == 1)
        check("it is marked skipped, with a reason",
              not project[0].loaded and project[0].note == "empty", project[0].note)

        print()
        print("=== 7. a legacy filename still works ===")
        (root / "MYRIAXON.md").unlink()
        (root / "IOH.md").write_text("Legacy rule: keep the old convention working.\n", encoding="utf-8")
        result = I.load_instructions(project_root=root, cwd=root)
        check("IOH.md is read when MYRIAXON.md is absent",
              "keep the old convention working" in result.text)
        (root / "IOH.md").unlink()

        print()
        print("=== 8. @imports still expand ===")
        (root / "extra.md").write_text("Imported detail: the port is 8080.\n", encoding="utf-8")
        (root / "MYRIAXON.md").write_text("Project rule.\n@extra.md\n", encoding="utf-8")
        result = I.load_instructions(project_root=root, cwd=root)
        check("the imported file's content is inlined", "the port is 8080" in result.text)
        check("it is labelled as imported", "Imported from" in result.text)

        print()
        print("=== 9. THE FIX: an oversized layer warns, and is NEVER truncated ===")
        big = "Rule number %d: keep this visible.\n"
        body = "".join(big % n for n in range(900))
        (root / "MYRIAXON.md").write_text(body, encoding="utf-8")
        result = I.load_instructions(project_root=root, cwd=root)
        check("it is flagged over budget", result.over_budget, f"{result.total_chars} chars")
        check("a warning is produced", bool(result.warnings), str(result.warnings)[:100])
        check("the warning explains the adherence cost",
              any("adherence" in w for w in result.warnings), str(result.warnings)[:160])
        check("NOTHING was dropped -- the last rule is still present",
              "Rule number 899: keep this visible." in result.text)
        check("the full size is reported honestly",
              result.total_chars >= len(body), f"{result.total_chars} vs body {len(body)}")

        print()
        print("=== 10. an enormous layer escalates to a stronger warning ===")
        (root / "MYRIAXON.md").write_text("Rule: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.\n" * 900, encoding="utf-8")
        result = I.load_instructions(project_root=root, cwd=root)
        check("it is over the hard threshold", result.total_chars >= I.HARD_CHARS,
              f"{result.total_chars} vs {I.HARD_CHARS}")
        check("the stronger warning names a remedy",
              any("save_skill" in w or "skill" in w for w in result.warnings),
              str(result.warnings)[:200])
        check("still nothing truncated", "Rule: xxxx" in result.text)

        print()
        print("=== 11. the dominant contributor is named when there are several ===")
        (root / "packages" / "api" / "AGENTS.md").write_text("small\n", encoding="utf-8")
        result = I.load_instructions(project_root=root, cwd=root / "packages" / "api")
        check("a multi-source layer is loaded", len(result.loaded) >= 2, str(len(result.loaded)))
        check("the largest file is called out",
              any("%" in w for w in result.warnings), str(result.warnings)[-160:])

        print()
        print("=== 12. running from outside the project degrades safely ===")
        other = Path(tempfile.mkdtemp())
        result = I.load_instructions(project_root=root, cwd=other)
        check("no crash when cwd is outside the root", isinstance(result.text, str))
        check("it says why no nested file was used",
              any(s.kind == "nested" and not s.loaded for s in result.sources))

        print()
        print("=== 13. the compatibility wrapper returns the same text ===")
        from myriaxon.project_instructions import load_project_instructions

        legacy = load_project_instructions(root)
        modern = I.load_instructions(project_root=root, cwd=Path.cwd()).text
        check("load_project_instructions still works", isinstance(legacy, str) and len(legacy) > 0)
        check("it delegates to the new loader", legacy == I.load_instructions(project_root=root).text)

        print()
        print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
        for label in FAIL:
            print(f"  FAILED: {label}")
        return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
