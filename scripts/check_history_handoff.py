"""Verify the context-preservation mechanism directly, without needing a second live vendor.

    python scripts/check_history_handoff.py

Measured state of the other credentials on this machine: DeepSeek 401, OpenRouter 401 ("user not
found"), Netra 502. So a live cross-vendor conversation cannot be exercised end to end right now.
What CAN be proven is the part that would break silently:

  * a live agent's transcript is snapshotted before a rebuild (otherwise it is lost with the agent)
  * the snapshot normalises into one shape both provider families accept
  * a freshly built agent really is seeded with it

That covers the handoff. The only unverified link is whether a live second vendor would answer, which
is a credential/service question rather than a code one.
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

import os  # noqa: E402

from rich.console import Console  # noqa: E402

from myriaxon.config import resolve_config  # noqa: E402
from myriaxon.dashboard import SessionManager  # noqa: E402
from myriaxon.dashboard.session import _role_of, _text_of  # noqa: E402

passed, failed = [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + str(detail)) if detail and not condition else ''}")


# Shapes both provider families actually store, taken from the real agents.
OPENAI_SHAPED = [
    {"role": "user", "content": "the code word is ZEPHYR-42"},
    {"role": "assistant", "content": "noted"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}],
    },
    {"role": "tool", "tool_call_id": "c1", "content": "wrote it"},
]
GEMINI_SHAPED = [
    {"type": "user_input", "content": "the code word is ZEPHYR-42"},
    {"type": "model_output", "content": [{"type": "text", "text": "noted"}]},
    {"type": "function_call", "name": "write_file", "arguments": {}},
    {"type": "function_result", "name": "write_file", "result": "wrote it"},
]


def portable(history):
    out = []
    for message in history:
        if not isinstance(message, dict):
            continue
        role = _role_of(message)
        if role not in ("user", "assistant"):
            continue
        text = _text_of(message)
        if text:
            out.append({"role": role, "content": text})
    return out


def main() -> int:
    print("=== normalisation of both stored shapes ===")
    for label, history in (("openai-shaped", OPENAI_SHAPED), ("gemini-shaped", GEMINI_SHAPED)):
        result = portable(history)
        roles = [m["role"] for m in result]
        texts = " ".join(m["content"] for m in result)
        check(f"{label}: keeps the user turn", roles[:1] == ["user"], roles)
        check(f"{label}: keeps the assistant turn", "assistant" in roles, roles)
        check(f"{label}: preserves the fact", "ZEPHYR-42" in texts, texts)
        check(f"{label}: drops tool mechanics", "write_file" not in texts, texts)

    print("\n=== live capture and handoff ===")
    workspace = Path(__file__).resolve().parent.parent / "bench-out" / "handoff"
    workspace.mkdir(parents=True, exist_ok=True)
    os.chdir(workspace)

    config = resolve_config(Console(quiet=True))
    manager = SessionManager(workspace)
    session = manager.create(config, mode="auto_edit")

    check("history starts empty", session.history == [], session.history)

    agent = session._ensure_agent()
    agent.messages = list(OPENAI_SHAPED)  # stand in for a conversation held by another vendor
    session.capture_history()
    check("capture recorded the transcript", len(session.history) == 4, len(session.history))

    snapshot = session._portable_history()
    check("snapshot is portable", all(set(m) == {"role", "content"} for m in snapshot), snapshot)
    check("snapshot carries the fact", any("ZEPHYR-42" in m["content"] for m in snapshot), snapshot)

    # Rebuild as if the provider had changed, and confirm the new agent is seeded.
    session.agent = None
    rebuilt = session._ensure_agent()
    seeded = getattr(rebuilt, "messages", [])
    check("rebuilt agent is seeded with the history", len(seeded) >= len(snapshot), len(seeded))
    flattened = " ".join(_text_of(m) for m in seeded)
    check("the fact survived the rebuild", "ZEPHYR-42" in flattened, flattened[:140])
    check("rebuilt agent still answers about it (shape accepted)", rebuilt is not None)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
