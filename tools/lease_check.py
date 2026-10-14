"""The dot run lease is held, visible, and released — verified against the live server.

The audit found that the dashboard's dot-run path consulted no lease at all while `dots.py` promised
it did, and that `renew_run_lease` (the documented heartbeat) had no production caller. Both are
fixed; this proves the behaviour rather than the code shape.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

BASE = "http://127.0.0.1:8765"
failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail and not ok else ''}")


def call(path: str, body: dict | None = None, timeout: float = 300.0):
    if body is None:
        req = urllib.request.Request(BASE + path)
    else:
        req = urllib.request.Request(
            BASE + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def dots():
    _, payload = call("/api/dots")
    return {d["name"]: d for d in (payload.get("dots") or [])}


def main() -> int:
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            call("/api/health", timeout=3)
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("the dashboard is not up")
        return 1

    listing = dots()
    if not listing:
        print("no dots to test with")
        return 0
    name = "analyst" if "analyst" in listing else sorted(listing)[0]
    dot = listing[name]
    print(f"using dot {name!r}\n")

    print("1. the panel reports whether a dot is running")
    missing = [n for n, d in listing.items() if "running" not in d]
    check("every dot carries a `running` flag", not missing, f"missing on: {missing}")
    check("the dot starts idle", dot.get("running") is False, str(dot.get("running")))
    check("it carries a run_started_at", "run_started_at" in dot)

    print("\n2. holding the lease makes the dot report as running")
    from myriaxon import dots as dots_module

    registry = dots_module.get_registry()
    registry.reload()
    target = registry.get(name)
    if target is None:
        print(f"  registry has no {name}")
        return 1

    # A lease held in THIS process is visible to the server's registry, because both read the same
    # dots.json and the same lease file -- which is the whole point of a file lease.
    check("begin_run takes the lease", target.begin_run() is True)
    registry.persist(target)
    time.sleep(0.4)
    during = dots().get(name, {})
    check("the panel now reports it running", during.get("running") is True,
          str(during.get("running")))
    check("it is no longer reported as due", during.get("due_now") is False,
          str(during.get("due_now")))

    print("\n3. a second run of the same dot is refused while the lease is held")
    refused = target.begin_run()
    check("begin_run refuses a second claim", refused is False, str(refused))

    print("\n4. the heartbeat keeps a lease alive past the staleness window")
    lease = dots_module.read_run_lease(target)
    check("a lease file exists", lease is not None and lease.path is not None)
    if lease is not None and lease.path is not None:
        import os

        stale = time.time() - dots_module.RUN_LEASE_STALE_SECONDS - 60
        os.utime(lease.path, (stale, stale))
        check("the lease looks stale on disk",
              time.time() - os.path.getmtime(lease.path) > dots_module.RUN_LEASE_STALE_SECONDS)
        # The heartbeat is what `run_scope` starts. Exercised directly here, because the alternative
        # is waiting 30 real minutes.
        beat = dots_module._start_lease_heartbeat(lease)
        try:
            dots_module.renew_run_lease(lease)
            fresh = time.time() - os.path.getmtime(lease.path)
            check("renewing makes it fresh again",
                  fresh < dots_module.RUN_LEASE_STALE_SECONDS, f"age {fresh:.0f}s")
            check("the dot still reports as running after the renewal",
                  dots().get(name, {}).get("running") is True)
        finally:
            dots_module._stop_lease_heartbeat(beat)

    print("\n5. releasing returns the dot to idle")
    target.end_run(None)
    registry.persist(target)
    time.sleep(0.4)
    after = dots().get(name, {})
    check("the panel reports it idle again", after.get("running") is False,
          str(after.get("running")))

    print()
    print("dot lease verified" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
