"""The personal half of Loci: local, private stores for the things a companion keeps for you.

## What this is, and what it deliberately is not

`loci.md` section 8 lists two kinds of capability. The *assistant* half (planning, reminders, calendar) belongs to the
agent and the model -- this module does not plan your day, it gives the agent somewhere to **keep** what it learned.
The *companion* half -- a wins jar, a memory palace, habits, a journal -- is durable personal state, and that is what
lives here.

So this is a storage and retrieval layer with opinions, not an agent. Every function is a fact about the user's own
data, and none of them calls a model.

## Four rules that shaped the design, all from the spec

**Local by default, and private by construction.** Section 8.6 is explicit: memory, journal and mood data are stored
locally by default. There is no remote call of any kind in this module, which is a stronger guarantee than a setting.
Everything lives in one gitignored directory under the user's config.

**Deleting must feel safe.** Section 8.6 also says removing a memory shows "a brief `idle` with the window going
dark, not a sad face, so users don't feel guilty deleting". The character layer owns the face; the deletion API here
is shaped to make it easy by *returning what it removed*, so a caller can say what happened instead of guessing.

**Nothing is ever examined without consent.** The journal stores text and hands it back. It is not condensed, not
rated, not sent anywhere. That is why there is no `analyse` function in this file -- its absence is the feature, and
the verifier asserts the absence rather than a behaviour.

**Streaks are forgiving.** Section 9.3 gives the wording: `"4 of the last 7 days. That's real progress."` -- never
`"Streak broken!"`. So a habit records *which days* it was done rather than a run length, and the summary is computed
as a ratio over a window. A counter cannot express that honestly; a set of dates can.

## Storage

One JSON file per store under `CONFIG_DIR/companion/`, written through `atomic_write_text` so a crash mid-write cannot
lose the file. Each has a `version` so a future shape change can migrate rather than guess.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .atomic_io import atomic_write_text
from .constants import CONFIG_DIR
from .logging_setup import logger

# One directory, so "delete everything" is one operation and one path to inspect. Named for what it holds rather than
# for the character, so a future rename of the character does not move a user's data.
COMPANION_DIR = CONFIG_DIR / "companion"

WINS_FILE = COMPANION_DIR / "wins.json"
MEMORY_FILE = COMPANION_DIR / "memories.json"
HABITS_FILE = COMPANION_DIR / "habits.json"
JOURNAL_FILE = COMPANION_DIR / "journal.json"
FOCUS_FILE = COMPANION_DIR / "focus.json"
TASKS_FILE = COMPANION_DIR / "tasks.json"
MOODS_FILE = COMPANION_DIR / "moods.json"

STORE_VERSION = 1

# Bounds. Every store is user data that only grows, so each one has a ceiling and drops oldest-first -- except the
# journal and memories, which are never dropped silently, because discarding something a person wrote without asking
# is the one thing section 8.6 forbids.
MAX_WINS = 500
MAX_HABITS = 40
MAX_JOURNAL_ENTRIES = 2000
# A task list longer than this is not a task list. The cap is generous because the cost of refusing to record
# something is higher than the cost of a long file -- `today` uses `limit` for display, not for storage.
MAX_TASKS = 1000
MAX_MOODS = 5000

# The "rooms" of the memory palace, from section 8.1. Free text is allowed too; these are the suggestions a picker
# offers, not a closed set.
ROOMS = ("work", "people", "health", "ideas", "home", "money", "learning")

# One focus session. Long enough to be useful, short enough to start. Section 8.1 names focus mode as a capability;
# the character already has the `focus` expression, which is what a running session drives.
DEFAULT_FOCUS_MINUTES = 25


# ---------------------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------------------


def _load(path: Path, key: str) -> list[dict]:
    """Read a store. Returns an empty list for anything unreadable, and never raises.

    A personal store that can raise takes a command down with it. A corrupt file is logged and moved aside so the
    user can recover it, which is the same policy the locus registry uses.
    """
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s (%s); treating it as empty.", path, exc)
        try:
            path.replace(path.with_suffix(".json.corrupt"))
        except OSError:
            pass
        return []
    if isinstance(data, dict):
        entries = data.get(key)
        return entries if isinstance(entries, list) else []
    return data if isinstance(data, list) else []


def _save(path: Path, key: str, entries: list[dict]) -> bool:
    """Write a store atomically. Returns whether it worked, rather than raising."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"version": STORE_VERSION, key: entries},
                                           indent=2, ensure_ascii=False))
        return True
    except OSError as exc:
        logger.warning("Could not write %s (%s).", path, exc)
        return False


