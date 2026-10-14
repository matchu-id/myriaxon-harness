"""Inspect how Gemini reports thinking, so the dashboard can display it.

Checks whether thought text arrives as streamed parts (with part.thought set) or only as a token
count in the final usage metadata. The dashboard can only show a live reasoning stream in the first
case; in the second it can only report how much the model thought.
"""
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from myriaxon.dotenv_support import load_dotenv_into_environ

load_dotenv_into_environ()

import google.genai as genai
from rich.console import Console

from myriaxon.config import resolve_config

config = resolve_config(Console(quiet=True))
client = genai.Client(vertexai=True, project=config.vertex_project, location=config.vertex_location)

print(f"model={config.model}\n")
thought_parts = 0
text_parts = 0
shown = 0
usage = None

for chunk in client.models.generate_content_stream(
    model=config.model, contents="What is 17*23? Think it through step by step, then answer."
):
    for candidate in getattr(chunk, "candidates", None) or []:
        for part in getattr(candidate.content, "parts", None) or []:
            is_thought = bool(getattr(part, "thought", False))
            text = getattr(part, "text", None) or ""
            if is_thought:
                thought_parts += 1
            elif text:
                text_parts += 1
            if shown < 8 and text:
                print(f"  part thought={is_thought} text={text[:70]!r}")
                shown += 1
    if getattr(chunk, "usage_metadata", None):
        usage = chunk.usage_metadata

print(f"\nthought parts: {thought_parts}   answer parts: {text_parts}")
if usage:
    print(
        "usage: thoughts=", getattr(usage, "thoughts_token_count", None),
        " candidates=", getattr(usage, "candidates_token_count", None),
        " prompt=", getattr(usage, "prompt_token_count", None),
    )
print()
if thought_parts:
    print("=> thinking is streamed as parts; the dashboard can show it live")
else:
    print("=> no streamed thought parts; only a token count is available, so the UI shows progress")
