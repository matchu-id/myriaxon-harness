"""Hash-bound approval: an approval authorises one exact action, once, and cannot be replayed.

## The gap this closes

Nothing in Myriaxon could ask "may I do this specific thing?" and remember the answer safely. The permission
modes gate *tool classes* (`--yolo` skips confirmation for write/edit/bash), which is a blanket. For a
consequential one-off — replacing a live skill, deleting a Locus, overwriting a config file — the choices were a
blanket mode or an interactive prompt with no memory.

The risk in adding a remembered approval is that it becomes a replayable token. OpenClaw's
`src/system-agent/operator-approval.ts` is careful about exactly this, and the two rules below are copied from it
verbatim in behaviour:

1. **the approval is bound to a content hash of the operation** — `sha256(stable_stringify(op))`. Approving
   "publish the release" does not approve "publish the release" with a different version in it. Their comment
   for `resolvePendingOperatorProposal`: it returns null when the pending hash does not match, so a stale
   approval cannot be spent on a different action that happens to share a name.
2. **authority is consumed *before* the action runs**, "so duplicate callbacks and failures cannot replay an
   already-approved operation." This is the one people get wrong. Consuming after means a crash between the
   action and the consume leaves a live token; consuming first means at worst the action is refused and the user
   re-approves, which is the safe direction to fail.

They also accept only `allow-once` here — `allow-always` is deliberately not honoured, because a standing grant
turned a single decision into an unbounded one.

## What is dropped

Their operation union has ~40 variants, `stableStringify`, and a separate live-inference gate that must be
satisfied before *any* mutation. The union and the inference gate are product surface, not the mechanism. What is
kept is the hash binding, the consume-first ordering, the one-shot decision, and a fixed approve/decline phrase
list so a conversational "yes" is recognisable without a model call.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# A single decision, once. Deliberately no `allow_always`: a standing grant converts one decision into an
# unbounded one, and that belongs behind a permission mode rather than behind a conversational "yes".
ALLOW_ONCE = "allow-once"
DENY = "deny"

# Whole-message approval phrases, from their `APPROVE_RE`. Whole-message rather than substring on purpose: "no,
# don't go ahead" must not match on the "go ahead" inside it.
APPROVE_RE = re.compile(
    r"^(?:y|yes|yeah|yep|yup|sure|ok|okay|approve|approved|apply|confirm|confirmed|do it|go ahead|"
    r"sounds good|yes please|please do)[.!]*$",
    re.IGNORECASE,
)

# Prefix match, from their `DECLINE_RE`: a decline must win over anything that follows it, because the dangerous
# misread is "no thanks, go ahead" being taken as assent.
DECLINE_RE = re.compile(
    r"^(?:n|no|nope|nah|skip|not now|cancel|stop|abort|later|decline|don'?t)\b",
    re.IGNORECASE,
)

# Approvals expire. A token minted for a decision made an hour ago is not a decision about the current state of
# the world, and holding one open indefinitely is how a forgotten prompt becomes a silent grant.
DEFAULT_TTL_SECONDS = 900.0

_LOCK = threading.RLock()
_PENDING: dict[str, "PendingApproval"] = {}


def stable_stringify(value: Any) -> str:
    """Canonical JSON: sorted keys, no incidental whitespace.

    The hash must depend on the operation's *content*, not on dict insertion order or spacing, or the same
    action would produce different hashes on different runs and every approval would fail to match.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def operation_hash(operation: Any) -> str:
    """`sha256` over the canonical form of an operation. This is what an approval is bound to."""
    return hashlib.sha256(stable_stringify(operation).encode("utf-8")).hexdigest()


def classify_reply(text: str) -> str:
    """Map a reply to `allow-once`, `deny`, or "" when it is not a decision at all.

    No model call. A conversational approval has to be recognisable deterministically, or the mechanism depends
    on the very thing it is meant to gate.
    """
    stripped = (text or "").strip()
    if not stripped:
        return ""
    # Decline first: "no, go ahead" and "don't apply this" both contain approval words, and reading them as
    # assent is the failure that matters.
    if DECLINE_RE.match(stripped):
        return DENY
    if APPROVE_RE.match(stripped):
        return ALLOW_ONCE
    return ""