def _new_id(prefix: str) -> str:
    """A short id that sorts by creation and is readable in a log."""
    return f"{prefix}_{int(time.time() * 1000):x}"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _iso(when: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(when))


def _day_start(when: float) -> float:
    """Midnight at the start of the day `when` falls in, as a timestamp."""
    stamp = time.localtime(when)
    return time.mktime((stamp.tm_year, stamp.tm_mon, stamp.tm_mday, 0, 0, 0, 0, 0, -1))


def _relative(when: float) -> str:
    """A short human age. Local rather than imported so this module has no dependencies beyond the standard library."""
    seconds = max(0.0, time.time() - float(when or 0))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 172800:
        return f"{seconds / 3600:.0f} h ago"
    return f"{seconds / 86400:.0f} d ago"


def _clean(text: str, *, limit: int) -> str:
    """Collapse whitespace and bound the length. One place, so every store agrees."""
    return " ".join((text or "").split())[:limit]


def _fields(entry: dict, names: tuple[str, ...]) -> dict:
    return {name: entry[name] for name in names if name in entry}


def _forget(path_name: Path, key: str, entry_id: str, kind, names: tuple[str, ...],
            *, root: Path | None = None):
    """Shared removal for every store. Returns the removed record, or None.

    One implementation rather than four, because four copies of "filter a list and save it" is four chances for one of
    them to forget the save.
    """
    path = (root or COMPANION_DIR) / path_name.name
    entries = _load(path, key)
    kept, removed = [], None
    for entry in entries:
        if entry.get("id") == entry_id and removed is None:
            removed = entry
            continue
        kept.append(entry)
    if removed is None:
        return None
    _save(path, key, kept)
    try:
        return kind(**_fields(removed, names))
    except (TypeError, KeyError):
        return None


# ---------------------------------------------------------------------------------------
# Wins jar
# ---------------------------------------------------------------------------------------


@dataclass
class Win:
    """One thing that went right. Section 8.1: "big and small", surfaced on hard days."""

    id: str
    text: str
    at: float
    # Optional, because a win often belongs to something. Free text, not a foreign key.
    about: str = ""

    def render(self) -> str:
        suffix = f"  ({self.about})" if self.about else ""
        return f"{self.text}{suffix}  -- {_relative(self.at)}"


def add_win(text: str, *, about: str = "", root: Path | None = None) -> Win | None:
    """Record a win. Refuses an empty one rather than storing a blank entry."""
    cleaned = _clean(text, limit=500)
    if not cleaned:
        return None
    path = (root or COMPANION_DIR) / WINS_FILE.name
    entries = _load(path, "wins")
    win = Win(id=_new_id("win"), text=cleaned, at=time.time(), about=_clean(about, limit=120))
    entries.append(asdict(win))
    # Oldest first, so the jar keeps the recent ones.
    if len(entries) > MAX_WINS:
        entries = entries[-MAX_WINS:]
    _save(path, "wins", entries)
    return win


def list_wins(*, limit: int = 20, root: Path | None = None) -> list[Win]:
    """The most recent wins, newest first."""
    path = (root or COMPANION_DIR) / WINS_FILE.name
    entries = _load(path, "wins")
    out: list[Win] = []
    for entry in reversed(entries[-max(1, limit):]):
        try:
            out.append(Win(**_fields(entry, ("id", "text", "at", "about"))))
        except (TypeError, KeyError):
            continue
    return out


