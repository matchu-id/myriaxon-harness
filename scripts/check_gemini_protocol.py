"""Show which Google protocol Myriaxon will use for each Gemini model, and why.

    python scripts/check_gemini_protocol.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from myriaxon.gemini_agent import _resolve_protocol  # noqa: E402

MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
]


def main() -> int:
    print(f"{'model':<24} {'auto':<14} note")
    for model in MODELS:
        protocol = _resolve_protocol(model, "auto")
        note = (
            "generateContent: measured 2.5s streaming on Vertex; the Interactions path returned 500"
            " and hung 252s, so it is opt-in only"
        )
        print(f"{model:<24} {protocol:<14} {note}")
    print()
    print("Override with --gemini-protocol generate|interactions or MYRIAXON_GEMINI_PROTOCOL.")
    print("A wrong choice is not fatal: a turn that gets 'Unsupported model interaction' retries once")
    print("on the other surface automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