@dataclass
class PendingApproval:
    """One action awaiting a decision."""

    approval_id: str
    title: str
    operation: Any
    fingerprint: str
    created_at: float
    expires_at: float
    detail: str = ""
    severity: str = "warning"     # "info" | "warning"
    requested_by: str = ""
    # Set when the decision is consumed. The record survives so a second attempt can be told *why* it failed
    # rather than being told the id is unknown.
    consumed_at: float = 0.0
    decision: str = ""
    terminal_reason: str = ""

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    def render(self) -> str:
        state = self.decision or ("expired" if self.expired else "pending")
        lines = [
            f"  {self.approval_id}  [{state}] {self.title}",
            f"      fingerprint {self.fingerprint[:16]}",
        ]
        if self.detail:
            lines.append(f"      {self.detail}")
        if self.decision:
            lines.append(f"      decided at {time.strftime('%H:%M:%S', time.localtime(self.consumed_at))}")
        return "\n".join(lines)


@dataclass
class ApprovalOutcome:
    """The result of asking for or deciding an approval."""

    approved: bool
    decision: str = ""
    reason: str = ""
    approval: PendingApproval | None = None
    # True when a decision existed but could not be used -- spent already, expired, or bound to a different
    # operation. Distinguished from "no decision" because the caller's next step differs: re-ask, or fix the call.
    unusable: bool = False


def request(
    title: str,
    operation: Any,
    *,
    detail: str = "",
    severity: str = "warning",
    requested_by: str = "",
    ttl_seconds: float = 0.0,
    approval_id: str = "",
) -> PendingApproval:
    """Record an action awaiting approval. Does not block and returns no decision.

    The split mirrors their `requestApproval` / `waitForApproval` pair: asking and deciding are separate steps,
    because a decision may arrive from a different place (another process, a CLI command, a later turn) than the
    one that asked.

    `ttl_seconds=0` means "use the default". Any positive value is honoured as given, including sub-second ones.
    An earlier version applied `max(1.0, ttl_seconds)`, which read like a safety floor but silently ignored every
    shorter request -- an expiry test passed 50 ms and got a full second, so the expiry path was never exercised.
    A floor nobody asked for is just a wrong answer.
    """
    import uuid

    now = time.time()
    lifetime = ttl_seconds if ttl_seconds > 0 else DEFAULT_TTL_SECONDS
    approval = PendingApproval(
        approval_id=approval_id or f"a-{uuid.uuid4().hex[:12]}",
        title=title,
        operation=operation,
        fingerprint=operation_hash(operation),
        created_at=now,
        expires_at=now + lifetime,
        detail=detail,
        severity=severity if severity in ("info", "warning") else "warning",
        requested_by=requested_by,
    )
    with _LOCK:
        _PENDING[approval.approval_id] = approval
    return approval


def decide(approval_id: str, reply: str, *, operation: Any = None) -> ApprovalOutcome:
    """Apply a conversational reply to a pending approval.

    Two refusals that both matter:

    * **the fingerprint must match.** When `operation` is given, its hash must equal the one the approval was
      bound to. A reply of "yes" to a differently-worded operation is not a decision about this one.
    * **the decision is consumed before it is returned.** Marking it spent first means a duplicate callback
      cannot spend it twice, and a crash between decide and act leaves a spent token rather than a live one.
    """
    classified = classify_reply(reply)
    if not classified:
        return ApprovalOutcome(approved=False, reason="that reply is not a yes or a no")

    with _LOCK:
        approval = _PENDING.get(approval_id)
        if approval is None:
            return ApprovalOutcome(approved=False, reason=f"no approval {approval_id!r}")
        if approval.consumed_at:
            return ApprovalOutcome(
                approved=False,
                unusable=True,
                approval=approval,
                reason=f"this approval was already {approval.decision or 'used'}",
            )
        if approval.expired:
            approval.decision = DENY
            approval.consumed_at = time.time()
            approval.terminal_reason = "expired"
            return ApprovalOutcome(
                approved=False, unusable=True, approval=approval,
                reason="the approval expired before it was decided",
            )
        if operation is not None and operation_hash(operation) != approval.fingerprint:
            return ApprovalOutcome(
                approved=False,
                unusable=True,
                approval=approval,
                reason=(
                    "this approval was granted for a different operation "
                    f"(approved fingerprint {approval.fingerprint[:12]}, this one "
                    f"{operation_hash(operation)[:12]})"
                ),
            )
        # Consumed FIRST. See the module docstring: the safe direction to fail is "refused, re-ask".
        approval.decision = classified
        approval.consumed_at = time.time()
        approval.terminal_reason = "decided"

    return ApprovalOutcome(
        approved=classified == ALLOW_ONCE,
        decision=classified,
        approval=approval,
    )


