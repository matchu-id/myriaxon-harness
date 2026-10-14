"""Section 8.2: the companion half.

Loci "is **not** a therapist, does not diagnose, and does not provide treatment." Everything here is written to that
line, and the line shapes the design rather than being a disclaimer:

  * nothing here assesses, scores or interprets. A mood is a label the USER chose, not a measurement Loci took;
  * nothing here offers advice unprompted. Section 8.3 says Loci "asks before offering advice", so the helpers
    PRODUCE A QUESTION and let the user decide;
  * each helper returns a SHAPE (a prompt to send, an exercise with a pacer, a single next step) rather than a
    paragraph to display, so the session's own model does the talking and a surface decides how to render.

## What is deliberately NOT here

**Grounding's 5-4-3-2-1 is text; box breathing needs a pacer.** The design document names Loci's `breathe` animation,
which is a UI component this harness does not have. `start_grounding` therefore returns the steps AND the timing, so a
surface with an animation can drive it and one without can print it. A breathing exercise with no pacer is a list of
instructions, which is what it falls back to.

**Pattern reflection is opt-in, and the opt-in is the user's.** Section 8.2's example is "You've felt lower on Mondays
lately. Want to look at that together?" -- so `mood_pattern` returns the observation and the QUESTION, and
`reflect_mood` refuses to produce anything until the user has opted in. A product that decided for them would be
doing the analysis section 8.5 forbids without an opt-in.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .constants import CONFIG_DIR
from .loci_capabilities import (
    STORE_VERSION,
    _clean,
    _iso,
    _load,
    _new_id,
    _save,
    _today,
)

COMPANION_DIR = CONFIG_DIR / "companion"
MOODS_FILE = COMPANION_DIR / "moods.json"

MAX_MOODS = 5000

# The five levels, in the design document's order. Named exactly as the document writes them.
MOODS = ("Great", "Okay", "Low", "Rough", "Not sure")

# Rough is the floor of the "worth noticing" range, not a diagnosis. Kept as data so a caller can see the rule.
LOWER_MOODS = ("Low", "Rough")

# Nights are for winding down. Section 8.2: "no productivity pushes at night".
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 7


@dataclass
class Mood:
    """One check-in: a label the user chose, and a note if they wanted one."""

    id: str
    mood: str
    note: str = ""
    at: float = 0.0
    # A tiebreak for same-millisecond check-ins. 	ime.time() is not fine-grained enough to order two entries
    # recorded in quick succession, and a stable sort then preserves INSERTION order -- which for a newest-first
    # list is exactly backwards. Measured: two check-ins in one tick listed oldest first.
    seq: int = 0

    def render(self) -> str:
        when = _iso(self.at) if self.at else ""
        line = f"{when}  {self.mood}"
        return f"{line}  -- {self.note}" if self.note else line


def check_in(mood: str, *, note: str = "", root: Path | None = None) -> Mood | None:
    """Record a mood. Returns None for a label that is not one of the five.

    Refusing an unknown label is deliberate: the set is closed so that a pattern can be honest about what it counted.
    A free-text mood would make "you have felt lower on Mondays" a claim about a model's reading rather than about
    what the user said.
    """
    label = (mood or "").strip()
    # Case-insensitive match to the canonical spelling, so `low` records as `Low` rather than being refused.
    for known in MOODS:
        if label.lower() == known.lower():
            label = known
            break
    else:
        return None

    path = (root / "moods.json") if root is not None else MOODS_FILE
    entries = _load(path, "moods")
    if len(entries) >= MAX_MOODS:
        del entries[: len(entries) - MAX_MOODS + 1]
    entry = Mood(id=_new_id("mood"), mood=label, note=_clean(note, limit=500), at=time.time(),
                 seq=len(entries))
    entries.append({"id": entry.id, "mood": entry.mood, "note": entry.note, "at": entry.at,
                    "seq": entry.seq})
    _save(path, "moods", entries)
    return entry


def list_moods(*, limit: int = 30, days: int = 0, root: Path | None = None) -> list[Mood]:
    """Check-ins, newest first. `days` limits to the last N days."""
    path = (root / "moods.json") if root is not None else MOODS_FILE
    moods = [
        Mood(id=str(e.get("id") or ""), mood=str(e.get("mood") or ""), note=str(e.get("note") or ""),
             at=float(e.get("at") or 0.0), seq=int(e.get("seq") or 0))
        for e in _load(path, "moods")
    ]
    if days > 0:
        cutoff = time.time() - days * 86_400
        moods = [m for m in moods if m.at >= cutoff]
    moods.sort(key=lambda m: (m.at, m.seq), reverse=True)
    return moods[: max(1, limit)]


def last_mood(*, root: Path | None = None) -> Mood | None:
    found = list_moods(limit=1, root=root)
    return found[0] if found else None


@dataclass
class MoodPattern:
    """An observation about mood over time, with the question that goes with it."""

    enough_data: bool = False
    weekday: str = ""
    lower_on: list[str] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    total: int = 0
    question: str = ""

    def render(self) -> str:
        if not self.enough_data:
            return ""
        if self.lower_on:
            days = " and ".join(self.lower_on)
            return f"You've felt lower on {days} lately. Want to look at that together?"
        return ""


def mood_pattern(*, days: int = 28, minimum: int = 5, root: Path | None = None) -> MoodPattern:
    """Whether the check-ins show a weekday leaning lower.

    Returns an OBSERVATION and a question, never a conclusion -- section 8.2's own example is phrased as an offer.
    `minimum` is the floor below which nothing is reported at all, because a pattern drawn from two entries is a
    coincidence wearing a finding's clothes.
    """
    moods = list_moods(limit=MAX_MOODS, days=days, root=root)
    counts: dict[str, int] = {}
    for mood in moods:
        counts[mood.mood] = counts.get(mood.mood, 0) + 1
    pattern = MoodPattern(counts=counts, total=len(moods))
    if len(moods) < max(2, minimum):
        return pattern
    pattern.enough_data = True

    # Per weekday, how many check-ins were in the lower range versus the total.
    by_day: dict[str, list[int]] = {}
    for mood in moods:
        name = time.strftime("%A", time.localtime(mood.at))
        bucket = by_day.setdefault(name, [0, 0])
        bucket[1] += 1
        if mood.mood in LOWER_MOODS:
            bucket[0] += 1
    # A weekday counts as lower when at least half of its check-ins were, AND it has more than one -- the second
    # condition is what keeps a single bad Monday from becoming a claim about Mondays.
    for name, (lower, total) in by_day.items():
        if total >= 2 and lower * 2 >= total:
            pattern.lower_on.append(name)
    pattern.lower_on.sort()
    pattern.question = pattern.render()
    return pattern


def reflect_mood(*, opted_in: bool = False, days: int = 28, root: Path | None = None) -> MoodPattern:
    """The pattern, but ONLY if the user opted in.

    Section 8.2: "Loci reflects patterns only if the user opts in." Section 8.5 forbids analysing mood data without
    explicit opt-in. So the default is an empty result and the opt-in is a parameter the caller has to pass -- a
    product that decided on the user's behalf would be doing the thing both clauses forbid.
    """
    if not opted_in:
        return MoodPattern(enough_data=False)
    return mood_pattern(days=days, root=root)


# ---------------------------------------------------------------------------------------
# Section 8.3: the listening protocol
# ---------------------------------------------------------------------------------------

# One gentle question per turn, at most. The count is a rule from section 8.2 ("asks at most one gentle question per
# turn"), so it is a named constant rather than a detail of the wording.
MAX_QUESTIONS_PER_TURN = 1

LISTENING_OPENERS = (
    "Do you want ideas, or do you just need to let it out?",
    "Would it help to talk it through, or would you rather sit with it a minute?",
    "Do you want me to think about it with you, or just listen?",
)


def listening_prompt(what_they_said: str, *, offer: str = "") -> str:
    """The prompt for a listening turn, following section 8.3's five steps.

    A PROMPT rather than a reply, because the five steps are: acknowledge, reflect one SPECIFIC detail, ask one
    question or offer a choice, follow their lead, close softly. Steps 1, 2 and 5 need the actual words -- only the
    session's own model has them -- so what this supplies is the SEQUENCE and the constraints, and the constraint that
    matters most is that Loci asks before offering advice.

    `offer` selects one of the three choices; left empty, the prompt asks the model to pick one and says it must be
    one and not several.
    """
    said = (what_they_said or "").strip()
    if not said:
        return ""
    choice = offer.strip() if offer.strip() else "one of: " + " / ".join(LISTENING_OPENERS)
    return (
        "The person shared something difficult. Follow this sequence exactly:\n"
        "1. Acknowledge the feeling in plain words.\n"
        "2. Reflect ONE specific detail they mentioned, so they can tell they were heard. Use their own words for it.\n"
        f"3. Ask at most {MAX_QUESTIONS_PER_TURN} gentle, open question, or offer a choice -- {choice}\n"
        "4. Follow their lead. If they want to vent, keep listening. If they want help, offer ONE small concrete "
        "thing and then stop.\n"
        "5. Close softly. Do not summarise their feelings back as a report, and do not offer a plan unless they "
        "asked for one.\n\n"
        "What they said:\n"
        f"{said}"
    )


# ---------------------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------------------

# 5-4-3-2-1: the counts come from the exercise's name, so they are data rather than five literals in a string.
SENSES = (
    (5, "things you can see"),
    (4, "things you can feel"),
    (3, "things you can hear"),
    (2, "things you can smell"),
    (1, "thing you can taste"),
)

# Box breathing, 4-4-4-4. A pacer with no animation is a list of timings, which is the fallback.
BOX_PHASES = (("Breathe in", 4), ("Hold", 4), ("Breathe out", 4), ("Hold", 4))


@dataclass
class Grounding:
    """One grounding exercise: the steps, and the timing a pacer would need."""

    kind: str
    steps: list[str] = field(default_factory=list)
    phases: list[tuple[str, int]] = field(default_factory=list)
    seconds: int = 0
    intro: str = ""

    def render(self) -> str:
        lines = [self.intro] if self.intro else []
        if self.phases:
            lines.append(f"Box breathing, {sum(p[1] for p in self.phases)} seconds a round:")
            lines.extend(f"  {name} -- {length}s" for name, length in self.phases)
        else:
            lines.extend(f"  {step}" for step in self.steps)
        return "\n".join(lines)


def start_grounding(kind: str = "5-4-3-2-1") -> Grounding:
    """A grounding exercise. `breathe` for box breathing, anything else for 5-4-3-2-1.

    Section 8.2 names Loci's `breathe` animation as the pacer. This harness has no animation, so the TIMING is
    returned and a surface can drive it -- and a surface without one prints the phases, which is still a usable
    exercise rather than a broken feature. Stated rather than pretended.
    """
    if (kind or "").strip().lower() in ("breathe", "box", "box-breathing", "breathing"):
        return Grounding(
            kind="breathe",
            phases=list(BOX_PHASES),
            seconds=sum(length for _name, length in BOX_PHASES),
            intro="Nothing to do for a minute. Follow the phases; there is no way to do this wrong.",
        )
    return Grounding(
        kind="5-4-3-2-1",
        steps=[f"Name {count} {what}." for count, what in SENSES],
        intro="This is about giving your attention somewhere specific to go. Take your time.",
    )


# ---------------------------------------------------------------------------------------
# The remaining helpers: prompts that constrain how the model may respond
# ---------------------------------------------------------------------------------------

REFRAME_PROMPT = (
    "Offer ONE alternative perspective -- as a QUESTION, never as a correction, and never as a reframe of what they "
    "feel. Section 8.2's example is \"Is there another way a friend might see this?\" Do not tell them the kinder "
    "reading is the true one; offer it and stop."
)

OVERWHELM_PROMPT = (
    "They are overwhelmed. Shrink the worry to the NEXT TINY STEP -- one action, small enough to start in the next "
    "ten minutes -- and then STOP. Do not add a second step, do not list what else is pending, and do not mention "
    "anything they have not. Section 8.2: \"Doesn't stack more tasks on an overwhelmed person.\""
)

COMPASSION_PROMPT = (
    "They said something harsh about themselves. Respond to WHICH thing they said, kindly and briefly, and do not "
    "lecture or explain why the self-criticism is irrational. Section 8.2's shape is \"That's a tough thing to say "
    "about yourself. You got through a hard week.\" Do not add advice unless they ask."
)

CONNECTION_PROMPT = (
    "Mention, lightly and once, that the person they mentioned would probably be glad to hear from them. Section 8.2: "
    "\"Loci wants the user to have a life beyond Loci.\" Suggest, do not instruct, and drop it if they are not "
    "interested -- the point is not to hand them a task."
)


def reframe_prompt(what_they_said: str) -> str:
    """A gentle reframe, as a question."""
    said = (what_they_said or "").strip()
    return f"{REFRAME_PROMPT}\n\nWhat they said:\n{said}" if said else ""


def overwhelm_prompt(what_they_said: str) -> str:
    """Shrink a worry to one small step, then stop."""
    said = (what_they_said or "").strip()
    return f"{OVERWHELM_PROMPT}\n\nWhat they said:\n{said}" if said else ""


def compassion_prompt(what_they_said: str) -> str:
    """Answer harsh self-talk kindly and briefly."""
    said = (what_they_said or "").strip()
    return f"{COMPASSION_PROMPT}\n\nWhat they said:\n{said}" if said else ""


def connection_prompt(*, who: str = "") -> str:
    """One light nudge toward a person, dropped if they are not interested."""
    name = (who or "").strip()
    return f"{CONNECTION_PROMPT}\n\nThe person they mentioned: {name}" if name else CONNECTION_PROMPT


# ---------------------------------------------------------------------------------------
# Rest nudges
# ---------------------------------------------------------------------------------------


def is_night(*, when: float | None = None) -> bool:
    """Whether it is the user's winding-down window.

    Section 8.2: "Night mode after the user's set bedtime; suggests winding down; no productivity pushes at night."
    The window wraps midnight, which is the case a naive `hour >= start` comparison gets wrong.
    """
    hour = int(time.strftime("%H", time.localtime(when if when is not None else time.time())))
    if NIGHT_START_HOUR > NIGHT_END_HOUR:
        return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR
    return NIGHT_START_HOUR <= hour < NIGHT_END_HOUR


def rest_nudge(*, when: float | None = None) -> str:
    """The night-mode line, or "" during the day.

    Returning "" rather than a cheerful nothing is what lets a caller use it in a branch without a second check.
    """
    if not is_night(when=when):
        return ""
    return (
        "It's late. Nothing here has to be finished tonight -- want to stop here, or leave a note for tomorrow?"
    )
