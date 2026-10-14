"""Small, self-authored, objectively-scored benchmark for Myriaxon's real agent loop -- not a
recognized third-party suite (no benchmark harness -- Terminal-Bench, SWE-bench, etc. -- is
installed or runnable in this Windows environment), but genuinely run against a real
DeepSeek-backed agent with pass/fail verified by actual file-system/output state, not LLM
judging. Each task builds an isolated temp workspace, runs one real turn, checks a concrete,
objective condition, and cleans up.

Not a unit test -- requires a real DEEPSEEK_API_KEY (via .env or the environment) and makes real,
billed API calls; takes roughly 1-2 minutes. Run manually when touching agent/tool-reliability
code (run_bash timeouts, Job Object process-tree kill, bounded search, grounding, schema
validation), not as part of every commit:

    python tests/agent_reliability_benchmark.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import openai
from rich.console import Console

from myriaxon.dotenv_support import load_dotenv_into_environ
from myriaxon.openai_agent import OpenAIAgent
from myriaxon.constants import PROVIDER_DEEPSEEK

load_dotenv_into_environ()
console = Console(quiet=True)  # suppress live tool-call/streaming noise, keep only our own prints

client = openai.OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")


def new_agent():
    return OpenAIAgent(client, "deepseek-v4-flash", console, yolo=True, provider=PROVIDER_DEEPSEEK)


def in_workspace(fn):
    def wrapper():
        tmp_dir = Path(tempfile.mkdtemp(prefix="myriaxon_bench_"))
        old_cwd = os.getcwd()
        os.chdir(tmp_dir)
        try:
            return fn(tmp_dir)
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return wrapper


@in_workspace
def task_bugfix(tmp_dir: Path) -> tuple[bool, str]:
    """Fix an off-by-one bug so a simple self-check script exits 0."""
    (tmp_dir / "mathutils.py").write_text(textwrap.dedent("""\
        def last_index(items):
            # BUG: off-by-one -- should be len(items) - 1
            return len(items)
    """), encoding="utf-8")
    (tmp_dir / "check.py").write_text(textwrap.dedent("""\
        from mathutils import last_index
        items = ["a", "b", "c"]
        assert last_index(items) == 2, f"expected 2, got {last_index(items)}"
        print("OK")
    """), encoding="utf-8")

    agent = new_agent()
    agent.run_turn(
        "There's a bug in mathutils.py's last_index function. Run check.py to see it fail, "
        "fix the bug, and confirm check.py passes afterward."
    )
    result = os.system(f'"{sys.executable}" check.py > nul 2>&1')
    return result == 0, "check.py exited 0" if result == 0 else "check.py still fails after the agent's fix"


@in_workspace
def task_grounding(tmp_dir: Path) -> tuple[bool, str]:
    """Answer must come from actually reading the file -- a fabricated answer fails."""
    (tmp_dir / "secret.txt").write_text("MARKER_VALUE=quokka-77-plasma\n", encoding="utf-8")
    agent = new_agent()
    agent.run_turn("Read secret.txt and tell me the exact value of MARKER_VALUE.")
    reply = agent.last_assistant_text()
    return "quokka-77-plasma" in reply, f"reply: {reply[:200]!r}"


@in_workspace
def task_conditional_no_clobber(tmp_dir: Path) -> tuple[bool, str]:
    """Instruction-following: must NOT overwrite an existing file, just report it exists."""
    (tmp_dir / "notes.md").write_text("ORIGINAL CONTENT -- DO NOT LOSE THIS\n", encoding="utf-8")
    agent = new_agent()
    agent.run_turn(
        "If notes.md doesn't already exist, create it with the text 'hello'. If it already "
        "exists, just tell me it exists -- don't modify it."
    )
    content = (tmp_dir / "notes.md").read_text(encoding="utf-8")
    preserved = "ORIGINAL CONTENT -- DO NOT LOSE THIS" in content
    return preserved, "file preserved" if preserved else f"file was overwritten: {content[:100]!r}"


@in_workspace
def task_multi_file_rename(tmp_dir: Path) -> tuple[bool, str]:
    """Consistent rename across two files -- both must be updated, old name gone from both."""
    (tmp_dir / "a.py").write_text("def calc_total(items):\n    return sum(items)\n", encoding="utf-8")
    (tmp_dir / "b.py").write_text(
        "from a import calc_total\n\nprint(calc_total([1, 2, 3]))\n", encoding="utf-8"
    )
    agent = new_agent()
    agent.run_turn(
        "Rename the function calc_total to compute_sum in a.py, and update every place that "
        "calls or imports it (including b.py) so everything stays consistent."
    )
    a_text = (tmp_dir / "a.py").read_text(encoding="utf-8")
    b_text = (tmp_dir / "b.py").read_text(encoding="utf-8")
    ok = "compute_sum" in a_text and "compute_sum" in b_text and "calc_total" not in a_text and "calc_total" not in b_text
    return ok, f"a.py: {a_text!r}  b.py: {b_text!r}"


@in_workspace
def task_read_only_no_side_effects(tmp_dir: Path) -> tuple[bool, str]:
    """Pure question about a file's content -- the file must remain byte-for-byte unchanged."""
    original = '{"retries": 7, "timeout": 30}\n'
    (tmp_dir / "config.json").write_text(original, encoding="utf-8")
    agent = new_agent()
    agent.run_turn("What is the 'retries' value in config.json? Just tell me, don't change anything.")
    reply = agent.last_assistant_text()
    unchanged = (tmp_dir / "config.json").read_text(encoding="utf-8") == original
    answered = "7" in reply
    return unchanged and answered, f"unchanged={unchanged} answered={answered} reply={reply[:150]!r}"