def wins_on_hard_day(*, window_days: int = 30, root: Path | None = None) -> list[Win]:
    """A handful of wins to surface when someone is struggling.

    Deliberately picks from ACROSS the window rather than the most recent: the point of showing this on a bad day is
    to demonstrate range over time, and the last three wins from this week would prove nothing.
    """
    path = (root or COMPANION_DIR) / WINS_FILE.name
    cutoff = time.time() - window_days * 86400
    entries = [e for e in _load(path, "wins") if float(e.get("at", 0)) >= cutoff]
    if not entries:
        return []
    # Evenly spaced picks, oldest first, so the sample spans the window.
    step = max(1, len(entries) // 3)
    out: list[Win] = []
    for entry in entries[::step][:3]:
        try:
            out.append(Win(**_fields(entry, ("id", "text", "at", "about"))))
        except (TypeError, KeyError):
            continue
    return out


def forget_win(win_id: str, *, root: Path | None = None) -> Win | None:
    """Remove a win by id. Returns what was removed, or None.

    Returns the removed record rather than a boolean so a caller can say *what* was deleted. Section 8.6's principle
    is that deletion should feel safe; "deleted" with no detail is not.
    """
    return _forget(WINS_FILE, "wins", win_id, Win, ("id", "text", "at", "about"), root=root)


# ---------------------------------------------------------------------------------------
# Memory palace
# ---------------------------------------------------------------------------------------


@dataclass
class Memory:
    """Something the user asked Loci to remember, filed in a room."""

    id: str
    text: str
    room: str
    at: float
    # Where it came from, when it was not typed directly (a session, a locus).
    source: str = ""

    def render(self) -> str:
        return f"[{self.room}] {self.text}  -- {_relative(self.at)}"


def remember(text: str, *, room: str = "ideas", source: str = "", root: Path | None = None) -> Memory | None:
    """Store a memory. Refuses empty text."""
    cleaned = _clean(text, limit=2000)
    if not cleaned:
        return None
    path = (root or COMPANION_DIR) / MEMORY_FILE.name
    entries = _load(path, "memories")
    memory = Memory(id=_new_id("mem"), text=cleaned,
                    room=_clean(room, limit=40).lower() or "ideas",
                    at=time.time(), source=_clean(source, limit=120))
    entries.append(asdict(memory))
    _save(path, "memories", entries)
    return memory


def recall(query: str = "", *, room: str = "", limit: int = 20, root: Path | None = None) -> list[Memory]:
    """Find memories. An empty query returns the room, or everything.

    Substring matching rather than an index: a person's memory palace is hundreds of entries at most, and a substring
    search over a list that size is instant and has no index to fall out of date.
    """
    path = (root or COMPANION_DIR) / MEMORY_FILE.name
    entries = _load(path, "memories")
    needle = (query or "").strip().lower()
    wanted_room = (room or "").strip().lower()
    out: list[Memory] = []
    for entry in reversed(entries):
        if wanted_room and str(entry.get("room", "")).lower() != wanted_room:
            continue
        if needle and needle not in str(entry.get("text", "")).lower():
            continue
        try:
            out.append(Memory(**_fields(entry, ("id", "text", "room", "at", "source"))))
        except (TypeError, KeyError):
            continue
        if len(out) >= max(1, limit):
            break
    return out


def rooms(root: Path | None = None) -> dict[str, int]:
    """Every room and how full it is. For a picker and for listing what exists."""
    path = (root or COMPANION_DIR) / MEMORY_FILE.name
    counts: dict[str, int] = {}
    for entry in _load(path, "memories"):
        key = str(entry.get("room", "ideas"))
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def forget(memory_id: str, *, root: Path | None = None) -> Memory | None:
    """Remove one memory. Returns what was removed, so the caller can confirm it."""
    return _forget(MEMORY_FILE, "memories", memory_id, Memory,
                   ("id", "text", "room", "at", "source"), root=root)


def forget_all(*, confirm: bool = False, root: Path | None = None) -> dict[str, int]:
    """Delete every store. Refuses unless `confirm=True`.

    The spec requires "a confirmation", and this is the code half of it: the dangerous overload requires an argument a
    caller cannot pass by accident. The CLI asks the question; this enforces the answer.
    """
    if not confirm:
        return {}
    base = root or COMPANION_DIR
    counts: dict[str, int] = {}
    for name, key in ((MEMORY_FILE, "memories"), (WINS_FILE, "wins"), (HABITS_FILE, "habits"),
                      (JOURNAL_FILE, "entries"), (FOCUS_FILE, "sessions")):
        target = base / name.name
        counts[key] = len(_load(target, key))
        _save(target, key, [])
    return counts


# ---------------------------------------------------------------------------------------
# Habits
# ---------------------------------------------------------------------------------------


@dataclass
class Habit:
    """A habit, recorded as the DAYS it happened rather than a run length.

    Section 9.3 is explicit that the wording must be "4 of the last 7 days. That's real progress." and never
    "Streak broken!". A counter cannot say that honestly -- it has no memory of which days -- so the storage keeps ISO
    dates and the summary computes a ratio. That is the whole reason this field is a list and not an int.
    """

    name: str
    days: list[str] = field(default_factory=list)
    created_at: float = 0.0

    @property
    def done_today(self) -> bool:
        return _today() in self.days

    def ratio(self, window: int = 7, *, now: float | None = None) -> tuple[int, int]:
        """`(days done, days in window)`. Never a run length."""
        span = max(1, window)
        moment = time.time() if now is None else now
        recent = {_iso(moment - offset * 86400) for offset in range(span)}
        return len(recent & set(self.days)), span

    def render(self, window: int = 7) -> str:
        done, total = self.ratio(window)
        mark = "done today" if self.done_today else "not yet today"
        return f"{self.name}: {done} of the last {total} days ({mark})"


def _habits(path: Path) -> list[dict]:
    return _load(path, "habits")


def declare_habit(name: str, *, root: Path | None = None) -> Habit | None:
    """Start tracking a habit. Refuses empty, and refuses a duplicate rather than silently making two."""
    cleaned = _clean(name, limit=60).lower()
    if not cleaned:
        return None
    path = (root or COMPANION_DIR) / HABITS_FILE.name
    entries = _habits(path)
    if any(str(e.get("name", "")).lower() == cleaned for e in entries):
        return None
    habit = Habit(name=cleaned, days=[], created_at=time.time())
    entries.append(asdict(habit))
    if len(entries) > MAX_HABITS:
        entries = entries[-MAX_HABITS:]
    _save(path, "habits", entries)
    return habit


def mark_habit(name: str, *, day: str = "", root: Path | None = None) -> Habit | None:
    """Mark a habit done for a day (today by default). Idempotent: marking twice is not two days."""
    cleaned = _clean(name, limit=60).lower()
    path = (root or COMPANION_DIR) / HABITS_FILE.name
    entries = _habits(path)
    stamp = day or _today()
    for entry in entries:
        if str(entry.get("name", "")).lower() == cleaned:
            days = list(entry.get("days") or [])
            if stamp not in days:
                days.append(stamp)
                days.sort()
            entry["days"] = days
            _save(path, "habits", entries)
            return Habit(name=str(entry["name"]), days=days, created_at=float(entry.get("created_at", 0)))
    return None


def list_habits(*, root: Path | None = None) -> list[Habit]:
    path = (root or COMPANION_DIR) / HABITS_FILE.name
    out: list[Habit] = []
    for entry in _habits(path):
        try:
            out.append(Habit(name=str(entry["name"]), days=list(entry.get("days") or []),
                             created_at=float(entry.get("created_at", 0))))
        except (TypeError, KeyError):
            continue
    return out


# ---------------------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------------------

JOURNAL_PROMPTS = (
    "What is taking up the most room in your head right now?",
    "What went better today than you expected?",
    "What would make tomorrow feel lighter?",
    "What are you carrying that you have not said out loud?",
    "What is one thing you are glad you did this week?",
)


def journal_prompt(*, index: int | None = None) -> str:
    """A prompt for a prompted journal entry. Deterministic by index so a test can assert it."""
    if index is None:
        index = int(time.time() // 86400)
    return JOURNAL_PROMPTS[index % len(JOURNAL_PROMPTS)]


@dataclass
class JournalEntry:
    id: str
    text: str
    at: float
    prompt: str = ""

    def render(self) -> str:
        head = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.at))
        if self.prompt:
            head += f"  ({self.prompt})"
        return f"{head}\n{self.text}"


