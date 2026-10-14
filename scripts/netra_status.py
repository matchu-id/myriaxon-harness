"""Is Netra up right now?

    python scripts/netra_status.py

Six plain requests, two models. Run this before blaming the harness: the gateway served 502s to
every request (including raw httpx with a bare payload) for a stretch on 2026-09-29, which looks
exactly like a broken CLI from the inside.
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

MODELS = ["deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4-flash-0731"]


def main() -> int:
    import httpx

    key = os.environ.get("NETRA_API_KEY")
    if not key:
        print("NETRA_API_KEY is not set (add it to .env)")
        return 2

    healthy = []
    for model in MODELS:
        results = []
        for _ in range(3):
            start = time.perf_counter()
            try:
                with httpx.stream(
                    "POST",
                    "https://api.netraruntime.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": "say pong"}], "max_tokens": 50},
                    timeout=60.0,
                ) as response:
                    response.read()
                    results.append(f"{response.status_code} {time.perf_counter() - start:.1f}s")
            except Exception as exc:  # noqa: BLE001
                results.append(f"{type(exc).__name__}")
        status = "UP  " if all(r.startswith("200") for r in results) else "DOWN"
        if status == "UP  ":
            healthy.append(model)
        print(f"  [{status}] {model:<34} {results}")

    print()
    if healthy:
        print(f"Netra is serving: {', '.join(healthy)}")
        print("Switch to it with:  python scripts\\\\set_provider.py netra")
    else:
        print("Netra is NOT serving requests right now -- this is the provider, not Myriaxon.")
        print("A 502 error page in ~0.4s means the gateway cannot route to a backend.")
        print("Use Gemini in the meantime:  python scripts\\\\set_provider.py gemini")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
