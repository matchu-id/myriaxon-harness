"""Dump the exact shape of a Gemini agent's stored messages after one turn."""
import json
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

workspace = Path(__file__).resolve().parent.parent / "bench-out" / "rolecheck"
workspace.mkdir(parents=True, exist_ok=True)
import os

os.chdir(workspace)

config = resolve_config(Console(quiet=True))
agent = _build_agent(
    config, config.model, Console(quiet=True), True, None, [], [], discover_skills(),
    discover_subagent_types(), load_hooks_config(), load_project_instructions(), Scheduler(),
    load_settings(), mode="auto_edit",
)
agent.run_turn("say the single word pong")

print(f"messages: {len(agent.messages)}")
for index, message in enumerate(agent.messages):
    print(f"\n--- [{index}] {type(message).__name__} ---")
    if isinstance(message, dict):
        for key, value in message.items():
            if key == "content" and isinstance(value, list):
                print(f"  content: list len={len(value)}")
                for block in value:
                    print(f"    block type={type(block).__name__} {str(block)[:90]}")
            else:
                print(f"  {key}: {str(value)[:100]}")
    else:
        print("  repr:", str(message)[:200])