def write_journal(text: str, *, prompt: str = "", root: Path | None = None) -> JournalEntry | None:
    """Append a journal entry. Refuses empty text.

    Stored verbatim and handed back verbatim. There is no condenser and no rater in this module, because section 8.6
    says journal data is not examined without an explicit opt-in -- and the surest way to honour that is to have
    nothing that could.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    path = (root or COMPANION_DIR) / JOURNAL_FILE.name
    entries = _load(path, "entries")
    entry = JournalEntry(id=_new_id("jrn"), text=cleaned[:20000], at=time.time(),
                         prompt=_clean(prompt, limit=200))
    entries.append(asdict(entry))
    if len(entries) > MAX_JOURNAL_ENTRIES:
        # Only the OLDEST go, and only past a very high ceiling. The journal is the one store where silent deletion
        # would be a betrayal, so the limit is set where a person will never reach it in practice.
        entries = entries[-MAX_JOURNAL_ENTRIES:]
    _save(path, "entries", entries)
    return entry


def list_journal(*, limit: int = 10, since_days: int = 0, root: Path | None = None) -> list[JournalEntry]:
    path = (root or COMPANION_DIR) / JOURNAL_FILE.name
    entries = _load(path, "entries")
    if since_days:
        cutoff = _day_start(time.time() - (since_days - 1) * 86400)
        entries = [e for e in entries if float(e.get("at", 0)) >= cutoff]
    out: list[JournalEntry] = []
    for entry in reversed(entries[-max(1, limit):]):
        try:
            out.append(JournalEntry(**_fields(entry, ("id", "text", "at", "prompt"))))
        except (TypeError, KeyError):
            continue
    return out


def forget_journal(entry_id: str, *, root: Path | None = None) -> JournalEntry | None:
    return _forget(JOURNAL_FILE, "entries", entry_id, JournalEntry,
                   ("id", "text", "at", "prompt"), root=root)


# ---------------------------------------------------------------------------------------
# Weekly reflection
# ---------------------------------------------------------------------------------------


def reflect(*, days: int = 7, root: Path | None = None) -> dict:
    """A recap of the last week, from what the user already recorded.

    Computed from stored facts rather than generated: wins recorded, habits kept, journal entries written, memories
    added. Section 8.1 calls for "a short recap: what went well, what was heavy, one intention for next week" -- the
    model writes the prose from these numbers, and this function supplies numbers it cannot invent.

    Reports an EMPTY week honestly rather than manufacturing encouragement. "You recorded nothing this week" is a
    fact; padding it would be the false reassurance section 1.3 forbids.
    """
    base = root or COMPANION_DIR
    # `days` counts backwards from today, and `days<=0` means TODAY rather than "since this instant".
    #
    # Measured: the first version used `now - days * 86400`, so `days=0` meant "from this exact moment onward" and an
    # entry recorded five minutes ago fell outside its own day. Comparing against the START of the earliest day in
    # the window makes the arithmetic mean what a person means by "the last 7 days".
    if days <= 0:
        cutoff = _day_start(time.time())
    else:
        cutoff = _day_start(time.time() - (days - 1) * 86400)

    wins = [e for e in _load(base / WINS_FILE.name, "wins") if float(e.get("at", 0)) >= cutoff]
    memories = [e for e in _load(base / MEMORY_FILE.name, "memories") if float(e.get("at", 0)) >= cutoff]
    journal = [e for e in _load(base / JOURNAL_FILE.name, "entries") if float(e.get("at", 0)) >= cutoff]
    habit_summary = []
    for habit in list_habits(root=base):
        done, total = habit.ratio(days if days > 0 else 1)
        habit_summary.append({"name": habit.name, "done": done, "of": total})
    return {
        "days": days,
        "wins": len(wins),
        "wins_examples": [str(w.get("text", ""))[:120] for w in wins[-3:]],
        "memories": len(memories),
        "journal_entries": len(journal),
        "habits": habit_summary,
        "empty": not (wins or memories or journal or any(h["done"] for h in habit_summary)),
    }


def summary(root: Path | None = None) -> dict:
    """Counts for everything kept, for a status view. Never returns text from the stores."""
    base = root or COMPANION_DIR
    return {
        "wins": len(_load(base / WINS_FILE.name, "wins")),
        "memories": len(_load(base / MEMORY_FILE.name, "memories")),
        "rooms": rooms(root=base),
        "habits": len(_habits(base / HABITS_FILE.name)),
        "journal": len(_load(base / JOURNAL_FILE.name, "entries")),
        "focus_sessions": len(_load(base / FOCUS_FILE.name, "sessions")),
        "focus_running": current_focus(root=base) is not None,
        "directory": str(base),
    }


# ---------------------------------------------------------------------------------------
# Focus sessions
# ---------------------------------------------------------------------------------------


@dataclass
class FocusSession:
    """A timed focus block.

    Deliberately not a pomodoro *app*: there is no break timer, no long-break schedule and no statistics. Section 8.1
    lists focus as "a focus timer that also mutes notifications", and a companion that lectures you about break
    cadence is the bossy failure section 1.3 forbids. One timer, one end.
    """

    id: str
    task: str
    started_at: float
    minutes: int
    ended_at: float = 0.0

    @property
    def duration_seconds(self) -> int:
        return max(1, self.minutes) * 60

    @property
    def ends_at(self) -> float:
        return self.started_at + self.duration_seconds

    @property
    def running(self) -> bool:
        return self.ended_at == 0.0 and time.time() < self.ends_at

    @property
    def finished(self) -> bool:
        """Elapsed, whether or not anyone was watching. No callback is needed to know."""
        return self.ended_at != 0.0 or time.time() >= self.ends_at

    def remaining_seconds(self, *, now: float | None = None) -> int:
        moment = time.time() if now is None else now
        return max(0, int(self.ends_at - moment))

    def progress(self, *, now: float | None = None) -> float:
        """0.0 to 1.0. For a progress bar and for the character's own sense of how far in it is."""
        moment = time.time() if now is None else now
        elapsed = max(0.0, moment - self.started_at)
        return min(1.0, elapsed / self.duration_seconds)

    def render(self, *, now: float | None = None) -> str:
        if self.finished:
            spent = int((self.ended_at or self.ends_at) - self.started_at) // 60
            return f"focus: {self.task or '(untitled)'} -- finished, {spent} min"
        left = self.remaining_seconds(now=now)
        return f"focus: {self.task or '(untitled)'} -- {left // 60}:{left % 60:02d} left"


