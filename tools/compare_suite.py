"""A held-out task suite for comparing agentic harnesses.

## Why this exists

Every capability matrix in this repository is **self-graded**: the rows were chosen by Myriaxon's author, measured by
reading rival source trees, and weighted by hand. That can rank features; it cannot establish which harness is
better at *doing work*. The only thing that can is the same tasks, executed on each harness, scored by an
independent check.

## The design rules, and why each one is there

**Tasks are executed in a throwaway copy.** Each task gets a fresh temporary directory seeded with its own files, so
a harness that breaks its workspace cannot affect the next task or the repository.

**Scoring is by running a check, not by reading the answer.** Every task has a `verify` callable that inspects
artefacts on disk. A harness that *says* it fixed a bug scores zero; one that fixed it scores one. This is the same
principle as the repository's own `tools/verify_*.py`, applied outward.

**No task needs the network, a specific toolchain, or more than a minute.** Otherwise the suite measures
infrastructure rather than capability, and half the harnesses fail for reasons that have nothing to do with them.

**Tasks are chosen to have ONE defensible answer.** A task whose correct result is arguable would need a judge, and
a judged comparison between the judge's author's harness and its rivals is not evidence.

**The suite is small on purpose.** Six tasks × six harnesses, run by hand, with the failures reported. A large suite
nobody can afford to run is worse than a small one that runs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class Task:
    """One task: a seeded workspace, a prompt, and an independent check."""

    name: str
    prompt: str
    # Files written into the workspace before the harness runs.
    seed: dict[str, str] = field(default_factory=dict)
    # Returns (passed, evidence). Runs AFTER the harness, against the same directory.
    verify: Callable[[Path], tuple[bool, str]] = lambda _root: (False, "no verifier")
    # Extra directories/files the check needs, created from the same seed.
    runs_command: str = ""


# ---------------------------------------------------------------------------------------
# The tasks
# ---------------------------------------------------------------------------------------


def _verify_off_by_one(root: Path) -> tuple[bool, str]:
    code = (root / "calc.py").read_text(encoding="utf-8", errors="replace")
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'.'); from calc import total; "
         "print(total([1,2,3]), total([]), total([5]))"],
        cwd=str(root), capture_output=True, text=True, timeout=60,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return False, f"running it failed: {(proc.stderr or '')[:80]}"
    if out == "6 0 5":
        return True, f"total() returns {out}"
    return False, f"total() returns {out!r}, expected '6 0 5'"


def _verify_slugify(root: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'.'); from text_util import slugify; "
         "print(repr(slugify('Hello, World! 2024')), repr(slugify('  A  B  ')), repr(slugify('')))"],
        cwd=str(root), capture_output=True, text=True, timeout=60,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return False, f"running it failed: {(proc.stderr or '')[:80]}"
    if out == "'hello-world-2024' 'a-b' ''":
        return True, f"slugify returns {out}"
    return False, f"slugify returns {out}, expected \"'hello-world-2024' 'a-b' ''\""


def _verify_dedupe(root: Path) -> tuple[bool, str]:
    """Order-preserving dedupe, checked with an input whose correct answer is NOT in sorted order.

    The first version used `[1,1,2,3,3,3,1]`, whose correct result `[1,2,3]` happens to be sorted -- so
    `sorted(set(items))` passed. The suite's own self-test caught it: "correct->True wrong->True". A check that
    cannot tell the right answer from a wrong one is not a check, and this one would have scored every harness on a
    task none of them had done.
    """
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'.'); from dedupe import dedupe; "
         "print(dedupe([3,1,3,2,1]), dedupe([]))"],
        cwd=str(root), capture_output=True, text=True, timeout=60,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return False, f"running it failed: {(proc.stderr or '')[:80]}"
    # First-occurrence order: 3 then 1 then 2. `sorted(set(...))` gives [1, 2, 3] and must fail.
    if out == "[3, 1, 2] []":
        return True, f"dedupe returns {out}"
    return False, f"dedupe returns {out}, expected '[3, 1, 2] []' (first-occurrence order, NOT sorted)"


def _verify_json_config(root: Path) -> tuple[bool, str]:
    target = root / "settings.json"
    if not target.is_file():
        return False, "settings.json was not created"
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return False, f"settings.json is not valid JSON: {exc}"
    if data.get("name") != "demo":
        return False, f"name is {data.get('name')!r}, expected 'demo'"
    if data.get("retries") != 3:
        return False, f"retries is {data.get('retries')!r} (must be the NUMBER 3, not a string)"
    tags = data.get("tags")
    if not isinstance(tags, list) or sorted(tags) != ["a", "b"]:
        return False, f"tags is {tags!r}, expected a list ['a','b']"
    return True, "settings.json has the right shape and types"


def _verify_find_the_bug(root: Path) -> tuple[bool, str]:
    """The interesting one: a plausible-looking function with a real defect in it."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'.'); from rng import in_range; "
         "print(in_range(5,1,10), in_range(1,1,10), in_range(10,1,10), in_range(0,1,10), in_range(11,1,10))"],
        cwd=str(root), capture_output=True, text=True, timeout=60,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return False, f"running it failed: {(proc.stderr or '')[:80]}"
    # Inclusive bounds: 5,1,10 all inside; 0 and 11 outside.
    if out == "True True True False False":
        return True, f"in_range returns {out}"
    return False, f"in_range returns {out}, expected 'True True True False False' (bounds are INCLUSIVE)"


