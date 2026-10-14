"""A rich Console that turns the agent's own output into structured events.

The agents already report everything they do by printing to `self.console`: status notices, warnings,
errors, reasoning fragments, and the final answer. Rather than adding a parallel reporting channel to
every agent (which would drift out of sync with what the terminal shows), the dashboard substitutes a
Console whose `print` emits events instead of writing to a terminal. The UI therefore shows what the
CLI would have shown, by construction.

Rich renderables are flattened to text: `Markdown` keeps its source in `.markup`, `Text` and
`TextualType` expose `.plain`, and anything else falls back to `str()`. Rich severity markup
(`[error]`, `[warning]`, `[success]`, `[dim]`) is mapped to an event severity so the UI can colour it
rather than printing literal brackets.
"""
from __future__ import annotations

import re
import threading
from typing import Any

from rich.console import Console

from .events import EventBus

_SEVERITY_RE = re.compile(r"^\[(error|warning|success|info|dim|bold|italic|prompt|muted)[^\]]*\]")


def renderable_to_text(renderable: Any) -> str:
    """Flatten a rich renderable to the text a user would have seen."""
    if renderable is None:
        return ""
    # Markdown keeps the original source; using .plain would give the raw markup stripped of
    # structure, but the UI renders markdown itself, so the source is the more useful payload.
    markup = getattr(renderable, "markup", None)
    if isinstance(markup, str):
        return markup
    plain = getattr(renderable, "plain", None)
    if isinstance(plain, str):
        return plain
    # ConsoleRenderable / Group / Table etc.
    try:
        from rich.text import Text

        if isinstance(renderable, Text):
            return renderable.plain
    except ImportError:  # pragma: no cover
        pass
    return str(renderable)


def classify(text: str) -> tuple[str, str]:
    """Returns (severity, cleaned_text) for a printed line."""
    match = _SEVERITY_RE.match(text)
    if not match:
        return "normal", text
    tag = match.group(1)
    severity = {
        "error": "error",
        "warning": "warning",
        "success": "success",
        "dim": "muted",
        "muted": "muted",
        "italic": "muted",
    }.get(tag, "normal")
    cleaned = text[match.end():]
    # Rich tags are often closed with a matching [/] that would otherwise leak into the text.
    if cleaned.endswith("[/]"):
        cleaned = cleaned[:-3]
    else:
        cleaned = re.sub(r"\[/[^\]]*\]$", "", cleaned)
    return severity, cleaned


