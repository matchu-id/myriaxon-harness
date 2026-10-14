import json

from rich.markup import escape
from rich.panel import Panel

from . import checkpoints
from .constants import NOTEBOOK_SOURCE_CHAR_CAP
from .tool_context import ToolContext
from .tools import _resolve, confirm_action, render_diff

NOTEBOOK_TOOL_DEF = {
    "name": "edit_notebook",
    "description": (
        "Edit a Jupyter notebook (.ipynb) cell: replace, insert, or delete. cell_index is "
        "0-indexed. Requires user confirmation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "cell_index": {"type": "integer"},
            "new_source": {"type": "string", "description": "Required unless edit_mode is delete."},
            "edit_mode": {
                "type": "string",
                "enum": ["replace", "insert", "delete"],
                "description": "Defaults to replace.",
            },
            "cell_type": {
                "type": "string",
                "enum": ["code", "markdown"],
                "description": "Used only for insert. Defaults to code.",
            },
        },
        "required": ["path", "cell_index"],
    },
}


def _cell_source_text(cell: dict) -> str:
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else source


def _source_to_nb_lines(text: str) -> list[str]:
    if text == "":
        return []
    lines = text.splitlines(keepends=True)
    return lines


def execute_edit_notebook(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    console = ctx.console
    path = _resolve(tool_input["path"])
    edit_mode = tool_input.get("edit_mode", "replace")
    cell_index = tool_input["cell_index"]

    if not path.exists():
        return f"Notebook not found: {path}", True

    try:
        notebook = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return f"Could not read notebook {path}: {exc}", True

    cells = notebook.get("cells")
    if not isinstance(cells, list):
        return f"{path} does not look like a valid notebook (no 'cells' list).", True

    if edit_mode == "insert":
        new_source = tool_input.get("new_source", "")
        if len(new_source) > NOTEBOOK_SOURCE_CHAR_CAP:
            return f"new_source exceeds {NOTEBOOK_SOURCE_CHAR_CAP} characters.", True
        if not (0 <= cell_index <= len(cells)):
            return f"cell_index {cell_index} out of range (0..{len(cells)}).", True

        new_cell = {
            "cell_type": tool_input.get("cell_type", "code"),
            "metadata": {},
            "source": _source_to_nb_lines(new_source),
        }
        if new_cell["cell_type"] == "code":
            new_cell["execution_count"] = None
            new_cell["outputs"] = []

        render_diff(console, path, "", new_source)
        panel = Panel(
            f"Insert a new cell at index {cell_index} in [path]{escape(str(path))}[/]?",
            style="tool.confirm.question",
        )
        proceed, blocked = confirm_action(console, panel, ctx, "edit_notebook")
        if blocked:
            return blocked, False
        if not proceed:
            return "User declined to run this tool call.", False

        cells.insert(cell_index, new_cell)

    elif edit_mode == "delete":
        if not (0 <= cell_index < len(cells)):
            return f"cell_index {cell_index} out of range (0..{len(cells) - 1}).", True

        old_source = _cell_source_text(cells[cell_index])
        render_diff(console, path, old_source, "")
        panel = Panel(f"Delete cell {cell_index} in [path]{escape(str(path))}[/]?", style="tool.confirm.question")
        proceed, blocked = confirm_action(console, panel, ctx, "edit_notebook")
        if blocked:
            return blocked, False
        if not proceed:
            return "User declined to run this tool call.", False

        del cells[cell_index]

    else:  # replace
        if not (0 <= cell_index < len(cells)):
            return f"cell_index {cell_index} out of range (0..{len(cells) - 1}).", True
        if "new_source" not in tool_input:
            return "new_source is required for edit_mode=replace.", True

        new_source = tool_input["new_source"]
        if len(new_source) > NOTEBOOK_SOURCE_CHAR_CAP:
            return f"new_source exceeds {NOTEBOOK_SOURCE_CHAR_CAP} characters.", True

        old_source = _cell_source_text(cells[cell_index])
        render_diff(console, path, old_source, new_source)
        panel = Panel(
            f"Apply this edit to cell {cell_index} in [path]{escape(str(path))}[/]?",
            style="tool.confirm.question",
        )
        proceed, blocked = confirm_action(console, panel, ctx, "edit_notebook")
        if blocked:
            return blocked, False
        if not proceed:
            return "User declined to run this tool call.", False

        cells[cell_index]["source"] = _source_to_nb_lines(new_source)

    checkpoints.snapshot_and_write(path, json.dumps(notebook, indent=1), "edit_notebook", ctx)
    return f"{edit_mode} applied to {path} (cell {cell_index}).", False