def _verify_write_tests(root: Path) -> tuple[bool, str]:
    """Can it author a check that actually catches a defect, rather than one that passes vacuously?"""
    test_file = root / "test_add.py"
    if not test_file.is_file():
        return False, "no test_add.py was written"
    body = test_file.read_text(encoding="utf-8", errors="replace")

    # 1. The harness's own test must PASS on the correct implementation.
    good = subprocess.run([sys.executable, "-m", "pytest", "-q", str(test_file)],
                          cwd=str(root), capture_output=True, text=True, timeout=180)
    if good.returncode != 0:
        return False, f"the written test does not pass on the CORRECT code: {(good.stdout or '')[-120:]}"

    # 2. And it must FAIL on a broken one. This is the part that separates a real test from a vacuous one.
    broken = root / "add_broken.py"
    broken.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    probe = root / "probe" 
    shutil.rmtree(probe, ignore_errors=True)
    probe.mkdir(parents=True)
    (probe / "add.py").write_text(broken.read_text(encoding="utf-8"), encoding="utf-8")
    (probe / "test_add.py").write_text(body, encoding="utf-8")
    bad = subprocess.run([sys.executable, "-m", "pytest", "-q", "test_add.py"],
                         cwd=str(probe), capture_output=True, text=True, timeout=180)
    shutil.rmtree(probe, ignore_errors=True)
    if bad.returncode == 0:
        return False, "the written test PASSES against `return a - b`, so it asserts nothing"
    return True, "the test passes on correct code and fails on a broken one"


TASKS: list[Task] = [
    Task(
        name="off-by-one",
        prompt=(
            "In calc.py, `total()` is wrong: it skips the last element of the list. Fix it so it sums all "
            "elements, and make sure `total([])` returns 0 rather than raising. Do not change the signature."
        ),
        seed={"calc.py": "def total(numbers):\n    return sum(numbers[:-1])\n"},
        verify=_verify_off_by_one,
    ),
    Task(
        name="slugify",
        prompt=(
            "Create text_util.py with a function `slugify(text)` that lowercases, replaces any run of "
            "non-alphanumeric characters with a single '-', and strips leading/trailing '-'. "
            "slugify('Hello, World! 2024') must be 'hello-world-2024' and slugify('') must be ''."
        ),
        verify=_verify_slugify,
    ),
    Task(
        name="dedupe-order",
        prompt=(
            "Create dedupe.py with `dedupe(items)` that removes duplicates while KEEPING FIRST-OCCURRENCE ORDER, "
            "and works on an empty list. dedupe([3,1,3,2,1]) must be [3, 1, 2] -- note that is NOT sorted order."
        ),
        verify=_verify_dedupe,
    ),
    Task(
        name="json-types",
        prompt=(
            "Create settings.json containing exactly: name as the string \"demo\", retries as the NUMBER 3 "
            "(not a string), and tags as a JSON array of the strings \"a\" and \"b\"."
        ),
        verify=_verify_json_config,
    ),
    Task(
        name="find-the-bug",
        prompt=(
            "rng.py has `in_range(value, low, high)` which should return True when value is within the INCLUSIVE "
            "bounds low..high. It currently gets the boundaries wrong. Fix it."
        ),
        seed={"rng.py": "def in_range(value, low, high):\n    return low < value < high\n"},
        verify=_verify_find_the_bug,
    ),
    Task(
        name="write-a-real-test",
        prompt=(
            "add.py defines `add(a, b)`. Write test_add.py containing a pytest test that would FAIL if `add` "
            "returned `a - b` instead of `a + b`. The test must pass against the current correct implementation."
        ),
        seed={"add.py": "def add(a, b):\n    return a + b\n"},
        verify=_verify_write_tests,
    ),
]


