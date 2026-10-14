"""Does a dashboard turn actually perform filesystem work in the workspace?

    python scripts/verify_dashboard_turn.py

Runs one turn in a clean workspace and checks the filesystem afterwards. A turn that reports success
while creating nothing is the worst failure mode for a coding harness, so this asserts on the disk
rather than on what the agent said.
"""
import os
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

from rich.console import Console  # noqa: E402

from myriaxon.config import resolve_config  # noqa: E402
from myriaxon.dashboard import SessionManager  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
WORKSPACE = REPO / "bench-out" / "dashverify"


def main() -> int:
    workspace = WORKSPACE.resolve()
    if workspace.exists():
        for child in workspace.iterdir():
            if child.is_file():
                child.unlink()
    workspace.mkdir(parents=True, exist_ok=True)

    config = resolve_config(Console(quiet=True))
    print(f"provider={config.provider} model={config.model}")
    print(f"workspace={workspace}")

    # Mirror what the server does: the workspace becomes the process cwd, because the agent's file
    # tools resolve relative paths against cwd. Without this the harness reports success while
    # writing into whatever directory the process happened to start in.
    os.chdir(workspace)
    print(f"process cwd={Path.cwd()}\n")

    manager = SessionManager(workspace)
    session = manager.create(config, mode="auto_edit")
    session.submit("create verify_dash.txt containing the word verified")

    for _ in range(300):
        if not session.busy:
            break
        time.sleep(1)

    print(f"busy={session.busy} error={session.state.error} turns={session.state.turn_count}")
    print(f"usage={session.usage()}")
    print("\ntrajectory:")
    for event in session.trajectory():
        print(f"  {event['tool']:<12} err={event['is_error']} args={str(event['args'])[:70]}")
        if event["result"]:
            print(f"       result={event['result'][:110]!r}")

    print("\nmessages:")
    for message in session.messages():
        print(f"  [{message['role']}] {message['text'][:90]!r}")

    target = workspace / "verify_dash.txt"
    print()
    if target.exists():
        print(f"PASS: {target.name} exists with content {target.read_text().strip()!r}")
        return 0
    print(f"FAIL: {target.name} was NOT created in the workspace")
    print("  workspace listing:", [p.name for p in workspace.iterdir()])
    print("  repo listing for a stray file:", [p.name for p in REPO.glob("verify_dash*")])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
