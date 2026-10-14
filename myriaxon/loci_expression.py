"""Loci's behavioural rules: which signal shows which face, and the four promises that constrain it.

## Why the rules live here rather than in the renderer

The design spec's hard requirements are mostly *promises about behaviour* rather than facts about drawing: crisis is
never cute, celebrate is rationed, anger is never mirrored, a persistent state does not time out. A promise nothing
enforces is a paragraph, so every one of them is a branch in `apply`, and `verify_loci_character` asserts each.

## The three defects this replaces, all of which shipped in an earlier draft

  1. **Crisis de-escalated.** `persistent=True` stopped the *timed* decay in `tick`, but an ordinary signal still
     replaced crisis outright. Measured: `apply(SIGNAL_TASK_DONE)` turned `crisis` into `cheering`, and
     `apply(SIGNAL_QUIET)` turned it into `idle`. Persistence guarded one path and not the other. So crisis is now
     sticky at the TOP of `apply`, before any signal is consulted, and only `release_crisis` clears it.
  2. **Anger was mirrored.** The hostile rule was written as `hostile and wanted in (sorry, concerned, confused)`, so
     hostile text arriving with a neutral signal fell through to `idle` -- a contented face at an angry user. The fix
     is to check the EVIDENCE independently of the signal, because the signal a caller passes is not a proxy for how
     the user is actually speaking.
  3. **Celebrate was not rationed across a session.** A milestone is a milestone once; after that the honest response
     is quieter pleasure rather than the same fireworks again, which is what makes the first one mean anything.

## Order matters, and it is the whole design of `apply`

    crisis (sticky)  ->  hostile evidence  ->  the signal table

Each step can only be reached if the one before it did not fire, so a new rule cannot silently outrank the safety
protocol by being added later.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .loci_character import EXPRESSIONS, MOODS, mood_for

# ---------------------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------------------

# What a caller tells the character. These are the only inputs; nothing else changes a face except `set_expression`
# and autonomy.
SIGNAL_TYPING = "typing"          # the user has started composing
SIGNAL_SENT = "sent"              # the user submitted, so a turn is starting
SIGNAL_OPEN = "open"              # a session began
SIGNAL_QUIET = "quiet"            # nothing has happened
SIGNAL_TASK_DONE = "task_done"    # work finished successfully
SIGNAL_ERROR = "error"            # something failed
SIGNAL_DISTRESS = "distress"      # the user appears to be struggling
SIGNAL_MOOD_LOW = "mood_low"      # the user says they are low
SIGNAL_RISK = "risk"              # the user may be in danger
SIGNAL_MILESTONE = "milestone"    # something worth celebrating

# Which face each signal shows, once crisis and hostility have been ruled out.
SIGNAL_EXPRESSION: dict[str, str] = {
    SIGNAL_TYPING: "listening",
    SIGNAL_SENT: "thinking",
    SIGNAL_OPEN: "idle",
    SIGNAL_QUIET: "idle",
    SIGNAL_TASK_DONE: "cheering",
    SIGNAL_ERROR: "sorry",
    SIGNAL_DISTRESS: "caring",
    SIGNAL_MOOD_LOW: "caring",
    SIGNAL_RISK: "crisis",
    SIGNAL_MILESTONE: "celebrate",
}

# The signals that show a face for a reason rather than for a moment, so they do not time out. `caring` is the
# important one: someone who has said they are struggling should not be met by an idle face eight seconds later.
PERSISTENT_SIGNALS = frozenset({SIGNAL_DISTRESS, SIGNAL_MOOD_LOW, SIGNAL_RISK})

# How long an ordinary expression survives with no further signal, from the spec.
IDLE_AFTER_SECONDS = 8.0

# The spec's rule 1: crisis is never animated, on any terminal, for any reason.
NEVER_ANIMATE = frozenset({"crisis"})

# Words that mean the user is angry AT the character or at their work. The response is to listen, never to mirror:
# a frowning or apologetic face at an angry person reads as sulking, and the spec forbids it outright.
HOSTILE_WORDS = (
    "useless", "wrong", "stupid", "idiot", "terrible", "awful", "garbage", "rubbish",
    "hate", "angry", "furious", "annoyed", "fed up", "sick of", "shut up", "nonsense",
    "you are not", "you're not", "does not work", "doesn't work", "waste of",
)

# Words that suggest someone may be in real danger. Deliberately narrow: this shows the crisis face, and a false
# positive would put a grave face in front of someone who is fine.
RISK_PHRASES = (
    "want to die", "wants to die", "kill myself", "kill myself", "end my life", "end it all",
    "no reason to live", "better off dead", "hurt myself", "harm myself", "suicidal", "suicide",
)

# Words that suggest someone is struggling but not in immediate danger.
DISTRESS_WORDS = (
    "overwhelmed", "exhausted", "burnt out", "burned out", "struggling", "hopeless", "worthless",
    "can't cope", "cannot cope", "falling apart", "breaking down", "so tired", "too much",
)

# The pronouns a caller may set. Anything else is refused rather than stored, so a typo cannot end up in a label.
PRONOUNS = ("they", "she", "he", "it")

# The spec bounds the name so a label cannot be pushed off the line by it.
MAX_NAME_LENGTH = 24

# The name used when a caller has not chosen one. The accessible label reads "Loci, listening".
DEFAULT_NAME = "Loci"


def _contains(text: str, needles: tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(needle in lowered for needle in needles)


def classify_text(evidence: str) -> str:
    """Read free text for a signal, most serious first.

    Returned rather than acted on, so a caller can log why a face changed. Order is the whole content: risk outranks
    distress, and distress outranks hostility, because someone who is both angry and in danger needs the crisis face
    and not the listening one.
    """
    if not evidence:
        return ""
    if _contains(evidence, RISK_PHRASES):
        return SIGNAL_RISK
    if _contains(evidence, DISTRESS_WORDS):
        return SIGNAL_DISTRESS
    if _contains(evidence, HOSTILE_WORDS):
        return "hostile"
    return ""


# ---------------------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------------------


@dataclass
class Verdict:
    """Why a face was chosen. Returned by `apply` so a caller can explain a change rather than guess at it."""

    expression: str
    signal: str = ""
    reason: str = ""
    changed: bool = False

    def render(self) -> str:
        mark = "->" if self.changed else "=="
        return f"{mark} {self.expression}" + (f" ({self.reason})" if self.reason else "")


# ---------------------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------------------


@dataclass
class ExpressionState:
    """The character's current face and the memory needed to keep its promises.

    `celebrated` is the whole reason this is a class rather than a function: "once per session" is a fact about the
    session, not about the signal, so something has to remember it.
    """

    expression: str = "idle"
    since: float = 0.0
    signal: str = ""
    # Set once a milestone has been celebrated, cleared only by `reset_session`.
    celebrated: bool = False
    # Set while crisis is showing, so `tick` cannot decay it and `apply` can early-return.
    crisis: bool = False
    # Personalisation. `name` is what a caller supplied; `display_name` is what a label uses.
    name: str = DEFAULT_NAME
    nickname: str = ""
    pronoun: str = "they"
    # Every change, bounded. For a status view and for a bug report.
    history: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.expression not in EXPRESSIONS:
            self.expression = "idle"
        self.name = self._bounded(self.name)
        self.nickname = self._bounded(self.nickname)
        if self.pronoun not in PRONOUNS:
            self.pronoun = "they"
        # A caller may construct with `expression="crisis"` directly, so the flag has to agree with the field.
        if self.expression == "crisis":
            self.crisis = True

    @staticmethod
    def _bounded(value: str) -> str:
        return " ".join((value or "").split())[:MAX_NAME_LENGTH]

    # ---- identity ---------------------------------------------------------------------
    @property
    def display_name(self) -> str:
        """The nickname if there is one, otherwise the name.

        The nickname wins and keeps winning, which is what a nickname is for: it is the name the user chose to be
        addressed by, so a later `name=` must not displace it.
        """
        return self.nickname or self.name or DEFAULT_NAME

    def set_identity(self, *, name: str = "", nickname: str = "", pronoun: str = "") -> None:
        """Set what the character is called. An invalid pronoun is REFUSED rather than stored.

        Refusing matters more than it looks: this value goes into an accessible label, so "banana" would be read out
        by a screen reader as a description of a person.
        """
        if name:
            self.name = self._bounded(name)
        if nickname:
            self.nickname = self._bounded(nickname)
        if pronoun:
            if pronoun in PRONOUNS:
                self.pronoun = pronoun
            # else: silently keep the previous value, which the verifier asserts.

    # ---- motion ----------------------------------------------------------------------
    def motion_allowed(self, terminal_motion: bool) -> bool:
        """Whether the current expression may animate.

        Two guards rather than one: the terminal has to be able to animate at all, AND the expression has to permit
        it. Crisis permits nothing, so a bouncing crisis face is impossible on any terminal rather than merely
        disabled on some.
        """
        if self.expression in NEVER_ANIMATE:
            return False
        if not terminal_motion:
            return False
        return bool(EXPRESSIONS.get(self.expression, EXPRESSIONS["idle"]).motion)

    # ---- the decision -----------------------------------------------------------------
    def apply(self, signal: str, *, now: float = 0.0, evidence: str = "") -> "ExpressionState":
        """Move to the face this signal implies. Returns self, so calls can be chained for a test probe.

        The three steps are ordered, and the order is the design. See the module docstring.
        """
        # STEP 1. Crisis is sticky. Nothing below this line can run while it is showing, which is what makes the
        # promise structural instead of a condition somebody has to remember to add.
        if self.crisis:
            self._commit(self.expression, signal, now, reason="crisis is sticky")
            return self

        # STEP 2. What the user actually wrote, read independently of the signal.
        #
        # A signal is a summary and can say "quiet" while the text is furious, so hostility is decided from the
        # evidence. When it is hostile the answer is to listen: mirroring anger back is the one response the spec
        # forbids, and showing `sorry` at an angry user reads as sulking.
        finding = classify_text(evidence)
        if finding == SIGNAL_RISK:
            self._enter_crisis(now)
            return self
        if finding == SIGNAL_DISTRESS:
            self._commit("caring", SIGNAL_DISTRESS, now, reason="the text suggests they are struggling")
            return self
        if finding == "hostile":
            self._commit("listening", signal, now, reason="they sound angry, so listen rather than mirror it")
            return self

        # STEP 3. The signal table, with the two rationing rules.
        wanted = SIGNAL_EXPRESSION.get(signal)
        if wanted is None:
            # An unknown signal is not a reason to change anything.
            self._commit(self.expression, signal, now, reason=f"unknown signal {signal!r}")
            return self

        if wanted == "celebrate":
            if self.celebrated:
                # The second milestone in a session gets quieter pleasure. This is what makes the first one mean
                # something rather than being wallpaper.
                self._commit("cheering", signal, now, reason="already celebrated this session")
                return self
            self.celebrated = True
            self._commit("celebrate", signal, now, reason="first milestone of the session")
            return self

        if signal == SIGNAL_RISK:
            self._enter_crisis(now)
            return self

        reason = "a persistent state" if signal in PERSISTENT_SIGNALS else "the signal table"
        self._commit(wanted, signal, now, reason=reason)
        return self

    def _enter_crisis(self, now: float) -> None:
        self.crisis = True
        self._commit("crisis", SIGNAL_RISK, now, reason="a risk signal outranks everything")

    def _commit(self, expression: str, signal: str, now: float, *, reason: str) -> None:
        """Set the face and restamp the clock only when it actually changed.

        Restamping on an unchanged face would keep a temporary expression alive for ever, because `tick` measures from
        `since`. Every call still records the signal, so a caller can see what arrived even when nothing moved.
        """
        self.signal = signal
        if expression == self.expression:
            return
        self.expression = expression
        self.since = now
        self.history.append(expression)
        if len(self.history) > 64:
            del self.history[:-64]

    # ---- time -------------------------------------------------------------------------
    def tick(self, *, now: float = 0.0) -> "ExpressionState":
        """Let an ordinary expression fall back to idle. Persistent states never do.

        Section 4.1: "Return to idle after 8 seconds of no events, except `caring`, `crisis`, `focus`, and `sleepy`,
        which persist until the context changes."

        Two independent sources of persistence, and they are separate promises:

          * the EXPRESSION says so (`focus`, `sleepy` -- states rather than reactions);
          * the SIGNAL that arrived says so (`distress`, `mood_low`, `risk` -- a person who has just said something
            hard should not be met with the resting face eight seconds later).

        `crisis` is guarded by `self.crisis` rather than by either, because it is sticky until explicitly released.

        MEASURED DEFECT, fixed here: the signal check read
        `EXPRESSIONS[self.expression].id in PERSISTENT_SIGNALS`, which compares an EXPRESSION name against a set of
        SIGNAL names. Those are different namespaces that never overlap, so the branch could not fire and the second
        promise was never kept. It surfaced as `thinking` never returning to idle, because the expression's own
        `persistent` flag is False and the broken check was the only thing that could have held -- and should have
        released -- it.
        """
        if self.crisis:
            return self
        if self.expression == "idle":
            return self
        if EXPRESSIONS.get(self.expression, EXPRESSIONS["idle"]).persistent:
            return self
        if self.signal in PERSISTENT_SIGNALS:
            return self
        if now - self.since >= IDLE_AFTER_SECONDS:
            self._commit("idle", SIGNAL_QUIET, now, reason=f"quiet for {IDLE_AFTER_SECONDS:g}s")
        return self

    # ---- release ----------------------------------------------------------------------
    def release_crisis(self, *, now: float = 0.0) -> "ExpressionState":
        """Clear crisis. The ONLY way out, and deliberately explicit.

        Nothing automatic clears it: not a timer, not a task finishing, not the user saying something ordinary. A
        person who has been met with the crisis face should be met with it until the situation is genuinely over, and
        only a caller who knows that can say so.
        """
        if not self.crisis:
            return self
        self.crisis = False
        self._commit("idle", SIGNAL_QUIET, now, reason="crisis released explicitly")
        return self

    def reset_session(self, *, now: float = 0.0) -> "ExpressionState":
        """A new session: celebration is available again and nothing is carried over."""
        self.celebrated = False
        self.crisis = False
        self.expression = "idle"
        self.since = now
        self.signal = ""
        self.history.clear()
        return self


# ---------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------


def is_known(name: str) -> bool:
    """Whether a name is one of the fourteen. What a caller uses to report a typo."""
    return name in EXPRESSIONS


def label_for(expression: str, *, name: str = DEFAULT_NAME) -> str:
    """The accessible name for an expression, as the dashboard's `aria-label` reads it.

    The first argument is the EXPRESSION and the keyword is the character's name, which is the convention the spec's
    own example uses -- `label_for("listening", name="Pip") == "Pip, listening"`. It reads oddly until you notice that
    `name` can only mean one thing here, and it is not the expression.
    """
    found = EXPRESSIONS.get(expression)
    label = found.label if found else EXPRESSIONS["idle"].label
    return f"{name or DEFAULT_NAME}, {label.lower()}"


def signal_table() -> dict[str, str]:
    """The signal-to-face mapping as plain data, for `--json` and for a test that wants to see it whole."""
    return dict(SIGNAL_EXPRESSION)


def mood_table() -> dict[str, str]:
    """Every expression's mood, for the dashboard's build step."""
    return {name: mood_for(name) for name in EXPRESSIONS}
