"""List every src/href in the served page, to prove it loads nothing from the network."""
import re
import sys
import threading
import time
import urllib.request
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from rich.console import Console

from myriaxon.config import resolve_config
from myriaxon.dashboard.server import serve

workspace = Path(__file__).resolve().parent.parent / "bench-out" / "refscan"
workspace.mkdir(parents=True, exist_ok=True)
PORT = 8807

config = resolve_config(Console(quiet=True))
threading.Thread(
    target=lambda: serve(workspace, config=config, host="127.0.0.1", port=PORT, mode="auto_edit", open_browser=False),
    daemon=True,
).start()
for _ in range(60):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2).read()
        break
    except Exception:  # noqa: BLE001
        time.sleep(0.5)

html = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=20).read().decode("utf-8")
pattern = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+)["']""")
refs = pattern.findall(html)
print(f"{len(refs)} src/href references:")
for ref in refs:
    kind = "LOCAL " if ref.startswith(("data:", "#", "javascript:")) else "NETWORK"
    print(f"  [{kind}] {ref[:100]}")

network = [r for r in refs if not r.startswith(("data:", "#", "javascript:"))]
print()
print("network references:", len(network), network)