class EventConsole(Console):
    """Console that publishes instead of printing.

    `no_color` and a non-terminal file keep rich from trying to emit ANSI codes or query the
    terminal, which matters because this runs in a server thread with no tty attached.
    """

    def __init__(self, bus: EventBus, *, session_id: str = "", **kwargs: Any) -> None:
        kwargs.setdefault("no_color", True)
        kwargs.setdefault("highlight", False)
        kwargs.setdefault("markup", True)
        kwargs.setdefault("emoji", False)
        kwargs.setdefault("width", 100)
        super().__init__(**kwargs)
        self._bus = bus
        self._session_id = session_id
        self._partial = ""
        self._lock = threading.Lock()
        # Whether the answer for the current turn has already been streamed token by token. When it
        # has, the agent's final print of the completed text must NOT be published again -- that was
        # producing the answer twice in the UI: once rendered as markdown from the deltas, and once
        # as a raw unformatted block from this final print.
        self._answer_streamed = False
        # What was streamed, so the agent's final print of the *same* text can be recognised as an
        # echo. Without it the guard suppressed every later print in the turn.
        self._streamed_answer = ""
        # Whether ANY answer text reached the browser during this turn, streamed or whole. The
        # session's fallback publishes `last_assistant_text()` when nothing was streamed -- and when a
        # turn FAILS, that fallback returned the PREVIOUS turn's reply, so a 401 was rendered
        # underneath a cheerful answer from a minute earlier. Measured in the browser as a failed turn
        # showing "Hi! Anything else I can help you with? 😊" directly below the authentication error.
        self._saw_answer = False

    def begin_turn(self) -> None:
        """Reset per-turn streaming state. Called by the session before each turn."""
        with self._lock:
            self._answer_streamed = False
            self._streamed_answer = ""
            self._partial = ""
            self._saw_answer = False

    def note_answer_event(self) -> None:
        """Record that an answer event was published for the current turn."""
        with self._lock:
            self._saw_answer = True

    @property
    def saw_answer(self) -> bool:
        """Whether the browser was given any answer text during this turn.

        A turn that failed before its first token has NOT been seen, so the fallback must stay quiet
        rather than repeating the last thing the model said in some earlier turn.
        """
        with self._lock:
            return self._saw_answer

    @property
    def answer_streamed(self) -> bool:
        """Whether this turn's answer already reached the browser as deltas."""
        with self._lock:
            return self._answer_streamed

    def print(self, *objects: Any, **kwargs: Any) -> None:  # type: ignore[override]
        end = kwargs.get("end", "\n")
        text = "".join(renderable_to_text(obj) for obj in objects)

        with self._lock:
            # `end=""` means the caller is streaming fragments of one logical line (reasoning text).
            # Buffering them avoids emitting one event per token, and the buffer is flushed as soon
            # as a normal print arrives.
            if end == "":
                self._partial += text
                if len(self._partial) >= 400:
                    self._flush_locked("thinking")
                return
            if self._partial:
                self._flush_locked("thinking")
            # The final answer is printed by every agent once the stream completes. If the same text
            # already went out as deltas, publishing it again duplicates the bubble.
            #
            # The test is EQUALITY against what was streamed, not "anything at all once something has
            # streamed". The old form returned early for every non-empty print after the first delta,
            # so the rest of the turn was silent: truncation warnings, prune and compaction notices,
            # delegation panels, tool-name lines. The bug was invisible because the answer itself is
            # normally the last thing printed. Now only the echo is suppressed.
            if self._answer_streamed and text.strip() and text.strip() == self._streamed_answer.strip():
                return
            self._emit_locked(text)

    def _flush_locked(self, kind: str) -> None:
        buffered, self._partial = self._partial, ""
        if buffered:
            self._bus.publish(kind, text=buffered)

    def _emit_locked(self, text: str) -> None:
        if not text.strip():
            return
        severity, cleaned = classify(text)
        if not cleaned.strip():
            return
        self._bus.publish("notice", text=cleaned, severity=severity)

    def flush_partial(self) -> None:
        """Emit anything still buffered -- called when a turn ends so nothing is lost."""
        with self._lock:
            if self._partial:
                self._flush_locked("thinking")

    def stream_answer(self, text: str) -> None:
        """Publish the cumulative answer text so the browser can render it as it grows.

        The agents already track the accumulating answer and pass it to `live.update(...)` on every
        delta, so hooking that call is the one place where streaming is already available without
        touching the agent loop. The payload is cumulative rather than incremental because the UI
        re-renders markdown on each update, and markdown cannot be rendered correctly from fragments
        (a half-received table or bold span would render as literal text).

        Called from the streaming loops; a no-op if the console is a normal one.
        """
        if not text:
            return
        with self._lock:
            self._answer_streamed = True
            self._saw_answer = True
            # Kept so `print` can recognise the echo of this exact text and suppress only that.
            self._streamed_answer = text
        self._bus.publish("answer_delta", text=text)

    def stream_thinking(self, text: str) -> None:
        """Publish reasoning text as it arrives, so the UI can show it while the model thinks."""
        if not text:
            return
        self._bus.publish("thinking", text=text)

    # The dashboard never prompts at a terminal; ask_user is answered over the API instead. A plain
    # input() here would deadlock a server thread with no stdin, so it returns empty rather than
    # blocking forever.
    def input(self, *args: Any, **kwargs: Any) -> str:  # type: ignore[override]
        return ""

    def is_terminal(self) -> bool:  # type: ignore[override]
        return False
