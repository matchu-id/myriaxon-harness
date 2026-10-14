import queue
import threading
import time
import uuid

SCHEDULE_PROMPT_TOOL_DEF = {
    "name": "schedule_prompt",
    "description": (
        "Schedule a prompt to be run automatically after a delay, as if the user had typed it. "
        "Session-scoped only -- it fires while this Myriaxon session stays open, and is lost if "
        "the terminal is closed before then (no persistence across process restarts)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "delay_seconds": {"type": "integer", "description": "How many seconds from now to fire."},
            "prompt": {"type": "string"},
        },
        "required": ["delay_seconds", "prompt"],
    },
}

LIST_SCHEDULED_TOOL_DEF = {
    "name": "list_scheduled",
    "description": "List prompts currently scheduled to fire later in this session. Read-only, no confirmation.",
    "input_schema": {"type": "object", "properties": {}},
}

CANCEL_SCHEDULED_TOOL_DEF = {
    "name": "cancel_scheduled",
    "description": "Cancel a previously scheduled prompt by its id.",
    "input_schema": {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
}

_POLL_INTERVAL_SECONDS = 1.0


class Scheduler:
    """Session-scoped only -- a background daemon thread (same pattern as tools.py's
    _BackgroundProcess, not McpBridge's asyncio loop, since there's no async I/O here) that wakes
    periodically and pushes due prompts onto a thread-safe queue. It never calls agent.run_turn()
    itself: Agent/OpenAIAgent/GeminiAgent and ToolContext are only ever touched from the main
    thread today, so firing a prompt means the main REPL loop must drain this queue between its
    own prompts and run the turn itself -- see cli.py's repl()."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, str]] = {}  # id -> (fire_at, prompt)
        self._due_queue: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            time.sleep(_POLL_INTERVAL_SECONDS)
            now = time.time()
            with self._lock:
                due_ids = [entry_id for entry_id, (fire_at, _) in self._entries.items() if fire_at <= now]
                due_prompts = [self._entries.pop(entry_id)[1] for entry_id in due_ids]
            for prompt in due_prompts:
                self._due_queue.put(prompt)

    def schedule(self, delay_seconds: int, prompt: str) -> str:
        entry_id = uuid.uuid4().hex[:8]
        with self._lock:
            self._entries[entry_id] = (time.time() + delay_seconds, prompt)
        return entry_id

    def list_entries(self) -> list[dict]:
        now = time.time()
        with self._lock:
            return [
                {"id": entry_id, "seconds_remaining": max(0, round(fire_at - now)), "prompt": prompt}
                for entry_id, (fire_at, prompt) in self._entries.items()
            ]

    def cancel(self, entry_id: str) -> bool:
        with self._lock:
            return self._entries.pop(entry_id, None) is not None

    def pop_due_prompt(self) -> str | None:
        """Non-blocking: returns the next due prompt if one is waiting, else None."""
        try:
            return self._due_queue.get_nowait()
        except queue.Empty:
            return None
