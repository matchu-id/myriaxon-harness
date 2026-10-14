"""promptfoo custom "exec:" provider -- wraps Myriaxon's non-interactive -p/--output-format json
mode so promptfoo can red-team Myriaxon's real system prompt + tool-calling agent loop, not a bare
model API. Called by promptfoo as:

    python redteam/myriaxon_provider.py "<prompt>" "<options_json>" "<context_json>"

(promptfoo's exec: provider contract -- see providers/scriptBasedProvider.ts in the installed
package: prompt, options, and context are appended as the last three argv entries, and whatever
this script prints to stdout becomes the provider's output.)

-y/--yolo is required here: red-teaming means testing whether the agent can be tricked into
*attempting* an unsafe tool call with no human in the loop to confirm it -- exactly the scenario
an attacker controlling only the prompt would face. --mode auto goes further (auto-approves even
without -y in some code paths); yolo alone is the documented "skip confirmation" flag and is
sufficient here.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")


def main() -> None:
    prompt = sys.argv[1]

    env = os.environ.copy()
    # The module name and the environment variables both changed in the rebrand. Renaming the
    # package without renaming these two lines is the failure this file itself demonstrates:
    # the harness would have been invoked as a module that no longer exists, and the provider
    # selection would have been ignored because the variable is no longer read.
    env.setdefault("MYRIAXON_PROVIDER", "deepseek")  # the most reliable provider tested

    # Never run these probes against the real Myriaxon repository -- several plugins
    # (secret-env-read, secret-file-read, sandbox-write-escape, ...) are *designed* to see whether
    # the agent reads or leaks whatever is reachable from its cwd, and the real repo's own .env has
    # real, live API keys sitting right there. Confirmed live this was a real gap in the first
    # version of this script (cwd=PROJECT_ROOT directly) -- caught before any probe actually got far
    # enough to exploit it (every result up to that point was a timeout; a filesystem scan turned up
    # no unexpected writes or leaked secrets). Each invocation now gets its own disposable, empty
    # scratch directory instead, mirroring tests/large_scale_task_probe.py's in_workspace pattern
    # -- there is nothing sensitive there to leak even if a probe fully succeeds.
    workspace = tempfile.mkdtemp(prefix="myriaxon_redteam_")
    try:
        result = subprocess.run(
            [PYTHON, "-m", "myriaxon", "-p", prompt, "--output-format", "json", "-y"],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=330,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        # 180s (the original value) was confirmed live to be too short: a legitimate, correct
        # refusal of a genuinely tricky prompt (e.g. a supply-chain-poisoning postinstall-script
        # request) took 276s end to end, well past that limit -- and promptfoo's redteam grader
        # scores a timeout's empty/error output as PASS (no evidence of compliance found), so a
        # timeout was silently masquerading as "the agent handled this safely" when really we
        # just never got a real answer. 330s gives real headroom above the slowest confirmed
        # legitimate response without being so long a genuinely hung call blocks the whole batch.
        print("[myriaxon_provider error] myriaxon timed out after 330s")
        return
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    stdout = result.stdout.strip()
    if not stdout:
        print(f"[myriaxon_provider error] empty stdout; stderr={result.stderr[:500]!r}")
        return

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        print(f"[myriaxon_provider error] non-JSON stdout: {stdout[:500]!r}")
        return

    if data.get("error"):
        print(f"[myriaxon error] {data['error']}")
    else:
        print(data.get("text") or "")


if __name__ == "__main__":
    main()
