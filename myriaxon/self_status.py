"""Self-inspection: what the agent can report about its own current state.

## Why this exists

OpenClaw ships `session_status` (verified in `src/agents/`: it reports a status card including a timestamp line and
can set a per-session model override). Myriaxon had nothing equivalent. An agent could not answer "what model am I
running as, how full is my context, which tools do I actually have, what is my budget" -- so it could not reason
about its own limits, and a person watching had to reconstruct the same facts from side channels.

## What makes this different from the capability directory

The capability directory (the `request_tools` group list plus `tool_search`) answers *"what can I do?"*. This
answers *"what state am I in?"* -- which model, which mode, how much context is used, how much budget is left,
which skills and sub-agents exist. Those are different questions and OpenClaw keeps them separate too: the
directory rides the cache-stable prefix, and volatile state travels as a **separate typed runtime-context
carrier** precisely so it does not invalidate that prefix.

So this is a *tool*, not a prompt section. A tool costs one call when the model wants to know, and nothing when it
does not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


def _field(ctx: Any, name: str, default: Any = "") -> Any:
    """Read a context attribute defensively.

    The three provider agents carry slightly different shapes, and a self-report that raised on one of them would
    be worse than useless: it would fail exactly when the model was trying to find out what was wrong.
    """
    return getattr(ctx, name, default)


@dataclass
class Section:
    """One block of the report."""

    title: str
    lines: list[str] = field(default_factory=list)

    def render(self) -> str:
        if not self.lines:
            return ""
        body = "\n".join(f"  {line}" for line in self.lines)
        return f"{self.title}\n{body}"


def _identity(ctx: Any) -> Section:
    section = Section("identity")
    model = _field(ctx, "model", "") or "(unset)"
    provider = _field(ctx, "provider", "") or "(unset)"
    section.lines.append(f"model: {model}")
    section.lines.append(f"provider: {provider}")
    section.lines.append(f"mode: {_field(ctx, 'mode', 'auto')}")
    owner = _field(ctx, "owner", None)
    if owner is not None:
        section.lines.append(f"running as: {type(owner).__name__}")
    locus_source = _field(ctx, "locus_run_source", "")
    section.lines.append(
        "turn source: " + (str(locus_source) if locus_source else "interactive")
        + ("" if locus_source == "human" else "")
    )
    return section


def _tools(ctx: Any) -> Section:
    section = Section("tools")
    tools = _field(ctx, "tools", None) or []
    section.lines.append(f"currently callable: {len(tools)}")
    try:
        from . import tool_registry

        everything = tool_registry.build_tool_defs()
        visible = tool_registry.apply_tool_visibility(everything, set())
        section.lines.append(f"visible by default: {len(visible)}")
        section.lines.append(f"total defined: {len(everything)}")
        groups = getattr(owner_of(ctx), "unlocked_tool_groups", None)
        if groups:
            section.lines.append(f"deferred groups loaded: {', '.join(sorted(groups))}")
        else:
            section.lines.append(
                "deferred groups loaded: none -- call tool_search to find one, or request_tools by name"
            )
    except Exception:
        section.lines.append("(the registry could not be read)")
    return section


def owner_of(ctx: Any) -> Any:
    """The agent a context belongs to, if it can be found."""
    owner = _field(ctx, "owner", None)
    return owner if owner is not None else ctx


def _context(ctx: Any) -> Section:
    section = Section("context")
    limit = _field(ctx, "result_char_limit", None)
    if limit:
        section.lines.append(f"current tool-result budget: {int(limit):,} chars")
    truncated = bool(_field(ctx, "truncated_any_result", False))
    spilled = bool(_field(ctx, "spilled_any_result", False))
    section.lines.append(f"a result has been truncated this session: {truncated}")
    section.lines.append(f"a result has been spilled to disk this session: {spilled}")
    if spilled:
        section.lines.append(
            "  (spilled results are recoverable: a preview names a file, and `recall` reads it back)"
        )
    try:
        from . import compaction_audit

        recorder = compaction_audit.active()
        if recorder is not None:
            summary = recorder.summary()
            entries = summary.get("entries", {})
            total = sum(int(v) for v in entries.values())
            section.lines.append(f"compaction events recorded: {total}")
            for kind, count in sorted(entries.items()):
                section.lines.append(f"  {kind}: {count}")
            if recorder.failures:
                section.lines.append(f"  audit write failures: {recorder.failures}")
            pending = recorder.interrupted_compaction()
            if pending is not None:
                section.lines.append(
                    "AN INTERRUPTED COMPACTION is on record for this session -- the history may not match "
                    "the summary."
                )
    except Exception:
        pass
    return section


def _budget(ctx: Any) -> Section:
    section = Section("budget")
    run = _field(ctx, "run_budget", None)
    if run is None:
        section.lines.append("no budget is attached to this turn")
        return section
    try:
        section.lines.append(f"ceiling: {run.budget.render()}")
        section.lines.append(f"used: {run.spend.render()}")
        remaining = run.remaining()
        parts = []
        for axis, value in remaining.items():
            parts.append(f"{axis}={'unbounded' if value is None else value}")
        section.lines.append("remaining: " + ", ".join(parts))
        if run.stopped is not None:
            section.lines.append(f"STOPPED: {run.stopped.value}")
    except Exception:
        section.lines.append("(the budget could not be read)")
    return section


def _skills(ctx: Any) -> Section:
    section = Section("skills")
    skills = _field(ctx, "skills", None)
    if skills is None:
        try:
            from . import skills as skills_module

            skills = skills_module.discover_skills()
        except Exception:
            skills = {}
    section.lines.append(f"available: {len(skills)}")
    for name in sorted(skills)[:12]:
        section.lines.append(f"  {name}")
    if len(skills) > 12:
        section.lines.append(f"  ... and {len(skills) - 12} more")
    if not skills:
        section.lines.append("  (none -- save_skill can create one)")
    return section


def _environment(ctx: Any) -> Section:
    """Which interpreter is real on this host, and which candidates are traps.

    Added because the executed comparison run exposed a concrete defect: asked to verify a Python fix, the agent
    called `run_bash` three times hunting for an interpreter and then reported "No Python installed on this machine"
    -- which was false. `python` and `python3` are on PATH but resolve to Microsoft Store App Execution Aliases that
    launch the Store instead of running anything, and there is no `py` launcher on this host.

    Reporting it here costs one line on a host where the aliases are absent, and prevents a wrong conclusion stated
    as fact on one where they are present.
    """
    import sys
    from pathlib import Path as _Path

    section = Section("environment")
    try:
        from . import interpreter

        best = interpreter.find()
        if best is None:
            section.lines.append("python: NONE FOUND -- every candidate was tried and rejected")
        else:
            section.lines.append(f"python: {best.path}")
            section.lines.append(f"  {best.version} (from {best.source})")
        # The rejected candidates are the useful part: they explain a conclusion the model would otherwise reach
        # by itself and get wrong.
        for path, reason in interpreter.rejected()[:3]:
            shown = path.split("  [")[0]
            section.lines.append(f"  do NOT use {shown} -- {reason}")
    except Exception:
        pass
    section.lines.append(f"platform: {sys.platform}")
    section.lines.append(f"working directory: {_Path.cwd()}")
    return section


def _work(ctx: Any) -> Section:
    section = Section("in flight")
    subagents = _field(ctx, "living_subagents", None)
    if isinstance(subagents, dict):
        section.lines.append(f"sub-agents alive: {len(subagents)}")
        for name in sorted(subagents)[:6]:
            section.lines.append(f"  {name}")
    background = _field(ctx, "background_procs", None)
    if isinstance(background, dict):
        section.lines.append(f"background processes: {len(background)}")
    todos = _field(ctx, "todos", None)
    if isinstance(todos, list):
        unfinished = [
            t for t in todos
            if isinstance(t, dict) and str(t.get("status", "")).lower() not in ("completed", "done", "cancelled")
        ]
        section.lines.append(f"todo items: {len(todos)} ({len(unfinished)} unfinished)")
    scheduled = _field(ctx, "scheduler", None)
    if scheduled is not None:
        try:
            pending = scheduled.pending() if hasattr(scheduled, "pending") else None
            if pending is not None:
                section.lines.append(f"scheduled prompts: {len(pending)}")
        except Exception:
            pass
    return section


def _guardrails(ctx: Any) -> Section:
    section = Section("guardrails and policy")
    try:
        from . import tool_registry

        rules = tool_registry._guardrail_rules()
        problems = tool_registry.guardrail_problems()
        section.lines.append(f"guardrail rules in force: {len(rules)}")
        for rule in rules[:8]:
            section.lines.append(f"  {rule.name} ({rule.event}, {rule.action})")
        if problems:
            # Named rather than counted: a rule that failed to load is a guardrail the operator believes is
            # active and is not, which is the single most important thing this report can say.
            section.lines.append(f"RULES THAT FAILED TO LOAD: {len(problems)}")
            for problem in problems[:5]:
                section.lines.append(f"  {problem}")
    except Exception:
        pass
    try:
        from . import sandbox_policy

        policy = sandbox_policy.policy_from_context(ctx)
        section.lines.append(f"sandbox mode: {policy.mode} (ceiling {policy.ceiling})")
        section.lines.append(f"escalation available: {policy.allow_escalation}")
    except Exception:
        pass
    return section


def report(ctx: Any, *, sections: list[str] | None = None) -> str:
    """A status card for the current turn.

    Every section is best-effort: a part that cannot be read says so and the rest still renders. A self-report that
    failed as a whole would be useless exactly when the model most needed it -- when something is wrong.
    """
    builders = {
        "identity": _identity,
        "tools": _tools,
        "context": _context,
        "budget": _budget,
        "skills": _skills,
        "work": _work,
        "guardrails": _guardrails,
        "environment": _environment,
    }
    wanted = sections or list(builders)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    blocks = [f"Status at {stamp} (self-report; every line reads the live state)"]
    for name in wanted:
        builder = builders.get(name)
        if builder is None:
            continue
        try:
            rendered = builder(ctx).render()
        except Exception as exc:  # noqa: BLE001
            rendered = f"{name}\n  (could not be read: {type(exc).__name__}: {exc})"
        if rendered:
            blocks.append(rendered)
    return "\n\n".join(blocks)


SESSION_STATUS_TOOL_DEF = {
    "name": "session_status",
    "description": (
        "Report your own current state: which model and mode you are running as, which tools are actually "
        "callable right now, how full the context is, how much budget is left, which skills and sub-agents "
        "exist, and which guardrail rules are in force.\n"
        "Use this when you need to reason about your own limits -- whether you have budget for a long job, "
        "whether a capability is available to you, or whether a previous compaction left the history "
        "inconsistent. It answers 'what state am I in', which is different from tool_search's 'what can I do'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "enum": ["identity", "tools", "context", "budget", "skills", "work", "guardrails", "all"],
                "description": "Which part to report. Defaults to all of them.",
            },
        },
        "required": [],
    },
}