@in_workspace
def task_bash_timeout_actually_returns(tmp_dir: Path) -> tuple[bool, str]:
    """Stress-tests the run_bash timeout fix (see tools.py's foreground-drain-thread change) --
    a command that sleeps far longer than its requested timeout must still return control to the
    harness near the timeout, not after the full sleep duration."""
    import time

    agent = new_agent()
    start = time.time()
    agent.run_turn(
        "Run this exact command with a 5 second timeout and nothing else: "
        "powershell -NoProfile -Command \"Start-Sleep -Seconds 60\""
    )
    elapsed = time.time() - start
    ok = elapsed < 30  # generous margin over the 5s tool timeout, still far short of the 60s sleep
    return ok, f"elapsed={elapsed:.1f}s (should be well under 60s if the timeout actually fired)"


@in_workspace
def task_bounded_search_completes(tmp_dir: Path) -> tuple[bool, str]:
    """Stress-tests the SEARCH_TIME_BUDGET_SECONDS/SEARCH_SCAN_CAP fix in tools.py -- grepping a
    directory with many files must complete promptly, not hang."""
    import time

    subdir = tmp_dir / "many_files"
    subdir.mkdir()
    for i in range(500):
        (subdir / f"file_{i}.txt").write_text(f"line one\nTARGET_STRING_{i % 7}\nline three\n", encoding="utf-8")

    agent = new_agent()
    start = time.time()
    agent.run_turn(f"Search {subdir} for the text TARGET_STRING_3 and tell me roughly how many files contain it.")
    elapsed = time.time() - start
    ok = elapsed < 60
    return ok, f"elapsed={elapsed:.1f}s over 500 files (should complete well under a minute)"


@in_workspace
def task_timeout_kills_grandchild(tmp_dir: Path) -> tuple[bool, str]:
    """Stress-tests the Job Object whole-tree-kill guarantee (job_object.py), but driven through
    the real model this time -- earlier verification of this exact guarantee called _run_bash
    directly from Python, bypassing the model. This is the first true end-to-end check: the
    model itself must produce a command whose grandchild process also dies on timeout, not just
    the immediate child."""
    import time

    marker = tmp_dir / "grandchild_marker.txt"
    agent = new_agent()
    command = (
        f'powershell -NoProfile -Command "Start-Process powershell -ArgumentList '
        f"'-NoProfile -Command Start-Sleep -Seconds 8; Set-Content -Path {marker} -Value done' "
        f'-WindowStyle Hidden; Start-Sleep -Seconds 30"'
    )
    agent.run_turn(
        f"Run this exact command with a 3 second timeout and nothing else: {command}"
    )
    time.sleep(2)  # let the (correctly killed) grandchild's 8s sleep have had time to finish if it survived
    survived = marker.exists()
    return not survived, "grandchild was killed (marker never written)" if not survived else "grandchild survived and wrote its marker -- Job Object tree-kill did not propagate through the model-driven path"


