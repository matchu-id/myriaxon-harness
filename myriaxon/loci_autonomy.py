"""Autonomy: what Loci does when nobody is telling it anything.

## The difference between a reactive character and an autonomous one

Everything else in this package is reactive. The session says "a turn started", the session says "a turn failed",
and the face moves. A reactive character is a *display*: it shows the last thing it was told, and if it is told
nothing it shows nothing for ever.

Autonomy means the character has its own reasons to change. It gets bored. It winds down in the evening. It notices
you have gone quiet. None of that is in the transcript -- it is in the character's own state, and the character is the
only thing that can decide it.

## How the decision is made

Five **motivations** -- social, rest, stimulation, diligence, care -- each rise on their own timescale, and each has a
threshold. At every poll the motivation with the greatest *pressure over its threshold* wins and its behaviour fires.
A behaviour that fires satisfies its motivation, which is what stops a loop of the same expression.

A rule list cannot arbitrate this: "it is late" and "it has been busy" are both true at 23:00 after a long day, and
something has to win. Comparing pressures makes that a number rather than an ordering problem, and the number is
inspectable through `status`.

## The property that took four rewrites, and is the reason this is testable at all

**The sequence of changes does not depend on how often `poll` is called.** A host ticking once a second and a host
ticking once an hour observe the same character doing the same things in the same order.

Each earlier version was wrong in an instructive way, and the wrong versions all looked plausible:

  1. test the threshold after adding `rate * elapsed` -- on a coarse tick every motivation leapt past its threshold
     inside one step, so EVERY poll fired: 288 changes in a day, one per step;
  2. add a cooldown -- no help, because it was compared against a timestamp that crept forward while the clock jumped;
  3. solve for the crossing TIME, one crossing per poll -- correct arithmetic, but a coarse tick skipped everything
     between its endpoints: 594 changes at a 1s tick against 0 at a 300s tick;
  4. loop over the crossings inside one poll -- right idea, still tick-dependent, because `poll` returned only the
     LAST firing and every earlier one in the window was dropped on the floor.

The resolution is to stop letting the caller's cadence into the simulation. `poll` advances in fixed-size sub-steps
until it has consumed the elapsed window, and every firing goes into a buffer that `run_for` drains. A tick larger
than the granularity is split; a tick smaller than it is accumulated.

## There is deliberately no randomness

A seeded jitter was added so the character would feel less mechanical, and it changed nothing: four seeds produced
byte-identical sequences AND identical pressures, because the delay was applied to a timestamp the grid-determined
crossing then cancelled. Rather than keep an `rng` parameter whose only effect is to make a caller believe the output
varies, it is gone -- and `verify_loci_autonomy` asserts its ABSENCE, because a test saying "the same seed gives the
same result" passed on that code and proved nothing at all.

## Two hard rules, in code rather than in a comment

**`crisis` is never a candidate.** A character that could wander into the safety face on its own would show a crisis
face to someone who is fine, which is worse than any amount of boredom. **`celebrate` is never a candidate either**,
for a different reason: it is rationed to a real milestone, and a timer is not one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .loci_character import EXPRESSIONS
from .loci_expressions_more import EXTRA_EXPRESSIONS

# The set autonomy may choose from: the spec's faces PLUS the extensions.
#
# Measured defect: the first version validated against `EXPRESSIONS` alone -- the fourteen spec faces. Every
# autonomous behaviour uses an extension (`waiting`, `bored`, `dozy`, `steady`, `mindful`), so the guard refused all
# five and Loci made ZERO autonomous changes in a simulated day. The reason string it produced was even
# correct-looking, which is why the bug survived a read-through: a guard that refuses everything looks like a guard
# that is working.
CHOOSABLE = frozenset(set(EXPRESSIONS) | set(EXTRA_EXPRESSIONS))


# ---------------------------------------------------------------------------------------
# Personality
# ---------------------------------------------------------------------------------------


@dataclass
class Personality:
    """What kind of character this is. Weights are multipliers on a motivation's pressure.

    Clamped toward 1.0 whatever a caller supplies, so a trait can make a motivation more or less likely but can never
    make it unreachable. A weight of zero would mean "this character never rests", which is a bug rather than a
    personality.
    """

    social: float = 1.0          # wants to interact, notices quiet
    rest: float = 1.0            # wants to wind down
    stimulation: float = 1.0     # wants something to do
    diligence: float = 1.0       # wants to keep working
    care: float = 1.0            # wants to check on the person
    name: str = ""

    def weight(self, motivation: str) -> float:
        raw = float(getattr(self, motivation, 1.0))
        return max(0.25, min(4.0, raw))

    def describe(self) -> str:
        traits = []
        for motivation, label in (("social", "sociable"), ("rest", "restful"), ("stimulation", "curious"),
                                  ("diligence", "diligent"), ("care", "caring")):
            weight = self.weight(motivation)
            if weight >= 1.35:
                traits.append(f"very {label}")
            elif weight >= 1.1:
                traits.append(label)
            elif weight <= 0.65:
                traits.append(f"not {label}")
        return ", ".join(traits) if traits else "even-tempered"


# The presets, so a user picks a character rather than five numbers. The names are the personality, not a mood.
PERSONALITIES: dict[str, Personality] = {
    "steady": Personality(),
    "warm": Personality(social=1.6, care=1.7, stimulation=0.8, rest=1.1),
    "curious": Personality(stimulation=1.8, social=1.2, rest=0.8, care=0.9),
    "diligent": Personality(diligence=1.8, rest=0.7, stimulation=1.1),
    "calm": Personality(rest=1.7, social=0.7, stimulation=0.6, care=1.1),
    "lively": Personality(stimulation=1.6, social=1.5, rest=0.6, diligence=1.1),
}


def personality_for(name: str) -> Personality:
    """A preset by name, or the default. An unknown name falls back rather than raising."""
    preset = PERSONALITIES.get((name or "").strip().lower())
    if preset is None:
        return Personality()
    # A COPY. Without this, a caller mutating the returned object would corrupt the preset for every later session --
    # and `verify_loci_autonomy` asserts the copy, because that is a bug nobody would find by reading the call site.
    return Personality(**dict(preset.__dict__))


# ---------------------------------------------------------------------------------------
# Motivations
# ---------------------------------------------------------------------------------------


@dataclass
class Motivation:
    """One drive. Its `pressure` is compared against every other drive's at each poll."""

    name: str
    pressure: float = 0.0
    # Pressure added per second. Small by construction: these fire over minutes, not seconds.
    rate: float = 0.0
    threshold: float = 1.0
    # The expression and signal applied when this motivation wins.
    expression: str = "idle"
    signal: str = ""
    # How much pressure is left after firing. Zero means satisfied; a high value means it nags.
    relief: float = 1.0

    def excess(self, personality: Personality) -> float:
        """How far over the line this motivation is, weighted by personality. Negative means not yet."""
        if self.threshold <= 0:
            return self.pressure * personality.weight(self.name)
        return (self.pressure - self.threshold) * personality.weight(self.name)


