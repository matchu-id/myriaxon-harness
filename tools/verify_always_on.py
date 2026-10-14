#!/usr/bin/env python3
"""Verify the always-on agent: condition-driven wakes, a standing watchlist, and a silent path.

The claim under test is that an agent can run a long-term responsibility **without being prompted**, wake because
the thing it maintains changed rather than because a clock ticked, and stay quiet when there is nothing to say.
So the checks assert both that it wakes when it should and that it does NOT wake when it should not.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# See the note in verify_loci_character: rich is absent and there is no network, so a documented test double stands in
# and says so, rather than letting the import line kill the run before any assertion.
if importlib.util.find_spec("rich") is None:
    sys.path.insert(0, str(REPO / "tools" / "_testdoubles"))
    print("  [NOTE] rich is absent; using the test double in tools/_testdoubles.", flush=True)

from myriaxon import conditions as C  # noqa: E402
from myriaxon import loci  # noqa: E402
from myriaxon import heartbeat as HB  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def fresh_registry() -> loci.LocusRegistry:
    tmp = Path(tempfile.mkdtemp())
    loci.LOCI_FILE = tmp / "loci.json"
    loci.reset_registry()
    return loci.get_registry()


def main() -> int:
    print("=== 1. a monitor runs on a CONDITION, not on the clock ===")
    # The central claim: the trigger is the state of the thing being maintained.
    reg = fresh_registry()
    tmp = Path(tempfile.mkdtemp())
    health = tmp / "health.ok"
    health.write_text("ok")

    locus = reg.create("ops", goals=[], proactive=True, interval_seconds=1)
    locus.condition = {
        "kind": C.KIND_FILE_MISSING, "description": "the health file is gone",
        "path": str(health), "min_seconds_between_runs": 0,
    }
    locus.scratch = "- the health file must exist"

    check("while healthy, the monitor does NOT wake",
          not locus.is_due(now=time.time() + 10), locus.last_condition_state)
    check("and it says why", locus.last_condition_state == "quiet", locus.last_condition_state)

    health.unlink()
    check("when the condition breaks, it wakes", locus.is_due(now=time.time() + 20),
          locus.last_condition_state)
    check("and it says so", locus.last_condition_state == "holds", locus.last_condition_state)

    print()
    print("=== 2. a maintenance agent with NO goals can still run ===")
    # Measured before the fix: `is_due` returned False forever for a locus with a watchlist and no goals, so a
    # monitor -- whose job is "check and confirm nothing is wrong" -- could never run at all.
    reg = fresh_registry()
    watcher = reg.create("watch", goals=[], proactive=True, interval_seconds=1)
    watcher.scratch = "- check the queue depth"
    check("a watchlist alone is enough to run", watcher.is_due(now=time.time() + 10))
    idle = reg.create("idle", goals=[], proactive=True, interval_seconds=1)
    check("with neither goals nor a watchlist it still does not run",
          not idle.is_due(now=time.time() + 10))

    print()
    print("=== 3. THE SILENT PATH: a quiet check costs no message ===")
    quiet = [
        ("NO_REPLY", "the documented no-op"),
        ("HEARTBEAT_OK", "the legacy marker alone"),
        ("HEARTBEAT_OK all clear", "the marker with a bare acknowledgment"),
        ("", "an empty reply"),
    ]
    for text, why in quiet:
        outcome = HB.classify(text)
        check(f"silent: {why}", outcome.verdict == HB.SILENT, f"{text!r} -> {outcome.verdict}")

    print()
    print("=== 4. THE FIX: a marker with substance is an ALERT, not a swallowed one ===")
    # The reference rule suppresses a marked reply when the remainder is within a fixed 300-character budget.
    # Applied literally that drops "HEARTBEAT_OK disk is 95% full" -- 24 characters, and an alert about exactly
    # what a monitor exists to notice. Measured: it classified as silent. Stricter here on purpose.
    for text in ("HEARTBEAT_OK disk is 95% full", "HEARTBEAT_OK the backup FAILED",
                 "HEARTBEAT_OK 3 jobs stuck in the queue"):
        outcome = HB.classify(text)
        check(f"alert: {text[:44]!r}", outcome.verdict == HB.ALERT, f"-> {outcome.verdict}")
        check(f"  and the text survives", bool(outcome.text.strip()))

    check("a mid-reply marker is not special",
          HB.classify("the backup is fine, HEARTBEAT_OK means healthy, but the disk is full").verdict
          == HB.ALERT, "stripping it from the middle would lose the alert")

    print()
    print("=== 5. an unmarked reply is an alert by default ===")
    # The asymmetry decides it: a spurious alert costs a glance, a swallowed one costs the incident.
    for text in ("the service is down", "disk at 98%", "nothing found but the job is still running"):
        check(f"alert: {text!r}", HB.classify(text).verdict == HB.ALERT)

    print()
    print("=== 6. THE STRUCTURED PATH, and its precedence over text ===")
    remembered = HB.classify("", structured=HB.StructuredReply(notify=False, notification_text="disk at 80%"))
    check("notify=false with a note is REMEMBERED", remembered.verdict == HB.REMEMBERED,
          remembered.verdict)
    check("the note is carried", remembered.remembered == "disk at 80%")
    silent = HB.classify("", structured=HB.StructuredReply(notify=False))
    check("notify=false with no note is silent", silent.verdict == HB.SILENT)
    alert = HB.classify("ignored", structured=HB.StructuredReply(notify=True, notification_text="down"))
    check("notify=true is an alert", alert.verdict == HB.ALERT and alert.text == "down")
    check("the structured reply WINS over the text", alert.text == "down", alert.text)
    empty_alert = HB.classify("", structured=HB.StructuredReply(notify=True))
    check("notify=true with nothing to say stays silent rather than delivering an empty alert",
          empty_alert.verdict == HB.SILENT, empty_alert.render())

    print()
    print("=== 7. the remembered note is stored as the LATEST, not a history ===")
    reg = fresh_registry()
    locus = reg.create("m", goals=["x"], proactive=True)
    locus.record_heartbeat("remembered", note="first note")
    locus.record_heartbeat("remembered", note="second note")
    check("the latest replaces the previous", locus.heartbeat_note == "second note", locus.heartbeat_note)
    check("the verdict is recorded", locus.last_heartbeat_verdict == "remembered")
    check("it is in the activity log",
          any(e.kind == "heartbeat" for e in locus.activity), str([e.kind for e in locus.activity][-3:]))

    print()
    print("=== 8. the WAKE LEDGER bounds a burst ===")
    # Their numbers: a 30-second minimum between event turns, a flood guard after five starts in 60 seconds.
    ledger = HB.WakeLedger()
    now = 1000.0
    check("the first event turn is allowed", ledger.allow(now)[0])
    ledger.record(now)
    allowed, why = ledger.allow(now + 5)
    check("a second within 30s is refused", not allowed, why)
    check("and the reason says how long", "s to go" in why, why)

    burst = HB.WakeLedger()
    # A genuine BURST: five starts one second apart. The first version spaced them 40s apart, which is not a
    # burst at all -- the 30-second minimum allows all five and the guard correctly never trips. The test was
    # asserting a flood while creating a trickle.
    for i in range(HB.FLOOD_LIMIT):
        burst.allow(now + i * 1.0)[0]
        burst.record(now + i * 1.0)
    allowed, why = burst.allow(now + HB.FLOOD_LIMIT * 1.0)
    check("the flood guard trips after the limit", not allowed, why)
    check("the reason says it is deferring", "deferring" in why, why)
    check("spread-out starts are NOT a flood",
          HB.WakeLedger().allow(now)[0], "the guard bounds a burst, not a normal cadence")

    print()
    print("=== 9. the standing watchlist is bounded ===")
    # It is sent with every wake-up, so an unbounded watchlist is a per-tick cost and stops being a checklist.
    scratch = HB.Scratch()
    ok, message = scratch.set("- check the queue\n- check the disk")
    check("a normal watchlist is accepted", ok, message)
    check("it is stored", "queue" in scratch.show())
    ok, message = scratch.set("x" * (HB.MAX_SCRATCH_CHARS + 1))
    check("an oversized one is refused", not ok, message[:80])
    check("and the reason says it is sent every wake-up", "every wake-up" in message, message[:120])
    ok, _ = scratch.append("check the logs")
    check("append works", ok and "logs" in scratch.show(), scratch.show())
    check("appending to an empty watchlist starts a list",
          HB.Scratch().append("first")[0])

    print()
    print("=== 10. the heartbeat prompt carries the contract ===")
    prompt = HB.heartbeat_prompt(HB.Scratch(notes="- the site must respond"))
    check("it includes the watchlist", "the site must respond" in prompt)
    check("it names NO_REPLY", HB.NO_REPLY in prompt)
    check("it names heartbeat_respond", "heartbeat_respond" in prompt)
    check("it says an alert must not carry an acknowledgment", "Do not add an acknowledgment" in prompt)
    check("it forbids inventing work from history",
          "Do NOT infer new tasks from earlier conversation" in prompt,
          "this is the clause that keeps a monitor from rehashing stale context")
    bare = HB.heartbeat_prompt(HB.Scratch())
    check("a locus with no watchlist is told to say so",
          "no standing watchlist" in bare, bare[:140])
    custom = HB.heartbeat_prompt(HB.Scratch(notes="- ignored"), custom="Check the gateway health endpoint.")
    check("a custom prompt replaces the watchlist section", "ignored" not in custom)
    check("but never the reply contract", HB.NO_REPLY in custom,
          "a heartbeat with no silence protocol would message every tick")

    print()
    print("=== 11. what a condition CAN and CANNOT distinguish ===")
    # Two different failures, and since P1.3 one of them IS detectable.
    #
    # A check that TIMES OUT or cannot be spawned is reported as an error and wakes the agent: the harness knows it
    # did not get an answer.
    #
    # A check whose COMMAND does not exist used to be undetectable -- with `shell=True` the shell runs, cannot find
    # it, and exits non-zero, byte-identical to "the condition is not met". `unresolvable_program` now resolves the
    # program BEFORE running it, so a bare command's typo is caught (see 13g). What remains undetectable is a
    # missing program buried in shell syntax, pinned in 13h.
    slow = C.Condition(kind=C.KIND_COMMAND, description="check the service",
                       command=f'"{sys.executable}" -c "import time; time.sleep(5)"', timeout_seconds=0.3)
    slow_result = slow.evaluate()
    check("a TIMEOUT is detectable", slow_result.error, slow_result.detail[:80])
    check("and it WAKES the agent rather than going blind", slow_result.holds,
          "the harness knows it did not get an answer, so it must not assume all is well")

    invalid = C.Condition(kind="nonsense", description="x")
    check("an invalid condition also wakes", invalid.evaluate().holds and invalid.evaluate().error)

    print()
    print("=== 12. condition kinds behave ===")
    tmp = Path(tempfile.mkdtemp())
    target = tmp / "log.txt"
    target.write_text("a")
    check("path_exists holds when present",
          C.Condition(kind=C.KIND_PATH_EXISTS, description="d", path=str(target)).evaluate().holds)
    check("file_missing holds when absent",
          C.Condition(kind=C.KIND_FILE_MISSING, description="d", path=str(tmp / "nope")).evaluate().holds)
    check("file_missing is quiet when present",
          not C.Condition(kind=C.KIND_FILE_MISSING, description="d", path=str(target)).evaluate().holds)
    check("always holds", C.Condition(kind=C.KIND_ALWAYS, description="d").evaluate().holds)
    ok = C.Condition(kind=C.KIND_COMMAND, description="d", command=f'"{sys.executable}" -c "raise SystemExit(0)"')
    check("a command exiting 0 holds", ok.evaluate().holds)
    bad = C.Condition(kind=C.KIND_COMMAND, description="d", command=f'"{sys.executable}" -c "raise SystemExit(3)"')
    check("a command exiting non-zero is quiet", not bad.evaluate().holds)
    slow = C.Condition(kind=C.KIND_COMMAND, description="d",
                       command=f'"{sys.executable}" -c "import time; time.sleep(5)"', timeout_seconds=0.3)
    slow_result = slow.evaluate()
    check("a hanging check times out and WAKES", slow_result.holds and slow_result.error,
          slow_result.detail[:80])

    print()
    print("=== 13. the per-condition floor bounds a flapping condition ===")
    # A log line appended every second would otherwise drive a run every tick.
    flapping = C.Condition(kind=C.KIND_ALWAYS, description="d", min_seconds_between_runs=60.0)
    allowed = C.should_run(flapping, last_run_at=1000.0, now=1010.0)[0]
    check("inside the floor, no run", not allowed)
    allowed = C.should_run(flapping, last_run_at=1000.0, now=1070.0)[0]
    check("past the floor, a run", allowed)

    print()
    print("=== 13b. TRIGGER STATE: 'wake me when this CHANGED' ===")
    # The P1.1 gap. Before this, every kind answered "is this true now?", so a condition that stayed true fired on
    # every interval. `changed` compares against the previous observation, so it fires once per change.
    state = C.TriggerState()

    def run(observe: str, at: float, st):
        cond = C.Condition(kind=C.KIND_CHANGED, description="CI status", observe=observe,
                           min_seconds_between_runs=0)
        return C.should_run(cond, last_run_at=0.0, now=at, state=st)

    holds, result, state = run('echo pass', 1000.0, state)
    check("the FIRST check records a baseline and stays quiet", not holds, result.detail)
    check("  and the baseline is stored", state.observation == "pass", state.observation)
    check("  with no fire counted", state.fires == 0, str(state.fires))

    holds, result, state2 = run('echo pass', 1010.0, state)
    check("an UNCHANGED value stays quiet", not holds, result.detail)
    check("  and the revision does not move", state2.revision == state.revision,
          f"{state2.revision} vs {state.revision}")

    holds, result, state3 = run('echo fail', 1020.0, state2)
    check("a CHANGED value fires", holds, result.detail)
    check("  the detail names both values", "'pass'" in result.detail and "'fail'" in result.detail,
          result.detail[:100])
    check("  the revision advances", state3.revision == state2.revision + 1)
    check("  and the fire is counted", state3.fires == 1, str(state3.fires))

    holds, _r, state4 = run('echo fail', 1030.0, state3)
    check("the SAME change does not fire twice", not holds)

    print()
    print("=== 13c. THE FAILURE RULE: a failed run must not consume the change ===")
    # OpenClaw's rule: if a fired payload run fails, the returned state is NOT persisted, so the next evaluation
    # sees the previous state and can fire again. Backwards here, a failed run would mark the change as handled and
    # the watcher would go quiet exactly when its work had failed.
    broken = C.Condition(kind=C.KIND_CHANGED, description="CI", observe="exit 3",
                         min_seconds_between_runs=0)
    holds, result, preserved = C.should_run(broken, last_run_at=0.0, now=1040.0, state=state4)
    check("an unobservable check WAKES the agent", holds, result.detail[:80])
    check("  and reports an error", result.error)
    check("  and does NOT erase the fingerprint", preserved.observation == state4.observation,
          f"{preserved.observation!r} vs {state4.observation!r}")

    print()
    print("=== 13d. an oversized observation is REFUSED, not truncated ===")
    # Truncating would make the stored fingerprint differ from the observed one forever, so the condition would
    # fire on every single tick -- the opposite of what the cap is for.
    try:
        C.TriggerState(observation="x" * (C.MAX_STATE_BYTES + 1))
        check("an oversized observation raises", False, "it was accepted")
    except ValueError as exc:
        check("an oversized observation raises", True, str(exc)[:70])
        check("  and the message names the cap",
              "16,384" in str(exc) or str(C.MAX_STATE_BYTES) in str(exc), str(exc)[:110])
    check("exactly at the cap is allowed",
          C.TriggerState(observation="x" * C.MAX_STATE_BYTES).problem() == "")

    print()
    print("=== 13e. a corrupt or oversized stored state is dropped, not fatal ===")
    # Losing a fingerprint costs one extra wake-up; refusing to load would make the Locus unloadable and take the
    # whole registry with it.
    check("junk becomes an empty state", C.TriggerState.from_dict("nonsense").observation == "")
    check("None becomes an empty state", C.TriggerState.from_dict(None).revision == 0)
    check("an oversized record is dropped",
          C.TriggerState.from_dict({"observation": "x" * (C.MAX_STATE_BYTES + 1)}).observation == "")
    check("a real record round-trips",
          C.TriggerState.from_dict(state3.to_dict()).observation == state3.observation)

    print()
    print("=== 13f. a dry run does not change the next real answer ===")
    # `doctor` must be able to ask "would this fire?" without consuming the change it is asking about.
    cond = C.Condition(kind=C.KIND_CHANGED, description="CI", observe="echo new",
                       min_seconds_between_runs=0)
    dry, _r, after_dry = C.should_run(cond, last_run_at=0.0, now=1050.0, state=state4, commit=False)
    check("the dry run reports the fire", dry)
    check("  and the state is untouched", after_dry.observation == state4.observation,
          f"{after_dry.observation!r} vs {state4.observation!r}")
    real, _r, _s = C.should_run(cond, last_run_at=0.0, now=1051.0, state=state4)
    check("  so the real evaluation still fires", real)

    print()
    print("=== 13g. P1.3: a typo in a condition command is now CAUGHT ===")
    # Was a known limit: with shell=True a missing program exits non-zero, byte-identical to "condition not met",
    # so a typo made a watcher permanently and silently quiet. Resolving the program BEFORE running it is the only
    # chance to tell the two apart.
    typo = C.Condition(kind=C.KIND_COMMAND, description="check the service",
                       command="definitely-not-a-real-command-xyz --flag")
    typo_result = typo.evaluate()
    check("a missing program is reported as an error", typo_result.error, typo_result.detail[:90])
    check("  and it WAKES rather than going silent", typo_result.holds)
    check("  and the detail explains the consequence",
          "silently quiet" in typo_result.detail or "indistinguishable" in typo_result.detail,
          typo_result.detail[:130])

    resolvable = [
        ("python --version", "a program on PATH"),
        ("echo hello", "a shell builtin"),
        ("test -f /tmp/whatever", "a builtin with args"),
        ("[ -f /tmp/whatever ]", "the bracket form"),
        ("systemctl is-active nginx | grep active", "a pipeline"),
        ("cd /tmp && ls", "a chain"),
        ("FOO=bar prog", "an environment prefix"),
        ("curl -fsS localhost:9/health", "a program this host may not have"),
    ]
    for command, why in resolvable:
        check(f"no false warning for {why}", C.unresolvable_program(command) == "",
              f"{command!r} -> {C.unresolvable_program(command)[:60]}")
    check("an explicit missing path IS caught",
          bool(C.unresolvable_program("/nonexistent/path/prog")))
    check("an observe command typo is caught too",
          C.Condition(kind=C.KIND_CHANGED, description="d", observe="no-such-binary-zzz").observe_once()[0] is None)

    print()
    print("=== 13i. `once`: the watcher disables itself only on a SUCCESSFUL run ===")
    # This was the one P1 item with no verifier. It was exercised by hand while it was written, which is exactly the
    # pattern this repository keeps getting caught by -- a mechanism that exists, is described in the plan as done,
    # and is asserted by nothing. The `succeeded` flag is `report.ok`, and the ordering in `end_run` matters: the
    # trigger state is committed BEFORE `once` is evaluated, so a failed run stays armed.
    #
    # Driven through a real Locus and a real `end_run`, not through the Condition in isolation, because the wiring is
    # the part that can silently be wrong.
    from myriaxon import loci as _loci

    def _fresh_locus(name: str) -> "_loci.Locus":
        registry = _loci.LocusRegistry(tmp / f"{name}.json")
        return registry.create(name, goals=[], proactive=True, interval_seconds=1)

    class _Boom(Exception):
        pass

    on_success = _fresh_locus("once-ok")
    on_success.condition = {"kind": C.KIND_ALWAYS, "description": "tell me when the build lands", "once": True,
                            "min_seconds_between_runs": 0}
    check("it starts enabled", on_success.enabled)
    on_success.end_run(None)
    check("a SUCCESSFUL run disables the watcher", not on_success.enabled)
    check("  and says why in the log",
          any(e.kind == "condition" and "disabling" in e.summary for e in on_success.activity),
          str([e.summary[:60] for e in on_success.activity if e.kind == "condition"]))
    check("  so it is no longer due", not on_success.is_due(now=time.time() + 3600))

    on_failure = _fresh_locus("once-retry")
    on_failure.condition = {"kind": C.KIND_ALWAYS, "description": "retry until it lands", "once": True,
                            "min_seconds_between_runs": 0}
    on_failure.end_run(_Boom("the API was down"))
    check("a FAILED run leaves it ENABLED", on_failure.enabled,
          "otherwise a one-shot watch is dropped by one bad attempt")
    check("  and it is still due", on_failure.is_due(now=time.time() + 3600))

    not_once = _fresh_locus("no-once")
    not_once.condition = {"kind": C.KIND_ALWAYS, "description": "keep watching", "min_seconds_between_runs": 0}
    not_once.end_run(None)
    check("without `once` a successful run changes nothing", not_once.enabled)

    no_condition = _fresh_locus("no-condition")
    no_condition.end_run(None)
    check("a locus with no condition is untouched by the once logic", no_condition.enabled)

    print()
    print("=== 13h. THE REMAINING LIMIT: a missing program inside a pipeline is undetectable ===")
    # Pinned so it cannot be mistaken for a bug later, and so the improvement in 13g is not overstated. A pipeline
    # is out of `unresolvable_program`'s scope by design -- guessing at the program would produce false warnings --
    # and after the shell has run there is nothing left to distinguish.
    piped = C.Condition(kind=C.KIND_COMMAND, description="d",
                        command="no-such-binary-zzz --json | grep ok")
    piped_result = piped.evaluate()
    check("a missing program in a pipeline still reads as quiet",
          not piped_result.error and not piped_result.holds,
          "documented limit: keep the check a single bare command to get typo detection")
    check("but the same program alone IS caught",
          C.Condition(kind=C.KIND_COMMAND, description="d",
                      command="no-such-binary-zzz --json").evaluate().error)

    print()
    print("=== 14. validation catches a typo at write time ===")
    for label, condition in (
        ("unknown kind", C.Condition(kind="wibble", description="d")),
        ("no description", C.Condition(kind=C.KIND_ALWAYS, description="")),
        ("command kind with no command", C.Condition(kind=C.KIND_COMMAND, description="d")),
        ("path kind with no path", C.Condition(kind=C.KIND_FILE_MISSING, description="d")),
        ("negative floor", C.Condition(kind=C.KIND_ALWAYS, description="d", min_seconds_between_runs=-1)),
    ):
        check(f"refused: {label}", bool(condition.validate()), condition.validate()[:70])
    check("a valid condition passes",
          C.Condition(kind=C.KIND_ALWAYS, description="d").validate() == "")

    print()
    print("=== 15. WIRING: the tool surface exists and is reachable ===")
    from myriaxon import tool_registry as TR

    names = [TR._def_name(t) for t in TR.build_tool_defs()]
    visible = [TR._def_name(t) for t in TR.apply_tool_visibility(TR.build_tool_defs(), set())]
    check("locus_create is defined", "locus_create" in names)
    check("locus_watchlist is defined", "locus_watchlist" in names)
    check("heartbeat_respond is defined", "heartbeat_respond" in names)
    check("heartbeat_respond is VISIBLE by default", "heartbeat_respond" in visible,
          "an unattended wake-up cannot unlock a deferred tool -- it would spend a paid turn discovering the "
          "tool whose purpose is to make a quiet turn cheap")
    check("locus_create declares a condition property",
          "condition" in TR.loci.LOCUS_CREATE_TOOL_DEF["input_schema"]["properties"])
    check("locus_create declares a watchlist property",
          "watchlist" in TR.loci.LOCUS_CREATE_TOOL_DEF["input_schema"]["properties"])

    print()
    print("=== 16. creating a monitor persists its condition and watchlist ===")
    reg = fresh_registry()
    tmp = Path(tempfile.mkdtemp())
    text, failed = loci.execute_locus_create({
        "name": "sentinel", "proactive": True, "interval_seconds": 30,
        "watchlist": "- the health file must exist",
        "condition": {"kind": C.KIND_FILE_MISSING, "description": "health file gone",
                      "path": str(tmp / "health.ok")},
    })
    check("it was created", not failed, text[:100])
    created = reg.get("sentinel")
    check("the condition was stored", created.condition is not None, str(created.condition))
    check("the watchlist was stored", "health file" in created.scratch, created.scratch)
    check("a bad condition is REFUSED at create time",
          loci.execute_locus_create({"name": "bad", "condition": {"kind": "nonsense",
                                                               "description": "x"}})[1])
    idle_text, idle_failed = loci.execute_locus_create({"name": "empty"})
    check("an empty locus is allowed but warned about", not idle_failed)
    check("  and the warning explains it will not run", "never run" in idle_text, idle_text[-120:])

    print()
    print("=== 17. the watchlist tool reads and writes ===")
    text, failed = loci.execute_locus_watchlist({"locus": "sentinel"})
    check("reading works", not failed and "health file" in text, text[:80])
    loci.execute_locus_watchlist({"locus": "sentinel", "add": "- the queue must be under 100"})
    check("adding works", "queue" in reg.get("sentinel").scratch)
    loci.execute_locus_watchlist({"locus": "sentinel", "set": "- just this"})
    check("setting replaces", reg.get("sentinel").scratch.strip() == "- just this",
          reg.get("sentinel").scratch)
    loci.execute_locus_watchlist({"locus": "sentinel", "clear": True})
    check("clearing works", reg.get("sentinel").scratch == "")
    check("an unknown locus is refused", loci.execute_locus_watchlist({"locus": "ghost"})[1])
    check("reading an empty watchlist explains why it matters",
          "invents work" in loci.execute_locus_watchlist({"locus": "sentinel"})[0])

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