@in_workspace
def task_long_horizon_checklist(tmp_dir: Path) -> tuple[bool, str]:
    """4-step interdependent task (create module, create test, create runner, run it) --
    must track and complete every step without losing state partway through. This is the exact
    failure mode long-horizon benchmarks (e.g. Terminal-Bench 3.0) are designed to expose."""
    agent = new_agent()
    agent.run_turn(
        "Do all of these steps, in order: "
        "1) Create shapes.py containing a Circle class with __init__(self, radius) and an "
        "area() method returning 3.14159 * radius ** 2. "
        "2) Create test_shapes.py that imports Circle from shapes and asserts "
        "abs(Circle(2).area() - 12.56636) < 0.001. "
        "3) Create run_all.py that runs the test from test_shapes.py and prints exactly "
        "'ALL TESTS PASSED' if it succeeds. "
        "4) Run run_all.py and confirm it prints ALL TESTS PASSED."
    )
    shapes_exists = (tmp_dir / "shapes.py").exists()
    test_exists = (tmp_dir / "test_shapes.py").exists()
    runner_exists = (tmp_dir / "run_all.py").exists()
    if not (shapes_exists and test_exists and runner_exists):
        return False, (
            f"shapes.py={shapes_exists} test_shapes.py={test_exists} "
            f"run_all.py={runner_exists} (not all 3 files created)"
        )

    result = subprocess.run([sys.executable, "run_all.py"], cwd=tmp_dir, capture_output=True, text=True)
    passed = result.returncode == 0 and "ALL TESTS PASSED" in result.stdout
    return passed, f"all 3 files created; returncode={result.returncode} stdout={result.stdout!r} stderr={result.stderr[:200]!r}"


@in_workspace
def task_error_recovery(tmp_dir: Path) -> tuple[bool, str]:
    """A naive edit_file attempt targeting "return items" is genuinely ambiguous -- it appears
    (as an exact or embedded substring) in all three functions below -- so a first attempt
    without enough surrounding context hits edit_file's own "not unique" guard (tools.py). The
    agent must recover by disambiguating with more context, not give up or edit the wrong
    function."""
    (tmp_dir / "calc.py").write_text(textwrap.dedent("""\
        def total(items):
            return items

        def first(items):
            return items[0]

        def passthrough(items):
            return items
    """), encoding="utf-8")

    agent = new_agent()
    agent.run_turn(
        "In calc.py, change ONLY the total function so it returns sum(items) instead of items. "
        "Leave first and passthrough exactly as they are."
    )
    content = (tmp_dir / "calc.py").read_text(encoding="utf-8")
    total_fixed = "def total(items):\n    return sum(items)" in content
    others_untouched = content.count("return items") == 2  # first()'s and passthrough()'s, unchanged
    ok = total_fixed and others_untouched
    return ok, f"total_fixed={total_fixed} others_untouched={others_untouched}\n{content}"


@in_workspace
def task_build_test_fix_loop(tmp_dir: Path) -> tuple[bool, str]:
    """Compressed SWE-bench-style loop: run a failing test, fix the underlying bug, re-run to
    confirm -- the same real-tool-use chain (run_bash/edit_file/re-verification) that real
    coding benchmarks score."""
    (tmp_dir / "stringutils.py").write_text(textwrap.dedent("""\
        def reverse_words(sentence):
            # BUG: splits into words but forgets to reverse their order
            return " ".join(sentence.split())
    """), encoding="utf-8")
    (tmp_dir / "test_stringutils.py").write_text(textwrap.dedent("""\
        from stringutils import reverse_words

        def test_reverse_words():
            assert reverse_words("hello world foo") == "foo world hello"

        if __name__ == "__main__":
            test_reverse_words()
            print("ALL TESTS PASSED")
    """), encoding="utf-8")

    agent = new_agent()
    agent.run_turn(
        "Run test_stringutils.py. It will fail. Fix the bug in stringutils.py's reverse_words "
        "function so the test passes, then re-run test_stringutils.py to confirm."
    )
    result = subprocess.run([sys.executable, "test_stringutils.py"], cwd=tmp_dir, capture_output=True, text=True)
    passed = result.returncode == 0 and "ALL TESTS PASSED" in result.stdout
    return passed, f"returncode={result.returncode} stdout={result.stdout!r} stderr={result.stderr[:200]!r}"


TASKS = [
    ("bugfix", task_bugfix),
    ("grounding", task_grounding),
    ("conditional_no_clobber", task_conditional_no_clobber),
    ("multi_file_rename", task_multi_file_rename),
    ("read_only_no_side_effects", task_read_only_no_side_effects),
    ("bash_timeout_actually_returns", task_bash_timeout_actually_returns),
    ("bounded_search_completes", task_bounded_search_completes),
    ("timeout_kills_grandchild", task_timeout_kills_grandchild),
    ("long_horizon_checklist", task_long_horizon_checklist),
    ("error_recovery", task_error_recovery),
    ("build_test_fix_loop", task_build_test_fix_loop),
]

if __name__ == "__main__":
    results = []
    for name, fn in TASKS:
        try:
            passed, detail = fn()
        except Exception as exc:
            passed, detail = False, f"EXCEPTION: {exc!r}"
        results.append((name, passed, detail))
        print(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}")

    total = len(results)
    passed_count = sum(1 for _, p, _ in results if p)
    print(f"\n{passed_count}/{total} passed")