def default_motivations() -> dict[str, Motivation]:
    """The five drives, tuned so a quiet session sees a change every few minutes.

    The rates are the tuning surface. Too fast and the character fidgets; too slow and it looks frozen.
    """
    return {
        "social": Motivation("social", rate=1.0 / 260.0, threshold=1.0,
                             expression="waiting", signal="quiet", relief=1.0),
        "rest": Motivation("rest", rate=1.0 / 900.0, threshold=1.0,
                           expression="dozy", signal="night", relief=1.0),
        "stimulation": Motivation("stimulation", rate=1.0 / 300.0, threshold=1.0,
                                  expression="bored", signal="quiet", relief=1.0),
        "diligence": Motivation("diligence", rate=1.0 / 1500.0, threshold=1.0,
                                expression="steady", signal="quiet", relief=0.85),
        "care": Motivation("care", rate=1.0 / 1200.0, threshold=1.0,
                           expression="mindful", signal="quiet", relief=1.0),
    }


# Expressions autonomy is FORBIDDEN to choose, whatever a caller configures.
NEVER_AUTONOMOUS = frozenset({"crisis", "celebrate"})

# Behaviour name -> the expression it selects. Each reads as "the character's own state" rather than a reaction.
BEHAVIOURS: dict[str, str] = {
    "social": "waiting",
    "rest": "dozy",
    "stimulation": "bored",
    "diligence": "steady",
    "care": "mindful",
}


