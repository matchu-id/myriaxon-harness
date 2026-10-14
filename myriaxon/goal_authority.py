"""Completion authority: who is allowed to declare a goal finished.

## The gap this closes

A Locus can mark its own goal complete. So can a sub-agent. So can a scheduled wake-up with no human anywhere in the
loop. The failure is not hypothetical and not subtle: **the worker grades its own work**, and a model that has run
out of ideas has a strong incentive to decide the objective is met.

DeepSeek's harness closes this in `goal/tool-goal/src/authority.ts` (verified), and their rule is precise:
completion is permitted only when one of two things holds —

  * a **direct human input in the current, open turn of the root agent** — attested by the host, not claimed by
    the caller; or
  * the turn is **exactly the current goal's admitted round**, matched on `(goalId, revision, round)`.

Their reasoning for the second clause: a round driver arms a synthetic message with its own identity stamped on
it, so a run that *is* the admitted round cannot be confused with an unrelated turn that happens to be running.
And their `Agent.options` documents that an omitted `followup()`/`steer()` source resolves to `user` — so a
non-human producer must supply its own source rather than inheriting human authority by default. That inversion is
the part most implementations get wrong.

## What this module does

`TurnSource` labels where a turn came from. `CompletionAuthority` decides whether that source may complete a
goal. A model's own `goal complete` call is a **request**, not a decision: it is recorded, and refused unless the
turn carries human authority or matches the admitted round.

## What is deliberately stricter than the reference

DSH permits completion when the turn is the admitted round — i.e. the round driver's own synthetic turn may
complete the goal. That is necessary there because an unattended goal has no human to ask. Myriaxon keeps the same
capability but makes the **self-report visible**: `CompletionDecision.model_claimed` records that the model asked,
and the decision says the authority came from the round rather than from a person. A goal completed by a round is
recorded as such, so an operator reading the log later can tell the two apart.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

# Where a turn came from. A closed vocabulary, because the whole mechanism is a decision about provenance and a
# free-form string would make it undecidable.
SOURCE_HUMAN = "human"            # typed by a person in an open turn
SOURCE_ROUND = "goal-round"       # the round driver's own admitted round
SOURCE_SCHEDULE = "schedule"      # a timer or cron
SOURCE_SUBAGENT = "subagent"      # a sub-agent reporting up
SOURCE_DOT = "locus"                # an unattended Locus wake-up
SOURCE_SYSTEM = "system"          # the harness itself
SOURCES = (SOURCE_HUMAN, SOURCE_ROUND, SOURCE_SCHEDULE, SOURCE_SUBAGENT, SOURCE_DOT, SOURCE_SYSTEM)

# Only these two may authorise completion from an *unattended* source. Everything else is recorded and refused.
AUTHORITATIVE_SOURCES = (SOURCE_HUMAN, SOURCE_ROUND)

# The sources whose completion claim is refused outright.
#
# ## Why this is narrower than "everything except human"
#
# The failure this module exists to prevent is specific: **the model marking its own work done inside the
# unattended run that did the work.** That is `SOURCE_DOT` and `SOURCE_SUBAGENT` -- a turn whose only participant
# is the agent grading itself.
#
# A caller that passes no source at all is a different thing. It is the harness, the dashboard, a panel handler,
# or a test driving the state machine -- a programmatic caller acting on a decision made outside the model's turn.
# Refusing those was the first version of this check, and it broke the Loci panel and eight existing tests, which
# was the signal that the rule was drawn in the wrong place: the danger is not "not a human", it is "the model
# deciding on its own behalf".
#
# `SOURCE_SCHEDULE` and `SOURCE_SYSTEM` are therefore allowed, and `SOURCE_DOT` / `SOURCE_SUBAGENT` are not. The
# Locus runner sets exactly `locus_run_source="locus"`, so the unattended 24/7 path -- the one that matters -- is refused.
SELF_GRADING_SOURCES = (SOURCE_DOT, SOURCE_SUBAGENT)

# Goal lifecycle.
ACTIVE = "active"
PAUSED = "paused"
BLOCKED = "blocked"
COMPLETE = "complete"
STATES = (ACTIVE, PAUSED, BLOCKED, COMPLETE)

# How many consecutive rounds a goal may be blocked for before it is reported rather than retried forever. Their
# rule is the same shape as the workflow cap: a tripped cap stops the run and says so.
BLOCKED_ROUND_LIMIT = 3


def source_of(value: str) -> str:
    """Normalise a source string, defaulting to the LEAST authoritative.

    An unrecognised or omitted source resolves to `system`, never to `human`. DSH documents the same inversion for
    its own omitted source, and the reasoning is that a non-human producer must *supply* human authority rather
    than inherit it by leaving a field blank. Getting this backwards is how "unattended" quietly becomes
    "authorised".
    """
    candidate = (value or "").strip().lower()
    return candidate if candidate in SOURCES else SOURCE_SYSTEM


@dataclass(frozen=True)
class RoundStamp:
    """The identity of an admitted round: `(goalId, revision, round)`.

    Frozen, so a stamp cannot be edited after it is issued. Their driver "arms identity as `(goalId, revision,
    round)` stamped on the synthetic message source", and matching all three is what stops a *different* round of
    the same goal — or the same round of a superseded revision — from completing it.
    """

    goal_id: str
    revision: int
    round: int

    def render(self) -> str:
        return f"{self.goal_id}@r{self.revision}#{self.round}"

    def matches(self, other: "RoundStamp | None") -> bool:
        if other is None:
            return False
        return (self.goal_id == other.goal_id
                and self.revision == other.revision
                and self.round == other.round)


@dataclass
class TurnContext:
    """Where the turn that made a request came from."""

    source: str = SOURCE_SYSTEM
    # True only when the host observed a real person typing in the root agent's open turn. A caller cannot set
    # this from the model's side: it is set by the input path.
    human_input: bool = False
    stamp: RoundStamp | None = None
    note: str = ""

    @classmethod
    def human(cls, note: str = "") -> "TurnContext":
        return cls(source=SOURCE_HUMAN, human_input=True, note=note)

    @classmethod
    def unattended(cls, source: str, stamp: RoundStamp | None = None) -> "TurnContext":
        return cls(source=source, human_input=False, stamp=stamp)


@dataclass
class CompletionDecision:
    """Whether a completion request is honoured, and on whose authority."""

    allowed: bool
    authority: str = ""          # "human" | "round" | ""
    reason: str = ""
    model_claimed: bool = False
    recorded_at: float = field(default_factory=time.time)

    def render(self) -> str:
        if self.allowed:
            return f"completion allowed on {self.authority} authority" + (
                " (the model claimed it; the round authorised it)" if self.model_claimed else ""
            )
        return f"completion refused: {self.reason}"


def may_complete(
    context: TurnContext,
    *,
    admitted: RoundStamp | None = None,
    model_claimed: bool = True,
) -> CompletionDecision:
    """Decide whether this turn may mark a goal complete.

    `model_claimed` records that the *model* asked, as distinct from a host-side decision. It does not change the
    verdict — it changes what the log says, which is what lets a reader separate "a person said we are done" from
    "the round ran out and the model said we are done".
    """
    source = source_of(context.source)

    if source == SOURCE_HUMAN:
        if not context.human_input:
            # A turn that CLAIMS the human source without observed input. The label alone must not be enough, or
            # the source becomes a string the caller can write and the whole mechanism is decorative. This case
            # fell through to the permissive branch in the first version of the narrowing, which is exactly the
            # hole it was meant to close.
            return CompletionDecision(
                allowed=False, model_claimed=model_claimed,
                reason=(
                    "this turn carries the human source label but no observed human input. The label is set by the "
                    "input path, not by the caller, so a turn that only claims it is refused. Supply a real input, "
                    "or let a programmatic caller complete the goal without claiming human authority."
                ),
            )
        return CompletionDecision(
            allowed=True, authority="human", model_claimed=model_claimed,
            reason="a person supplied this turn",
        )

    if source == SOURCE_ROUND:
        if context.stamp is None or admitted is None:
            return CompletionDecision(
                allowed=False, model_claimed=model_claimed,
                reason=(
                    "this turn is a goal round but carries no round stamp, or the goal has no admitted round, so "
                    "the turn cannot be shown to BE the admitted round. A round identity is what stops an "
                    "unrelated turn of the same goal from completing it."
                ),
            )
        if not context.stamp.matches(admitted):
            return CompletionDecision(
                allowed=False, model_claimed=model_claimed,
                reason=(
                    f"this turn is {context.stamp.render()} but the admitted round is {admitted.render()}. "
                    "A different round, or the same round of a superseded revision, cannot complete the goal."
                ),
            )
        return CompletionDecision(
            allowed=True, authority="round", model_claimed=model_claimed,
            reason=f"this turn is exactly the admitted round {admitted.render()}",
        )

    # Everything else: a self-grading turn is refused and named; any other programmatic caller is allowed,
    # because the decision was made outside the model's own turn.
    if source in SELF_GRADING_SOURCES:
        return CompletionDecision(
            allowed=False, model_claimed=model_claimed,
            reason=(
                f"a {source} turn cannot complete its own goal. This turn is the agent that did the work, so "
                "declaring it finished is the agent grading itself -- the one thing completion authority exists to "
                "prevent. Only a direct human input, the goal's own admitted round, or a decision from outside the "
                "run may do that. Report the objective as met and let a person confirm it."
            ),
        )

    return CompletionDecision(
        allowed=True, authority="caller", model_claimed=model_claimed,
        reason=(
            f"a {source} turn is a programmatic caller acting from outside the model's own run, so it may "
            "complete the goal; the decision is recorded with its source."
        ),
    )


@dataclass
class GoalState:
    """A goal's durable state, with its completion authority recorded rather than assumed."""

    goal_id: str
    objective: str
    revision: int = 1
    state: str = ACTIVE
    rounds_started: int = 0
    admitted: RoundStamp | None = None
    blocked_rounds: int = 0
    blocked_reason: str = ""
    completed_at: float = 0.0
    completed_by: str = ""
    history: list[dict] = field(default_factory=list)

    def admit_round(self) -> RoundStamp:
        """Open a new round and return its stamp.

        The revision is captured *now*, so a later revision bump invalidates this stamp — which is how "the same
        round of a superseded revision" is refused without any extra bookkeeping.
        """
        self.rounds_started += 1
        stamp = RoundStamp(goal_id=self.goal_id, revision=self.revision, round=self.rounds_started)
        self.admitted = stamp
        return stamp

    def complete(self, decision: CompletionDecision) -> bool:
        """Apply a completion decision. Returns whether the state changed."""
        if not decision.allowed:
            self.history.append({
                "at": decision.recorded_at, "event": "completion-refused",
                "reason": decision.reason, "model_claimed": decision.model_claimed,
            })
            return False
        if self.state == COMPLETE:
            return False
        self.state = COMPLETE
        self.completed_at = decision.recorded_at
        self.completed_by = decision.authority
        self.history.append({
            "at": decision.recorded_at, "event": "completed",
            "authority": decision.authority, "model_claimed": decision.model_claimed,
            "round": self.admitted.render() if self.admitted else "",
        })
        return True

    def block(self, reason: str) -> tuple[bool, str]:
        """Record a blocked round. Returns `(should_report, message)`.

        A goal that blocks repeatedly is not making progress, and retrying it forever is the failure the cap
        exists to stop. The count resets on an unblock, so a goal that recovers is not penalised for an old stall.
        """
        self.blocked_rounds += 1
        self.blocked_reason = reason
        self.state = BLOCKED
        self.history.append({
            "at": time.time(), "event": "blocked",
            "reason": reason, "count": self.blocked_rounds,
        })
        if self.blocked_rounds >= BLOCKED_ROUND_LIMIT:
            return True, (
                f"blocked {self.blocked_rounds} round(s) in a row on: {reason}. Reporting rather than retrying, "
                "because the same condition recurring is not difficulty -- it needs a decision."
            )
        return False, f"blocked ({self.blocked_rounds}/{BLOCKED_ROUND_LIMIT}): {reason}"

    def unblock(self) -> None:
        """Clear the blocked streak. Called when a round makes progress."""
        if self.blocked_rounds:
            self.history.append({"at": time.time(), "event": "unblocked", "after": self.blocked_rounds})
        self.blocked_rounds = 0
        self.blocked_reason = ""
        if self.state == BLOCKED:
            self.state = ACTIVE

    def bump_revision(self) -> int:
        """Supersede the current round, invalidating its stamp.

        The revision exists so that editing a goal's objective cannot be completed by the round that was admitted
        for the *previous* wording.
        """
        self.revision += 1
        self.admitted = None
        self.history.append({"at": time.time(), "event": "revision", "revision": self.revision})
        return self.revision


def stamp_digest(stamp: RoundStamp) -> str:
    """A short stable digest of a round stamp, for a log line. Not a secret; just compact and comparable."""
    return hashlib.sha256(stamp.render().encode("utf-8")).hexdigest()[:12]
