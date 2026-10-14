"""Always-on turns: a heartbeat that stays quiet unless something needs attention.

## What this is, and where it comes from

An agent that runs on a long-term objective without being prompted needs three things that a scheduler alone does
not provide. OpenClaw's heartbeat has all three, and Myriaxon had none of them:

1. **A response contract that permits silence.** Their rule, verbatim: reply `NO_REPLY` "if nothing needs
   attention"; or call `heartbeat_respond` with `notify: false` for no visible update, `notify: true` plus
   `notificationText` for an alert. A structured tool response takes precedence over the text fallback. The legacy
   `HEARTBEAT_OK` marker is accepted at the **start or end** of a reply and the reply is dropped when the rest is
   at most 300 characters — with a fixed suppression budget, and *not* treated specially in the middle of a reply.

   Without this, an unattended agent has only two behaviours: say nothing ever, or speak every tick. The contract
   is what makes "I checked and there is nothing to report" a legitimate outcome rather than a wasted turn.

2. **A standing watchlist.** Their monitor *scratch* is a small persistent checklist that the heartbeat prompt
   follows, with an explicit instruction **not** to infer or repeat old tasks from prior chats — "so a default
   install stays quiet instead of rehashing stale conversation context." That last clause is the important one:
   an always-on agent with no watchlist invents work from whatever is in its context.

3. **Rate limits that survive a burst.** A 30-second minimum between event-driven turns, and a flood guard after
   five starts within 60 seconds, with deferred work resuming when the guard expires.

## The judgement this module makes

Deciding whether a reply is an alert or an acknowledgment is **deterministic** here, not a model call. A model
asked "was that an alert?" would add a second paid call to every quiet tick, which is exactly the cost a silent
path exists to avoid. The rules are a fixed budget and a fixed marker, copied from theirs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# The text fallback for "nothing needs attention". Their literal.
NO_REPLY = "NO_REPLY"

# The legacy acknowledgment marker. Accepted at the START or END of a reply only.
HEARTBEAT_OK = "HEARTBEAT_OK"

# Their fixed suppression budget: a reply carrying the marker is dropped when the remaining content is at most
# this many characters. Fixed rather than configurable, because a budget that can be raised is one that will be.
SUPPRESSION_BUDGET = 300

# Rate limits, from their doc: "a 30-second minimum between event turns, and a flood guard after five starts
# within 60 seconds. Deferred work resumes when its guard expires."
MIN_SECONDS_BETWEEN_EVENT_TURNS = 30.0
FLOOD_WINDOW_SECONDS = 60.0
FLOOD_LIMIT = 5

# Verdicts. A closed set so a caller branches on it rather than matching prose.
ALERT = "alert"            # something needs attention; deliver it
SILENT = "silent"          # nothing to report; say nothing
REMEMBERED = "remembered"  # nothing visible now, but keep it as bounded context for the next turn
DEFERRED = "deferred"      # suppressed by a rate limit; resume when the guard expires
VERDICTS = (ALERT, SILENT, REMEMBERED, DEFERRED)


@dataclass
class Outcome:
    """What a heartbeat turn produced, and what should happen with it."""

    verdict: str
    text: str = ""
    reason: str = ""
    # Set for REMEMBERED: bounded internal context carried to the next user turn. Their rule is that this is the
    # LATEST outcome for the session, "not an alert history or exact-delivery replay queue".
    remembered: str = ""
    at: float = field(default_factory=time.time)

    @property
    def visible(self) -> bool:
        return self.verdict == ALERT

    def render(self) -> str:
        if self.verdict == ALERT:
            return f"ALERT: {self.text}"
        return f"{self.verdict}: {self.reason or self.text or 'nothing to report'}"


def _strip_marker(text: str) -> tuple[str, bool]:
    """Remove a `HEARTBEAT_OK` at the start or end of a reply. Returns `(remaining, was_marked)`.

    Position matters, and their rule is explicit: a marker in the **middle** of a reply "is not treated
    specially". Stripping it from the middle would turn "the backup is fine, HEARTBEAT_OK means healthy, but the
    disk is 95% full" into an acknowledgment and lose the alert.

    Stripped at most once from each end, so `HEARTBEAT_OK HEARTBEAT_OK` leaves one behind rather than vanishing.
    """
    body = (text or "").strip()
    marked = False

    for _ in range(2):  # start, then end
        if body.startswith(HEARTBEAT_OK):
            rest = body[len(HEARTBEAT_OK):].lstrip(" \t\n:-,.")
            if rest != body:
                body = rest
                marked = True
                continue
        if body.endswith(HEARTBEAT_OK):
            rest = body[: -len(HEARTBEAT_OK)].rstrip(" \t\n:-,.")
            if rest != body:
                body = rest
                marked = True
        break

    return body.strip(), marked


def _is_pure_acknowledgment(text: str) -> bool:
    """Whether a reply that carried a marker says nothing else worth reading.

    ## Stricter than the reference, on purpose

    OpenClaw suppresses a marked reply when the remaining content is at most 300 characters. Applied literally that
    drops **"HEARTBEAT_OK disk is 95% full"** -- 24 characters, inside the budget, and an alert about the exact
    thing a monitor exists to notice. Measured: it classified as silent.

    Their own guidance points the other way ("For alerts, return only the alert text. Do not include a silent
    acknowledgment"), so this is a case of a source rule that is right about its own well-behaved models and wrong
    about a model that appends a warning to the marker. For an unattended monitor the asymmetry decides it: a
    spurious alert costs one glance, and a swallowed one costs the incident.

    So suppression now requires the remainder to be *nothing* or a bare acknowledgment phrase from a closed list.
    A reply with any other content is an alert, whatever its length.
    """
    body = (text or "").strip().strip(".,!;:-").lower()
    if not body:
        return True
    return body in {
        "ack", "acknowledged", "all clear", "all good", "no change", "no changes",
        "nothing to report", "nothing to do", "nothing new", "ok", "okay", "fine", "healthy",
    }


def classify(text: str, *, structured: "StructuredReply | None" = None) -> Outcome:
    """Decide what to do with a heartbeat turn's reply.

    A structured reply wins over the text, which is their rule: "when present, the structured tool response takes
    precedence over the text fallback."

    Order of the checks, and each is deliberate:

    1. a structured `notify: false` with text is **remembered**, not dropped — their distinction between "no visible
       update" and "nothing happened" is the one that lets an unattended agent build context across quiet ticks;
    2. `NO_REPLY` alone is silent;
    3. a marker at start or end, with the rest being *nothing or a bare acknowledgment*, is silent — the legacy
       path, deliberately narrower than the reference's fixed 300-character budget (see
       `_is_pure_acknowledgment`);
    4. anything else is an alert. **The default is to speak**, because a false alert costs a glance and a
       swallowed alert costs the thing it was watching for.
    """
    if structured is not None:
        if structured.notify:
            alert_text = (structured.notification_text or text or "").strip()
            if not alert_text:
                # `notify: true` with nothing to say. Delivering an empty alert is worse than staying quiet.
                return Outcome(
                    verdict=SILENT, reason="notify was true but no alert text was given"
                )
            return Outcome(verdict=ALERT, text=alert_text, reason="structured alert")
        note = (structured.notification_text or text or "").strip()
        if note:
            return Outcome(
                verdict=REMEMBERED, remembered=note,
                reason="no visible update, kept as context for the next turn",
            )
        return Outcome(verdict=SILENT, reason="structured no-change")

    stripped = (text or "").strip()
    if not stripped:
        # An empty reply. Treated as silent rather than as an empty alert, which is what their reasoning-only rule
        # amounts to: a turn that produced no payload produces no alert.
        return Outcome(verdict=SILENT, reason="the turn produced no reply")

    if stripped == NO_REPLY:
        return Outcome(verdict=SILENT, reason="NO_REPLY")

    remaining, marked = _strip_marker(stripped)
    if marked:
        if _is_pure_acknowledgment(remaining):
            return Outcome(
                verdict=SILENT,
                reason=f"acknowledged with {HEARTBEAT_OK} and nothing else to report",
            )
        # Marked, but there is a real message attached. Their rule for alerts: return only the alert text, do not
        # include a silent acknowledgment.
        return Outcome(
            verdict=ALERT, text=remaining,
            reason=(
                f"{HEARTBEAT_OK} was present but the reply also carried "
                f"{len(remaining)} characters of substance, so it is treated as an alert"
            ),
        )

    return Outcome(verdict=ALERT, text=stripped, reason="no acknowledgment present")


@dataclass
class StructuredReply:
    """`heartbeat_respond`'s payload, as the reference harness defines it."""

    notify: bool = False
    notification_text: str = ""


