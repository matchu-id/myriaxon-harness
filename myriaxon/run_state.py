"""Durable run state: survive a crash, and never repeat a side effect.

Why this exists, in the owner's terms: Level 4 is *"manages complex workflows"*, and a workflow that
evaporates when the process dies is not managed. Measured before this module: a Locus run held its
progress only in memory, so a kill at step 9 of 12 lost every step, and the next wake-up started the
objective over. For a task that costs minutes and dollars, that is the difference between a long task
and a long task that happens to finish.

Two separate guarantees live here, and they are separate on purpose:

**Resumability.** A run writes a snapshot after every step -- goal, step number, budget, and the last
thing it said. A killed run leaves that file behind; the next run finds it and CONTINUES from the
recorded step instead of restarting. The snapshot is small and fixed-size, so this costs nothing that
grows with the length of the run.

**Exactly-once side effects.** A resumed run must not re-send a message or re-charge a card. Anything
outbound asks `already_done(key)` first and calls `record_side_effect(key)` after. The key is the
caller's, because only the caller knows what makes two actions "the same action" -- a message is
identified by its recipient and body, not by when it was sent.

What this deliberately does NOT do: it does not snapshot the model's conversation. Persisting a
message list that can hold hundreds of thousands of tokens on every step would make every step a disk
write proportional to the context, and the conversation is reconstructible -- the objective, the step
number and the workspace are what a continuation actually needs. Recorded here because the obvious
"just save everything" version is the one that gets written first and regretted later.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .atomic_io import atomic_write_text
from .constants import CONFIG_DIR

# Where run state lives. Under the config directory rather than the workspace, deliberately: a Locus
# is confined to its workspace, and its own bookkeeping should not be something it can read, edit or
# delete as part of doing its job. Confinement that excludes the agent's own control files is
# confinement that survives a confused agent.
RUN_STATE_DIR = CONFIG_DIR / "runs"

# A snapshot older than this is treated as abandoned rather than resumable.
#
# Bounded because "resume an interrupted run" and "silently continue a run the owner cancelled three
# weeks ago" are the same mechanism with different feelings. A week is long enough for a crash and a
# weekend, short enough that a stale objective does not reappear as though nothing happened.
RUN_STATE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


@dataclass
class RunState:
    """One Locus run's durable record. Written after every step, atomically."""

    run_id: str
    locus_id: str = ""
    locus_name: str = ""
    goal_id: str = ""
    goal_title: str = ""
    step: int = 0                 # steps COMPLETED, so resuming starts at step + 1
    budget: int = 0
    started_at: float = 0.0
    updated_at: float = 0.0
    finished: bool = False
    # Why the run ended, when it did: "done", "failed", "budget", or "" while still in progress.
    outcome: str = ""
    # The last thing the agent said, truncated. Enough for a continuation prompt to know where it was
    # without storing a transcript that grows with the context window.
    last_text: str = ""
    # Keys of side effects already performed, so a resumed run cannot repeat one.
    side_effects: list[str] = field(default_factory=list)

    @property
    def resumable(self) -> bool:
        return not self.finished and self.step > 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "RunState":
        data = json.loads(text)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def _state_path(run_id: str) -> Path:
    return RUN_STATE_DIR / f"{run_id}.json"


def _previous_path(run_id: str) -> Path:
    """Where the last COMPLETED run of this kind is remembered.

    A completed run deletes its own state file, so without this a re-run of the same objective would
    start from step 0 with no memory that it had already sent the message. Keeping the finished record
    is what makes the side-effect ledger outlive the run that filled it.
    """
    return RUN_STATE_DIR / f"{run_id}.finished.json"


def begin_run(run_id: str, *, locus: object = None, goal: object = None,
              budget: int = 0, resume: bool = True) -> RunState:
    """Start a run, or resume an interrupted one with the same id.

    Resuming is the DEFAULT, because the alternative is that a crash silently converts a finished
    objective into an unfinished one with no indication that anything was lost.
    """
    if resume:
        existing = load_state(run_id)
        if existing is not None and existing.resumable:
            existing.updated_at = time.time()
            save_state(existing)
            return existing

    now = time.time()
    state = RunState(
        run_id=run_id,
        locus_id=str(getattr(locus, "id", "") or ""),
        locus_name=str(getattr(locus, "name", "") or ""),
        goal_id=str(getattr(goal, "id", "") or ""),
        goal_title=str(getattr(goal, "title", "") or ""),
        step=0,
        budget=int(budget or 0),
        started_at=now,
        updated_at=now,
    )
    # A fresh run inherits the side-effect ledger of the last COMPLETED run with this id. That is the
    # mechanism that stops "run it again" from meaning "send it again".
    carried = _load_finished(run_id)
    if carried is not None:
        state.side_effects = list(carried.side_effects)
    save_state(state)
    return state


