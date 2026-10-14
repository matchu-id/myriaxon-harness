"""Sandbox escalation: a strictly-wider, one-call-only grant with a required justification.

## The gap this closes

Myriaxon has `sandbox_enabled`, a boolean: `run_bash` either runs inside a throwaway Docker container or directly
on the host. That is one switch for the whole session, and turning it off to run one command that needs the
network turns it off for every later command too. There is no middle ground, no per-call grant, and no record of
who widened what.

DeepSeek's `sandbox/sandbox-policy` and `sandbox/sandbox/escalation.ts` model this properly, and four of their
decisions are worth copying exactly:

**Three modes, ordered, with the safest as the default.** `read-only` → `workspace-write` → `danger-full-access`.
A session starts at the safest and each step up is explicit. (Their default is `read-only`; Myriaxon's equivalent
default is `workspace-write`, because a coding harness whose default cannot write files is not usable — that is a
deliberate divergence, recorded here rather than silently taken.)

**Escalation must be STRICTLY wider.** Their `approveEscalation` refuses any target that is not strictly wider
than the current mode, and they check it *at execution* rather than in the schema enum, "because effective mode is
per-call truth". An enum in a tool schema is a suggestion to the model; the check that matters happens where the
decision is made.

**One approval, one call.** `approveEscalation` "returns the mode for THAT ONE CALL". A grant that outlives the
call it was given for is how a one-time exception becomes a standing permission nobody remembers granting.
Verified: a second call at the elevated mode is refused without a second grant.

**The retry hint travels inside the denial.** Their refusal returns a shared marker
`[sandbox: file access denied under <mode> mode]` **plus** "a same-turn escalation hint naming the exact retry
shape — so the sanctioned retry doesn't depend on the model recalling the tool description." Adding the
`justification` and `sandbox_permissions` arguments is not discoverable from memory under pressure, so the denial
says how to ask. That is the difference between a guardrail that redirects and one that just blocks.

## What is dropped

Their platform enforcement providers — Windows ACLs, Landlock, seatbelt — which are per-OS FFI with no Python
equivalent worth hand-rolling. What is kept is the *policy*: the mode ladder, the strictly-wider rule, the
one-call grant and the hint. Enforcement on this machine remains the existing Docker-or-host choice, and this
module composes the decision that feeds it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Ordered safest first. The index IS the comparison, so the ordering cannot drift from the checks that use it.
READ_ONLY = "read-only"
WORKSPACE_WRITE = "workspace-write"
DANGER_FULL_ACCESS = "danger-full-access"
MODES = (READ_ONLY, WORKSPACE_WRITE, DANGER_FULL_ACCESS)

# The absolute ceiling. A caller may lower it (a locked-down deployment) but never raise it, which is the rule
# their workflow agent cap uses and the reason a config file cannot widen a restriction.
CEILING = DANGER_FULL_ACCESS

# What each mode permits, for reporting. Kept as data rather than prose scattered through the checks.
MODE_PERMITS: dict[str, tuple[str, ...]] = {
    READ_ONLY: ("read any file the process can read", "run commands that do not write"),
    WORKSPACE_WRITE: (
        "read any file the process can read",
        "write inside the workspace",
        "run commands that write inside the workspace",
    ),
    DANGER_FULL_ACCESS: (
        "read any file the process can read",
        "write anywhere the process can write",
        "run any command, including network access and package installs",
    ),
}

# Their marker, kept verbatim because it is a stable string other tooling can match on.
DENIAL_MARKER = "[sandbox: file access denied under {mode} mode]"

# The two arguments an escalation needs. Named here so the hint and the validation cannot disagree.
ARG_MODE = "sandbox_permissions"
ARG_JUSTIFICATION = "justification"


def rank(mode: str) -> int:
    """Position in the ladder. An unknown mode ranks as the SAFEST, so a typo cannot widen anything."""
    try:
        return MODES.index(mode)
    except ValueError:
        return 0


def normalise(mode: str) -> str:
    """A mode string from user or config input, defaulting to the safest on anything unrecognised."""
    candidate = (mode or "").strip().lower().replace("_", "-")
    return candidate if candidate in MODES else READ_ONLY


def is_wider(candidate: str, current: str) -> bool:
    """Whether `candidate` is STRICTLY wider than `current`."""
    return rank(candidate) > rank(current)


@dataclass
class Grant:
    """One approval, bound to one call.

    `spent` is the whole point: the grant is consumed when the call it was approved for runs, so a second call
    cannot inherit it.
    """

    mode: str
    justification: str
    granted_at: float = field(default_factory=time.time)
    spent: bool = False
    call_id: str = ""

    def render(self) -> str:
        state = "spent" if self.spent else "live"
        return f"  grant {self.mode} [{state}]: {self.justification[:90]}"


@dataclass
class EscalationDecision:
    """The outcome of asking to run at a wider mode."""

    allowed: bool
    mode: str
    reason: str = ""
    hint: str = ""
    grant: Grant | None = None
    # True when the request was refused for a reason the caller can fix by re-asking correctly.
    retryable: bool = False

    def render(self) -> str:
        lines = [f"  {'allow' if self.allowed else 'deny'}: {self.mode}"]
        if self.reason:
            lines.append(f"      {self.reason}")
        if self.hint:
            lines.append(f"      {self.hint}")
        return "\n".join(lines)


class SandboxPolicy:
    """The composed policy for one run: a session mode, a ceiling, and the live grants.

    "Effective mode is per-call truth" is their phrase, and it is why the mode lives here rather than being read
    from a tool schema: the schema says what the model may *ask* for, and this decides what it *gets*.
    """

    def __init__(
        self,
        mode: str = WORKSPACE_WRITE,
        *,
        ceiling: str = CEILING,
        workspace: str = "",
        allow_escalation: bool = True,
    ) -> None:
        self.ceiling = normalise(ceiling)
        # A configured mode above the ceiling is clamped rather than honoured, so a config file cannot widen what
        # the deployment allows.
        requested = normalise(mode)
        self.mode = requested if rank(requested) <= rank(self.ceiling) else self.ceiling
        self.workspace = workspace
        self.allow_escalation = allow_escalation
        self.grants: list[Grant] = []
        self.denials = 0

    # ---- asking

    def request(
        self,
        target: str,
        justification: str,
        *,
        call_id: str = "",
    ) -> EscalationDecision:
        """Ask to run one call at `target`. Returns a decision; never raises.

        Four refusals, each with a distinct message because each is fixed differently:

        * no justification -- the argument is required, not decorative: it is what a person reads later to decide
          whether the grant was reasonable;
        * not strictly wider -- asked-for escalation to the mode already in force is not an escalation, and
          granting it would make every call consume a grant for nothing;
        * above the ceiling -- refused regardless of justification;
        * escalation disabled for this run.
        """
        if not self.allow_escalation:
            return self._refuse(
                target,
                "escalation is disabled for this run",
                retryable=False,
            )

        candidate = normalise(target)
        if not (justification or "").strip():
            return self._refuse(
                candidate,
                f"{ARG_JUSTIFICATION} is required when asking for {ARG_MODE}: it is what a person reads later "
                "to judge whether the wider access was reasonable",
                retryable=True,
            )

        if rank(candidate) > rank(self.ceiling):
            return self._refuse(
                candidate,
                f"{candidate} is above this run's ceiling of {self.ceiling}, so it cannot be granted at all",
                retryable=False,
            )

        if not is_wider(candidate, self.mode):
            return self._refuse(
                candidate,
                f"the run is already at {self.mode}, which is the same or wider. Escalation must be strictly "
                "wider, so this needs no grant -- call the tool without it",
                retryable=True,
            )

        grant = Grant(mode=candidate, justification=justification.strip(), call_id=call_id)
        self.grants.append(grant)
        return EscalationDecision(
            allowed=True,
            mode=candidate,
            reason=f"granted for one call at {candidate}",
            grant=grant,
        )

    def _refuse(self, mode: str, reason: str, *, retryable: bool) -> EscalationDecision:
        self.denials += 1
        return EscalationDecision(
            allowed=False,
            mode=mode,
            reason=reason,
            hint=self.retry_hint(mode if rank(mode) > rank(self.ceiling) else "") if retryable else "",
            retryable=retryable,
        )

    # ---- using

    def consume(self, grant: Grant | None) -> bool:
        """Spend a grant. Returns whether it was live. A grant is usable exactly once."""
        if grant is None:
            return False
        if grant.spent:
            return False
        grant.spent = True
        return True

    def effective_mode(self, grant: Grant | None = None) -> str:
        """The mode for the current call: the grant's while it is live, otherwise the run's own.

        Per-call truth. A spent grant contributes nothing, so a forgotten grant cannot keep widening calls.
        """
        if grant is not None and not grant.spent:
            return grant.mode
        return self.mode

    def live_grants(self) -> list[Grant]:
        return [g for g in self.grants if not g.spent]

    # ---- refusing

    def denial(self, *, needed: str = "") -> str:
        """The text a refused operation returns, with the sanctioned retry attached.

        Their rule, and the reason this method exists rather than a bare string: the retry hint must travel
        *inside* the denial, "so the sanctioned retry doesn't depend on the model recalling the tool
        description". Knowing that you may ask is not the same as knowing how, and under a refusal the model is
        least likely to reconstruct the argument shape from memory.
        """
        marker = DENIAL_MARKER.format(mode=self.mode)
        return f"{marker} {self.retry_hint(needed)}".strip()

    def retry_hint(self, needed: str = "") -> str:
        """How to ask for more, in the exact shape the call needs."""
        if not self.allow_escalation:
            return (
                "Escalation is disabled for this run, so re-asking will not help. "
                "Report the limitation rather than working around it."
            )
        target = needed if rank(needed) > rank(self.mode) else self._next_mode()
        if target == "" or rank(target) > rank(self.ceiling):
            return (
                f"This run's ceiling is {self.ceiling}, so the access needed is not available here. "
                "Report what you would need and why."
            )
        return (
            f"To retry this one call with more access, pass {ARG_MODE}=\"{target}\" together with a "
            f"{ARG_JUSTIFICATION} saying why that access is needed. The grant covers this call only; "
            "a later call needs its own."
        )

    def _next_mode(self) -> str:
        index = rank(self.mode)
        return MODES[index + 1] if index + 1 < len(MODES) else ""

    def render(self) -> str:
        lines = [f"  sandbox: {self.mode} (ceiling {self.ceiling})"]
        for permit in MODE_PERMITS.get(self.mode, ()):
            lines.append(f"      permits: {permit}")
        for grant in self.grants:
            lines.append(grant.render())
        if self.denials:
            lines.append(f"      {self.denials} denial(s) this run")
        return "\n".join(lines)


# --------------------------------------------------------------------------- tool-facing helpers


def escalation_arguments() -> dict:
    """The two properties an enforcing tool adds to its schema.

    Their pairing rule is copied: `sandbox_permissions` and `justification` are rejected *alone*. Accepting the
    mode without a reason produces grants nobody can evaluate afterwards, which is the same as no justification
    at all.
    """
    return {
        ARG_MODE: {
            "type": "string",
            "enum": list(MODES),
            "description": (
                "Ask to run this ONE call with wider access than the current sandbox mode. Must be strictly "
                f"wider than the mode in force, and {ARG_JUSTIFICATION} is required alongside it."
            ),
        },
        ARG_JUSTIFICATION: {
            "type": "string",
            "description": (
                "Why this call needs the wider access. Required when sandbox_permissions is set, and read by a "
                "person afterwards to judge whether it was reasonable."
            ),
        },
    }


def validate_escalation_arguments(tool_input: dict) -> str:
    """Check the two arguments as a pair. Returns a problem, or "" when they are consistent.

    The pairing rule matters because each alone is useless in a different way: a mode with no reason produces a
    grant nobody can evaluate, and a reason with no mode asks for nothing.
    """
    if not isinstance(tool_input, dict):
        return ""
    mode = str(tool_input.get(ARG_MODE) or "").strip()
    justification = str(tool_input.get(ARG_JUSTIFICATION) or "").strip()
    if mode and not justification:
        return (
            f"{ARG_MODE} was given without {ARG_JUSTIFICATION}. Pass both, or neither -- a grant with no "
            "stated reason cannot be judged afterwards."
        )
    if justification and not mode:
        return (
            f"{ARG_JUSTIFICATION} was given without {ARG_MODE}, so it asks for nothing. Either name the mode "
            "you need or drop the justification."
        )
    return ""


def policy_from_context(ctx) -> SandboxPolicy:
    """The policy for a run, derived from the context.

    Reads `sandbox_enabled` (the existing Docker-or-host flag) rather than replacing it: with the container on,
    the run is at `workspace-write` and escalation is unavailable, because the container IS the boundary and
    widening past it would defeat the reason it was enabled.
    """
    enabled = bool(getattr(ctx, "sandbox_enabled", False))
    return SandboxPolicy(
        mode=WORKSPACE_WRITE if enabled else WORKSPACE_WRITE,
        ceiling=WORKSPACE_WRITE if enabled else CEILING,
        workspace=str(getattr(ctx, "workspace", "") or ""),
        allow_escalation=not enabled,
    )