def respond(notify: bool, notification_text: str = "") -> str:
    """Render a `heartbeat_respond` call for a tool to return.

    Kept as text rather than a rich object because that is what a tool returns, and the classification above reads
    it back through `parse_respond`.
    """
    import json

    return json.dumps({"notify": bool(notify), "notificationText": notification_text}, ensure_ascii=False)


def parse_respond(text: str) -> StructuredReply | None:
    """Read a `heartbeat_respond` payload back, or None when the text is not one."""
    import json

    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or "notify" not in data:
        return None
    return StructuredReply(
        notify=bool(data.get("notify")),
        notification_text=str(data.get("notificationText") or data.get("notification_text") or ""),
    )


# --------------------------------------------------------------------------- rate limiting


@dataclass
class WakeLedger:
    """Rate limits for event-driven wakes.

    Their numbers, and their reasoning for having them at all: an event that fires repeatedly -- a file written in
    a loop, a process that exits and restarts -- would otherwise drive an unbounded number of paid turns, and the
    agent would look busy rather than broken.
    """

    last_event_turn_at: float = 0.0
    recent_starts: list[float] = field(default_factory=list)
    deferred_until: float = 0.0

    def allow(self, now: float | None = None) -> tuple[bool, str]:
        """Whether an event-driven turn may start. Returns `(allowed, reason)`.

        ## The flood guard is checked FIRST, and that ordering is the fix

        The first version checked the 30-second minimum before the flood window. Measured consequence: the minimum
        always fires first for any burst, so the flood guard's backlog is only ever reached at exactly 30 seconds
        after a start -- and at 61 seconds it allows the turn through. The guard was effectively dead code, which
        is the worst state for a limit: present in the source, believed to be protecting something, and
        unreachable.

        With the flood check first, both do their own job: this rejects a *burst* inside a 60-second window, and
        the minimum then spaces the turns that survive it.
        """
        moment = time.time() if now is None else now

        if moment < self.deferred_until:
            return False, f"flood guard active for another {int(self.deferred_until - moment)}s"

        # The flood window, rolled forward.
        window_start = moment - FLOOD_WINDOW_SECONDS
        self.recent_starts = [t for t in self.recent_starts if t >= window_start]
        if len(self.recent_starts) >= FLOOD_LIMIT:
            self.deferred_until = moment + FLOOD_WINDOW_SECONDS
            return False, (
                f"{len(self.recent_starts)} starts within {int(FLOOD_WINDOW_SECONDS)}s; deferring work until the "
                "guard expires"
            )

        if self.last_event_turn_at and (moment - self.last_event_turn_at) < MIN_SECONDS_BETWEEN_EVENT_TURNS:
            remaining = MIN_SECONDS_BETWEEN_EVENT_TURNS - (moment - self.last_event_turn_at)
            return False, f"within the {int(MIN_SECONDS_BETWEEN_EVENT_TURNS)}s minimum ({int(remaining)}s to go)"

        return True, ""

    def record(self, now: float | None = None) -> None:
        moment = time.time() if now is None else now
        self.last_event_turn_at = moment
        self.recent_starts.append(moment)