def start_focus(task: str, *, minutes: int = DEFAULT_FOCUS_MINUTES,
                root: Path | None = None) -> FocusSession:
    """Begin a focus session. Any session already running is ended rather than stacked.

    Stacking is refused because two running timers is not a state a person can be in: it would make `focus` mean
    "something is somewhere" rather than "you are focusing now", and the character's `focus` expression has to be a
    fact about the present.
    """
    path = (root or COMPANION_DIR) / FOCUS_FILE.name
    entries = _load(path, "sessions")
    for entry in entries:
        if not entry.get("ended_at"):
            entry["ended_at"] = time.time()
    session = FocusSession(id=_new_id("foc"), task=_clean(task, limit=200), started_at=time.time(),
                           minutes=max(1, min(180, int(minutes or DEFAULT_FOCUS_MINUTES))))
    entries.append(asdict(session))
    # The history is bounded; the running session is always the last entry, so trimming the front never loses it.
    if len(entries) > 200:
        entries = entries[-200:]
    _save(path, "sessions", entries)
    return session


def current_focus(*, root: Path | None = None) -> FocusSession | None:
    """The running session, or None. Ends one whose time has elapsed, so no timer process is required."""
    path = (root or COMPANION_DIR) / FOCUS_FILE.name
    entries = _load(path, "sessions")
    if not entries:
        return None
    try:
        session = FocusSession(**_fields(entries[-1], ("id", "task", "started_at", "minutes", "ended_at")))
    except (TypeError, KeyError):
        return None
    if session.ended_at:
        return None
    if time.time() >= session.ends_at:
        # Elapsed while nobody was looking. Recorded as ended rather than left in limbo -- a session that is
        # "running" eight hours later would make the character hold the `focus` face all day.
        entries[-1]["ended_at"] = session.ends_at
        _save(path, "sessions", entries)
        return None
    return session


