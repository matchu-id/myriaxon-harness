"""The live wiring: one `Character` per agent, driven by the real turn path.

## The failure this file exists to prevent

Every mechanism in the Loci layer can be correct, verified, and shown to nobody. That is this repository's signature
failure mode and it happened here: the manifest, the state machine, the surfaces and the SVG were all built and all
passing while **nothing called them**, so a real session never changed the face. A character needs a caller.

So this module is deliberately thin. It owns no rendering rules -- those live in `loci_character` and `loci_surface`
-- and no behavioural rules, which live in `loci_expression`. What it owns is the *sequence*: when a turn starts, when
it ends, what counts as work, and which expression follows. That sequence is what a session actually experiences.

## One character per agent, not one per process

The dashboard runs several sessions in one process. A shared character would have them fighting over one face -- one
session's error turning another session's expression sad. `for_agent` keys on the agent object's identity, so two
sessions get two characters and asking twice returns the same one.

## It must never raise

Every call site is inside a turn. A character that can raise is worse than one showing the wrong face, so every
`after_turn` path is guarded and the whole thing degrades to `idle` rather than to an exception. The agent it is
handed is arbitrary -- a real agent, a test double, or an object whose attributes raise on access -- and the character
has to survive all of them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import loci_expression as expression
from .loci_character import EXPRESSIONS, Terminal, detect_terminal
from .loci_surface import prompt_prefix as _prompt_prefix
from .loci_surface import status_fragment as _status_fragment

# Stop reasons that mean something went wrong in a way worth a face.
ERROR_REASONS = frozenset({"error", "failed", "exception"})
# Reasons that mean the turn ran out of room rather than finished. The face is concern, not failure, because nothing
# broke -- the work is simply unfinished, and a `sorry` face would misreport that.
BUDGET_REASONS = frozenset({"step_budget", "token_budget", "time_budget", "max_steps", "max_tokens"})
# Reasons that mean the turn did not happen. Deliberately no face: a cancelled turn is not an event in the session's
# life, and reacting to it would make the character twitch whenever someone pressed Escape.
QUIET_REASONS = frozenset({"cancelled", "canceled", "aborted", "interrupted"})

# The signal a session opens on. `idle`'s own glyphs with a warmer mouth.
SIGNAL_OPEN = "open"


def _registry() -> dict:
    """The per-agent character map, created on first use.

    A plain dict keyed by `id(object)`. It holds a strong reference to each agent, which is correct here: a character
    lives exactly as long as the session it belongs to, and a weak map would let the character vanish mid-turn.
    """
    global _REGISTRY
    try:
        return _REGISTRY
    except NameError:  # pragma: no cover - only on the very first call
        _REGISTRY = {}
        return _REGISTRY


_REGISTRY: dict = {}


def _did_work(agent) -> bool:
    """Whether the turn actually did something, measured by TOOLS RUN rather than by words spoken.

    Four shapes for one event, and every one of them was a separate defect:

      * Anthropic SDK delivers content blocks as **objects**, so `isinstance(block, dict)` misses them entirely;
      * the same protocol can arrive as plain dicts when a transcript is re-read from disk;
      * this host's default OpenAI-compatible path records `tool_calls` on the assistant message;
      * and it records the result as a separate `role: "tool"` message.

    A model that TALKED about running a tool has not run one, which is why this looks at the transcript rather than at
    the reply text.
    """
    try:
        transcript = getattr(agent, "messages", None)
    except Exception:  # noqa: BLE001 - an agent whose attributes raise counts as "no evidence"
        return False
    if not isinstance(transcript, (list, tuple)):
        return False
    for message in transcript:
        if not isinstance(message, dict):
            # A transcript of junk is not evidence of work, and must not raise.
            continue
        if message.get("role") == "tool":
            return True
        if message.get("tool_calls"):
            return True
        content = message.get("content")
        if not isinstance(content, (list, tuple)):
            continue
        for block in content:
            # Object shape first: the SDK's blocks are not dicts.
            if getattr(block, "type", None) == "tool_use":
                return True
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return True
    return False


@dataclass
class Character:
    """One session's character. See the module docstring for why the sequence lives here."""

    state: expression.ExpressionState = field(default_factory=expression.ExpressionState)
    turns: int = 0
    enabled: bool = True
    # Injected so a test can force a tier instead of depending on the host's real terminal.
    _terminal: Terminal | None = None

    # ---- setup -----------------------------------------------------------------------
    def begin(self) -> "Character":
        """A session started, so greet. Returns self so a caller can chain.

        `happy` rather than `idle`: the resting face is what the character shows when nothing is happening, and a
        greeting is something. The distinction is the whole reason both exist.
        """
        self.state = expression.ExpressionState()
        self.turns = 0
        self.state.apply(SIGNAL_OPEN)
        self.state.expression = "happy"
        return self

    def release_crisis(self) -> "Character":
        """Clear the safety state. The only way out, and deliberately explicit. See `loci_expression`."""
        self.state.release_crisis(now=time.time())
        return self

    @property
    def terminal(self) -> Terminal:
        if self._terminal is None:
            self._terminal = detect_terminal()
        return self._terminal

    @property
    def expression(self) -> str:
        return self.state.expression

    # ---- the turn --------------------------------------------------------------------
    def before_turn(self, *, typing: bool = False) -> "Character":
        """Something is about to happen. Typing shows listening; sending shows thinking."""
        self.state.apply(expression.SIGNAL_TYPING if typing else expression.SIGNAL_SENT, now=time.time())
        return self

    def after_turn(self, agent=None, *, error: BaseException | None = None) -> str:
        """The turn is over. Returns the expression now showing, so a caller can assert on it.

        Mapped in priority order, and the order is the point: an exception outranks a recorded stop reason, because a
        turn that blew up did not succeed whatever the transcript says.
        """
        self.turns += 1
        now = time.time()
        # An exception is the strongest evidence there is.
        if error is not None:
            self.state.apply(expression.SIGNAL_ERROR, now=now)
            return self.state.expression

        reason = ""
        worked = False
        try:
            reason = str(getattr(agent, "last_stop_reason", "") or "")
        except Exception:  # noqa: BLE001 - a hostile agent counts as "no reason recorded"
            reason = ""
        try:
            worked = _did_work(agent)
        except Exception:  # noqa: BLE001
            worked = False

        if reason in ERROR_REASONS:
            self.state.apply(expression.SIGNAL_ERROR, now=now)
        elif reason in QUIET_REASONS:
            self.state.apply(expression.SIGNAL_QUIET, now=now)
        elif reason in BUDGET_REASONS:
            # Concern rather than failure: nothing broke, the work is just unfinished.
            self.state.apply(expression.SIGNAL_DISTRESS, now=now)
            self.state.expression = "concerned"
        elif worked and reason in ("", "completed"):
            # `""` is NOT a missing value here. The OpenAI-compatible path leaves `last_stop_reason` as None, which is
            # the norm on this host rather than the exception -- treating it as "no information" showed `idle` after a
            # turn that had done real work.
            self.state.apply(expression.SIGNAL_TASK_DONE, now=now)
        else:
            self.state.apply(expression.SIGNAL_QUIET, now=now)
        return self.state.expression

    def tick(self, *, now: float | None = None) -> "Character":
        """Let a held expression decay. Called on the prompt loop, not on a timer."""
        self.state.tick(now=time.time() if now is None else now)
        return self

    # The verifier calls this on the CLASS, so it is a staticmethod rather than a module function: the logic belongs
    # to the character even though it reads nothing from an instance.
    _did_work = staticmethod(_did_work)

    # ---- forcing ---------------------------------------------------------------------
    def set_expression(self, name: str) -> bool:
        """Force an expression by name. Returns whether the name was known.

        ## The bug this shape fixes

        Measured: the first version assigned the expression and only THEN checked whether the name was known, so
        `set_expression("listning")` returned False and left the character sitting on the typo. A refused change that
        changes something is worse than one that raises: the caller sees False, believes nothing happened, and the face
        is wrong. The name is validated before anything is touched.
        """
        cleaned = (name or "").strip()
        if not cleaned or cleaned not in EXPRESSIONS:
            return False
        self.state.expression = cleaned
        self.state.crisis = cleaned == "crisis"
        self.state.since = time.time()
        return True

    # ---- the three channels ----------------------------------------------------------
    def prompt_prefix(self, label: str = "") -> str:
        """The prompt fragment, or a bare marker when the character is turned off.

        A disabled character returns the plain prompt rather than an empty string, so a caller can concatenate it
        without checking: `"> "` is a correct prompt, and `""` is not.

        `label` asks for a name instead of the drawn character; see `loci_surface.prompt_prefix` for why the
        CLI wants that (raw ANSI escapes in a prompt_toolkit prompt are displayed, not interpreted).
        """
        if not self.enabled:
            return f"{label} > " if label else "> "
        return _prompt_prefix(self.state, self.terminal, label=label)

    def status_fragment(self) -> str:
        """The status fragment. Never raises, whatever the state holds."""
        try:
            return _status_fragment(self.state, self.terminal)
        except Exception:  # noqa: BLE001 - a status line is not worth an exception
            return ""


def for_agent(agent) -> Character:
    """The character belonging to this agent, created on first use.

    Keyed by the agent's identity so two sessions cannot share one face. An agent that refuses attributes still gets a
    character, because a session with no character is worse than one with a default.
    """
    registry = _registry()
    try:
        key = id(agent)
    except Exception:  # noqa: BLE001
        key = 0
    found = registry.get(key)
    if found is None:
        found = Character()
        registry[key] = found
    return found


def forget_agent(agent) -> None:
    """Drop an agent's character. For a session that has ended, so the registry does not grow for ever."""
    try:
        _registry().pop(id(agent), None)
    except Exception:  # noqa: BLE001
        pass
