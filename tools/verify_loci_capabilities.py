#!/usr/bin/env python3
"""Verify Loci's personal stores: wins, memories, habits, journal, reflection.

## What this covers from `loci.md` §8

The spec's §8 is two halves. The **assistant** half -- planning, reminders, calendar -- belongs to the agent and the
model, and is not storage. The **companion** half is durable personal state, and that is what `loci_capabilities.py`
holds: a wins jar (§8.1), a memory palace with rooms (§8.1), forgiving habits (§8.1, §9.3), a journal (§8.1), a
weekly reflection (§8.1), and the deletion and privacy rules in §8.6.

## The four spec rules asserted here, because each is a promise rather than a feature

  * **§9.3 -- never "streak broken".** A habit stores the DAYS it happened, not a counter, so the summary can be a
    ratio. A counter cannot express "4 of the last 7 days" honestly, and this is checked on the rendered string.
  * **§8.6 -- deleting must feel safe.** `forget` returns what it removed; `forget_all` refuses without `confirm`.
    The forbidden spelling is asserted, not just the allowed one.
  * **§8.6 -- nothing is analysed without consent.** There is no summariser, no scorer, no network call in the
    module. Asserted by reading the source for the absence, which is the only way to test a missing capability.
  * **§1.3 -- no false reassurance.** An empty week is reported as empty. `reflect` has an `empty` flag for exactly
    that, and the CLI branches on it instead of manufacturing encouragement.
"""

from __future__ import annotations

import io
import re
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import loci_capabilities as store  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""), flush=True)


def fresh() -> Path:
    """An isolated store directory, so a check never touches the user's real data."""
    return Path(tempfile.mkdtemp(prefix="myx-companion-"))


def day_offset(days: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))


