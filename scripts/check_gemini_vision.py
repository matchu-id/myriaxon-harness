"""Does a Gemini turn actually see an attached image?

    python scripts/check_gemini_vision.py <image-path> ["question"]

Asks the model something it can only answer by looking at the picture, so a text-only fallback fails
loudly instead of producing a plausible-sounding answer.
"""
import sys
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


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: check_gemini_vision.py <image-path> [question]")
        return 2
    image = Path(sys.argv[1])
    question = sys.argv[2] if len(sys.argv) > 2 else (
        "Describe exactly what you see in this image, including any text or numbers, in one or two sentences."
    )
    if not image.is_file():
        print(f"not a file: {image}")
        return 2

    from rich.console import Console

    from myriaxon.cli import _build_agent
    from myriaxon.config import resolve_config
    from myriaxon.hooks import load_hooks_config
    from myriaxon.project_instructions import load_project_instructions
    from myriaxon.scheduler import Scheduler
    from myriaxon.settings import load_settings
    from myriaxon.skills import discover_skills
    from myriaxon.subagents import discover_subagent_types

    console = Console(quiet=True)
    config = resolve_config(console)
    print(f"provider={config.provider} model={config.model}")
    print(f"image={image} ({image.stat().st_size:,} bytes)")
    print(f"question={question!r}\n")

    agent = _build_agent(
        config, config.model, console, True, None, [], [], discover_skills(),
        discover_subagent_types(), load_hooks_config(), load_project_instructions(), Scheduler(),
        load_settings(), mode="auto_edit",
    )
    agent.run_turn(f'@"{image}"\n\n{question}')

    answer = ""
    try:
        answer = agent.last_assistant_text()
    except Exception:  # noqa: BLE001
        pass
    print(f"\nanswer:\n{answer}\n")

    # The stored user message must carry the attachment, or the image never reached the model.
    stored = [m for m in agent.messages if m.get("type") == "user_input"]
    carried = sum(len(m.get("attachments") or []) for m in stored)
    print(f"attachment blocks stored on user messages: {carried}")

    if not answer.strip():
        print("FAIL: no answer")
        return 1
    lowered = answer.lower()
    if any(phrase in lowered for phrase in ("don't see", "do not see", "no image", "unable to view", "can't see", "cannot see")):
        print("FAIL: the model reports it cannot see an image")
        return 1
    print("PASS: the model produced an answer that describes the image")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
