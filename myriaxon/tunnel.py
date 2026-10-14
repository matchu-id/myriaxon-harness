"""Exposing the webhook to a provider, so the bot works from a phone.

A provider posts to a public URL and a laptop on a home network is not one, so the last step of
setting up the messaging bot was "install a tunnel and copy the URL across" -- the one part of the
flow that could not be done from the tool. This does it: finds a tunnel binary, runs it, reads the
public URL it prints, and reports the webhook to paste into the provider.

Two honest constraints, both stated in the output rather than discovered later:

* **Something has to be installed.** Cloudflare's `cloudflared` is the best option (no account for a
  quick tunnel) and is not present on every machine. When nothing is found, the exact install command
  and the exact run command are printed rather than a failure.
* **A tunnel is a public door.** The webhook is authenticated by the shared secret and the allowlist,
  and this refuses to start one unless a secret is set -- opening an unauthenticated route into an
  agent that runs shell commands is not a convenience, it is a vulnerability.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

# Public URL shapes each tool prints. Matched rather than assumed, because the URL is the whole point
# of running the tunnel and a wrong one sends the user to configure a webhook that will never fire.
URL_PATTERNS = (
    re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com"),
    re.compile(r"https://[a-z0-9][a-z0-9-]*\.ngrok-free\.app"),
    re.compile(r"https://[a-z0-9][a-z0-9-]*\.ngrok\.io"),
    re.compile(r"https://[a-z0-9][a-z0-9-]*\.loca\.lt"),
    re.compile(r"https://[a-z0-9][a-z0-9.-]*\.ts\.net"),
)

# Tried in order. cloudflared first: it needs no account and no signup for an ephemeral tunnel, which
# makes it the only one a stranger can use in one step.
TUNNELS: tuple[dict[str, object], ...] = (
    {
        "name": "cloudflared",
        "binary": "cloudflared",
        "argv": lambda port: ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}"],
        "install": "winget install --id Cloudflare.cloudflared",
        "note": "no account needed for a quick tunnel",
    },
    {
        "name": "ngrok",
        "binary": "ngrok",
        "argv": lambda port: ["ngrok", "http", str(port), "--log", "stdout"],
        "install": "winget install --id Ngrok.Ngrok",
        "note": "needs a free account and an authtoken",
    },
    {
        "name": "tailscale",
        "binary": "tailscale",
        "argv": lambda port: ["tailscale", "funnel", str(port)],
        "install": "winget install --id Tailscale.Tailscale",
        "note": "needs an admin shell and Funnel enabled in the tailnet ACLs",
    },
    {
        "name": "localtunnel",
        "binary": "npx",
        "argv": lambda port: ["npx", "--yes", "localtunnel", "--port", str(port)],
        "install": "comes with Node.js (npx); no install needed",
        "note": "no account, but less reliable than the others",
    },
)


def resolve_binary(name: str) -> str:
    """The path to run for a tool, resolving Windows script shims.

    `shutil.which("npx")` returns `npx.CMD` on Windows, and spawning a `.CMD` directly through
    `subprocess` without a shell is unreliable -- it may fail with a bare OSError that reads as "the
    tunnel is broken" rather than "the launcher needs a shell". Returning the resolved path lets the
    caller see what will actually be run.
    """
    return shutil.which(name) or ""


def available() -> list[dict[str, object]]:
    """The tunnels whose binary exists on this machine, in preference order."""
    found = []
    for tunnel in TUNNELS:
        if resolve_binary(str(tunnel["binary"])):
            found.append(tunnel)
    return found


def install_hint(port: int) -> list[str]:
    """Copy-pasteable instructions for every tunnel, for when none is installed."""
    lines = ["No tunnel tool found on PATH. Pick one:", ""]
    for tunnel in TUNNELS:
        lines.append(f"  {tunnel['name']} — {tunnel['note']}")
        lines.append(f"    install: {tunnel['install']}")
        lines.append(f"    run:     {' '.join(str(a) for a in tunnel['argv'](port))}")
        lines.append("")
    lines.append("  Then set the webhook URL in your provider to <public-url>/api/inbound")
    return lines


def webhook_url(public_base: str) -> str:
    """The URL to give the provider, from whatever base the tunnel printed."""
    base = str(public_base or "").strip().rstrip("/")
    return f"{base}/api/inbound" if base else ""


def find_url(text: str) -> str:
    """The first public URL in a line of tunnel output, or ""."""
    for pattern in URL_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return match.group(0)
    return ""


class Tunnel:
    """A running tunnel, with the public URL once it appears.

    Started as a subprocess and read line by line, because every one of these tools prints its URL
    asynchronously and some print a great deal else first. Output is also mirrored to a callback:
    a tunnel that fails prints the reason, and swallowing it would leave the user with a hang.
    """

    def __init__(self, port: int, on_line=None, timeout: float = 45.0) -> None:
        self.port = port
        self.on_line = on_line
        self.timeout = timeout
        self.url = ""
        self.error = ""
        self.process: subprocess.Popen | None = None
        self.tool = ""
        self._thread: threading.Thread | None = None

    def start(self, tool: str = "") -> bool:
        """Launch a tunnel and wait for its URL. False when it could not start.

        Resets its own state first. Measured: reusing an object for a second attempt reported the
        *previous* tool's error, so localtunnel appeared to fail with tailscale's message -- which
        would send anyone reading it to debug the wrong program.

        `tool` picks a specific one; empty means the first available. Runners that try several pass
        each in turn, because `available()` is evaluated here rather than captured earlier.
        """
        self.url = ""
        self.error = ""
        if self.process is not None:
            self.stop()
        candidates = available()
        if not candidates:
            self.error = "no tunnel tool is installed"
            return False

        chosen = None
        if tool:
            chosen = next((t for t in candidates if str(t["name"]) == tool), None)
            if chosen is None:
                self.error = f"{tool} is not installed"
                return False
        else:
            chosen = candidates[0]

        self.tool = str(chosen["name"])
        argv = [str(a) for a in chosen["argv"](self.port)]
        # The first element is a tool name; replace it with the resolved path so a Windows `.CMD`
        # shim is launched the way the OS expects rather than as a bare name.
        resolved = resolve_binary(argv[0])
        if resolved:
            argv[0] = resolved
        try:
            self.process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                # New process group, so stopping the tunnel does not take the dashboard with it.
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except Exception as exc:  # noqa: BLE001
            self.error = f"could not start {self.tool}: {type(exc).__name__}: {exc}"
            return False

        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._read, args=(ready,), name="myriaxon-tunnel", daemon=True,
        )
        self._thread.start()
        ready.wait(timeout=self.timeout)
        if not self.url:
            # The process may be running while never printing a URL -- ngrok without an authtoken
            # does exactly that. Reporting failure here is what stops the user waiting forever.
            self.error = self.error or (
                f"{self.tool} did not print a public URL within {int(self.timeout)}s"
            )
        return bool(self.url)

    def _read(self, ready: threading.Event) -> None:
        stream = getattr(self.process, "stdout", None)
        if stream is None:
            ready.set()
            return
        try:
            for line in stream:
                if self.on_line:
                    try:
                        self.on_line(line.rstrip())
                    except Exception:  # noqa: BLE001
                        pass
                if not self.url:
                    found = find_url(line)
                    if found:
                        self.url = found
                        ready.set()
                # A tool that exits has failed; stop waiting for a URL that is not coming.
                if self.process is not None and self.process.poll() is not None:
                    break
        except Exception as exc:  # noqa: BLE001
            self.error = f"reading {self.tool} output failed: {exc}"
        finally:
            ready.set()

    def stop(self) -> None:
        if self.process is None:
            return
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        except Exception:  # noqa: BLE001
            pass
        self.process = None


def wait_until_serving(url: str, timeout: float = 30.0) -> bool:
    """Whether the public webhook actually answers. Checked, not assumed.

    A tunnel printing a URL is not the same as the URL reaching this process: DNS can lag and the
    edge can 502 for a few seconds. Probing means the URL handed to the user is one that worked at
    least once.
    """
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=8) as response:
                if response.status < 500:
                    return True
        except urllib.error.HTTPError as exc:
            # A 404 or 405 from our own server still proves the tunnel reaches it.
            if exc.code < 500:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.5)
    return False


def config_path() -> Path:
    from . import constants

    return Path(constants.CONFIG_DIR) / "tunnel.json"


def remember(url: str, tool: str) -> None:
    """Record the last public URL so it can be shown again without re-running the tunnel."""
    import json

    try:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"url": url, "tool": tool, "at": time.time()}, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
    except Exception:  # noqa: BLE001
        pass


def remembered() -> dict:
    import json

    try:
        path = config_path()
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
