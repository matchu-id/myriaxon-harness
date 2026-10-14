from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt

from .tool_context import ToolContext

ASK_USER_QUESTION_TOOL_DEF = {
    "name": "ask_user_question",
    "description": (
        "Ask the user a clarifying question with a fixed set of options when you're genuinely blocked on "
        "a decision only they can make. Available in every mode, including one-shot (-p), since asking is "
        "not a mutating action; there, a closed stdin yields no answer rather than an error."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["label"],
                },
            },
        },
        "required": ["question", "options"],
    },
}


def execute_ask_user_question(tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    console: Console = ctx.console
    question = tool_input["question"]
    options = tool_input.get("options") or []
    if not options:
        return "ask_user_question requires at least one option.", True
    # A plain string is a label (models often send ["Yes", "No"]); an object may call it name, text or value.
    normalized = []
    for option in options:
        if not isinstance(option, dict):
            option = {"label": str(option)}
        label = str(option.get("label") or option.get("name") or option.get("text") or option.get("value") or "").strip()
        if not label:
            return "ask_user_question: every option needs a 'label'.", True
        normalized.append(dict(option, label=label))
    options = normalized

    lines = [question, ""]
    for i, option in enumerate(options, start=1):
        label = option.get("label", "")
        description = option.get("description", "")
        lines.append(f"{i}. {label}" + (f" — {description}" if description else ""))
    console.print(Panel("\n".join(lines), title="[tool.confirm.question]Question[/]"))

    valid_indices = {str(i) for i in range(1, len(options) + 1)}
    valid_labels = {opt.get("label", "").lower(): opt for opt in options}

    try:
        while True:
            answer = Prompt.ask("Your choice (number or label)", console=console).strip()
            if answer in valid_indices:
                chosen = options[int(answer) - 1]
                break
            if answer.lower() in valid_labels:
                chosen = valid_labels[answer.lower()]
                break
            console.print(f"[warning]Not a valid option: {escape(answer)}[/]")
    except EOFError:
        return (
            "No interactive terminal available to answer; proceeding without a choice -- "
            "make a reasonable default or ask via plain text instead.",
            True,
        )

    label = chosen.get("label", "")
    description = chosen.get("description", "")
    return f"{label}" + (f": {description}" if description else ""), False
