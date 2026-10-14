"""Is the cached prefix stable across requests?

Cache hits on Anthropic and OpenAI-compatible providers depend on an identical *prefix*: tools, then
system, then the start of the conversation. Anything that varies per request -- a timestamp, a session
id, a set re-serialised into a different order -- invalidates the cache, and a stable prefix is worth
far more than any schema trim. The user's own status line reports the cache hit rate, so this is
measurable rather than theoretical.

This builds the tool list and system prompt twice from a fresh state and compares the exact bytes.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PROBE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from myriaxon import model_profiles, tool_registry
from myriaxon.system_prompt import build_system_prompt

defs = tool_registry.build_tool_defs()

class A:
    profile = model_profiles.detect_profile("deepseek/deepseek-v4.1-flash", agent_mode="single")
    tools = defs
    project_instructions = ""
    skills = []
    persona_identity = ""

print(json.dumps({
    "tools": [d.get("name") for d in defs],
    "tools_json": json.dumps(defs),
    "prompt": build_system_prompt(A()),
    "openai_tools": [d.get("function", {}).get("name") for d in tool_registry.build_tool_defs_openai()],
}))
'''


def run_once(index: int) -> dict:
    """Build in a fresh interpreter, so module-level caching cannot hide per-request variation."""
    import os

    # The full environment plus a different hash seed. An allowlisted environment broke this: without
    # USERPROFILE, `Path.home()` raises on Windows and the probe died before printing anything.
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = str(index * 7)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", PROBE, str(REPO)],
        capture_output=True, text=True, timeout=180, env=env, cwd=str(REPO),
    )
    if result.returncode != 0:
        print(result.stderr[-2000:])
        raise SystemExit(1)
    return json.loads(result.stdout.strip().splitlines()[-1])


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def main() -> int:
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        if not ok:
            failures += 1
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail and not ok else ''}")

    print("building the prefix three times in separate processes, with different hash seeds\n")
    runs = [run_once(i) for i in range(3)]

    for index, run in enumerate(runs, 1):
        print(f"  run {index}: tools={digest(run['tools_json'])} prompt={digest(run['prompt'])}")

    print()
    check("the tool ORDER is identical every time",
          runs[0]["tools"] == runs[1]["tools"] == runs[2]["tools"],
          "a reordered tool list invalidates the cached prefix")
    check("the tool BYTES are identical every time",
          len({digest(r["tools_json"]) for r in runs}) == 1,
          "the serialised schemas differ between runs")
    check("the system prompt is identical every time",
          len({digest(r["prompt"]) for r in runs}) == 1,
          "the system prompt contains something that varies")
    check("the OpenAI-shaped tool order is identical too",
          runs[0]["openai_tools"] == runs[1]["openai_tools"] == runs[2]["openai_tools"])

    # Where does the prefix actually need to be stable? Tools come first, then system. If a variable
    # element sits EARLY, everything after it misses the cache.
    print("\n  prefix composition:")
    tools_chars = len(runs[0]["tools_json"])
    prompt_chars = len(runs[0]["prompt"])
    print(f"    tools           {tools_chars:>8,} chars  (position 1 -- must be byte-stable)")
    print(f"    system prompt   {prompt_chars:>8,} chars  (position 2 -- must be byte-stable)")
    print(f"    total           {tools_chars + prompt_chars:>8,} chars")

    # Does the system prompt contain anything time- or process-varying?
    import re

    prompt = runs[0]["prompt"]
    suspicious = []
    for pattern, label in (
        (r"\b\d{4}-\d{2}-\d{2}T?\d{0,2}", "an ISO timestamp"),
        (r"\b[0-9a-f]{8,}\b", "a hex id"),
        (r"\b\d{10,}\b", "an epoch-like number"),
        (r"session|pid|process id", "a process/session reference"),
    ):
        found = re.findall(pattern, prompt, re.I)
        if found:
            suspicious.append(f"{label}: {found[:3]}")
    check("the system prompt carries no time- or process-varying text",
          not suspicious, "; ".join(suspicious))

    print()
    print("prefix stability verified" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
