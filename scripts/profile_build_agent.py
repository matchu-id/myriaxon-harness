"""Break down the 12s agent build: which sub-step costs what, and can it be deferred?

    python scripts/profile_build_agent.py
"""
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from myriaxon.dotenv_support import load_dotenv_into_environ  # noqa: E402

load_dotenv_into_environ()

import os  # noqa: E402

MARK = time.perf_counter


def main() -> int:
    from rich.console import Console

    from myriaxon.config import resolve_config

    workspace = Path(__file__).resolve().parent.parent / "bench-out" / "buildprof"
    workspace.mkdir(parents=True, exist_ok=True)
    os.chdir(workspace)

    t0 = MARK()
    config = resolve_config(Console(quiet=True))
    print(f"resolve_config                 {MARK() - t0:6.2f}s")

    t = MARK()
    from myriaxon.skills import discover_skills

    skills = discover_skills()
    print(f"discover_skills                {MARK() - t:6.2f}s")

    t = MARK()
    from myriaxon.subagents import discover_subagent_types

    agents = discover_subagent_types()
    print(f"discover_subagent_types        {MARK() - t:6.2f}s")

    t = MARK()
    from myriaxon import tool_registry

    tools = tool_registry.build_tool_defs_anthropic() if False else None
    print(f"import tool_registry           {MARK() - t:6.2f}s")

    t = MARK()
    openai_tools = tool_registry.build_tool_defs_openai()
    print(f"build openai tool defs         {MARK() - t:6.2f}s  ({len(openai_tools)} tools)")

    t = MARK()
    gemini_tools = tool_registry.build_tool_defs_gemini()
    print(f"build gemini tool defs         {MARK() - t:6.2f}s  ({len(gemini_tools)} tools)")

    t = MARK()
    from myriaxon.model_profiles import detect_profile

    profile = detect_profile(config.model)
    print(f"detect_profile                 {MARK() - t:6.2f}s")

    t = MARK()
    import google.genai  # noqa: F401

    print(f"import google.genai            {MARK() - t:6.2f}s")

    t = MARK()
    from myriaxon.cli import _build_agent
    from myriaxon.hooks import load_hooks_config
    from myriaxon.project_instructions import load_project_instructions
    from myriaxon.scheduler import Scheduler
    from myriaxon.settings import load_settings

    print(f"import cli + helpers           {MARK() - t:6.2f}s")

    t = MARK()
    agent = _build_agent(
        config, config.model, Console(quiet=True), True, None, [], [], skills, agents,
        load_hooks_config(), load_project_instructions(), Scheduler(), load_settings(),
        mode="auto_edit",
    )
    print(f"_build_agent (SDK warm)        {MARK() - t:6.2f}s")
    print(f"total from process start       {MARK() - t0:6.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
