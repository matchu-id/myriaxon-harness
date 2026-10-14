"""End-to-end smoke test for the rebranded dashboard: real server, real HTTP, real checks.

Why a temporary in-repo script rather than the existing check suite: the suite's own
runners (`scripts/run_dashboard_checks.py`) start the server in a thread but expect a
configured provider and write into the real user config directory. This one is hermetic --
it points ``MYRIAXON_CONFIG_DIR`` at a scratch directory, seeds a provider so the setup
wizard cannot fire, starts the actual server on an ephemeral port, and asserts over real
HTTP. It is written to be deleted after the rebrand verification pass.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="myriaxon-smoke-"))
os.environ["MYRIAXON_CONFIG_DIR"] = str(SCRATCH)
os.environ["MYRIAXON_API_KEY"] = "sk-ant-smoke-not-a-real-key"
os.environ["MYRIAXON_PROVIDER"] = "anthropic"
os.environ["MYRIAXON_MODEL"] = "claude-opus-5"

# Seed config after the env vars, so both paths are exercised.
(SCRATCH / "config.json").write_text(
    json.dumps(
        {
            "provider": "anthropic",
            "api_key": "sk-ant-smoke-not-a-real-key",
            "base_url": "https://api.anthropic.com",
            "model": "claude-opus-5",
        }
    ),
    encoding="utf-8",
)

WORKSPACE = Path(__file__).resolve().parent.parent
# Run the repository's own source rather than whatever is installed, so this verifies the tree
# it sits in. Needed because the script is invoked by path, which puts tools/ (not the repo
# root) on sys.path.
sys.path.insert(0, str(WORKSPACE))
PORT = 8794

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, bool(ok), detail))


def get(path: str, timeout: float = 15.0):
    url = f"http://127.0.0.1:{PORT}{path}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def main() -> int:
    from myriaxon.config import resolve_config
    from myriaxon.constants import CONFIG_DIR
    from myriaxon.dashboard.server import serve
    from myriaxon.theme import get_console

    check("config dir honours the override", str(CONFIG_DIR).startswith(str(SCRATCH)), str(CONFIG_DIR))

    cfg = resolve_config(get_console(quiet=True))
    check("provider resolved without a wizard", bool(cfg.api_key), cfg.provider)

    thread = threading.Thread(
        target=lambda: serve(
            WORKSPACE, config=cfg, host="127.0.0.1", port=PORT, mode="auto_edit", open_browser=False
        ),
        daemon=True,
    )
    thread.start()

    # Wait for the server to accept connections rather than sleeping a fixed amount.
    deadline = time.time() + 40
    up = False
    while time.time() < deadline:
        try:
            get("/api/health", timeout=3)
            up = True
            break
        except Exception:
            time.sleep(0.4)
    check("server accepts connections", up, f"port {PORT}")
    if not up:
        for label, ok, detail in results:
            print(f"  {'PASS' if ok else 'FAIL'}  {label}  {detail}")
        return 1

    # ---- /api/health
    status, body = get("/api/health")
    health = json.loads(body)
    check("/api/health ok", status == 200 and health.get("ok") is True, str(health))

    # ---- / (the page itself)
    status, body = get("/")
    html = body.decode("utf-8", "replace")
    check("GET / serves 200", status == 200)
    check("page is Myriaxon-titled", "<title>Myriaxon</title>" in html)
    check("favicon is a data URI (cannot 404)", 'rel="icon"' in html and "data:image/svg+xml" in html)
    check("mark template present", 'id="marktemplate"' in html)
    check("mark is the compass, not the old logogram", 'class="mark"' in html and 'viewBox="0 0 200 200"' in html)
    check("old logogram colours are gone", "#ec0f8c" not in html and "#e8232a" not in html)
    check("signal token present", "#7CF5C8" in html)
    check("ink token present", "#0A0C11" in html)
    check("header carries the wordmark", 'class="headmark"' in html)
    check("pulse keyframe renamed", "@keyframes myriaxon-pulse" in html)
    check("page makes no external requests", "fonts.googleapis" not in html and "cdn." not in html)
    check("legacy CSS aliases resolved", "--mono:" in html and "--accent:" in html)

    # Every id the inline script looks up must exist in the markup: a dangling $("id") throws at
    # runtime and the page half-renders, which is the failure mode the visual check exists for.
    import re

    ids = set(re.findall(r'id="([^"]+)"', html))
    wanted = set(re.findall(r'\$\("([^"]+)"\)', html)) - {"marktemplate", "buffering-msg"}
    check("no dangling $() id references", not (wanted - ids), str(sorted(wanted - ids)))

    # ---- /api/config
    status, body = get("/api/config")
    cfg = json.loads(body)
    check("/api/config reports a provider", bool(cfg.get("provider")), str(cfg.get("provider")))
    check(
        "/api/config reports all four modes",
        set(cfg.get("modes", [])) == {"manual", "auto_edit", "plan", "auto"},
        str(cfg.get("modes")),
    )
    check("/api/config reports models", len(cfg.get("models") or []) >= 1, str(cfg.get("models"))[:80])
    check("/api/config reports vendors", len(cfg.get("providers") or []) >= 4, str(len(cfg.get("providers") or [])))
    check("workspace is the requested one", Path(cfg.get("workspace", "")).name == WORKSPACE.name, str(cfg.get("workspace")))
    check("api key is never sent to the browser", "api_key" not in json.dumps(cfg) or cfg.get("has_api_key") is True)

    # ---- sessions
    status, body = get("/api/sessions")
    sessions = json.loads(body)
    check("/api/sessions responds", isinstance(sessions, (list, dict)), type(sessions).__name__)

    # ---- files API (the workspace-confinement boundary)
    status, body = get("/api/files?path=.")
    listing = json.loads(body)
    check("/api/files lists the workspace", not listing.get("error"), str(listing.get("error", ""))[:80])

    try:
        get("/api/file?path=../../../etc/hosts")
        escaped = True
    except urllib.error.HTTPError as exc:
        escaped = exc.code not in (403, 404, 500)
    except Exception:
        escaped = False
    check("path traversal is refused", not escaped)

    # ---- cleanup
    for label, ok, detail in results:
        suffix = f"  — {detail}" if detail and not ok else ""
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{suffix}")
    failed = [r for r in results if not r[1]]
    print()
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
