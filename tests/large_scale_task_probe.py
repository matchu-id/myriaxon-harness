"""50-task, self-authored probe of Myriaxon's real agent loop against real DeepSeek -- NOT literal
SWE-bench/Terminal-Bench task instances (those need external GitHub repos pinned to exact commits
plus Docker/conda environments per task, none of which are set up in this environment), but
tasks written to match their real category mix: bug fixing, multi-file refactors, feature-to-spec,
test writing, data manipulation, grounding/Q&A, CLI building, build-test-fix loops, and security
fixes. Every task is scored objectively (file content, subprocess exit code, or exact substring
match) -- never LLM-judged.

Real, billed DeepSeek API calls; expect roughly 15-30 minutes for all 50. Run manually:

    python tests/large_scale_task_probe.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

# Confirmed live this was a real bug: this Windows environment's stdout is cp1252, not UTF-8, so
# printing a model's own reply (which can contain a non-breaking hyphen, curly quote, etc.) or
# any Unicode character crashes the whole probe with an unhandled UnicodeEncodeError -- the exact
# same class of bug found and fixed in myriaxon/cli.py's main(). Applied here too so one
# non-ASCII character in a single task's output never aborts the entire 50-task run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import openai
from rich.console import Console

from myriaxon.dotenv_support import load_dotenv_into_environ
from myriaxon.openai_agent import OpenAIAgent
from myriaxon.constants import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_GEMINI_BASE_URL,
    DEFAULT_OPENROUTER_BASE_URL,
    PROVIDER_DEEPSEEK,
    PROVIDER_GEMINI,
    PROVIDER_OPENROUTER,
)

load_dotenv_into_environ()
console = Console(quiet=True)

# PROBE_TARGET selects which provider/model this run drives -- same 50 tasks either way, so a
# rerun against a different model/provider never means duplicating the task list.
_PROBE_TARGETS = {
    "deepseek": (PROVIDER_DEEPSEEK, "deepseek-v4-flash", DEFAULT_DEEPSEEK_BASE_URL, "DEEPSEEK_API_KEY"),
    "openrouter-gpt-oss-120b": (PROVIDER_OPENROUTER, "openai/gpt-oss-120b", DEFAULT_OPENROUTER_BASE_URL, "OPENROUTER_API_KEY"),
    "openrouter-nemotron": (PROVIDER_OPENROUTER, "nvidia/nemotron-3.5-lightning:free", DEFAULT_OPENROUTER_BASE_URL, "OPENROUTER_API_KEY"),
    "openrouter-ling": (PROVIDER_OPENROUTER, "inclusionai/ling-3.0-flash-fin:free", DEFAULT_OPENROUTER_BASE_URL, "OPENROUTER_API_KEY"),
    "gemini-3.8-flash": (PROVIDER_GEMINI, "gemini-3.8-flash", DEFAULT_GEMINI_BASE_URL, "GEMINI_API_KEY"),
    # Direct Gemini's Interactions API was quota-blocked on this key (confirmed live) -- routing
    # through OpenRouter instead, same model. The ":batch" variant (50% cheaper) was tested and
    # confirmed to 404 on the normal synchronous /chat/completions endpoint ("only available
    # through the Batch API") -- incompatible with this agent's real-time tool-calling loop, so
    # this uses the full-price, synchronous-capable non-batch id instead.
    "openrouter-gemini-3.8-flash": (PROVIDER_OPENROUTER, "google/gemini-3.8-flash", DEFAULT_OPENROUTER_BASE_URL, "OPENROUTER_API_KEY"),
}
_target = os.environ.get("PROBE_TARGET", "deepseek")
_provider, _model, _base_url, _api_key_env = _PROBE_TARGETS[_target]

if _provider == PROVIDER_GEMINI:
    import google.genai as genai

    client = genai.Client(api_key=os.environ[_api_key_env], http_options={"base_url": _base_url})
else:
    client = openai.OpenAI(api_key=os.environ[_api_key_env], base_url=_base_url)


def new_agent():
    if _provider == PROVIDER_GEMINI:
        from myriaxon.gemini_agent import GeminiAgent

        return GeminiAgent(client, _model, console, yolo=True)
    return OpenAIAgent(client, _model, console, yolo=True, provider=_provider)


def run_script(tmp_dir: Path, script: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, script], cwd=tmp_dir, capture_output=True, text=True)


# --- verification helpers (all objective -- file state / exit codes / exact substrings) --------

def verify_script(script="check.py", marker=None):
    def _verify(tmp_dir, agent):
        result = run_script(tmp_dir, script)
        ok = result.returncode == 0 and (marker is None or marker in result.stdout)
        return ok, f"rc={result.returncode} stdout={result.stdout[:250]!r} stderr={result.stderr[:200]!r}"
    return _verify


def verify_file(fname, must_contain=(), must_not_contain=()):
    def _verify(tmp_dir, agent):
        content = (tmp_dir / fname).read_text(encoding="utf-8") if (tmp_dir / fname).exists() else ""
        ok = all(s in content for s in must_contain) and all(s not in content for s in must_not_contain)
        return ok, content[:400]
    return _verify


def verify_files(checks: dict):
    """checks: {filename: (must_contain_tuple, must_not_contain_tuple)}"""
    def _verify(tmp_dir, agent):
        details = []
        ok = True
        for fname, (must, must_not) in checks.items():
            path = tmp_dir / fname
            content = path.read_text(encoding="utf-8") if path.exists() else ""
            file_ok = all(s in content for s in must) and all(s not in content for s in must_not)
            ok = ok and file_ok
            details.append(f"{fname}: ok={file_ok} content={content[:150]!r}")
        return ok, " | ".join(details)
    return _verify


def verify_reply(*substrings):
    def _verify(tmp_dir, agent):
        reply = agent.last_assistant_text()
        ok = all(s in reply for s in substrings)
        return ok, reply[:300]
    return _verify


def verify_reply_excludes(*substrings):
    def _verify(tmp_dir, agent):
        reply = agent.last_assistant_text()
        ok = all(s not in reply for s in substrings)
        return ok, reply[:300]
    return _verify


def in_workspace(fn):
    def wrapper():
        tmp_dir = Path(tempfile.mkdtemp(prefix="myriaxon_probe50_"))
        old_cwd = os.getcwd()
        os.chdir(tmp_dir)
        try:
            return fn(tmp_dir)
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return wrapper


def make_task(name, files: dict, prompt: str, verify):
    @in_workspace
    def _run(tmp_dir):
        for fname, content in files.items():
            path = tmp_dir / fname
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        agent = new_agent()
        agent.run_turn(prompt)
        return verify(tmp_dir, agent)
    return (name, _run)


TASKS = []

# === A. Bug fixes (10) ==========================================================================

TASKS.append(make_task(
    "bug_off_by_one_slice",
    {
        "mod.py": "def last_n(items, n):\n    # BUG: off-by-one, drops the final element\n    return items[-n:-1]\n",
        "check.py": "from mod import last_n\nassert last_n([1,2,3,4,5], 3) == [3,4,5], last_n([1,2,3,4,5], 3)\nprint('OK')\n",
    },
    "Run check.py, it fails due to a bug in mod.py's last_n. Fix it so check.py passes.",
    verify_script(marker="OK"),
))

TASKS.append(make_task(
    "bug_wrong_comparison",
    {
        "mod.py": "def is_adult(age):\n    # BUG: should be >= 18, not > 18\n    return age > 18\n",
        "check.py": "from mod import is_adult\nassert is_adult(18) is True\nassert is_adult(17) is False\nprint('OK')\n",
    },
    "Run check.py, it fails due to a bug in mod.py's is_adult. Fix it so check.py passes.",
    verify_script(marker="OK"),
))

TASKS.append(make_task(
    "bug_wrong_boolean_logic",
    {
        "mod.py": "def can_vote(age, is_citizen):\n    # BUG: uses 'or' instead of 'and'\n    return age >= 18 or is_citizen\n",
        "check.py": "from mod import can_vote\nassert can_vote(20, False) is False\nassert can_vote(20, True) is True\nprint('OK')\n",
    },
    "Run check.py, it fails due to a bug in mod.py's can_vote. Fix it so check.py passes.",
    verify_script(marker="OK"),
))

TASKS.append(make_task(
    "bug_mutable_default_arg",
    {
        "mod.py": "def add_item(item, bucket=[]):\n    # BUG: mutable default argument shared across calls\n    bucket.append(item)\n    return bucket\n",
        "check.py": (
            "from mod import add_item\n"
            "a = add_item('x')\n"
            "b = add_item('y')\n"
            "assert b == ['y'], b\n"
            "print('OK')\n"
        ),
    },
    "Run check.py, it fails due to a classic mutable-default-argument bug in mod.py's add_item. Fix it so check.py passes.",
    verify_script(marker="OK"),
))

TASKS.append(make_task(
    "bug_wrong_string_method",
    {
        "mod.py": "def clean(text):\n    # BUG: uses .strip('x') which strips a literal 'x', not whitespace/punctuation cleanup intended\n    return text.strip(',')\n",
        "check.py": "from mod import clean\nassert clean('  hello, world  ') == 'hello, world', repr(clean('  hello, world  '))\nprint('OK')\n",
    },
    "Run check.py, it fails due to a bug in mod.py's clean function (it should strip leading/trailing whitespace). Fix it so check.py passes.",
    verify_script(marker="OK"),
))

TASKS.append(make_task(
    "bug_wrong_exception_caught",
    {
        "mod.py": (
            "def safe_div(a, b):\n"
            "    try:\n"
            "        return a / b\n"
            "    except TypeError:\n"  # BUG: should catch ZeroDivisionError
            "        return None\n"
        ),
        "check.py": "from mod import safe_div\nassert safe_div(10, 0) is None\nassert safe_div(10, 2) == 5\nprint('OK')\n",
    },
    "Run check.py, it fails/crashes due to a bug in mod.py's safe_div (wrong exception type is caught). Fix it so check.py passes.",
    verify_script(marker="OK"),
))

TASKS.append(make_task(
    "bug_wrong_dict_default",
    {
        "mod.py": "def get_count(counts, key):\n    # BUG: KeyError instead of returning 0 for missing keys\n    return counts[key]\n",
        "check.py": "from mod import get_count\nassert get_count({'a': 1}, 'a') == 1\nassert get_count({'a': 1}, 'b') == 0\nprint('OK')\n",
    },
    "Run check.py, it fails due to a bug in mod.py's get_count (missing keys should return 0). Fix it so check.py passes.",
    verify_script(marker="OK"),
))

TASKS.append(make_task(
    "bug_float_rounding",
    {
        "mod.py": "def to_two_decimals(x):\n    # BUG: doesn't actually round\n    return x\n",
        "check.py": "from mod import to_two_decimals\nassert to_two_decimals(3.14159) == 3.14, to_two_decimals(3.14159)\nprint('OK')\n",
    },
    "Run check.py, it fails due to a bug in mod.py's to_two_decimals (should round to 2 decimal places). Fix it so check.py passes.",
    verify_script(marker="OK"),
))

TASKS.append(make_task(
    "bug_loop_range_missing_last",
    {
        "mod.py": "def sum_up_to(n):\n    total = 0\n    for i in range(1, n):  # BUG: should be range(1, n+1)\n        total += i\n    return total\n",
        "check.py": "from mod import sum_up_to\nassert sum_up_to(5) == 15, sum_up_to(5)\nprint('OK')\n",
    },
    "Run check.py, it fails due to a bug in mod.py's sum_up_to (off-by-one in the range). Fix it so check.py passes.",
    verify_script(marker="OK"),
))

TASKS.append(make_task(
    "bug_recursive_base_case",
    {
        "mod.py": "def factorial(n):\n    if n == 1:  # BUG: missing n == 0 base case\n        return 1\n    return n * factorial(n - 1)\n",
        "check.py": "from mod import factorial\nassert factorial(0) == 1\nassert factorial(5) == 120\nprint('OK')\n",
    },
    "Run check.py, it fails due to a bug in mod.py's factorial (wrong/missing base case). Fix it so check.py passes.",
    verify_script(marker="OK"),
))

# === B. Multi-file refactors (5) ================================================================

TASKS.append(make_task(
    "refactor_rename_three_files",
    {
        "core.py": "def process(data):\n    return data.upper()\n",
        "cli.py": "from core import process\nprint(process('hi'))\n",
        "tests.py": "from core import process\nassert process('a') == 'A'\n",
    },
    "Rename the function process to transform_text in core.py, and update every caller "
    "(cli.py and tests.py) so everything stays consistent.",
    verify_files({
        "core.py": (("def transform_text",), ("def process",)),
        "cli.py": (("transform_text",), ("process(",)),
        "tests.py": (("transform_text",), ("process(",)),
    }),
))

TASKS.append(make_task(
    "refactor_move_function",
    {
        "utils.py": "def helper_a():\n    return 1\n\ndef square(x):\n    return x * x\n",
        "main.py": "from utils import square\nprint(square(4))\n",
    },
    "Move the square function out of utils.py into a new file called mathops.py, and update "
    "main.py to import it from there instead.",
    verify_files({
        "mathops.py": (("def square",), ()),
        "utils.py": ((), ("def square",)),
        "main.py": (("from mathops import square",), ()),
    }),
))

TASKS.append(make_task(
    "refactor_add_parameter_with_default",
    {
        "greet.py": "def greet(name):\n    return f'Hello, {name}!'\n",
        "app.py": "from greet import greet\nprint(greet('Alice'))\n",
    },
    "Add an optional 'excited' parameter (default False) to greet in greet.py -- when True, "
    "the greeting should end with '!!!' instead of '!'. Keep app.py working without changes "
    "to its existing call.",
    verify_script("app.py"),
))

TASKS.append(make_task(
    "refactor_consolidate_constant",
    {
        "a.py": "MAX_RETRIES = 3\ndef run_a():\n    return MAX_RETRIES\n",
        "b.py": "MAX_RETRIES = 3\ndef run_b():\n    return MAX_RETRIES\n",
    },
    "a.py and b.py both define their own MAX_RETRIES = 3. Create a shared config.py with a "
    "single MAX_RETRIES constant, and update both a.py and b.py to import it from there "
    "instead of defining their own copy.",
    verify_files({
        "config.py": (("MAX_RETRIES = 3",), ()),
        "a.py": (("from config import MAX_RETRIES",), ()),
        "b.py": (("from config import MAX_RETRIES",), ()),
    }),
))

TASKS.append(make_task(
    "refactor_class_attribute_rename",
    {
        "account.py": (
            "class Account:\n"
            "    def __init__(self, bal):\n"
            "        self.bal = bal\n"
            "    def show(self):\n"
            "        return self.bal\n"
        ),
        "report.py": "from account import Account\na = Account(100)\nprint(a.bal)\n",
    },
    "Rename the Account class's bal attribute to balance in account.py, and update every "
    "usage (including report.py) to match.",
    # A plain "a.bal" substring check is broken here -- "a.balance" *contains* "a.bal" as a
    # prefix, so a correct rename would incorrectly fail a naive must-not-contain check.
    # Word-boundary regex distinguishes the old, un-renamed "self.bal"/"a.bal" from the new,
    # correctly-renamed "self.balance"/"a.balance".
    (lambda tmp_dir, agent: (
        (lambda account_text, report_text: (
            "self.balance" in account_text
            and re.search(r"\bself\.bal\b", account_text) is None
            and "a.balance" in report_text
            and re.search(r"\ba\.bal\b", report_text) is None,
            f"account.py={account_text!r} report.py={report_text!r}",
        ))(
            (tmp_dir / "account.py").read_text(encoding="utf-8"),
            (tmp_dir / "report.py").read_text(encoding="utf-8"),
        )
    )),
))

# === C. Feature addition to spec (10) ===========================================================

TASKS.append(make_task(
    "feature_add_method_to_class",
    {"stack.py": "class Stack:\n    def __init__(self):\n        self.items = []\n    def push(self, x):\n        self.items.append(x)\n"},
    "Add a pop() method to the Stack class in stack.py that removes and returns the last item, "
    "and an is_empty() method that returns True if the stack has no items.",
    (lambda tmp_dir, agent: (
        (lambda r: (r.returncode == 0, f"rc={r.returncode} out={r.stdout!r} err={r.stderr[:200]!r}"))(
            subprocess.run([sys.executable, "-c", "from stack import Stack\ns=Stack()\ns.push(1)\ns.push(2)\nassert s.pop()==2\nassert not s.is_empty()\nassert s.pop()==1\nassert s.is_empty()\nprint('OK')"], cwd=tmp_dir, capture_output=True, text=True)
        )
    )),
))

TASKS.append(make_task(
    "feature_input_validation",
    {"mod.py": "def set_age(age):\n    return age\n"},
    "Add input validation to set_age in mod.py: raise ValueError if age is negative or not an "
    "integer. Keep the return value the same for valid input.",
    (lambda tmp_dir, agent: (
        (lambda r: (r.returncode == 0, f"rc={r.returncode} out={r.stdout!r} err={r.stderr[:300]!r}"))(
            subprocess.run([sys.executable, "-c",
                "from mod import set_age\n"
                "assert set_age(5) == 5\n"
                "try:\n    set_age(-1)\n    raise SystemExit('should have raised')\nexcept ValueError:\n    pass\n"
                "try:\n    set_age('x')\n    raise SystemExit('should have raised')\nexcept ValueError:\n    pass\n"
                "print('OK')"
            ], cwd=tmp_dir, capture_output=True, text=True)
        )
    )),
))

TASKS.append(make_task(
    "feature_implement_from_docstring",
    {"mod.py": (
        "def flatten(nested):\n"
        "    \"\"\"Flatten a list of lists into a single flat list, e.g. "
        "flatten([[1,2],[3],[4,5]]) == [1,2,3,4,5]. Not implemented yet.\"\"\"\n"
        "    raise NotImplementedError\n"
    )},
    "Implement the flatten function in mod.py according to its docstring.",
    (lambda tmp_dir, agent: (
        (lambda r: (r.returncode == 0, f"rc={r.returncode} out={r.stdout!r} err={r.stderr[:200]!r}"))(
            subprocess.run([sys.executable, "-c",
                "from mod import flatten\nassert flatten([[1,2],[3],[4,5]]) == [1,2,3,4,5]\nprint('OK')"
            ], cwd=tmp_dir, capture_output=True, text=True)
        )
    )),
))

TASKS.append(make_task(
    "feature_memoization",
    {"mod.py": (
        "call_count = 0\n\n"
        "def slow_square(x):\n"
        "    global call_count\n"
        "    call_count += 1\n"
        "    return x * x\n"
    )},
    "Add memoization/caching to slow_square in mod.py so calling it twice with the same "
    "argument only does the actual computation once (call_count should not increase on a "
    "repeated call with the same input). Keep the function's return values correct.",
    (lambda tmp_dir, agent: (
        (lambda r: (r.returncode == 0, f"rc={r.returncode} out={r.stdout!r} err={r.stderr[:300]!r}"))(
            subprocess.run([sys.executable, "-c",
                "import mod\n"
                "assert mod.slow_square(4) == 16\n"
                "before = mod.call_count\n"
                "assert mod.slow_square(4) == 16\n"
                "assert mod.call_count == before, 'should not have recomputed'\n"
                "print('OK')"
            ], cwd=tmp_dir, capture_output=True, text=True)
        )
    )),
))

TASKS.append(make_task(
    "feature_retry_decorator",
    {"mod.py": (
        "attempts = []\n\n"
        "def flaky():\n"
        "    attempts.append(1)\n"
        "    if len(attempts) < 3:\n"
        "        raise RuntimeError('not yet')\n"
        "    return 'success'\n"
    )},
    "Write a retry decorator in mod.py that retries a function up to 3 times if it raises an "
    "exception, then apply it to the flaky function so calling flaky() once from outside "
    "eventually returns 'success' instead of raising.",
    (lambda tmp_dir, agent: (
        (lambda r: (r.returncode == 0, f"rc={r.returncode} out={r.stdout!r} err={r.stderr[:300]!r}"))(
            subprocess.run([sys.executable, "-c", "from mod import flaky\nassert flaky() == 'success'\nprint('OK')"], cwd=tmp_dir, capture_output=True, text=True)
        )
    )),
))

TASKS.append(make_task(
    "feature_lru_cache_class",
    {"mod.py": "# Implement a simple LRUCache class here.\n"},
    "In mod.py, implement a class LRUCache(capacity) with get(key) (returns -1 if missing) and "
    "put(key, value) methods, evicting the least-recently-used entry when capacity is exceeded.",
    (lambda tmp_dir, agent: (
        (lambda r: (r.returncode == 0, f"rc={r.returncode} out={r.stdout!r} err={r.stderr[:300]!r}"))(
            subprocess.run([sys.executable, "-c",
                "from mod import LRUCache\n"
                "c = LRUCache(2)\n"
                "c.put(1, 'a'); c.put(2, 'b')\n"
                "assert c.get(1) == 'a'\n"
                "c.put(3, 'c')  # evicts key 2 (least recently used)\n"
                "assert c.get(2) == -1\n"
                "assert c.get(3) == 'c'\n"
                "print('OK')"
            ], cwd=tmp_dir, capture_output=True, text=True)
        )
    )),
))

TASKS.append(make_task(
    "feature_context_manager",
    {"mod.py": "class Resource:\n    def __init__(self):\n        self.open = False\n"},
    "Make the Resource class in mod.py usable as a context manager (with statement): "
    "__enter__ should set self.open = True and return self, __exit__ should set self.open = False.",
    (lambda tmp_dir, agent: (
        (lambda r: (r.returncode == 0, f"rc={r.returncode} out={r.stdout!r} err={r.stderr[:200]!r}"))(
            subprocess.run([sys.executable, "-c",
                "from mod import Resource\n"
                "r = Resource()\n"
                "assert not r.open\n"
                "with r as x:\n    assert x.open\n"
                "assert not r.open\n"
                "print('OK')"
            ], cwd=tmp_dir, capture_output=True, text=True)
        )
    )),
))

TASKS.append(make_task(
    "feature_cli_flag",
    {"cli.py": (
        "import argparse\n\n"
        "def main():\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('name')\n"
        "    args = parser.parse_args()\n"
        "    print(f'Hello, {args.name}')\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )},
    "Add a --shout flag to cli.py's argparse setup -- when passed, the greeting should be "
    "printed in all uppercase instead.",
    (lambda tmp_dir, agent: (
        (lambda r: (r.returncode == 0 and "HELLO, BOB" in r.stdout, f"rc={r.returncode} out={r.stdout!r} err={r.stderr[:200]!r}"))(
            subprocess.run([sys.executable, "cli.py", "bob", "--shout"], cwd=tmp_dir, capture_output=True, text=True)
        )
    )),
))

TASKS.append(make_task(
    "feature_logging_no_behavior_change",
    {"mod.py": "def add(a, b):\n    return a + b\n"},
    "Add logging to the add function in mod.py (log the inputs and result using the standard "
    "logging module) without changing its return value or behavior.",
    (lambda tmp_dir, agent: (
        (lambda r: (r.returncode == 0, f"rc={r.returncode} out={r.stdout!r} err={r.stderr[:200]!r}"))(
            subprocess.run([sys.executable, "-c", "from mod import add\nassert add(2, 3) == 5\nprint('OK')"], cwd=tmp_dir, capture_output=True, text=True)
        )
    )),
))

TASKS.append(make_task(
    "feature_computed_property",
    {"shapes.py": "class Rectangle:\n    def __init__(self, width, height):\n        self.width = width\n        self.height = height\n"},
    "Add a computed 'area' property to the Rectangle class in shapes.py (accessed as "
    "rect.area, not rect.area()) that returns width * height.",
    (lambda tmp_dir, agent: (
        (lambda r: (r.returncode == 0, f"rc={r.returncode} out={r.stdout!r} err={r.stderr[:200]!r}"))(
            subprocess.run([sys.executable, "-c",
                "from shapes import Rectangle\nr = Rectangle(3, 4)\nassert r.area == 12, r.area\nprint('OK')"
            ], cwd=tmp_dir, capture_output=True, text=True)
        )
    )),
))

# === D. Test writing (5) =========================================================================

TASKS.append(make_task(
    "test_write_basic_unit_test",
    {"mod.py": "def is_palindrome(s):\n    return s == s[::-1]\n"},
    "Write a test file test_mod.py with a pytest-style test function for is_palindrome in "
    "mod.py, covering both a palindrome and a non-palindrome case. Then run it with pytest "
    "(or plain assertions if pytest isn't available) and confirm it passes.",
    verify_file("test_mod.py", must_contain=("is_palindrome",)),
))

TASKS.append(make_task(
    "test_edge_case_empty_input",
    {"mod.py": "def average(numbers):\n    return sum(numbers) / len(numbers)\n"},
    "Write test_mod.py testing average([1,2,3]) == 2, and also testing that calling "
    "average([]) raises a ZeroDivisionError. Print 'ALL TESTS PASSED' at the end if everything holds, "
    "and run the file to confirm.",
    verify_script("test_mod.py", marker="ALL TESTS PASSED"),
))

TASKS.append(make_task(
    "test_multiple_cases_loop",
    {"mod.py": "def is_even(n):\n    return n % 2 == 0\n"},
    "Write test_mod.py that checks is_even for the numbers 0, 1, 2, 3, 4 all give the correct "
    "result (using a loop over expected pairs), printing 'ALL TESTS PASSED' if they all match. Run it to confirm.",
    verify_script("test_mod.py", marker="ALL TESTS PASSED"),
))

TASKS.append(make_task(
    "test_would_have_caught_bug",
    {"mod.py": "def clamp(x, lo, hi):\n    # subtly wrong: doesn't clamp the lower bound\n    return min(x, hi)\n"},
    # Explicitly specifies the runnable-via-`python file.py`-with-a-print convention, matching
    # every other test-writing task in this suite -- an earlier, ambiguous version of this
    # prompt let the model reasonably choose idiomatic pytest-style `def test_...():` functions
    # with no __main__ guard, which produce silent, no-op output when run directly (not a real
    # harness/model bug, just an unfair, inconsistent task spec).
    "Write test_clamp.py as a plain script (not pytest-style) with tests for clamp(5, 0, 10) == 5, "
    "clamp(-5, 0, 10) == 0, and clamp(15, 0, 10) == 10, run directly via 'if __name__ == \"__main__\":' "
    "that prints 'ALL TESTS PASSED' if all assertions hold. Run it -- it will reveal a real bug in "
    "mod.py's clamp. Fix the bug so all the tests pass and it prints ALL TESTS PASSED.",
    verify_script("test_clamp.py", marker="ALL TESTS PASSED"),
))

TASKS.append(make_task(
    "test_exception_raised",
    {"mod.py": "def parse_positive_int(s):\n    n = int(s)\n    if n <= 0:\n        raise ValueError('must be positive')\n    return n\n"},
    "Write test_mod.py verifying parse_positive_int('5') == 5 and that parse_positive_int('-1') "
    "raises ValueError. Print 'ALL TESTS PASSED' if both hold, and run it to confirm.",
    verify_script("test_mod.py", marker="ALL TESTS PASSED"),
))

# === E. File / data manipulation (5) ============================================================

TASKS.append(make_task(
    "data_update_json_value",
    {"config.json": '{"retries": 3, "timeout": 10}\n'},
    "Update config.json so 'retries' is 5, leaving 'timeout' unchanged.",
    verify_file("config.json", must_contain=('"retries": 5', '"timeout": 10')),
))

TASKS.append(make_task(
    "data_csv_sum",
    {"data.csv": "name,amount\nalice,10\nbob,20\ncarol,30\n"},
    "Read data.csv and write the total of the amount column to a new file total.txt, "
    "containing just the number.",
    verify_file("total.txt", must_contain=("60",)),
))

TASKS.append(make_task(
    "data_merge_json_files",
    {"a.json": '{"x": 1}\n', "b.json": '{"y": 2}\n'},
    "Merge a.json and b.json into a new file merged.json containing both keys.",
    verify_file("merged.json", must_contain=('"x"', "1", '"y"', "2")),
))

TASKS.append(make_task(
    "data_filter_lines",
    {"log.txt": "INFO: started\nERROR: disk full\nINFO: retrying\nERROR: timeout\nINFO: done\n"},
    "Read log.txt and write only the lines containing ERROR to errors.txt.",
    verify_files({
        "errors.txt": (("disk full", "timeout"), ("started", "retrying", "done")),
    }),
))

TASKS.append(make_task(
    "data_update_single_line_config",
    {"app.conf": "host=localhost\nport=8080\ndebug=false\n"},
    "In app.conf, change only the port value to 9090, leaving host and debug exactly as they are.",
    verify_file("app.conf", must_contain=("host=localhost", "port=9090", "debug=false")),
))

# === F. Grounding / Q&A (5) ======================================================================

TASKS.append(make_task(
    "qa_combine_two_files",
    {"a.txt": "The secret code is BANANA-42.\n", "b.txt": "The code from a.txt must be repeated twice to unlock.\n"},
    "Read both a.txt and b.txt, then tell me: what should I type to unlock (the secret code, repeated twice)?",
    (lambda tmp_dir, agent: (
        (lambda reply: (reply.count("BANANA-42") >= 2, reply[:300]))(agent.last_assistant_text())
    )),
))

TASKS.append(make_task(
    "qa_count_word_occurrences",
    {"doc.txt": "cat dog cat bird cat dog\n"},
    "Count how many times the word 'cat' appears in doc.txt and tell me the exact number.",
    verify_reply("3"),
))

TASKS.append(make_task(
    "qa_which_file_has_function",
    {"a.py": "def alpha():\n    pass\n", "b.py": "def beta():\n    pass\n", "c.py": "def gamma():\n    pass\n"},
    "Which file (a.py, b.py, or c.py) defines the function named beta? Just tell me the filename.",
    verify_reply("b.py"),
))

TASKS.append(make_task(
    "qa_honest_not_found",
    {"notes.txt": "Meeting notes: discussed budget and timeline.\n"},
    "Does notes.txt mention anything about a function called quantum_teleport? Answer honestly "
    "based on what's actually in the file.",
    verify_reply_excludes("quantum_teleport("),  # should NOT fabricate code/details that aren't there
))

TASKS.append(make_task(
    "qa_summarize_real_detail",
    {"readme.txt": "This tool is called Widgetizer. Its main entry point is run_widget.py. Version: 4.2.1\n"},
    "What is the version number mentioned in readme.txt? Just tell me the exact version string.",
    verify_reply("4.2.1"),
))

# === G. CLI / argument parsing (3) ==============================================================

TASKS.append(make_task(
    "cli_build_from_spec",
    {},
    "Create a script calc.py using argparse with two required positional int arguments 'a' and "
    "'b', and an optional --op flag (choices: add, sub; default add). It should print the result "
    "of a+b or a-b depending on --op.",
    (lambda tmp_dir, agent: (
        (lambda r1, r2: (
            r1.returncode == 0 and "8" in r1.stdout and r2.returncode == 0 and "2" in r2.stdout,
            f"add: rc={r1.returncode} out={r1.stdout!r} | sub: rc={r2.returncode} out={r2.stdout!r}"
        ))(
            subprocess.run([sys.executable, "calc.py", "5", "3"], cwd=tmp_dir, capture_output=True, text=True),
            subprocess.run([sys.executable, "calc.py", "5", "3", "--op", "sub"], cwd=tmp_dir, capture_output=True, text=True),
        )
    )),
))

TASKS.append(make_task(
    "cli_add_subcommand",
    {"tool.py": (
        "import argparse\n\n"
        "def main():\n"
        "    parser = argparse.ArgumentParser()\n"
        "    sub = parser.add_subparsers(dest='command')\n"
        "    greet_p = sub.add_parser('greet')\n"
        "    greet_p.add_argument('name')\n"
        "    args = parser.parse_args()\n"
        "    if args.command == 'greet':\n"
        "        print(f'Hello, {args.name}')\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )},
    "Add a new subcommand 'farewell' to tool.py that takes a name argument and prints "
    "'Goodbye, <name>'.",
    (lambda tmp_dir, agent: (
        (lambda r: (r.returncode == 0 and "Goodbye, Sam" in r.stdout, f"rc={r.returncode} out={r.stdout!r} err={r.stderr[:200]!r}"))(
            subprocess.run([sys.executable, "tool.py", "farewell", "Sam"], cwd=tmp_dir, capture_output=True, text=True)
        )
    )),
))

TASKS.append(make_task(
    "cli_fix_missing_arg_crash",
    {"app.py": (
        "import argparse\n\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--name')\n"
        "args = parser.parse_args()\n"
        "print(args.name.upper())\n"  # crashes with AttributeError if --name isn't passed
    )},
    "Running app.py without --name crashes with an ugly AttributeError. Fix app.py so that "
    "if --name isn't provided, it prints 'no name given' instead of crashing.",
    (lambda tmp_dir, agent: (
        (lambda r: (r.returncode == 0 and "no name given" in r.stdout.lower(), f"rc={r.returncode} out={r.stdout!r} err={r.stderr[:200]!r}"))(
            subprocess.run([sys.executable, "app.py"], cwd=tmp_dir, capture_output=True, text=True)
        )
    )),
))

# === H. Build-test-fix loops (4) =================================================================

TASKS.append(make_task(
    "loop_import_typo",
    {
        "helpers.py": "def double(x):\n    return x * 2\n",
        "main.py": "from helper import double\nprint(double(5))\n",  # BUG: wrong module name
    },
    "Run main.py. It fails with an import error. Fix the bug and re-run to confirm it prints 10.",
    (lambda tmp_dir, agent: (
        (lambda r: (r.returncode == 0 and "10" in r.stdout, f"rc={r.returncode} out={r.stdout!r} err={r.stderr[:200]!r}"))(
            subprocess.run([sys.executable, "main.py"], cwd=tmp_dir, capture_output=True, text=True)
        )
    )),
))

TASKS.append(make_task(
    "loop_wrong_file_path",
    {
        "data/values.txt": "10\n20\n30\n",
        "main.py": "with open('value.txt') as f:\n    print(sum(int(x) for x in f))\n",  # wrong path
    },
    "Run main.py. It fails because the file path is wrong -- the real data is at data/values.txt. "
    "Fix main.py and re-run to confirm it prints 60.",
    (lambda tmp_dir, agent: (
        (lambda r: (r.returncode == 0 and "60" in r.stdout, f"rc={r.returncode} out={r.stdout!r} err={r.stderr[:200]!r}"))(
            subprocess.run([sys.executable, "main.py"], cwd=tmp_dir, capture_output=True, text=True)
        )
    )),
))

TASKS.append(make_task(
    "loop_two_bugs",
    {
        "mod.py": "def add(a, b):\n    return a - b\n\ndef mul(a, b):\n    return a + b\n",  # both wrong
        "check.py": "from mod import add, mul\nassert add(2, 3) == 5, add(2,3)\nassert mul(2, 3) == 6, mul(2,3)\nprint('OK')\n",
    },
    "Run check.py. It fails. There are TWO separate bugs in mod.py (in both add and mul). Find "
    "and fix both, then re-run check.py to confirm it passes.",
    verify_script(marker="OK"),
))

TASKS.append(make_task(
    "loop_fix_only_failing_test",
    {
        "mod.py": "def square(x):\n    return x * x\n\ndef cube(x):\n    return x * x\n",  # cube is wrong
        "check.py": (
            "from mod import square, cube\n"
            "assert square(4) == 16, 'square broken'\n"
            "assert cube(2) == 8, 'cube broken'\n"
            "print('OK')\n"
        ),
    },
    "Run check.py. One assertion passes and one fails. Fix ONLY the broken function in mod.py "
    "without touching the one that's already correct, then re-run to confirm both pass.",
    verify_script(marker="OK"),
))

# === I. Security-ish fixes (3, structural verification) =========================================

TASKS.append(make_task(
    "security_path_traversal",
    {"reader.py": (
        "import os\n\n"
        "BASE_DIR = 'safe_files'\n\n"
        "def read_file(name):\n"
        "    # BUG: no traversal protection, name could be '../../etc/passwd'\n"
        "    path = os.path.join(BASE_DIR, name)\n"
        "    with open(path) as f:\n"
        "        return f.read()\n"
    ), "safe_files/hello.txt": "hello world\n"},
    "The read_file function in reader.py is vulnerable to path traversal (a caller could pass "
    "'../something' to escape BASE_DIR). Fix it so it refuses to read any path that resolves "
    "outside BASE_DIR, while still correctly reading files that ARE inside BASE_DIR (like hello.txt).",
    (lambda tmp_dir, agent: (
        (lambda r: (
            r.returncode == 0 and "hello world" in r.stdout and "BLOCKED" in r.stdout,
            f"rc={r.returncode} out={r.stdout!r} err={r.stderr[:300]!r}",
        ))(
            subprocess.run([sys.executable, "-c",
                "from reader import read_file\n"
                "print(read_file('hello.txt').strip())\n"
                "try:\n"
                "    read_file('../outside.txt')\n"
                "    print('NOT BLOCKED')\n"
                "except Exception:\n"
                "    print('BLOCKED')\n"
            ], cwd=tmp_dir, capture_output=True, text=True)
        )
    )),
))

TASKS.append(make_task(
    "security_shell_string_concat",
    {"runner.py": (
        "import subprocess\n\n"
        "def list_dir(path):\n"
        "    # BUG: shell=True with naive string concatenation is command-injection-prone\n"
        "    return subprocess.run(f'dir {path}', shell=True, capture_output=True, text=True).stdout\n"
    )},
    "The list_dir function in runner.py builds a shell command via naive string concatenation "
    "with shell=True, which is command-injection-prone. Fix it to use a safer subprocess call "
    "(a list of arguments, no shell=True) while keeping the same behavior for a normal path.",
    verify_file("runner.py", must_not_contain=("shell=True",)),
))

TASKS.append(make_task(
    "security_hardcoded_secret",
    {"config.py": "API_KEY = 'sk-hardcoded-super-secret-12345'\n\ndef get_key():\n    return API_KEY\n"},
    "config.py has a hardcoded API key, which is a security risk. Fix it to read the key from "
    "the environment variable API_KEY instead (using os.environ), removing the hardcoded literal value.",
    verify_file("config.py", must_contain=("os.environ",), must_not_contain=("sk-hardcoded-super-secret-12345",)),
))

if __name__ == "__main__":
    print(f"=== PROBE_TARGET={_target}  model={_model}  provider={_provider} ===\n")
    results = []
    start_all = time.time()
    for name, fn in TASKS:
        t0 = time.time()
        try:
            passed, detail = fn()
        except Exception as exc:
            passed, detail = False, f"EXCEPTION: {exc!r}"
        elapsed = time.time() - t0
        results.append((name, passed, detail, elapsed))
        print(f"[{'PASS' if passed else 'FAIL'}] ({elapsed:.1f}s) {name}: {detail}")

    total = len(results)
    passed_count = sum(1 for _, p, _, _ in results if p)
    total_elapsed = time.time() - start_all
    print(f"\n{passed_count}/{total} passed  (total {total_elapsed:.0f}s)")
    failed = [r[0] for r in results if not r[1]]
    if failed:
        print("Failed tasks:", ", ".join(failed))