def build_workspace(task: Task, base: Path | None = None) -> Path:
    """Create a fresh directory for a task. The harness under test operates here and nowhere else."""
    root = Path(tempfile.mkdtemp(dir=str(base) if base else None))
    for name, content in task.seed.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    return root


def score(task: Task, root: Path) -> tuple[bool, str]:
    """Run the task's independent check. Never raises: a crash is a failure with the reason attached."""
    try:
        return task.verify(root)
    except Exception as exc:  # noqa: BLE001
        return False, f"the check itself raised {type(exc).__name__}: {exc}"


def self_test() -> int:
    """Prove the suite's checks work, by running each against a KNOWN-GOOD and a KNOWN-BAD workspace.

    Without this the suite is worthless: a check that always passes would score every harness 6/6, and a check that
    always fails would score everyone 0/6. Both would look like a result.
    """
    print("  Each task's check must PASS on a correct solution and FAIL on a wrong one.")
    print()
    print(f"  {'task':<20}{'correct':>10}{'wrong':>10}   note")
    print("  " + "-" * 78)

    # A correct and an incorrect solution for each task, written by hand.
    solutions = {
        "off-by-one": (
            {"calc.py": "def total(numbers):\n    return sum(numbers)\n"},
            {"calc.py": "def total(numbers):\n    return 0\n"},
        ),
        "slugify": (
            {"text_util.py": "import re\n\n\ndef slugify(text):\n"
                             "    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n"},
            {"text_util.py": "def slugify(text):\n    return text\n"},
        ),
        "dedupe-order": (
            {"dedupe.py": "def dedupe(items):\n    out = []\n    for i in items:\n"
                          "        if i not in out:\n            out.append(i)\n    return out\n"},
            {"dedupe.py": "def dedupe(items):\n    return sorted(set(items))\n"},
        ),
        "json-types": (
            {"settings.json": json.dumps({"name": "demo", "retries": 3, "tags": ["a", "b"]})},
            {"settings.json": json.dumps({"name": "demo", "retries": "3", "tags": ["a", "b"]})},
        ),
        "find-the-bug": (
            {"rng.py": "def in_range(value, low, high):\n    return low <= value <= high\n"},
            {"rng.py": "def in_range(value, low, high):\n    return low < value < high\n"},
        ),
        "write-a-real-test": (
            {"add.py": "def add(a, b):\n    return a + b\n",
             "test_add.py": "from add import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"},
            {"add.py": "def add(a, b):\n    return a + b\n",
             "test_add.py": "def test_nothing():\n    assert True\n"},
        ),
    }

    ok = True
    for task in TASKS:
        right, wrong = solutions.get(task.name, ({}, {}))
        good_root = build_workspace(task)
        for name, content in right.items():
            (good_root / name).write_text(content, encoding="utf-8", newline="\n")
        bad_root = build_workspace(task)
        for name, content in wrong.items():
            (bad_root / name).write_text(content, encoding="utf-8", newline="\n")

        good_pass, good_why = score(task, good_root)
        bad_pass, bad_why = score(task, bad_root)
        note = ""
        if good_pass and not bad_pass:
            note = "discriminates correctly"
        else:
            ok = False
            note = f"BROKEN: correct->{good_pass} wrong->{bad_pass}"
        print(f"  {task.name:<20}{str(good_pass):>10}{str(bad_pass):>10}   {note}")
        shutil.rmtree(good_root, ignore_errors=True)
        shutil.rmtree(bad_root, ignore_errors=True)

    print()
    print(f"  SELF-TEST: {'every check discriminates' if ok else 'THE SUITE IS NOT TRUSTWORTHY'}")
    return 0 if ok else 1


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "--self-test":
        return self_test()
    print(f"  {len(TASKS)} tasks:")
    for task in TASKS:
        print(f"    {task.name:<20} {len(task.seed)} seed file(s)")
    print()
    print("  Run --self-test to prove the checks discriminate before trusting any result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
