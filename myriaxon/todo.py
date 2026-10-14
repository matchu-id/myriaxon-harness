from rich.markup import escape

from .tool_context import ToolContext

TODO_WRITE_TOOL_DEF = {
    "name": "todo_write",
    "description": (
        "Replace the current task checklist with an updated list. Call proactively as multi-step "
        "work starts and finishes. No confirmation required. Optionally give each item an 'id' "
        "and list other items' ids in 'depends_on' to express ordering -- items with incomplete "
        "dependencies are shown as blocked, which is useful for tracking a plan with real "
        "ordering constraints instead of just a flat list."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Optional short id, for use in depends_on."},
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional ids of other items that must be 'completed' first.",
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    },
}

_STATUS_STYLE = {
    "pending": "dim",
    "in_progress": "tool.pending",
    "completed": "success",
}
_STATUS_MARK = {
    "pending": "[ ]",
    "in_progress": "[~]",
    "completed": "[x]",
}


def _blocked_dependency_ids(item: dict, by_id: dict[str, dict]) -> list[str]:
    """Ids this item depends on that aren't 'completed' yet. Unknown ids (typo, or the
    dependency was removed in a later todo_write call) are silently ignored rather than
    treated as blocking -- a stale/missing id shouldn't deadlock the whole checklist."""
    return [
        dep_id
        for dep_id in item.get("depends_on") or []
        if dep_id in by_id and by_id[dep_id].get("status") != "completed"
    ]


def render_todos(todos: list[dict], console) -> None:
    """Shared by execute_todo_write (after the model updates the list) and /todos (the user
    just looking at the current list without needing to ask the model to summarize it)."""
    by_id = {item["id"]: item for item in todos if item.get("id")}
    for item in todos:
        blocked_on = _blocked_dependency_ids(item, by_id)
        if blocked_on and item["status"] != "completed":
            style, mark = "dim", "[#]"
            suffix = f"  [dim](blocked on: {', '.join(blocked_on)})[/]"
        else:
            style = _STATUS_STYLE.get(item["status"], "dim")
            mark = _STATUS_MARK.get(item["status"], "[ ]")
            suffix = ""
        # escape(mark) -- confirmed live this was a real bug: "[x]" and "[#]" were silently
        # swallowed by Rich's markup parser (it matched them as attempted style tags), while
        # "[~]" happened to survive since "~" isn't a valid tag character. Same bracket-swallow
        # class of bug hit repeatedly elsewhere this session (/help, /mcp list) -- style is a
        # real, intentional Rich style name here and stays unescaped; mark is literal text that
        # happens to contain brackets.
        console.print(f"[{style}]{escape(mark)} {escape(item['content'])}[/]{suffix}")


_STATUS_ALIASES = {"done": "completed", "complete": "completed", "finished": "completed", "todo": "pending",
                   "open": "pending", "doing": "in_progress", "active": "in_progress", "inprogress": "in_progress"}


def normalize_todos(raw) -> tuple[list[dict] | None, str]:
    """The checklist as `{content, status, ...}` items, accepting what a model plainly means.

    A plain string is a pending item; an item without a status is pending; "done" means completed. What cannot be guessed (an
    item with no content, a status that is none of the three) is refused by item number, so the model can fix that one.
    """
    cleaned: list[dict] = []
    for index, item in enumerate(raw or [], start=1):
        if isinstance(item, str):
            item = {"content": item}
        if not isinstance(item, dict):
            return None, f"item {index} must be an object with 'content' and 'status' (or a plain string), got {type(item).__name__}."
        content = str(item.get("content") or item.get("title") or item.get("text") or "").strip()
        if not content:
            return None, f"item {index} has no 'content'."
        status = str(item.get("status") or "pending").strip().lower().replace("-", "_").replace(" ", "_")
        status = _STATUS_ALIASES.get(status, status)
        if status not in _STATUS_MARK:
            return None, f"item {index} has status {item.get('status')!r}; it must be one of: pending, in_progress, completed."
        entry = dict(item)
        entry["content"] = content
        entry["status"] = status
        cleaned.append(entry)
    return cleaned, ""


def execute_todo_write(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    todos, problem = normalize_todos(tool_input["todos"])
    if todos is None:
        return f"todo_write: {problem}", True
    ctx.todos[:] = todos
    render_todos(todos, ctx.console)
    return f"Todo list updated ({len(todos)} items).", False
