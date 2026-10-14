"""Validate .github/workflows/ci.yml, and run the parts of it that can run here.

## Why this runs the logic instead of only parsing the YAML

A workflow is code that nobody executes until it is pushed, so a typo in an embedded Python block is discovered by a
red CI run hours later -- or, worse, the block silently succeeds because it checked nothing. So this does three
things:

  1. **Parses the YAML** and asserts the structure a workflow needs (jobs, steps, `uses`/`run`).
  2. **Extracts every embedded Python block** and syntax-checks it with `ast.parse`. That is what catches an
     `import tomllib` left at the wrong indentation, or a heredoc whose dedent is wrong.
  3. **Runs the packaging job's assertions against the real artifacts** already in `dist/`, because those are the
     checks that matter and they are platform-independent.
"""

from __future__ import annotations

import ast
import glob
import re
import sys
import tarfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    print("=== 1. the workflow file parses as YAML ===")
    check("the workflow exists", WORKFLOW.is_file(), str(WORKFLOW))
    if not WORKFLOW.is_file():
        print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
        return 1
    text = WORKFLOW.read_text(encoding="utf-8")

    parsed = None
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(text)
        check("PyYAML parses it", isinstance(parsed, dict))
    except ImportError:
        check("PyYAML not installed; falling back to a structural check", True,
              "install pyyaml to validate fully -- the checks below still apply")
    except Exception as exc:  # noqa: BLE001
        check("PyYAML parses it", False, f"{type(exc).__name__}: {exc}")

    print()
    print("=== 2. the structure a workflow needs ===")
    check("declares a trigger", "on:" in text)
    check("  on push", re.search(r"^\s{2}push:", text, re.M) is not None)
    check("  on pull_request", "pull_request:" in text)
    check("  and manual dispatch", "workflow_dispatch:" in text)
    check("has concurrency control so stale runs cancel", "concurrency:" in text)
    check("declares least-privilege permissions", "permissions:" in text and "contents: read" in text)

    if parsed:
        jobs = parsed.get("jobs") or {}
        check("declares jobs", bool(jobs), str(list(jobs)))
        for name in ("test", "package", "verify", "lint"):
            check(f"  has a {name} job", name in jobs)
        for name, job in jobs.items():
            steps = job.get("steps") or []
            check(f"  {name}: has steps", bool(steps), f"{len(steps)}")
            for step in steps:
                # Every step must either use an action or run something. A step with neither is a no-op that reads
                # as work.
                if "uses" not in step and "run" not in step:
                    check(f"  {name}: every step does something", False, str(step)[:80])
        # A job that runs on three OSes needs fail-fast off, or one platform's failure hides the others' results.
        package = jobs.get("package") or {}
        check("package runs on more than one OS",
              len(((package.get("strategy") or {}).get("matrix") or {}).get("os") or []) >= 2,
              str(((package.get("strategy") or {}).get("matrix") or {}).get("os")))
        check("  with fail-fast disabled", (package.get("strategy") or {}).get("fail-fast") is False)

    print()
    print("=== 3. every embedded Python block PARSES ===")
    # This is the check a plain YAML validation cannot do, and the one that catches a broken heredoc or a mis-indented
    # block. A workflow is code nobody runs until it is pushed, so a syntax error in an embedded block is discovered
    # by a red CI run hours later.
    #
    # The first version used a regex over the whole file and then dedented a fixed two spaces, which was wrong twice
    # over: the block's indentation varies, and the regex ran past the end of one block into the next. It reported
    # five failures against a workflow whose blocks are all correct. This version walks the lines and uses the
    # `run: |` line's own indentation as the block's base, which is the actual structure.
    blocks: list[tuple[int, str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.rstrip().endswith("run: |"):
            base = len(line) - len(line.lstrip())
            body: list[str] = []
            index += 1
            while index < len(lines):
                candidate = lines[index]
                if candidate.strip() and (len(candidate) - len(candidate.lstrip())) <= base:
                    break
                body.append(candidate[base + 2:] if len(candidate) > base + 2 else "")
                index += 1
            blocks.append((index, "\n".join(body)))
            continue
        index += 1

    check("found embedded run blocks", len(blocks) > 0, f"{len(blocks)} block(s)")
    python_blocks = 0
    shell_blocks = 0
    for number, (at, body) in enumerate(blocks, start=1):
        # Python or shell, decided STRUCTURALLY.
        #
        # A block preceded by `shell: python` in the same step is Python. Anything else is shell -- including a
        # heredoc like `python - <<'PY'`, which embeds Python inside shell. Deciding by "does this contain a top-level
        # import" was the first attempt and it matched that heredoc, reporting a syntax error on the line
        # `python - <<'PY'` in a block that is perfectly valid shell.
        #
        # The step's own text is everything back to the previous `- ` step marker, so look backwards for it.
        preceding = lines[:at]
        step_start = 0
        for position in range(len(preceding) - 1, -1, -1):
            if re.match(r"^\s*- (uses|name|run):", preceding[position]):
                step_start = position
                break
        step_text = "\n".join(preceding[step_start:])
        is_python = "shell: python" in step_text
        if not is_python:
            shell_blocks += 1
            continue
        python_blocks += 1
        try:
            ast.parse(body)
            check(f"  python block {number} parses", True, f"{len(body.splitlines())} lines")
        except SyntaxError as exc:
            bad_line = body.splitlines()[max(0, (exc.lineno or 1) - 1)] if body.splitlines() else ""
            check(f"  python block {number} parses", False,
                  f"line {exc.lineno}: {exc.msg}\n         {bad_line[:90]}")
    check("there is at least one Python block to validate", python_blocks > 0, f"{python_blocks} found")
    check("  shell blocks were recognised and skipped", shell_blocks > 0, f"{shell_blocks} shell block(s)")

    print()
    print("=== 4. `shell: python` is declared where Python is used ===")
    # A `run:` block containing Python but no `shell: python` executes under bash or cmd, and fails confusingly on
    # Windows where the default shell differs.
    #
    # The check splits the file into STEP units (each beginning with `- ` at step indentation) and asks whether a step
    # containing a Python block declares the shell. The first version split on a six-space dash, which is not where
    # steps begin in this file, so every step was one giant string and the check misfired.
    step_starts = [i for i, line in enumerate(lines) if re.match(r"^\s*- (uses|name|run):", line)]
    offenders = []
    for position, start in enumerate(step_starts):
        end = step_starts[position + 1] if position + 1 < len(step_starts) else len(lines)
        step = "\n".join(lines[start:end])
        if re.search(r"^import \w+", step, re.M) and "shell: python" not in step:
            offenders.append(step.strip().splitlines()[0][:60])
    check("every Python run block sets shell: python", not offenders, str(offenders))

    print()
    print("=== 5. the packaging assertions PASS against the real artifacts ===")
    # `dist/` holds artifacts only after someone has built a release. When it is empty they are built here from a clean
    # copy of the checkout (tools/_artifacts.py), so this check does not depend on a build having been run first.
    import _artifacts

    try:
        wheels = [str(w) for w in _artifacts.wheels()]
        sdists = [str(s) for s in _artifacts.sdists()]
    except Exception as exc:  # noqa: BLE001
        wheels, sdists = [], []
        print("  could not build the artifacts:", str(exc)[-300:])
    check("a wheel was built", bool(wheels), str([Path(w).name for w in wheels]))
    check("an sdist was built", bool(sdists), str([Path(s).name for s in sdists]))

    if wheels:
        with zipfile.ZipFile(wheels[-1]) as archive:
            names = archive.namelist()
            entry_files = [n for n in names if n.endswith("entry_points.txt")]
            entry_points = archive.read(entry_files[0]).decode() if entry_files else ""
            metadata_file = [n for n in names if n.endswith("METADATA")][0]
            metadata = archive.read(metadata_file).decode("utf-8", "replace")
        required = {
            "the shipped skill": "myriaxon/builtin_skills/graphify/SKILL.md",
            "the benchmark tasks": "myriaxon/bench/tasks/p01_goal_bugfix_end_to_end/task.json",
            "the brand assets": "myriaxon/brand/assets/myriaxon-mark.svg",
            "a py.typed marker": "myriaxon/py.typed",
            "the CLI module": "myriaxon/cli.py",
            "the dashboard": "myriaxon/dashboard/server.py",
        }
        missing = [label for label, path in required.items() if path not in names]
        check("the wheel carries all package data", not missing, str(missing))
        forbidden = ("openclaw-src/", "opencode-dev-src/", "pi-harness-main-src/", "claude-code-main-src/",
                     "deepseek-harness-master-src/", ".venv/", ".myriaxon/", "node_modules/", ".tooling/")
        leaked = [n for n in names if any(bad in n for bad in forbidden)]
        check("the wheel leaks nothing forbidden", not leaked, f"{len(leaked)}: {leaked[:3]}")
        check("the entry point routes through __main__", "myriaxon.__main__:main" in entry_points,
              entry_points.strip().replace("\n", " | "))
        check("License-Expression is MIT (the deprecation fix)", "License-Expression: MIT" in metadata,
              [l for l in metadata.splitlines() if "License" in l][:2])
        check("no license classifier remains",
              "Classifier: License ::" not in metadata,
              [l for l in metadata.splitlines() if l.startswith("Classifier: License")])

    if sdists:
        with tarfile.open(sdists[-1]) as tar:
            stripped = [n.split("/", 1)[1] for n in tar.getnames() if "/" in n]
        required_sdist = ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in", "myriaxon/py.typed",
                          "myriaxon/builtin_skills/graphify/SKILL.md")
        missing = [item for item in required_sdist if item not in stripped]
        check("the sdist carries what a rebuild needs", not missing, str(missing))
        forbidden = ("openclaw-src/", "opencode-dev-src/", ".venv/", ".myriaxon/", "node_modules/")
        leaked = [n for n in stripped if any(bad in n for bad in forbidden)]
        check("the sdist leaks nothing forbidden", not leaked, f"{len(leaked)}: {leaked[:3]}")
        check("the sdist is a sane size", len(stripped) < 1000, f"{len(stripped)} entries")

    print()
    print("=== 6. the verifier list in CI matches the documented one ===")
    # An explicit list is used rather than a glob, because tools/ also holds verify_*.py scripts for another project
    # (the Certivo content checks) that need that project's files. A list that drifts from the disk is how a verifier
    # silently stops being run, so it is checked in BOTH directions below.
    listed = re.findall(r'"(verify_\w+)"', text)
    check("CI lists verifiers", len(listed) > 20, f"{len(listed)}")
    check("  no duplicates in the list", len(listed) == len(set(listed)),
          str([n for n in set(listed) if listed.count(n) > 1]))
    on_disk = {p.stem for p in (REPO / "tools").glob("verify_*.py")}
    missing_from_disk = [n for n in listed if n not in on_disk]
    check("  every listed verifier exists on disk", not missing_from_disk, str(missing_from_disk))
    check("  the packaging verifier is included", "verify_packaging" in listed)
    # The other direction: a verifier somebody adds must be run, not just present. Myriaxon's own set is whatever
    # tools/run_checks.py runs, so the two cannot disagree about what "every verifier" means.
    import run_checks

    ours = {name[:-3] for name in run_checks.verifier_names()}
    unlisted = sorted(ours - set(listed))
    check("  every Myriaxon verifier on disk is listed", not unlisted, str(unlisted))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