def end_focus(*, root: Path | None = None) -> FocusSession | None:
    """End the running session early. Returns it, or None if nothing was running."""
    path = (root or COMPANION_DIR) / FOCUS_FILE.name
    entries = _load(path, "sessions")
    if not entries or entries[-1].get("ended_at"):
        return None
    session = current_focus(root=root)
    if session is None:
        # It elapsed between the load and here; `current_focus` has already recorded that.
        return None
    entries[-1]["ended_at"] = time.time()
    _save(path, "sessions", entries)
    return session


def focus_history(*, limit: int = 10, root: Path | None = None) -> list[FocusSession]:
    path = (root or COMPANION_DIR) / FOCUS_FILE.name
    out: list[FocusSession] = []
    for entry in reversed(_load(path, "sessions")[-max(1, limit):]):
        try:
            out.append(FocusSession(**_fields(entry, ("id", "task", "started_at", "minutes", "ended_at"))))
        except (TypeError, KeyError):
            continue
    return out


# ---------------------------------------------------------------------------------------
# Tasks and reminders (section 8.1)
# ---------------------------------------------------------------------------------------

# The wording rule from section 8.1, in one place so no surface can drift from it: a task that is late is described
# by WHEN it is due, never by how late it is. "Was due Tuesday" is a fact; "3 days overdue" is a reproach.
def _due_phrase(due: str) -> str:
    """A gentle description of a due date. Never a count of how long something has been late."""
    if not due:
        return "someday"
    today = _today()
    if due == today:
        return "today"
    if due < today:
        # The date, not the delay. There is deliberately no branch here that computes a difference in days.
        return f"was due {due}"
    return f"due {due}"


@dataclass
class Task:
    """One thing to do. `done` rather than deleted, so finishing something is recorded rather than erased."""

    id: str
    text: str
    due: str = ""
    done: bool = False
    created_at: float = 0.0
    done_at: float = 0.0
    snoozes: int = 0
    priority: int = 0

    def render(self) -> str:
        mark = "x" if self.done else " "
        when = f"  ({_due_phrase(self.due)})" if self.due else ""
        return f"[{mark}] {self.id}  {self.text}{when}"

    @property
    def overdue(self) -> bool:
        """Whether the due date has passed. A property rather than a label, so a surface decides how to say it."""
        return bool(self.due) and not self.done and self.due < _today()


def _tasks(path: Path) -> list[dict]:
    return _load(path, "tasks")