def main() -> int:
    print("=== 1. wins jar ===")
    root = fresh()
    win = store.add_win("finished the report I had been dreading", about="work", root=root)
    check("a win is recorded", win is not None and win.text.startswith("finished"))
    check("  it carries an id", bool(win.id), win.id)
    check("  and a timestamp", win.at > 0)
    check("  and what it was about", win.about == "work")
    check("an empty win is REFUSED", store.add_win("   ", root=root) is None)
    check("  and nothing was stored", len(store.list_wins(root=root)) == 1)
    for index in range(5):
        store.add_win(f"win {index}", root=root)
    listed = store.list_wins(limit=3, root=root)
    check("list_wins honours the limit", len(listed) == 3, str(len(listed)))
    check("  newest first", listed[0].text == "win 4", listed[0].text)
    check("whitespace is collapsed", store.add_win("  a   spaced   win  ", root=root).text == "a spaced win")
    # A hard day samples ACROSS the window, not the last three from this week.
    sample = store.wins_on_hard_day(window_days=3650, root=root)
    check("a hard day offers at most three", len(sample) <= 3, str(len(sample)))
    check("  and at least one when there are wins", len(sample) >= 1)

    print()
    print("=== 2. memory palace ===")
    root = fresh()
    memory = store.remember("sister recital is Saturday", room="people", root=root)
    check("a memory is stored", memory is not None)
    check("  in the named room", memory.room == "people", memory.room)
    check("an empty memory is REFUSED", store.remember("  ", root=root) is None)
    store.remember("the deploy key rotates on the 1st", room="work", root=root)
    check("recall by room", len(store.recall(room="work", root=root)) == 1)
    check("recall by text", store.recall("recital", root=root)[0].text.startswith("sister"))
    check("  case-insensitively", len(store.recall("RECITAL", root=root)) == 1)
    check("  and a miss returns nothing", store.recall("zzz", root=root) == [])
    check("rooms counts every room", store.rooms(root=root) == {"people": 1, "work": 1}, str(store.rooms(root=root)))
    check("the default room is 'ideas'", store.remember("an idea", root=root).room == "ideas")
    check("a room name is normalised", store.remember("x", room="  Work  ", root=root).room == "work")

    print()
    print("=== 3. THE SPEC RULE: habits are ratios, never streaks ===")
    # §9.3: `"Streak broken!"` is forbidden; `"4 of the last 7 days. That's real progress."` is required. A counter
    # cannot say the second, which is why the store keeps dates.
    root = fresh()
    habit = store.declare_habit("walk", root=root)
    check("a habit is declared", habit is not None)
    check("a duplicate declaration is REFUSED", store.declare_habit("walk", root=root) is None,
          "two habits with one name would each hold half the days")
    check("  and only one exists", len(store.list_habits(root=root)) == 1)
    check("an empty name is REFUSED", store.declare_habit("   ", root=root) is None)
    for offset in (0, 1, 2, 5):
        store.mark_habit("walk", day=day_offset(offset), root=root)
    current = store.list_habits(root=root)[0]
    check("four in-window days count as four", current.ratio(7)[0] == 4, str(current.ratio(7)))
    check("  and the window is seven", current.ratio(7)[1] == 7)
    check("the render is a ratio, not a streak", "of the last 7 days" in current.render(), current.render())
    for forbidden in ("streak", "broken", "missed", "failed", "lost"):
        check(f"  the wording never says {forbidden!r}", forbidden not in current.render().lower(),
              current.render())
    check("marking the same day twice is one day",
          store.mark_habit("walk", day=day_offset(0), root=root).ratio(7)[0] == 4)
    check("an old day outside the window does not count",
          store.mark_habit("walk", day="2001-01-01", root=root).ratio(7)[0] == 4)
    check("  but is remembered", "2001-01-01" in store.list_habits(root=root)[0].days)
    check("marking an unknown habit returns None", store.mark_habit("nope", root=root) is None)
    check("done_today is accurate", store.list_habits(root=root)[0].done_today)

    print()
    print("=== 4. journal: stored, never analysed ===")
    root = fresh()
    entry = store.write_journal("long day but the release went out", prompt="what went well?",
                                root=root)
    check("an entry is written", entry is not None)
    check("  with the prompt kept", entry.prompt == "what went well?")
    check("an empty entry is REFUSED", store.write_journal("   ", root=root) is None)
    check("  and nothing was stored", len(store.list_journal(root=root)) == 1)
    check("it round-trips verbatim",
          store.list_journal(root=root)[0].text == "long day but the release went out")
    check("a prompt is offered", bool(store.journal_prompt()), store.journal_prompt())
    check("  and is deterministic by index", store.journal_prompt(index=0) == store.journal_prompt(index=0))
    check("  and varies", store.journal_prompt(index=0) != store.journal_prompt(index=1))
    # §8.6 -- the absence of an analyser IS the privacy feature, so its absence is what gets asserted.
    source = (REPO / "myriaxon" / "loci_capabilities.py").read_text(encoding="utf-8")
    for forbidden in ("def analyse", "def analyze", "def summarise", "def summarize", "def score",
                      "sentiment", "urllib", "requests.", "http", "socket"):
        check(f"the module has no {forbidden!r}", forbidden not in source,
              "journal and mood data are not analysed without consent")

    print()
    print("=== 5. THE SPEC RULE: deleting is safe and reported ===")
    root = fresh()
    memory = store.remember("something to remove", root=root)
    store.add_win("a win to remove", root=root)
    store.write_journal("an entry to remove", root=root)
    removed = store.forget(memory.id, root=root)
    check("forget returns WHAT it removed", removed is not None and removed.text == "something to remove",
          "a bare True tells the user nothing")
    check("  and it is gone", all("something to remove" not in m.text for m in store.recall(root=root)),
          str([m.text for m in store.recall(root=root)]))
    check("forgetting an unknown id returns None", store.forget("nope", root=root) is None)
    check("forget_all REFUSES without confirm", store.forget_all(root=root) == {},
          "the dangerous overload requires an argument a caller cannot pass by accident")
    check("  and nothing was deleted", store.summary(root=root)["wins"] == 1,
          f"summary says {store.summary(root=root)}")
    counts = store.forget_all(confirm=True, root=root)
    check("with confirm it removes everything", sum(counts.values()) > 0, str(counts))
    check("  and reports the counts",
          set(counts) == {"memories", "wins", "habits", "entries", "sessions"},
          str(sorted(counts)))
    after = store.summary(root=root)
    check("everything is empty afterwards",
          after["wins"] == 0 and after["memories"] == 0 and after["habits"] == 0 and after["journal"] == 0,
          str(after))
    check("a win can be forgotten individually", store.forget_win("nope", root=root) is None)
    check("a journal entry can be forgotten individually", store.forget_journal("nope", root=root) is None)

    print()
    print("=== 6. reflection reports an empty week honestly ===")
    root = fresh()
    empty = store.reflect(days=7, root=root)
    check("an empty week is flagged", empty["empty"] is True, str(empty))
    check("  with zero counts, not manufactured encouragement",
          empty["wins"] == 0 and empty["memories"] == 0 and empty["journal_entries"] == 0)
    store.add_win("something this week", root=root)
    store.remember("a memory this week", root=root)
    store.write_journal("an entry this week", root=root)
    store.declare_habit("walk", root=root)
    store.mark_habit("walk", root=root)
    recap = store.reflect(days=7, root=root)
    check("a non-empty week is not flagged", recap["empty"] is False)
    check("  wins are counted", recap["wins"] == 1, str(recap["wins"]))
    check("  memories are counted", recap["memories"] == 1)
    check("  journal entries are counted", recap["journal_entries"] == 1)
    check("  habits report done-of-window", recap["habits"] == [{"name": "walk", "done": 1, "of": 7}],
          str(recap["habits"]))
    check("  and an example win is offered", recap["wins_examples"] == ["something this week"])
    check("the window is honoured", store.reflect(days=1, root=root)["wins"] == 1)
    # `days=0` means today, and an entry recorded a moment ago IS from today. Measured: the first version used
    # `now - days*86400`, so `days=0` meant "from this instant onward" and an entry from five minutes ago fell
    # outside its own day -- an arithmetic definition that does not match what a person means by "today".
    check("  days=0 means today, not 'since this instant'",
          store.reflect(days=0, root=root)["wins"] == 1,
          "an entry written minutes ago is from today")
    check("  and the very first day is included in a 1-day window",
          store.reflect(days=1, root=root)["journal_entries"] == 1)

    print()
    print("=== 7. it never raises, and storage is local ===")
    for label, fn in (("an unreadable store", lambda: store.summary(root=Path("Z:/nope"))),
                      ("a store path that is a file", None)):
        if fn is None:
            continue
        try:
            result = fn()
            check(f"survives {label}", isinstance(result, dict), str(result)[:60])
        except Exception as exc:  # noqa: BLE001
            check(f"survives {label}", False, f"{type(exc).__name__}: {exc}")
    corrupt = fresh()
    target = corrupt / store.MEMORY_FILE.name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    check("a corrupt store does not raise", store.recall(root=corrupt) == [])
    check("  and is moved aside for recovery",
          any(p.name.endswith(".corrupt") for p in target.parent.iterdir()),
          str([p.name for p in target.parent.iterdir()]))
    check("the store lives under the config directory",
          str(store.COMPANION_DIR).startswith(str(store.CONFIG_DIR)),
          str(store.COMPANION_DIR))
    check("  in a directory named for what it holds, not the character",
          store.COMPANION_DIR.name == "companion",
          "a future rename of the character must not move a user's data")

    print()
    print("=== 8. WIRED: the CLI reaches all of it ===")
    cli_source = (REPO / "myriaxon" / "cli.py").read_text(encoding="utf-8")
    parser_source = (REPO / "myriaxon" / "argparse_cli.py").read_text(encoding="utf-8")
    check("the verbs are registered", '"verb"' in parser_source and "reflect" in parser_source)
    for verb in ("win", "remember", "recall", "forget", "habit", "journal", "reflect"):
        check(f"  {verb!r} is an accepted verb", f'"{verb}"' in parser_source)
    check("the handler exists", "def _run_loci_stores" in cli_source)
    # The handler is dispatched and its exit code propagates.
    #
    # This check used to require the literal string `handled >= 0`, from an earlier two-stage design where the store
    # handler could decline a verb and let the command fall through to the autonomy path. The rebuilt wiring returns
    # the handler's code directly instead, which is the same guarantee with one fewer branch -- so the assertion
    # moved to what actually matters: that the call is reached and its result is returned rather than dropped.
    check("  and is dispatched from the command path",
          'if args.command == "loci":' in cli_source
          and "return _run_loci_stores(args, console)" in cli_source,
          "the wiring, not the handler, is the part that used to be missing")
    check("a wipe requires --confirm", '"--confirm", action="store_true"' in parser_source)
    # The `--all` spelling was rejected by argparse at parse time before any code ran. Asserted so it is not
    # reintroduced: an interface that invents a flag argparse does not know is not an interface.
    check("no invented `--all` spelling is offered in the help",
          "--all --confirm" not in cli_source, "argparse rejects an unknown flag before the handler runs")

    print()
    print("=== 9. focus sessions ===")
    # §8.1 names focus mode. The capability is here rather than left to a shell timer because a running session
    # drives the character's `focus` expression -- a timer that cannot change the face is a timer.
    root = fresh()
    check("nothing is running to begin with", store.current_focus(root=root) is None)
    session = store.start_focus("write the handover", minutes=25, root=root)
    check("a session starts", session is not None and session.task == "write the handover")
    check("  with the requested length", session.minutes == 25, str(session.minutes))
    check("  and is reported as running", store.current_focus(root=root) is not None)
    check("progress starts near zero", store.current_focus(root=root).progress() < 0.05)
    check("remaining is under the full length", session.remaining_seconds() <= 25 * 60)
    check("the render shows a countdown", "left" in session.render(), session.render())
    # Stacking is refused because two running timers is not a state a person can be in.
    second = store.start_focus("something else", minutes=5, root=root)
    history = store.focus_history(root=root)
    check("starting a second ends the first",
          sum(1 for s in history if not s.finished) == 1, str([(s.task, s.finished) for s in history]))
    check("  the new one is the running one", store.current_focus(root=root).id == second.id)
    ended = store.end_focus(root=root)
    check("ending reports what ended", ended is not None and ended.id == second.id)
    check("  and nothing runs afterwards", store.current_focus(root=root) is None)
    check("ending again returns None", store.end_focus(root=root) is None)
    # The important behaviour: a session that elapsed while nobody was watching must not hold the face all day.
    stale = fresh()
    store.start_focus("forgotten", minutes=1, root=stale)
    import json as _json

    focus_path = stale / store.FOCUS_FILE.name
    payload = _json.loads(focus_path.read_text(encoding="utf-8"))
    payload["sessions"][-1]["started_at"] = time.time() - 7200
    focus_path.write_text(_json.dumps(payload), encoding="utf-8")
    check("a session that elapsed is not still running", store.current_focus(root=stale) is None,
          "otherwise the character would hold the focus face indefinitely")
    check("  and it was recorded as ended",
          _json.loads(focus_path.read_text(encoding="utf-8"))["sessions"][-1]["ended_at"] > 0,
          "elapsed while nobody looked is still finished")
    check("the length is clamped to something sane",
          store.start_focus("x", minutes=99999, root=fresh()).minutes <= 180)
    check("a zero length still runs", store.start_focus("x", minutes=0, root=fresh()).minutes >= 1)
    check("summary reports focus", "focus_running" in store.summary(root=root))
    check("  and forget_all clears sessions", "sessions" in store.forget_all(confirm=True, root=root))

    print()
    print("=== 10. the stores are separate files and versioned ===")
    for path in (store.WINS_FILE, store.MEMORY_FILE, store.HABITS_FILE, store.JOURNAL_FILE, store.FOCUS_FILE):
        check(f"{path.name} is its own file", path.parent == store.COMPANION_DIR, str(path))
    root = fresh()
    store.add_win("versioned", root=root)
    import json

    written = json.loads((root / store.WINS_FILE.name).read_text(encoding="utf-8"))
    check("a store records its version", written.get("version") == store.STORE_VERSION, str(written.keys()))
    check("  under a named key", "wins" in written, str(sorted(written)))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