def save_state(state: RunState) -> None:
    """Write the snapshot atomically. Safe to call on every step.

    Deliberately does NOT touch `updated_at`. That field means "when this run last made PROGRESS", and
    it is the clock `list_resumable` uses to decide that an interrupted run has gone stale. Setting it
    here would make it mean "when this file was last written", so any bookkeeping save -- recording a
    side effect, for instance -- would push the staleness deadline back. Found by a test that wrote a
    stale snapshot and watched it refuse to expire: a run that kept re-saving state could then be
    resumed forever, which is how "continue my interrupted work" quietly becomes "resurrect a run the
    owner abandoned in March".

    Progress is recorded where progress happens: `record_step` and `begin_run`.
    """
    try:
        RUN_STATE_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(_state_path(state.run_id), state.to_json())
    except OSError:
        # A run must not die because its bookkeeping could not be written. Losing resumability is a
        # degraded run; raising here would turn it into a failed one, which is worse in every case
        # except the one where the disk is full and the failure is about to happen anyway.
        pass


def load_state(run_id: str) -> RunState | None:
    """The in-progress snapshot, or None. Repairs nothing and raises nothing."""
    path = _state_path(run_id)
    try:
        return RunState.from_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_finished(run_id: str) -> RunState | None:
    try:
        return RunState.from_json(_previous_path(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def record_step(state: RunState, step: int, text: str = "") -> RunState:
    """Checkpoint after a step. Called once per step, so it must stay cheap.

    The ONE place `updated_at` advances after a run has started, because completing a step is the only
    event that constitutes progress. See `save_state` for why that distinction decides whether an
    abandoned run can be resumed forever.
    """
    state.step = int(step)
    state.last_text = (text or "")[:2000]
    state.updated_at = time.time()
    save_state(state)
    return state


def finish_run(state: RunState, outcome: str = "done") -> None:
    """Close a run: file the finished record, then remove the in-progress snapshot.

    Order matters. The finished record is written FIRST, because it carries the side-effect ledger
    forward; deleting the snapshot first would leave a window where a crash loses the record that a
    message was ever sent.
    """
    state.finished = True
    state.outcome = outcome
    try:
        RUN_STATE_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(_previous_path(state.run_id), state.to_json())
    except OSError:
        pass
    try:
        _state_path(state.run_id).unlink(missing_ok=True)
    except OSError:
        pass


def already_done(state: RunState | None, key: str) -> bool:
    """Whether this side effect has already been performed under this run id.

    Conservative in the direction that matters: an unknown state (None) reports False, so a caller with
    no ledger behaves exactly as it did before this module existed rather than silently suppressing
    real work.
    """
    if state is None or not key:
        return False
    return key in state.side_effects


def record_side_effect(state: RunState | None, key: str) -> None:
    """Remember that a side effect happened, durably, BEFORE its result is reported.

    Persisted immediately rather than at the end of the run: the crash this defends against is the one
    that happens after the send succeeded and before anything was written down.
    """
    if state is None or not key:
        return
    if key not in state.side_effects:
        state.side_effects.append(key)
    save_state(state)


def list_resumable(now: float | None = None) -> list[RunState]:
    """Interrupted runs young enough to resume, newest first."""
    now = time.time() if now is None else now
    found: list[RunState] = []
    try:
        paths = list(RUN_STATE_DIR.glob("*.json"))
    except OSError:
        return found
    for path in paths:
        if path.name.endswith(".finished.json"):
            continue
        try:
            state = RunState.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not state.resumable:
            continue
        if now - state.updated_at > RUN_STATE_MAX_AGE_SECONDS:
            continue
        found.append(state)
    found.sort(key=lambda s: s.updated_at, reverse=True)
    return found


def format_run_state(state: RunState) -> str:
    """One line for a status panel."""
    where = f"step {state.step}/{state.budget}" if state.budget else f"step {state.step}"
    tail = f" ({state.outcome})" if state.outcome else " (interrupted)"
    goal = state.goal_title or "(no objective)"
    return f"{state.locus_name or state.run_id}: {where}{tail} -- {goal}"


def run_id_for(locus: object, goal: object) -> str:
    """A stable id for "this Locus working on this objective".

    Stable across restarts and across goals with the same title, because the whole point is that the
    next process recognises the previous one's work. Deliberately NOT including a timestamp: a run id
    that changes every time makes every resume a fresh start.
    """
    locus_id = str(getattr(locus, "id", "") or getattr(locus, "name", "") or "locus")
    goal_id = str(getattr(goal, "id", "") or getattr(goal, "title", "") or "goal")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in f"{locus_id}--{goal_id}")
    return safe[:120]