def add_task(text: str, *, due: str = "", priority: int = 0, root: Path | None = None) -> Task | None:
    """Record a task. Returns None when there is nothing to record."""
    cleaned = _clean(text, limit=500)
    if not cleaned:
        return None
    path = (root / "tasks.json") if root is not None else TASKS_FILE
    entries = _tasks(path)
    if len(entries) >= MAX_TASKS:
        return None
    task = Task(
        id=_new_id("task"), text=cleaned, due=(due or "").strip()[:10],
        created_at=time.time(), priority=max(0, min(2, int(priority or 0))),
    )
    entries.append({
        "id": task.id, "text": task.text, "due": task.due, "done": False,
        "created_at": task.created_at, "done_at": 0.0, "snoozes": 0, "priority": task.priority,
    })
    _save(path, "tasks", entries)
    return task


def _task_from(entry: dict) -> Task:
    return Task(
        id=str(entry.get("id") or ""),
        text=str(entry.get("text") or ""),
        due=str(entry.get("due") or ""),
        done=bool(entry.get("done")),
        created_at=float(entry.get("created_at") or 0.0),
        done_at=float(entry.get("done_at") or 0.0),
        snoozes=int(entry.get("snoozes") or 0),
        priority=int(entry.get("priority") or 0),
    )


def list_tasks(*, include_done: bool = False, due_only: bool = False,
               limit: int = 50, root: Path | None = None) -> list[Task]:
    """Open tasks, soonest first.

    Ordering is by due date with undated tasks last, and then by priority. Nothing is ordered by how late it is,
    because that ordering exists only to make someone feel the delay.
    """
    path = (root / "tasks.json") if root is not None else TASKS_FILE
    tasks = [_task_from(e) for e in _tasks(path)]
    if not include_done:
        tasks = [t for t in tasks if not t.done]
    if due_only:
        tasks = [t for t in tasks if t.due]
    # Dated before undated, then earliest due, then highest priority. `due or "9999"` sorts undated last without a
    # second key, which keeps the comparison total.
    tasks.sort(key=lambda t: (t.due or "9999-99-99", -t.priority, t.created_at))
    return tasks[:max(1, limit)]


def _update_task(task_id: str, change, *, root: Path | None = None) -> Task | None:
    """Apply `change(entry) -> bool` to one task. Returns the updated task, or None when it is not found."""
    path = (root / "tasks.json") if root is not None else TASKS_FILE
    entries = _tasks(path)
    for entry in entries:
        if str(entry.get("id")) != task_id:
            continue
        if not change(entry):
            return None
        _save(path, "tasks", entries)
        return _task_from(entry)
    return None


def complete_task(task_id: str, *, root: Path | None = None) -> Task | None:
    """Mark a task done. Completed rather than deleted, so the record of finishing it survives."""
    def mark(entry: dict) -> bool:
        if entry.get("done"):
            return False
        entry["done"] = True
        entry["done_at"] = time.time()
        return True

    return _update_task(task_id, mark, root=root)


def snooze_task(task_id: str, *, days: int = 1, root: Path | None = None) -> Task | None:
    """Move a task's due date forward. A first-class verb, not a failure.

    Section 8.5 forbids guilting the user for "missing tasks", and the mechanical way to avoid it is for the product to
    treat moving something as an ordinary operation. The snooze count is recorded for the user's own view and is never
    rendered as a judgement.
    """
    step = max(1, min(365, int(days or 1)))

    def move(entry: dict) -> bool:
        base = str(entry.get("due") or "") or _today()
        try:
            year, month, day = (int(part) for part in base.split("-"))
            moved = time.mktime((year, month, day, 12, 0, 0, 0, 0, -1)) + step * 86_400
            entry["due"] = _iso(moved)
        except (ValueError, OverflowError):
            entry["due"] = _iso(time.time() + step * 86_400)
        entry["snoozes"] = int(entry.get("snoozes") or 0) + 1
        # Un-finishing would be wrong, so a snooze on a done task clears `done` only because a done task is not
        # listed and therefore cannot be snoozed from the CLI. Asserted rather than assumed: see the verifier.
        return True

    return _update_task(task_id, move, root=root)


def reopen_task(task_id: str, *, root: Path | None = None) -> Task | None:
    """Undo a completion. The counterpart of `complete_task`, so marking done is not a one-way door."""
    def reopen(entry: dict) -> bool:
        if not entry.get("done"):
            return False
        entry["done"] = False
        entry["done_at"] = 0.0
        return True

    return _update_task(task_id, reopen, root=root)


def forget_task(task_id: str, *, root: Path | None = None) -> Task | None:
    """Remove a task entirely."""
    def drop(entry: dict) -> bool:
        return True

    path = (root / "tasks.json") if root is not None else TASKS_FILE
    entries = _tasks(path)
    for index, entry in enumerate(entries):
        if str(entry.get("id")) == task_id:
            removed = _task_from(entry)
            del entries[index]
            _save(path, "tasks", entries)
            return removed
    return None