def spend(approval_id: str, *, operation: Any = None) -> ApprovalOutcome:
    """Consume an already-granted approval immediately before performing the action.

    The second half of the two-phase pattern: `decide` records the decision, `spend` redeems it at the moment of
    the side effect and re-checks the fingerprint. Splitting them means the gap between "the user said yes" and
    "the thing happened" is explicit and each end can be tested.
    """
    with _LOCK:
        approval = _PENDING.get(approval_id)
        if approval is None:
            return ApprovalOutcome(approved=False, reason=f"no approval {approval_id!r}")
        if approval.decision != ALLOW_ONCE:
            return ApprovalOutcome(
                approved=False, approval=approval,
                reason=f"this approval is {approval.decision or 'undecided'}, not granted",
            )
        if approval.consumed_at and approval.terminal_reason == "spent":
            return ApprovalOutcome(
                approved=False, unusable=True, approval=approval,
                reason="this approval has already been spent",
            )
        if operation is not None and operation_hash(operation) != approval.fingerprint:
            return ApprovalOutcome(
                approved=False, unusable=True, approval=approval,
                reason="the operation does not match what was approved",
            )
        approval.terminal_reason = "spent"
        approval.consumed_at = approval.consumed_at or time.time()

    return ApprovalOutcome(approved=True, decision=ALLOW_ONCE, approval=approval)


def get(approval_id: str) -> PendingApproval | None:
    with _LOCK:
        return _PENDING.get(approval_id)


def pending() -> list[PendingApproval]:
    """Undecided, unexpired approvals, oldest first."""
    now = time.time()
    with _LOCK:
        return sorted(
            (a for a in _PENDING.values() if not a.decision and now <= a.expires_at),
            key=lambda a: a.created_at,
        )


def history() -> list[PendingApproval]:
    """Every approval, newest first, including spent and expired ones."""
    with _LOCK:
        return sorted(_PENDING.values(), key=lambda a: a.created_at, reverse=True)


def clear() -> int:
    with _LOCK:
        count = len(_PENDING)
        _PENDING.clear()
        return count


def before_acting(
    approval_id: str,
    operation: Any,
    action: Callable[[], Any],
    *,
    require_live_capability=None,
) -> tuple[bool, Any, str]:
    """Spend the approval, optionally re-assert a run capability, then act. Returns `(ok, result, reason)`.

    The ordering is the whole point and is easy to get wrong in the other direction: capability first (the run
    must still be alive), then the spent approval (the authorisation must still be valid and unused), then the
    action. Reversing any two of those leaves a window where a cancelled run or a replayed approval can still
    act.
    """
    if require_live_capability is not None:
        try:
            require_live_capability.assert_active()
        except Exception as exc:  # noqa: BLE001
            return False, None, f"the run is no longer active: {exc}"

    outcome = spend(approval_id, operation=operation)
    if not outcome.approved:
        return False, None, outcome.reason

    try:
        return True, action(), ""
    except Exception as exc:  # noqa: BLE001
        return False, None, f"{type(exc).__name__}: {exc}"
