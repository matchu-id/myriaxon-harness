"""What role do the agent's stored messages actually carry? (Gemini shape)"""
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

config = resolve_config(Console(quiet=True))
agent = _build_agent(
    config, config.model, Console(quiet=True), True, None, [], [], discover_skills(),
    discover_subagent_types(), load_hooks_config(), load_project_instructions(), Scheduler(),
    load_settings(), mode="auto_edit",
)
print("agent:", type(agent).__name__)
print("messages before:", len(agent.messages))
for index, message in enumerate(agent.messages):
    print(f"  {index}: type={type(message).__name__} keys={list(message.keys()) if isinstance(message, dict) else '-'} role={message.get('role') if isinstance(message, dict) else '-'!r}")