# --------------------------------------------------------------------------- the standing watchlist


MAX_SCRATCH_CHARS = 4_000


@dataclass
class Scratch:
    """The standing watchlist: a short checklist the heartbeat follows.

    Their name for it is *monitor scratch*, and their prompt tells the agent to follow it and **not** to infer or
    repeat old tasks from prior chats. That second instruction is the whole reason the field exists: an always-on
    agent with no watchlist invents work from whatever happens to be in its context, which is how a quiet monitor
    turns into a chatty one.
    """

    notes: str = ""
    updated_at: float = 0.0

    def show(self) -> str:
        return self.notes.strip()

    def set(self, notes: str) -> tuple[bool, str]:
        """Replace the watchlist, refusing one over the budget.

        Bounded because the scratch is prepended to every heartbeat prompt: an unbounded watchlist is a per-tick
        cost, and a long one stops being a checklist.
        """
        cleaned = (notes or "").strip()
        if len(cleaned) > MAX_SCRATCH_CHARS:
            return False, (
                f"the watchlist is {len(cleaned)} characters, over the {MAX_SCRATCH_CHARS} budget. It is sent with "
                "every wake-up, so keep it to the things worth checking every time."
            )
        self.notes = cleaned
        self.updated_at = time.time()
        return True, "watchlist updated"

    def append(self, note: str) -> tuple[bool, str]:
        line = " ".join((note or "").split())
        if not line:
            return False, "nothing to add"
        combined = (self.notes + "\n" + "- " + line).strip() if self.notes else "- " + line
        return self.set(combined)


def heartbeat_prompt(scratch: Scratch, *, custom: str = "", extra: str = "") -> str:
    """The turn text for an unprompted heartbeat.

    Three parts, and the middle one is the contract that makes silence legitimate:

    * the watchlist, when there is one;
    * the reply rules, stated so a quiet turn is an *answer* rather than a failure;
    * an explicit instruction not to invent work from earlier conversation.

    A custom prompt replaces the watchlist section but never the reply contract: a heartbeat with no silence
    protocol would message on every tick.
    """
    lines: list[str] = []

    if custom.strip():
        lines.append(custom.strip())
    elif scratch.show():
        lines.append("Your standing watchlist for this check:")
        lines.append(scratch.show())
        lines.append("Check these. Do the work if any of them needs it.")
    else:
        lines.append(
            "This is a periodic check with no standing watchlist yet. If there is genuinely nothing that needs "
            "attention, say so and stop."
        )

    if extra.strip():
        lines.append(extra.strip())

    lines.append(
        "Reply rules -- read these before answering:\n"
        f"  - If nothing needs attention, reply exactly {NO_REPLY} and nothing else.\n"
        "  - If you checked and want to record something for later without notifying anyone, call "
        "heartbeat_respond with notify=false and your note.\n"
        "  - If something DOES need attention, reply with the alert text alone. Do not add an acknowledgment.\n"
        "  - When you do report, say what you found, what you did, and what remains. A bare \"all good\" is only "
        "useful alongside the evidence for it."
    )
    lines.append(
        "Do NOT infer new tasks from earlier conversation, and do not repeat work you already reported on. If "
        "your watchlist does not cover something you think matters, add it with the scratch tool rather than "
        "acting on it silently."
    )
    return "\n\n".join(lines)
