"""Conditions: an always-on agent that wakes because something is wrong, not because a clock ticked.

## The gap this closes

Myriaxon's Loci are always-on in the sense that they persist and run without prompting, but their trigger is a
**clock**: `is_due()` compares elapsed time against an interval. So a maintenance agent wakes every N seconds and
spends a paid turn working on its next goal whether or not anything needs doing. For a long-term objective that
"maintains something", that is backwards — the work should be triggered by the thing it maintains changing, and a
tick where nothing changed should cost nothing.

Two consequences today, both real:

* **cost scales with the interval, not with the work.** A 60-second Locus over a quiet night spends ~1,440 model
  calls discovering there was nothing to do.
* **the trigger cannot express the objective.** "Keep the service healthy" cannot be checked by looking at the
  clock. The only thing the current design knows how to ask is "has 60 seconds passed?".

## What a condition is

A **cheap, deterministic predicate evaluated before the turn starts**, so a tick that is not warranted never
becomes a model call:

  * `command` — a shell command; exit 0 means the condition holds. `systemctl is-active nginx`, a test suite, a
    health endpoint via curl.
  * `file_newer_than` — a path modified since the last run, for output directories and log files.
  * `file_missing` / `path_exists` — presence checks that are their own answer.
  * `always` — the explicit opt-out: behave exactly as before, wake on the interval.

## The three decisions that matter

**Fail-open or fail-closed?** If a condition command cannot run at all, the agent must **wake**. A broken
`sytemctl` typo that silently disables the watchdog is the worst outcome available: the agent reports healthy
forever because it never checked. So an unrunnable condition is reported and the wake proceeds — the opposite
default from the guardrail layer, and for a different reason: a guardrail's failure mode is doing something
harmful, a watchdog's is not noticing.

**Does a wake reset the clock?** Yes — `last_evaluated_at` advances on every tick whether or not a turn ran, so a
quiet condition does not cause a burst of catch-up wakes.

**What if the condition is true forever?** Then the agent works every tick, which is correct but must be bounded:
`min_seconds_between_runs` is a floor independent of the interval, so a condition that flaps cannot drive an
unbounded loop. This is the same shape as the workflow cap vocabulary in the reference harness.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# Condition kinds. A closed set, because each has its own cheap evaluation and an unknown kind would have to be
# guessed at.
KIND_COMMAND = "command"
KIND_FILE_NEWER = "file_newer_than"
KIND_FILE_MISSING = "file_missing"
KIND_PATH_EXISTS = "path_exists"
KIND_ALWAYS = "always"
# Fires when the observed VALUE differs from the last observation, rather than when the value is true.
#
# This is the difference between "is the queue backed up?" and "has the queue changed?" -- the first fires on every
# tick while the condition holds, the second fires once per change. Modelled on OpenClaw's condition watchers,
# whose trigger script compares against `trigger.state` and returns `fire: status !== trigger.state?.status`.
KIND_CHANGED = "changed"
KINDS = (KIND_COMMAND, KIND_FILE_NEWER, KIND_FILE_MISSING, KIND_PATH_EXISTS, KIND_ALWAYS, KIND_CHANGED)

# Kinds that need an `observe` command to produce a value worth comparing.
OBSERVING_KINDS = (KIND_CHANGED,)

# A condition command must not become the thing that hangs the watchdog.
DEFAULT_TIMEOUT_SECONDS = 30.0

# The floor between runs regardless of how often the condition fires. Without it a condition that flaps -- a log
# line appended every second -- drives a run every tick, which is the unbounded-retry failure the attempt ceiling
# exists to prevent elsewhere.
DEFAULT_MIN_SECONDS_BETWEEN_RUNS = 60.0

# Cap on the persisted observation state, copied from OpenClaw ("State is capped at 16 KB").
#
# Bounded because this is written to the loci file on every evaluation: an unbounded value -- a command that prints
# a whole log -- would grow the registry without limit, and the state exists to be a fingerprint, not an archive.
MAX_STATE_BYTES = 16 * 1024

# Shell builtins and constructs that are NOT files on PATH. A resolution check must not report these as missing,
# or it would cry wolf on `test -f x` and `[ -f x ]`, which are two of the most useful things to put in a
# condition. Cross-shell superset, deliberately generous: a false "cannot resolve" is worse than no check at all,
# because it would train a reader to ignore the warning.
SHELL_BUILTINS = frozenset({
    "alias", "bg", "bind", "break", "builtin", "call", "cd", "chdir", "command", "continue", "declare", "dir",
    "dirs", "disown", "do", "done", "echo", "elif", "else", "enable", "eval", "exec", "exit", "export", "false",
    "fc", "fg", "fi", "for", "foreach", "function", "getopts", "hash", "help", "history", "if", "in", "jobs",
    "let", "local", "logout", "mapfile", "popd", "printf", "pushd", "pwd", "read", "readonly", "return", "select",
    "set", "shift", "shopt", "source", "suspend", "test", "then", "time", "times", "trap", "true", "type",
    "typeset", "ulimit", "umask", "unalias", "unset", "until", "wait", "while", "[", "]", ".",
})


def unresolvable_program(command: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Return why `command`'s executable cannot be found, or "" if it looks resolvable.

    ## The hole this narrows

    With `shell=True` a missing command is **not** an error: the shell runs, fails to find it, and exits non-zero --
    byte-identical to "the condition is not met". So a typo in a condition command produces a watcher that is
    permanently and silently quiet, which is the dangerous direction: a broken watchdog looks exactly like a calm
    one.

    ## Why this is a check and not a fix

    It cannot be a fix. After the shell has run, a missing program and a failed check have produced the same exit
    status, and nothing downstream can tell them apart. So the only honest remedy is to resolve the program
    *before* running it, and that can only be done when the command's shape is unambiguous.

    ## When this stays silent

    It returns "" -- meaning "cannot tell, do not warn" -- for anything it cannot be sure about: a pipeline, a
    redirect, a substitution, a chain, a quoted path, an environment prefix, or a shell builtin. That is most
    commands, and the narrowness is the point. A check that produced false warnings would be worse than none,
    because it would teach the reader to ignore the one case that matters.

    What it DOES catch is the shape a condition almost always has: a bare program name, optionally with arguments.
    `systemctl is-active nginx` and `curl -fsS localhost:8080/health` are resolved; a typo in either is reported.
    """
    import shlex
    import shutil

    raw = (command or "").strip()
    if not raw:
        return ""

    # Anything with shell syntax in it is out of scope: the first "word" would not be the program, and guessing
    # would produce exactly the false positives this avoids.
    if any(token in raw for token in ("|", "&&", "||", ";", ">", "<", "`", "$(", "${", "&", "\n", "*", "?")):
        return ""

    try:
        parts = shlex.split(raw, posix=False)
    except ValueError:
        return ""
    if not parts:
        return ""

    first = parts[0].strip('"').strip("'")
    if not first:
        return ""

    # An explicit path, or anything with a separator: check the path itself.
    if "/" in first or "\\" in first:
        candidate = Path(first)
        if candidate.exists():
            return ""
        return f"the program {first!r} does not exist"

    # An environment assignment prefix (`FOO=bar prog`) means the first token is not the program.
    if "=" in first:
        return ""

    if first.lower() in SHELL_BUILTINS:
        return ""

    # `shutil.which` honours PATHEXT on Windows, so `python` resolves to `python.exe` and is not a false positive.
    if shutil.which(first) is not None:
        return ""

    # Some shells can run a builtin-ish command we did not list. Only report when the name looks like a plain
    # program name -- no wildcards, no quotes, a plausible identifier -- so exotic input is left alone.
    if any(ch in first for ch in "\"'`$(){}[]<>|&;*?!"):
        return ""
    return (
        f"the program {first!r} was not found on PATH, so this check cannot run. With a shell, a missing program "
        "exits non-zero and is indistinguishable from 'the condition is not met' -- so a typo makes the watcher "
        "silently quiet forever"
    )