# ---------------------------------------------------------------------------------------
# The choice
# ---------------------------------------------------------------------------------------


@dataclass
class AutonomousChoice:
    """Why Loci changed. Every field exists so a behaviour can be explained rather than merely observed."""

    expression: str = ""
    motivation: str = ""
    reason: str = ""
    pressure: float = 0.0
    # The full table at decision time, so a caller can see what lost as well as what won.
    standings: list[tuple[str, float]] = field(default_factory=list)

    @property
    def fired(self) -> bool:
        return bool(self.expression)

    def render(self) -> str:
        if not self.fired:
            return f"nothing fired ({self.reason})"
        runners = ", ".join(f"{name}={value:.2f}" for name, value in self.standings[:3])
        return f"{self.motivation} chose {self.expression} ({self.reason}); pressure {self.pressure:.2f} vs {runners}"


# ---------------------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------------------


@dataclass
class Autonomy:
    """The character's own reasons to change.

    `poll` is called on whatever loop the host has, and returns the LAST thing that fired. `run_for` drives the clock
    headlessly and returns everything, which is what a verifier and a demo use.
    """

    personality: Personality = field(default_factory=Personality)
    motivations: dict[str, Motivation] = field(default_factory=default_motivations)
    enabled: bool = True
    # Injected so a verifier can drive a whole day in microseconds.
    clock: object = None
    # Set while a human is driving. No autonomous behaviour may fire.
    holds: int = 0
    last_poll: float = 0.0
    last_fired: float = 0.0
    # Never fire twice in a row faster than this, however high a pressure gets.
    min_gap_seconds: float = 45.0
    # The simulation step. `poll` sub-steps in units of this whatever the host's cadence, which is what makes the
    # observed sequence independent of the host. Twenty seconds sits well below the shortest cooldown, so a suppressed
    # crossing is always reconsidered within the same poll rather than skipped.
    granularity: float = 20.0
    # Circadian phase hook: `None` reads the real clock, a number forces the hour so 3am is testable.
    hour: float | None = None
    # Set when the user has just been active, which suppresses rest and social for a while.
    busy: bool = False
    # Pressure ceiling: high enough that a long quiet stretch still crosses, low enough to stay readable.
    ceiling: float = 8.0
    # The longest gap that is SIMULATED rather than skipped. A character that was not running did not have a day of
    # feelings, so a host returning after a long pause resumes from the present instead of replaying the gap.
    max_catch_up_seconds: float = 3600.0
    # Every choice made, bounded, for a status view.
    log: list[AutonomousChoice] = field(default_factory=list)
    # Firings since the last `drain`.
    pending: list[AutonomousChoice] = field(default_factory=list)
    # How many changes have ever fired.
    crossings: int = 0

    def __post_init__(self) -> None:
        if self.clock is None:
            self.clock = time.time
        if not self.last_poll:
            self.last_poll = self._now()

    # ---- the clock -------------------------------------------------------------------
    def _now(self) -> float:
        return float(self.clock())

    def _circadian(self, now: float | None = None) -> float:
        """How strongly `rest` should be rising, from the local hour.

        A smooth bump centred on 22:30 with a half-width of about five hours, so it is near zero through the working
        day and peaks late in the evening. `hour` overrides the real clock, which is how the verifier tests 3am without
        waiting until 3am.
        """
        if self.hour is not None:
            hour = float(self.hour) % 24.0
        else:
            hour = time.localtime(now if now is not None else self._now()).tm_hour
        distance = min(abs(hour - 22.5), 24 - abs(hour - 22.5))
        if distance >= 5:
            return 0.0
        return 1.0 - (distance / 5.0)

    # ---- what the host tells it ------------------------------------------------------
    def hold(self) -> None:
        """Stop autonomous changes: a human is driving."""
        self.holds += 1

    def release(self) -> None:
        """Let them resume. Forgiveness on release, so a long turn does not end in an instant twitch."""
        self.holds = max(0, self.holds - 1)
        self.last_fired = self._now()

    def note_activity(self, *, busy: bool = True) -> None:
        """The user did something. Satisfies social and stimulation; feeds diligence."""
        self.busy = busy
        if busy:
            self.motivations["social"].pressure = 0.0
            self.motivations["stimulation"].pressure = 0.0
        else:
            self.motivations["diligence"].pressure = min(
                2.0, self.motivations["diligence"].pressure + 0.35)

    def note_hard_turn(self) -> None:
        """The turn was difficult, so care rises faster for a while."""
        self.motivations["care"].pressure = min(2.0, self.motivations["care"].pressure + 0.5)

    # ---- rates -----------------------------------------------------------------------
    def _rates(self) -> dict[str, float]:
        """Per-second rates for this poll, including the circadian and busy adjustments."""
        boost = 1.0 + 2.5 * self._circadian()
        rates: dict[str, float] = {}
        for name, motivation in self.motivations.items():
            rate = motivation.rate
            if name == "rest":
                rate *= boost
            elif name == "diligence" and not self.busy:
                # Nothing is happening, so there is no progress to keep up with.
                rate *= 0.25
            elif name == "stimulation" and self.busy:
                rate *= 0.3
            rates[name] = rate
        return rates

    def _standings(self, elapsed: float, rates: dict[str, float]) -> list[tuple[str, float]]:
        return sorted(
            ((name, self.motivations[name].excess(self.personality)) for name in self.motivations),
            key=lambda pair: pair[1],
            reverse=True,
        )

    # ---- the decision -----------------------------------------------------------------
    def poll(self, *, now: float | None = None) -> AutonomousChoice:
        """Advance to `now`, return the LAST thing that fired, and leave every firing on the buffer."""
        moment = self._now() if now is None else float(now)

        if self.holds:
            self.last_poll = moment
            return AutonomousChoice(reason=f"held by the session ({self.holds})")
        if not self.enabled:
            self.last_poll = moment
            return AutonomousChoice(reason="autonomy is off")

        total = max(0.0, moment - self.last_poll)
        # The budget is derived from the WINDOW, not a fixed number. Measured: a fixed budget of 4096 was exhausted on
        # a long window, so `poll` returned early and simply did not simulate the rest -- which is why a 60-second tick
        # produced 791 changes against 802, and a 1200-second tick only 72. A cap that silently truncates the
        # simulation is worse than no cap, because the result still looks like a result.
        window = min(total, self.max_catch_up_seconds)
        start = moment - window
        steps = int(window / max(1e-9, self.granularity)) + 2
        consumed = 0.0
        last: AutonomousChoice = AutonomousChoice(reason="nothing crossed its threshold")
        while consumed < window - 1e-9 and steps > 0:
            step = min(self.granularity, window - consumed)
            consumed += step
            steps -= 1
            choice = self._advance(start + consumed, step)
            if choice.fired:
                last = choice
        self.last_poll = moment
        return last

    def _advance(self, at: float, elapsed: float) -> AutonomousChoice:
        """One fixed-size sub-step. The caller has already handled holds and the disabled state."""
        rates = self._rates()
        baseline = {name: m.pressure for name, m in self.motivations.items()}

        # A crossing may already be sitting at the baseline, from a cooldown that has since expired.
        crossings: list[tuple[float, str]] = []
        for name, motivation in self.motivations.items():
            weight = self.personality.weight(name)
            rate = rates[name] * weight
            if rate <= 0:
                continue
            remaining = motivation.threshold - baseline[name]
            crosses_at = 0.0 if remaining <= 0 else remaining / rate
            if crosses_at <= elapsed:
                crossings.append((crosses_at, name))

        # Advance every meter over the whole sub-step FIRST, so a sub-step with no crossing still moves time on.
        for name, meter in self.motivations.items():
            meter.pressure = min(self.ceiling, baseline[name] + rates[name] * elapsed)

        if not crossings:
            return AutonomousChoice(reason="no crossing in this sub-step",
                                    standings=self._standings(elapsed, rates))

        crossings.sort()
        offset, winner = crossings[0]
        instant = at - elapsed + offset

        if instant < self.last_fired + self.min_gap_seconds:
            return AutonomousChoice(reason="suppressed by the cooldown",
                                    standings=self._standings(elapsed, rates))

        motivation = self.motivations[winner]
        expression = motivation.expression
        if expression in NEVER_AUTONOMOUS or expression not in CHOOSABLE:
            # Satisfied rather than retried, or a misconfigured behaviour would swallow every sub-step for ever.
            motivation.pressure = max(0.0, motivation.pressure - motivation.relief)
            return AutonomousChoice(reason=f"{expression!r} may not be chosen autonomously",
                                    standings=self._standings(elapsed, rates))

        self.crossings += 1
        self.last_fired = instant
        motivation.pressure = max(0.0, motivation.pressure - motivation.relief)
        choice = AutonomousChoice(
            expression=expression,
            motivation=winner,
            reason=self._explain(winner, expression),
            pressure=motivation.excess(self.personality),
            standings=self._standings(elapsed, rates),
        )
        self.log.append(choice)
        if len(self.log) > 64:
            del self.log[:-64]
        # Every firing goes on the buffer. `poll` returns the LAST one because a host loop wants the current state;
        # `drain` is how a headless run gets them all.
        self.pending.append(choice)
        return choice

    def _explain(self, motivation: str, expression: str) -> str:
        when = f"h={self.hour:g}" if self.hour is not None else "now"
        return {
            "social": f"it has been quiet for a while ({when})",
            "rest": f"the hour is late ({when})",
            "stimulation": f"nothing has happened for a while ({when})",
            "diligence": f"work has been going on a long time ({when})",
            "care": f"it has been a while since it checked in ({when})",
        }.get(motivation, f"{motivation} reached its threshold")

    # ---- collecting -------------------------------------------------------------------
    def drain(self) -> list[AutonomousChoice]:
        """Take every change since the last drain, in order. For a headless simulation."""
        taken = list(self.pending)
        self.pending.clear()
        return taken

    def run_for(self, seconds: float, *, step: float | None = None,
                start: float | None = None) -> list[AutonomousChoice]:
        """Drive the clock forward and return every change. For a demo and for a verifier.

        Deliberately NOT how a host integrates -- a host calls `poll` on its own loop. This exists so a whole day can be
        simulated in microseconds.

        The step AND the total are snapped to the simulation grid. Measured: snapping the step alone left the LAST
        partial step off-grid (86400 is not a multiple of 60), so a 60-second run diverged from a 20-second one on the
        final tick. The character had not changed; the observation had.
        """
        grid = max(1e-6, self.granularity)
        requested = grid if step is None else max(grid, float(step))
        snapped = max(1, round(requested / grid)) * grid
        total = max(1, round(max(0.0, float(seconds)) / grid)) * grid

        # Anchored to `last_poll`, NOT to `self._now()`. Measured: using the real clock for the start while `last_poll`
        # had also been initialised from real time put the two timelines about a decade apart, so the cooldown
        # suppressed everything and a 30-minute simulation produced ZERO changes -- a plausible-looking result (a calm
        # character) from a broken clock.
        moment = self.last_poll if start is None else float(start)
        out: list[AutonomousChoice] = []
        remaining = total
        chunk = min(snapped, self.max_catch_up_seconds) if self.max_catch_up_seconds > 0 else snapped
        while remaining > 1e-9:
            advance = min(chunk, remaining)
            moment += advance
            remaining -= advance
            self.poll(now=moment)
            # Collect from the BUFFER, not from `poll`'s return value: one poll can contain several firings, and
            # taking only the last made a coarse step look like a quieter character.
            out.extend(self.drain())
        return out

    # ---- introspection ----------------------------------------------------------------
    def status(self) -> dict:
        """The pressure table, for a status view or a bug report."""
        return {
            "enabled": self.enabled,
            "held": self.holds,
            "personality": self.personality.describe(),
            "hour": self.hour if self.hour is not None else time.localtime().tm_hour,
            "circadian": round(self._circadian(), 2),
            "pressures": {name: round(m.pressure, 3) for name, m in sorted(self.motivations.items())},
            "excess": {name: round(m.excess(self.personality), 3)
                       for name, m in sorted(self.motivations.items())},
            "fired": len(self.log),
        }

    def reset(self) -> None:
        """Back to a fresh, satisfied character. For a new session."""
        for motivation in self.motivations.values():
            motivation.pressure = 0.0
        self.last_fired = 0.0
        self.last_poll = self._now()
        self.log.clear()
        self.pending.clear()
