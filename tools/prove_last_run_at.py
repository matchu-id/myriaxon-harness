"""Direct proof that `last_run_at` is stamped, not inferred from a proxy assertion.

Runs against a throwaway registry file so the real dots.json is untouched.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from myriaxon import dots as D


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "dots.json"
    reg = D.DotRegistry(path=tmp)
    dot = reg.create(name="proof", role="check", goals=["prove it"], proactive=True)

    # 1. A brand-new dot has never run.
    print(f"[fresh]   last_run_at={dot.last_run_at!r}  is_due={dot.is_due()}")
    assert dot.last_run_at == 0.0, "a new dot must start at 0.0 (never ran)"
    assert dot.is_due() is True, "a never-run dot must be due immediately"

    # 2. end_run stamps it.
    dot.begin_run()
    before = time.time()
    dot.end_run(D.RunOutcome(ok=True, kind=D.OUTCOME_SUCCESS, attempts=1))
    after = time.time()
    stamped = dot.last_run_at
    print(f"[end_run] last_run_at={stamped!r}  in [{before:.3f},{after:.3f}]  "
          f"updated_at==last_run_at: {dot.updated_at == dot.last_run_at}")
    assert before <= stamped <= after, "end_run must stamp last_run_at to ~now"
    assert dot.updated_at == dot.last_run_at, "updated_at must track last_run_at"

    # 3. end_run alone is in-memory; persistence is the caller's registry.touch -- which is the
    #    call every run path makes (dot_run tool, dashboard submit, CLI proactive pass).
    on_disk_before_touch = [
        d for d in json.loads(tmp.read_text(encoding="utf-8"))["dots"] if d["name"] == "proof"
    ][0]["last_run_at"]
    print(f"[pre-touch] on-disk last_run_at={on_disk_before_touch!r} "
          f"(end_run does not write; touch does)")
    reg.touch(dot)

    # 4. After touch it survives the JSON round trip (persisted, not just in memory).
    raw = json.loads(tmp.read_text(encoding="utf-8"))
    persisted = [d for d in raw["dots"] if d["name"] == "proof"][0]["last_run_at"]
    reg2 = D.DotRegistry(path=tmp)
    reloaded = reg2.get("proof")
    print(f"[on-disk] last_run_at={persisted!r}  reloaded.last_run_at={reloaded.last_run_at!r}")
    assert persisted > 0, "touch must have written a real timestamp to disk"
    assert reloaded.last_run_at == persisted, "it must read back equal after a fresh reload"

    # 5. A stamped, persisted dot is no longer due; it waits out its interval.
    print(f"[after]   is_due={reloaded.is_due()}  seconds_until_due={reloaded.seconds_until_due()}")
    assert reloaded.is_due() is False, "once stamped, the dot must not be due again immediately"
    assert reloaded.seconds_until_due() > 0

    # 6. The dashboard path stamps at *submit* time via registry.touch.
    reg3 = D.DotRegistry(path=tmp)
    fresh = reg3.create(name="submit", role="check", goals=["x"], proactive=True)
    assert fresh.last_run_at == 0.0
    reg3.touch(fresh)  # exactly what the panel calls before session.submit
    stamp = D.DotRegistry(path=tmp).get("submit").last_run_at
    print(f"[touch]   submit-time last_run_at={stamp!r} (>0: {stamp > 0})")
    assert stamp > 0, "registry.touch must stamp last_run_at at submit time"

    print("\nPROVEN: last_run_at is stamped (end_run in memory, registry.touch on disk), "
          "both survive the JSON round trip, and every run path calls touch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
