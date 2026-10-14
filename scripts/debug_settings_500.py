"""Reproduce the /api/settings 500 in-process, with the traceback visible."""
import json
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from myriaxon.dotenv_support import load_dotenv_into_environ

load_dotenv_into_environ()

from rich.console import Console

from myriaxon.config import resolve_config
from myriaxon.dashboard.server import serve

PORT = 8799
sentinel: dict = {}


def main() -> int:
    workspace = Path(__file__).resolve().parent.parent / "bench-out" / "probe_settings_ws"
    workspace.mkdir(parents=True, exist_ok=True)
    config = resolve_config(Console(quiet=True))

    def run():
        try:
            serve(workspace, config=config, host="127.0.0.1", port=PORT, mode="auto_edit", open_browser=False)
        except BaseException:  # noqa: BLE001
            sentinel["traceback"] = traceback.format_exc()

    threading.Thread(target=run, daemon=True).start()
    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=3).read()
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)

    body = json.dumps({"effort": "not-a-level", "max_tokens": -12, "workflow": "quadruple", "theme": "neon"}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/settings", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=30)
        print("status:", response.status)
        print(response.read().decode()[:400])
        return 0
    except urllib.error.HTTPError as exc:
        print("HTTP", exc.code)
        print(exc.read().decode()[:600])
        # The handler logs at exception level through _error; surface anything the server captured.
        if sentinel.get("traceback"):
            print(sentinel["traceback"][-1500:])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
