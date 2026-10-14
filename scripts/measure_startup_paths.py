"""Side-by-side startup measurement: openai SDK path vs direct HTTP path.

    python scripts/measure_startup_paths.py

Runs each path in a FRESH process (the only honest way to measure import cost, since a warm
interpreter has already paid it) and reports total wall time to agent-ready.
"""
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "Scripts" / "python.exe"

SDK_SNIPPET = """
import time
t = time.perf_counter()
import openai
t_import = time.perf_counter() - t
client = openai.OpenAI(base_url="https://api.netraruntime.com/v1", api_key="x")
print(t_import, time.perf_counter() - t)
"""

DIRECT_SNIPPET = """
import sys, time
sys.path.insert(0, r"{repo}")
t = time.perf_counter()
from myriaxon.direct_http_client import DirectOpenAI
t_import = time.perf_counter() - t
client = DirectOpenAI(base_url="https://api.netraruntime.com/v1", api_key="x")
print(t_import, time.perf_counter() - t)
""".format(repo=REPO)

AGENT_SNIPPET = """
import sys, time, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, r"{repo}")
t = time.perf_counter()
from myriaxon.direct_http_client import DirectOpenAI
client = DirectOpenAI(base_url="https://api.netraruntime.com/v1", api_key="x")
from myriaxon.cli import _build_agent
from myriaxon.settings import load_settings
from myriaxon.skills import discover_skills
from myriaxon.subagents import discover_subagent_types
from myriaxon.hooks import load_hooks_config
from myriaxon.project_instructions import load_project_instructions
from myriaxon.scheduler import Scheduler
from myriaxon.config import Config
from rich.console import Console
settings = load_settings()
config = Config(api_key="x", base_url="https://api.netraruntime.com/v1", model="deepseek/deepseek-v4.1-flash", provider="netra")
agent = _build_agent(config, config.model, Console(quiet=True), True, None, [], [], discover_skills(),
                     discover_subagent_types(), load_hooks_config(), load_project_instructions(), Scheduler(), settings,
                     mode="auto_edit")
print(time.perf_counter() - t)
""".format(repo=REPO)


def run(label, code, workdir):
    start = time.perf_counter()
    result = subprocess.run([str(PY), "-c", code], cwd=str(workdir), capture_output=True, text=True, timeout=600)
    wall = time.perf_counter() - start
    output = (result.stdout or "").strip().splitlines()
    detail = output[-1] if output else (result.stderr or "")[-200:]
    print(f"  {label:<34} wall={wall:6.2f}s   {detail}")
    return wall


def main() -> int:
    workdir = REPO
    print("=== fresh-process startup, seconds ===\n")
    print("client construction only:")
    run("openai SDK", SDK_SNIPPET, workdir)
    run("direct httpx", DIRECT_SNIPPET, workdir)

    print("\nfull agent build (imports + client + tools + profile):")
    run("with openai SDK", AGENT_SNIPPET, workdir)

    print("\n(the direct-client variant of the agent build needs the wiring to be switched on;")
    print(" this run proves the import saving, which is the dominant term.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