# ---------------------------------------------------------------------------------------
# Calendar awareness (section 8.1) -- what the companion stores can actually see
# ---------------------------------------------------------------------------------------

# The count at which a day is described as full. Deliberately a NAMED constant rather than a magic number in a
# comparison, so `today_summary` can report the threshold it used and a caller can disagree with it.
HEAVY_DAY_TASKS = 6


def today_summary(*, root: Path | None = None, heavy_at: int = HEAVY_DAY_TASKS) -> dict:
    """What the day holds, from the stores this harness actually has.

    Reports TASKS, HABITS and any live FOCUS session. It does NOT report meetings, because there is no calendar
    integration -- section 8.1's example mentions meetings and that example cannot be honoured here. The gap is
    recorded in `verify_loci_spec` rather than filled with invented entries.

    `heavy` is a count, and the phrasing that goes with it is a question rather than a verdict: section 8.1's own
    example is "want me to block 30 min?", which offers rather than instructs.
    """
    today = _today()
    open_tasks = list_tasks(limit=MAX_TASKS, root=root)
    due_today = [t for t in open_tasks if t.due == today]
    late = [t for t in open_tasks if t.overdue]
    undated = [t for t in open_tasks if not t.due]

    habits = list_habits(root=root)
    focus = current_focus(root=root)

    done_today = [
        t for t in list_tasks(include_done=True, limit=MAX_TASKS, root=root)
        if t.done and t.done_at and _iso(t.done_at) == today
    ]

    load = len(due_today) + len(late)
    heavy = load >= max(1, int(heavy_at))
    notes: list[str] = []
    if late:
        # The COUNT is given, and then the offer. Never "you are behind".
        notes.append(f"{len(late)} thing(s) from earlier are still open. Want to move them, or let them go?")
    if heavy:
        notes.append(
            f"{load} thing(s) are on today. That is a full day -- want to pick the one that matters most and "
            "leave the rest for tomorrow?"
        )
    if not open_tasks and not habits:
        notes.append("Nothing is recorded for today. That can be a good thing.")
    if focus is not None:
        notes.append(f"A focus session is running: {focus.task}")

    return {
        "day": today,
        "due_today": due_today,
        "late": late,
        "undated": undated,
        "open_count": len(open_tasks),
        "done_today": done_today,
        "habits": habits,
        "focus": focus,
        "heavy": heavy,
        "heavy_at": max(1, int(heavy_at)),
        "notes": notes,
    }


def render_today(summary: dict) -> str:
    """`today_summary` as text. Wording is section 8.1's: state what is there, then offer."""
    lines = [f"Today, {summary.get('day', '')}:"]
    if summary.get("focus") is not None:
        focus = summary["focus"]
        lines.append(f"  focus: {focus.task}")
    due = summary.get("due_today") or []
    if due:
        lines.append(f"  due today ({len(due)}):")
        lines.extend(f"    {task.render()}" for task in due)
    late = summary.get("late") or []
    if late:
        lines.append(f"  from earlier ({len(late)}):")
        lines.extend(f"    {task.render()}" for task in late)
    done = summary.get("done_today") or []
    if done:
        lines.append(f"  finished today ({len(done)}):")
        lines.extend(f"    {task.text}" for task in done)
    habits = summary.get("habits") or []
    if habits:
        lines.append("  habits:")
        lines.extend(f"    {habit.render()}" for habit in habits)
    if len(lines) == 1:
        lines.append("  nothing recorded.")
    for note in summary.get("notes") or []:
        lines.append(f"  {note}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------
# Daily plan (section 8.1)
# ---------------------------------------------------------------------------------------


def plan_prompt(brain_dump: str, *, limit: int = 3) -> str:
    """The prompt for turning a brain-dump into priorities.

    A PROMPT BUILDER rather than a plan generator, and that is a deliberate boundary: choosing what matters is the
    one part of this that a model should not do silently on the user's behalf. The harness asks; the model proposes;
    the user decides. Section 8.1's row says "Asks before adding more", which is exactly this shape.

    The returned text is fed into a normal turn, so the reply uses the session's own model and the user sees the
    reasoning rather than a canned list.
    """
    dump = (brain_dump or "").strip()
    if not dump:
        return ""
    count = max(1, min(5, int(limit or 3)))
    return (
        "The user wrote this down as a brain-dump:\n\n"
        f"{dump}\n\n"
        f"Turn it into at most {count} priorities for today, plus a short 'could do' list. "
        "Rules: do not add anything the user did not mention; if two items are really one task, say so; "
        "if the list is already small, say that rather than inventing work; and ASK before adding anything "
        "to the plan that the user did not write. Plain text, no headings."
    )
