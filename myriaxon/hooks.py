import json
import os
import re
import subprocess
from pathlib import Path

from .constants import HOOK_TIMEOUT_SECONDS, PROJECT_HOOKS_SUBPATH, USER_HOOKS_FILE
from .logging_setup import logger
from .tool_context import ToolContext
from .tools import shell_argv

_EVENT_KEYS = ("PreToolUse", "PostToolUse", "UserPromptSubmit")


def _load_hooks_file(path: Path) -> dict[str, list[dict]]:
    if not path.exists():
        return {}
    try:
        # utf-8-sig transparently strips a leading BOM if present (common from Windows tools
        # like Notepad or PowerShell's Set-Content -Encoding utf8) and behaves like plain utf-8
        # otherwise -- a bare "utf-8" decode would leave the BOM in place and break json.loads.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {key: data.get(key, []) for key in _EVENT_KEYS if data.get(key)}


def load_hooks_config(project_root: Path | None = None) -> dict[str, list[dict]]:
    """Merges the user-global and project-local hooks files by list-append per event key --
    deliberately a third, distinct merge policy from skills' name-override and MCP's
    single-file-only, since hook entries are unnamed/independent handlers, not named singletons."""
    merged: dict[str, list[dict]] = {key: [] for key in _EVENT_KEYS}
    user_hooks = _load_hooks_file(USER_HOOKS_FILE)
    root = project_root if project_root is not None else Path.cwd()
    project_hooks = _load_hooks_file(root / PROJECT_HOOKS_SUBPATH)

    for key in _EVENT_KEYS:
        merged[key] = list(user_hooks.get(key, [])) + list(project_hooks.get(key, []))

    return merged


def _matches(matcher: str, tool_name: str) -> bool:
    return not matcher or bool(re.search(matcher, tool_name))


def _run_hook_command(command: str, env_extra: dict[str, str], payload: dict) -> tuple[int, str]:
    """Returns (returncode, combined_stdout_stderr). On timeout, fails open: returncode 0,
    empty output -- a hung hook must never stall every tool call forever."""
    env = {**os.environ, **env_extra}
    try:
        result = subprocess.run(
            shell_argv(command),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=HOOK_TIMEOUT_SECONDS,
        )
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired:
        logger.warning("Hook command timed out after %ss, failing open: %s", HOOK_TIMEOUT_SECONDS, command)
        return 0, ""
    except OSError as exc:
        logger.warning("Hook command failed to start: %s (%s)", command, exc)
        return 0, ""


def run_pre_tool_use(tool_name: str, tool_input: dict, ctx: ToolContext) -> str | None:
    """Returns a block reason string, or None if allowed. A non-zero exit code blocks the tool
    call -- this pre-empts even the interactive confirm_action prompt, since it runs before
    _dispatch_tool ever reaches write_file/edit_file/run_bash/edit_notebook's own gate."""
    for hook in ctx.hooks.get("PreToolUse", []):
        if not _matches(hook.get("matcher", ""), tool_name):
            continue
        returncode, output = _run_hook_command(
            hook["command"], {"MYRIAXON_TOOL_NAME": tool_name}, {"tool_name": tool_name, "tool_input": tool_input}
        )
        if returncode != 0:
            return output.strip() or f"Blocked by PreToolUse hook for {tool_name}."
    return None


def run_post_tool_use(tool_name: str, tool_input: dict, result: str, is_error: bool, ctx: ToolContext) -> None:
    """Fire-and-forget: logs failures, never raises, doesn't affect the tool result already
    returned to the model."""
    for hook in ctx.hooks.get("PostToolUse", []):
        if not _matches(hook.get("matcher", ""), tool_name):
            continue
        try:
            _run_hook_command(
                hook["command"],
                {"MYRIAXON_TOOL_NAME": tool_name, "MYRIAXON_TOOL_IS_ERROR": str(is_error)},
                {"tool_name": tool_name, "tool_input": tool_input, "result": result, "is_error": is_error},
            )
        except Exception:
            logger.exception("PostToolUse hook failed for %s", tool_name)


def run_user_prompt_submit(prompt: str, ctx: ToolContext) -> str:
    """Fire-and-forget except: any hook's stdout gets appended to the prompt as extra context.
    No prompt-blocking -- bounded scope."""
    extra_context = []
    for hook in ctx.hooks.get("UserPromptSubmit", []):
        _, output = _run_hook_command(hook["command"], {}, {"prompt": prompt})
        if output.strip():
            extra_context.append(output.strip())

    if not extra_context:
        return prompt
    return prompt + "\n\n" + "\n\n".join(f"[hook context]\n{text}" for text in extra_context)
