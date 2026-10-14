"""cProfile the agent build and summarize the hot functions."""
import cProfile
import io
import os
import pstats
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from myriaxon.dotenv_support import load_dotenv_into_environ

load_dotenv_into_environ()

from rich.console import Console

from myriaxon.cli import _build_agent
from myriaxon.config import resolve_config
from myriaxon.hooks import load_hooks_config
from myriaxon.project_instructions import load_project_instructions
from myriaxon.scheduler import Scheduler
from myriaxon.settings import load_settings
from myriaxon.skills import discover_skills
from myriaxon.subagents import discover_subagent_types

workspace = Path(__file__).resolve().parent.parent / "bench-out" / "cprof"
workspace.mkdir(parents=True, exist_ok=True)
os.chdir(workspace)

import google.genai  # noqa: F401  -- warm the import so only build work is profiled

config = resolve_config(Console(quiet=True))
settings = load_settings()
skills = discover_skills()
agents = discover_subagent_types()
hooks = load_hooks_config()
instructions = load_project_instructions()

profiler = cProfile.Profile()
profiler.enable()
agent = _build_agent(
    config, config.model, Console(quiet=True), True, None, [], [], skills, agents, hooks,
    instructions, Scheduler(), settings, mode="auto_edit",
)
profiler.disable()

stream = io.StringIO()
stats = pstats.Stats(profiler, stream=stream).sort_stats("cumulative")
stats.print_stats(22)
output = stream.getvalue()
print("\n".join(output.splitlines()[:40]))