@dataclass
class ConditionResult:
    """What one evaluation found."""

    holds: bool
    detail: str = ""
    # True when the condition could not be evaluated. Distinguished from `holds is False` because the two are
    # different facts and the caller decides what to do -- and here it wakes.
    error: bool = False
    checked_at: float = field(default_factory=time.time)
    # The observation this evaluation produced, for the observing kinds. `None` means "nothing to compare", which
    # is different from a value of `""`: a command that printed nothing observed an empty string, deliberately.
    observation: str | None = None

    def render(self) -> str:
        state = "ERROR" if self.error else ("holds" if self.holds else "quiet")
        return f"[{state}] {self.detail}"


@dataclass
class TriggerState:
    """What a condition remembers between evaluations, persisted with the Locus.

    ## Why this is a separate object

    `Locus.is_due` rebuilds the `Condition` from a stored dict on every tick, so a `Condition` cannot hold mutable
    state -- it is discarded before the next evaluation. The state therefore lives here, is owned by the `Locus`, and
    is passed in and returned by `should_run`.

    ## The rule copied from OpenClaw, and why it is subtle

    Their documented behaviour: *"If a fired payload run fails, the returned `state` is **not** persisted -- the
    next evaluation sees the previous state and can fire again."* That is the correct default and it is easy to get
    backwards. If a failed run persisted its state, the change that triggered the run would be marked as handled by
    a run that did nothing -- so the watcher would go quiet exactly when its work had failed. `commit()` below is
    only called for a run that finished, and `discard()` exists so the intent is explicit at the call site rather
    than expressed by omission.
    """

    # The last observation, for the observing kinds. A string so any command output is comparable.
    observation: str = ""
    # Bumped whenever `observation` is replaced, so "changed" is expressible without diffing strings.
    revision: int = 0
    # When the observation was taken, for the status line.
    observed_at: float = 0.0
    # How many times this condition has fired, for a bounded activity trail.
    fires: int = 0

    def __post_init__(self) -> None:
        problem = self.problem()
        if problem:
            # Refuse rather than truncate: a silently shortened fingerprint would compare unequal to itself and
            # fire on every tick, which is the opposite of what the cap is for.
            raise ValueError(problem)

    def problem(self) -> str:
        """A problem with this state, or "". Used by `__post_init__` and by `validate`."""
        size = len(self.observation.encode("utf-8", errors="replace"))
        if size > MAX_STATE_BYTES:
            return (
                f"the observation is {size:,} bytes, over the {MAX_STATE_BYTES:,} byte cap. State is written to "
                "the registry on every check and exists to be a fingerprint, not an archive -- narrow the command "
                "so it prints something short, for example a hash, a count or a single field."
            )
        return ""

    def commit(self, observation: str | None, *, now: float | None = None) -> bool:
        """Record an observation. Returns whether it actually CHANGED.

        An unchanged observation does not bump `revision`, so `changed` stays false and the next tick is quiet.
        `None` means the evaluation produced nothing to compare -- a command kind, or an error -- and leaves the
        state alone, so a broken check cannot erase the fingerprint it was meant to maintain.
        """
        if observation is None:
            return False
        moment = time.time() if now is None else now
        if observation == self.observation and self.observed_at:
            self.observed_at = moment
            return False
        self.observation = observation
        self.revision += 1
        self.observed_at = moment
        self.fires += 1
        return True

    def to_dict(self) -> dict:
        return {
            "observation": self.observation,
            "revision": self.revision,
            "observed_at": self.observed_at,
            "fires": self.fires,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "TriggerState":
        """Rebuild from storage, tolerating a malformed or oversized record.

        A state that cannot be restored is replaced with an empty one rather than raising: losing a fingerprint
        costs one extra wake-up, while refusing to load would make the Locus unloadable and take the whole registry
        with it. An oversized record is dropped for the same reason -- it may predate the cap.
        """
        if not isinstance(data, dict):
            return cls()
        observation = str(data.get("observation") or "")
        if len(observation.encode("utf-8", errors="replace")) > MAX_STATE_BYTES:
            return cls()
        return cls(
            observation=observation,
            revision=int(data.get("revision") or 0),
            observed_at=float(data.get("observed_at") or 0.0),
            fires=int(data.get("fires") or 0),
        )


@dataclass
class Condition:
    """A predicate the agent checks before spending a turn.

    `description` is required and is shown to the model when the condition fires, so the agent knows *why* it was
    woken. A wake-up with no stated reason is one the model has to guess about, which is how an unattended agent
    drifts off its objective.
    """

    kind: str = KIND_ALWAYS
    description: str = ""
    command: str = ""
    path: str = ""
    # For the observing kinds: the command whose OUTPUT is the value being watched.
    #
    # Separate from `command` because the two answer different questions. `command` is a yes/no check -- exit 0
    # means the condition holds. `observe` produces a value that is compared against the previous value, so a
    # watcher can fire on "the status changed" rather than "the status is bad".
    observe: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    # A floor on how often the condition may cause a run.
    min_seconds_between_runs: float = DEFAULT_MIN_SECONDS_BETWEEN_RUNS
    # Set when the condition first fires, cleared when a run happens. Used by `file_newer_than`.
    last_fired_at: float = 0.0
    # Disable the Locus after the first run this condition fires.
    #
    # Their rule, and the reason it is not simply "after the first fire": OpenClaw ties `once` to a **successful
    # fired payload**, so a `once` watcher whose run failed stays armed and can retry. Getting this backwards
    # would silently drop a one-shot watch whose one attempt failed.
    once: bool = False

    def validate(self) -> str:
        """A problem with this condition, or "". Checked at write time so a typo is caught by the author."""
        if self.kind not in KINDS:
            return f"unknown kind {self.kind!r}; use one of {', '.join(KINDS)}"
        if not self.description.strip():
            return "a condition needs a description: it is what the agent is told when it wakes"
        if self.kind == KIND_COMMAND and not self.command.strip():
            return "kind 'command' needs a command"
        if self.kind in OBSERVING_KINDS and not self.observe.strip():
            return f"kind {self.kind!r} needs an `observe` command: its output is the value that is compared"
        if self.kind in (KIND_FILE_NEWER, KIND_FILE_MISSING, KIND_PATH_EXISTS) and not self.path.strip():
            return f"kind {self.kind!r} needs a path"
        if self.timeout_seconds <= 0:
            return "timeout_seconds must be positive"
        if self.min_seconds_between_runs < 0:
            return "min_seconds_between_runs cannot be negative"
        return ""

    def evaluate(self, *, since: float = 0.0) -> ConditionResult:
        """Check the condition. Never raises; an unrunnable condition reports `error=True`.

        `since` is the timestamp of the last run, for the time-based kinds.

        The observing kinds need the previous observation, so they are evaluated by `should_run` instead, which has
        the state. Calling this directly on one returns an error result rather than a wrong answer, because a
        `changed` condition with nothing to compare against cannot say anything true.
        """
        problem = self.validate()
        if problem:
            return ConditionResult(holds=True, error=True, detail=f"invalid condition: {problem}")

        if self.kind == KIND_ALWAYS:
            return ConditionResult(holds=True, detail=self.description or "always")

        if self.kind == KIND_COMMAND:
            return self._evaluate_command()

        if self.kind in OBSERVING_KINDS:
            return ConditionResult(
                holds=True, error=True,
                detail=(
                    f"{self.description}: this kind compares against a previous observation, so it must be "
                    "evaluated through should_run(), which holds the trigger state"
                ),
            )

        return self._evaluate_path(since)

    def observe_once(self) -> tuple[str | None, ConditionResult]:
        """Run the `observe` command and return `(value, result)`.

        The value is returned separately from `holds` because the decision to fire needs the PREVIOUS value, which
        this method does not have. It is a pure reader: comparing and committing is `should_run`'s job, so a failure
        here cannot leave the state half-updated.

        A command that cannot run yields `value=None` -- "nothing observed" -- which leaves the stored fingerprint
        intact. Reporting an empty string instead would erase the fingerprint and make the next tick fire on a
        change that did not happen.
        """
        if not self.observe.strip():
            return None, ConditionResult(
                holds=False, error=True,
                detail=f"{self.description}: no observe command is configured",
            )
        # Same reasoning as `_evaluate_command`: catch the typo before the shell hides it. An `observe` command that
        # cannot be found exits non-zero with no output, which `observe_once` would otherwise treat as "nothing
        # observed" and stay quiet about -- the silent direction again.
        missing = unresolvable_program(self.observe)
        if missing:
            return None, ConditionResult(
                holds=True, error=True, detail=f"{self.description}: {missing}", observation=None
            )
        try:
            completed = subprocess.run(
                self.observe,
                shell=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                text=True,
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return None, ConditionResult(
                holds=True, detail=f"{self.description}: the observation did not finish within "
                                   f"{self.timeout_seconds:.0f}s", error=True,
                observation=None,
            )
        except OSError as exc:
            return None, ConditionResult(
                holds=True, error=True,
                detail=f"{self.description}: the observation could not be run ({exc})",
                observation=None,
            )

        output = (completed.stdout or "").strip()
        if completed.returncode != 0 and not output:
            # Non-zero AND silent: nothing was observed, so there is nothing to compare. Waking here is the safe
            # direction -- an observation command that keeps failing must not look like a quiet watcher.
            return None, ConditionResult(
                holds=True, error=True,
                detail=f"{self.description}: the observation exited {completed.returncode} without printing "
                       "anything, so there is nothing to compare",
                observation=None,
            )
        return output, ConditionResult(holds=False, detail=self.description, observation=output)

    def _evaluate_command(self) -> ConditionResult:
        """Run the command; exit 0 means the condition holds.

        A command that cannot run **wakes the agent**. The reasoning is the opposite of the guardrail layer's and
        for a different reason: a guardrail that fails might let something harmful through, while a watchdog that
        fails goes blind. A typo in `systemctl` must not silently disable the agent that was meant to notice the
        service was down -- so this reports the failure and returns `holds=True`.

        The resolution check runs FIRST, because it is the only chance to catch a typo. Once the shell has run, a
        missing program and a failed check have the same exit status and nothing can separate them. See
        `unresolvable_program` for exactly which command shapes can and cannot be checked.
        """
        missing = unresolvable_program(self.command)
        if missing:
            return ConditionResult(holds=True, error=True, detail=f"{self.description}: {missing}")

        try:
            completed = subprocess.run(
                self.command,
                shell=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                text=True,
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return ConditionResult(
                holds=True, error=True,
                detail=f"{self.description}: the check did not finish within {self.timeout_seconds:.0f}s",
            )
        except OSError as exc:
            return ConditionResult(
                holds=True, error=True,
                detail=f"{self.description}: the check could not be run ({exc})",
            )

        output = (completed.stdout or completed.stderr or "").strip().splitlines()
        excerpt = output[0][:160] if output else ""
        if completed.returncode == 0:
            return ConditionResult(
                holds=True, detail=f"{self.description}: the check passed" + (f" ({excerpt})" if excerpt else "")
            )
        return ConditionResult(
            holds=False,
            detail=f"{self.description}: quiet (exit {completed.returncode})" + (f" ({excerpt})" if excerpt else ""),
        )

    def _evaluate_path(self, since: float) -> ConditionResult:
        target = Path(self.path).expanduser()
        try:
            exists = target.exists()
        except OSError as exc:
            return ConditionResult(
                holds=True, error=True, detail=f"{self.description}: {self.path} could not be examined ({exc})"
            )

        if self.kind == KIND_PATH_EXISTS:
            return ConditionResult(
                holds=exists,
                detail=f"{self.description}: {'exists' if exists else 'is absent'}",
            )
        if self.kind == KIND_FILE_MISSING:
            return ConditionResult(
                holds=not exists,
                detail=f"{self.description}: {'missing' if not exists else 'present'}",
            )

        # KIND_FILE_NEWER: modified since the last run.
        if not exists:
            return ConditionResult(holds=False, detail=f"{self.description}: {self.path} does not exist")
        try:
            modified = target.stat().st_mtime
        except OSError as exc:
            return ConditionResult(
                holds=True, error=True, detail=f"{self.description}: {self.path} could not be stat-ed ({exc})"
            )
        if since <= 0:
            # No previous run: treating this as "changed" would fire on every first evaluation, and treating it as
            # quiet would mean a fresh agent never noticed an existing file. It reports the state and holds, so the
            # first run happens and the timestamp is then meaningful.
            return ConditionResult(
                holds=True, detail=f"{self.description}: first evaluation, {self.path} exists"
            )
        age = time.time() - modified
        holds = modified > since
        return ConditionResult(
            holds=holds,
            detail=(
                f"{self.description}: {self.path} "
                + (f"changed {int(age)}s ago" if holds else f"unchanged for {int(age)}s")
            ),
        )


def should_run(
    condition: Condition | None,
    *,
    last_run_at: float,
    now: float | None = None,
    state: TriggerState | None = None,
    commit: bool = True,
) -> tuple[bool, ConditionResult, TriggerState]:
    """Whether a condition warrants a run, why, and the state after the evaluation.

    Returns `(should_run, result, state)`. The state is returned rather than mutated in place so the caller decides
    when it becomes durable -- see `TriggerState` for why a failed run must not keep the new state.

    `commit=False` evaluates against the stored observation **without** recording a new one, and returns the state
    **unchanged**. That is what a dry run needs: `myriaxon doctor` must be able to ask "would this fire?" without
    changing the answer for the next real tick. Returning a mutated state with `commit=False` was the first
    version, and it was a trap -- a caller that stored the returned value would have consumed the very change it
    was only asking about.

    `min_seconds_between_runs` is enforced **first**, before the condition is even evaluated. A condition that
    flaps -- a log appended every second -- would otherwise drive a run every tick, and the floor is cheaper to
    check than the condition itself.
    """
    moment = time.time() if now is None else now
    current = state if state is not None else TriggerState()

    if condition is None:
        return True, ConditionResult(holds=True, detail="no condition configured: the interval decides"), current

    floor = max(0.0, condition.min_seconds_between_runs)
    if last_run_at and floor and (moment - last_run_at) < floor:
        remaining = int(floor - (moment - last_run_at))
        return False, ConditionResult(
            holds=False,
            detail=f"{condition.description}: within the {int(floor)}s floor ({remaining}s to go)",
        ), current

    if condition.kind in OBSERVING_KINDS:
        return _should_run_observing(condition, current, moment, commit=commit)

    result = condition.evaluate(since=last_run_at)
    return result.holds, result, current


def _should_run_observing(
    condition: Condition,
    state: TriggerState,
    moment: float,
    *,
    commit: bool,
) -> tuple[bool, ConditionResult, TriggerState]:
    """The `changed` kind: fire when the observed value differs from the last one.

    ## The first evaluation is quiet, deliberately

    With no previous observation there is nothing to have changed *from*, so the first check records a baseline and
    does not fire. Firing on the first tick would make every watcher speak once on creation for no reason, which is
    the noise the whole condition mechanism exists to remove.

    ## An error fires, and does not touch the fingerprint

    If the observation cannot be taken, the result is an error and the condition holds -- so the agent wakes and
    someone finds out. The stored observation is returned unchanged, so the next successful check is compared
    against the last *good* value rather than against nothing.
    """
    value, result = condition.observe_once()
    if value is None:
        # Cannot observe: wake, and leave the fingerprint alone.
        return True, result, state

    if not state.observed_at:
        # Baseline. Quiet, but recorded, so the next check has something to compare against.
        updated = state if not commit else TriggerState(
            observation=value, revision=state.revision, observed_at=moment, fires=state.fires
        )
        if commit:
            return False, ConditionResult(
                holds=False,
                detail=f"{condition.description}: baseline recorded, watching for changes",
                observation=value,
            ), updated
        return False, ConditionResult(
            holds=False, detail=f"{condition.description}: no baseline recorded yet", observation=value
        ), state

    if value == state.observation:
        # Unchanged. Quiet, and the revision does NOT move, so `revision` stays a faithful count of changes.
        return False, ConditionResult(
            holds=False,
            detail=f"{condition.description}: unchanged ({len(value)} chars)",
            observation=value,
        ), state

    changed = TriggerState(
        observation=value,
        revision=state.revision + 1,
        observed_at=moment,
        fires=state.fires + 1,
    )
    previous = state.observation
    return True, ConditionResult(
        holds=True,
        detail=(
            f"{condition.description}: CHANGED from {_summarise(previous)} to {_summarise(value)}"
        ),
        observation=value,
    ), (changed if commit else state)


def _summarise(value: str, limit: int = 60) -> str:
    """A short, single-line rendering of an observation, for a log line or an alert."""
    flat = " ".join(value.split())
    if len(flat) <= limit:
        return repr(flat)
    return repr(flat[: limit - 1] + "…")
