"""Slash commands for the dashboard, mirroring the CLI's own command set.

The CLI's REPL handles these inline against a long-lived agent. The dashboard has the same kind of
long-lived agent per session, so the same semantics apply -- but the results have to reach the
browser as events rather than console output, and some commands (mode, model, provider) change
configuration that the agent bakes in at construction time and therefore require a rebuild.

Only commands that are meaningful and safe without a terminal are implemented; anything interactive
(reauth, rewind selection, mcp add) reports that it needs the CLI instead of pretending to work.
Every implemented command returns {handled, output, severity, rebuilt}.
"""
from __future__ import annotations

from typing import Any

# Commands deliberately not supported over HTTP, with the reason shown to the user.
CLI_ONLY = {
    "reauth": "interactive credential prompts -- run `myriaxon auth` in a terminal",
    "rewind": "interactive checkpoint selection -- run /rewind in the CLI",
    "undo": "restores files from a checkpoint -- run /undo in the CLI",
    "vim": "terminal editing mode -- not applicable to a browser",
    "theme": "terminal colour theme -- not applicable to a browser",
    "shells": "lists background shells for this terminal session",
    "trace": "writes a trace file next to the CLI -- use the Trajectory tab instead",
    "diff": "interactive diff review -- run /diff in the CLI",
    "doctor": "environment diagnostics -- run `myriaxon` and /doctor in a terminal",
    "mcp": "edits MCP server configuration on disk -- run /mcp in the CLI",
    "policy": "prints the merged permission policy -- run /policy in the CLI",
    "sandbox": "changes process sandboxing -- run /sandbox in the CLI",
    "profile": "switches approval profiles that gate terminal prompts -- run /profile in the CLI",
    # These three are real CLI commands that the dashboard had no entry for at all, so `/retry`
    # answered "unknown command -- try /help", which sends the reader looking for a typo instead of
    # telling them where the command lives. Every slash command either works here or names why not.
    "retry": "re-sends the last message as a fresh turn -- use the composer, or /retry in the CLI",
    "plugins": "lists loaded plugins and their failures -- see the Plugins panel in the rail",
    "memory": "full-text search across past sessions -- run /memory in the CLI",
}


def is_command(text: str) -> bool:
    return text.lstrip().startswith("/")


# What each command does, in the browser. Kept beside the implementation so a command cannot be
# added without a description appearing in the composer's suggestions -- the popup is built from
# this table, so a missing entry is a command nobody can discover.
COMMAND_HELP: dict[str, str] = {
    "help": "List the commands available in the dashboard.",
    "commands": "Alias for /help.",
    "loci": ("Persistent agents and bots. /loci list · show <name> · run <name> [message] · "
             "enable|disable <name> · create · goal · proactive · rm <name> --yes. "
             "(chat/off are terminal modes — use Message… on a card here.)"),
    "?": "Alias for /help.",
    "mode": "Set the permission mode: manual, auto_edit, plan or auto.",
    "model": "Switch the active model for this session.",
    "models": "List the models you can switch to.",
    "provider": "Switch vendor/provider for this session.",
    "vendor": ("Switch between the three gateways this build targets: /vendor deepseek | netra | ollama. "
               "Reuses a key already in .env or already saved; no argument shows the active one."),
    "clear": "Clear this conversation.",
    "compact": "Summarise older turns to free context.",
    "context": "Show what is consuming the context window.",
    "cost": "Token usage for this session.",
    "todos": "Show the current task checklist.",
    "yolo": "Toggle auto-accepting write, edit and shell confirmations.",
    "thinking": "Toggle showing the model's reasoning.",
    "effort": "Set the reasoning effort level.",
    "max-tokens": "Set the reply token ceiling.",
    "fallback-model": "Set or clear the model used when the provider rate-limits.",
    "ground": "Toggle the warning when a reply mentions files no tool touched.",
    "export": "Export this transcript as Markdown.",
    "mission": "Mission control as text: health, usage, runs, approvals, schedules, trace.",
}


def catalogue() -> list[dict[str, str]]:
    """Every slash command with its description and whether the browser can run it.

    The composer's suggestion popup is built from this, so the list a user sees is the list that
    actually works -- including the terminal-only ones, marked rather than hidden, because a command
    that vanishes from the menu is one the user will keep trying to remember.
    """
    entries: list[dict[str, str]] = []
    for name, description in sorted(COMMAND_HELP.items()):
        entries.append({"name": name, "slash": f"/{name}", "description": description,
                        "available": True, "reason": ""})
    for name, reason in sorted(CLI_ONLY.items()):
        entries.append({"name": name, "slash": f"/{name}",
                        "description": f"Terminal only — {reason}",
                        "available": False, "reason": reason})
    return sorted(entries, key=lambda e: e["name"])


def _context_lines(agent: Any) -> list[str]:
    """Profile-aware context reading, using the same sources the CLI's /context uses."""
    lines: list[str] = []
    try:
        from ..model_adapt import budget_for_agent
        from ..model_profiles import describe_profile

        profile = getattr(agent, "profile", None)
        if profile is not None:
            lines.append(f"Profile  {describe_profile(profile)}")
        system_prompt = agent._system_prompt()
        tools_chars = len(str(agent.tools))
        state = getattr(agent.tool_context, "budget_state", None) or budget_for_agent(
            agent, system_prompt=system_prompt, tools_chars=tools_chars
        )
        if state is not None:
            lines.append(
                f"Context  {state.used_tokens:,}/{state.window:,} tokens ({state.percent}%) "
                f"{state.meter()}  action={state.action}  estimate={state.confidence}"
            )
            lines.append(f"Headroom {state.headroom_tokens:,} tokens ({state.used_fraction:.1%} of window in use)")
            breakdown = getattr(state, "breakdown", None) or {}
            for name, value in sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True):
                lines.append(f"  {name:<20} {value:,} tokens")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"context unavailable: {type(exc).__name__}: {exc}")
    return lines


def execute(session: Any, text: str) -> dict:
    """Run one slash command against the session's agent. Never raises."""
    from ..constants import EFFORT_LEVELS, MODES, PROMPT_HISTORY_FILE

    raw = text.strip()
    head, _, rest = raw.partition(" ")
    name = head[1:].lower()
    argument = rest.strip()

    def ok(lines: list[str], severity: str = "normal", rebuilt: bool = False) -> dict:
        return {"handled": True, "output": "\n".join(lines), "severity": severity, "rebuilt": rebuilt}

    if name in CLI_ONLY:
        return ok([f"/{name} needs the CLI: {CLI_ONLY[name]}"], "warning")

    try:
        agent = session._ensure_agent()

        if name == "help" or name == "commands":
            # Built from the catalogue, so /help can never list a command the composer does not
            # suggest, or omit one it does.
            available = [e for e in catalogue() if e["available"] and e["name"] not in ("commands", "help")]
            lines = ["Available here:"]
            for entry in available:
                lines.append(f"  {entry['slash']:20} {entry['description']}")
            lines.append("")
            lines.append("Terminal only: " + ", ".join(f"/{k}" for k in sorted(CLI_ONLY)))
            return ok(lines)

        if name == "mode":
            if not argument:
                return ok([f"mode is {session.mode}; choose from {', '.join(MODES)}"])
            if argument not in MODES:
                return ok([f"unknown mode {argument!r}; choose from {', '.join(MODES)}"], "error")
            session.mode = argument
            session.state.mode = argument
            session.agent = None  # mode decides which tools exist, so it is baked in at build time
            return ok([f"mode set to {argument}"], "success", rebuilt=True)

        if name == "model":
            if not argument:
                return ok([f"model is {session.state.model}"])
            session.config = _replace_config(session.config, model=argument)
            session.agent = None
            session.state.model = argument
            return ok([f"model set to {argument}"], "success", rebuilt=True)

        if name == "models":
            try:
                from ..dashboard.server import _models_for

                models = _models_for(session.config, session.endpoints)
            except Exception:  # noqa: BLE001
                models = [session.config.model]
            return ok(["Selectable models:"] + [f"  {m}" for m in models])

        if name == "vendor":
            # The three gateways this build targets, switchable from the composer as well as from the
            # picker. Same idea as `/provider`, different affordance: this one reports a MISSING KEY
            # as a message telling you what to do, where `/provider` takes the key on the command line
            # and would rather fail the switch than ask.
            from ..config import switch_vendor

            if not argument:
                return ok([f"vendor is {session.config.provider}:{session.config.model or '(no model)'}",
                           "  choose one with:  /vendor deepseek | /vendor netra | /vendor ollama"])

            new_config, problem = switch_vendor(session.config, argument)
            if new_config is None:
                return ok([problem], "error")
            session.capture_history()
            session.config = new_config
            session.agent = None
            session.state.provider = new_config.provider
            session.state.model = new_config.model
            session.state.title = session.state.title or f"New session"
            lines = [
                f"vendor set to {new_config.provider}",
                f"  endpoint {new_config.base_url}",
                f"  model    {new_config.model}",
            ]
            if new_config.provider == "ollama":
                from .. import ollama as ollama_module

                lines.extend(ollama_module.startup_notes(new_config.base_url, new_config.model))
            return ok(lines)

        if name == "provider":
            if not argument:
                return ok([f"provider is {session.config.provider}"])
            # Capture first: the rebuild below discards the agent that holds the transcript.
            session.capture_history()
            kept = len(session.history)
            previous_provider = session.config.provider
            try:
                from .server import _switch_provider

                # The endpoints store is passed so a user-defined endpoint is switchable by name from
                # the command as well as the dropdown.
                session.config = _switch_provider(session.config, argument, session.endpoints)
            except ValueError as exc:
                return ok([str(exc)], "error")

            # Refuse a switch that cannot possibly work, BEFORE the agent is rebuilt.
            #
            # This is the fix for a reported `401 Authentication Fails (governor)`. Measured on the
            # real machine: `/provider deepseek` set the provider, reported "key MISSING -- check .env"
            # as one line among five, and switched anyway. The next message went to api.deepseek.com
            # with an EMPTY key and came back 401 -- which reads as "your key is wrong" when the truth
            # is "no key was ever sent". The user cannot tell those apart, and the one place that knew
            # was a line of output they had already scrolled past.
            #
            # The diagnosis matters as much as the refusal: there is no `DEEPSEEK_API_KEY` anywhere on
            # this machine and the only key present belongs to netra, so "check .env" was actively
            # misleading. It says where the key must come from instead.
            provider_id = str(argument).strip().lower()
            if provider_id != "ollama" and not (session.config.api_key or "").strip():
                from ..config import PROVIDER_ENV_KEY_FALLBACK

                env_name = PROVIDER_ENV_KEY_FALLBACK.get(provider_id, f"{provider_id.upper()}_API_KEY")
                # Undo the switch rather than leaving a half-applied state: a provider set but
                # unusable is worse than the previous one, which at least worked.
                session.config = _switch_provider(session.config, previous_provider, session.endpoints)
                return ok(
                    [
                        f"Not switching to {provider_id}: it needs an API key and none is set.",
                        f"  put {env_name}=sk-... in the .env beside the harness and restart it, OR",
                        "  open the Vendor dropdown under the composer and paste the key there.",
                        "  A key for another provider cannot be reused -- this machine has a netra "
                        "key, which api.deepseek.com rejects.",
                        f"Still on {session.config.provider}.",
                    ],
                    "error",
                )

            session.agent = None
            session.state.provider = argument
            session.state.model = session.config.model
            lines = [
                f"provider set to {argument}",
                f"  endpoint {session.config.base_url}",
                f"  model    {session.config.model or '(choose one below)'}",
            ]
            # Ollama has no credential, and saying `key from OLLAMA_API_KEY` for it was actively
            # misleading -- it implies a key is needed and that one was found, when the field holds a
            # placeholder the server ignores.
            if provider_id == "ollama":
                lines.append("  key      none needed -- runs on this machine")
            else:
                lines.append(f"  key      loaded ({len(session.config.api_key)} chars)")
            if kept:
                lines.append(f"  context  {kept} messages carried over (tool calls from the previous vendor are dropped)")
            return ok(lines, "success", rebuilt=True)

        if name == "clear":
            agent.messages = []
            # The portable snapshot is the source of truth across rebuilds, so clearing the live
            # agent without this would let the previous conversation come back on the next switch.
            session.history = []
            todos = getattr(getattr(agent, "tool_context", None), "todos", None)
            if todos is not None:
                todos.clear()
            if session._trace_state is not None:
                session._trace_state.events.clear()
            return ok(["conversation cleared"], "success")

        if name == "compact":
            before = len(getattr(agent, "messages", []))
            agent.compact()
            after = len(getattr(agent, "messages", []))
            return ok([f"compacted: {before} -> {after} messages"], "success")

        if name == "context":
            return ok(_context_lines(agent))

        if name == "cost":
            try:
                from ..usage import format_usage_summary

                summary = format_usage_summary(agent.usage_totals, session.config.provider, agent.model)
            except Exception as exc:  # noqa: BLE001
                summary = f"usage unavailable: {type(exc).__name__}: {exc}"
            return ok(summary.splitlines() or [summary])

        if name == "todos":
            todos = getattr(getattr(agent, "tool_context", None), "todos", None) or []
            if not todos:
                return ok(["no todos yet"])
            lines = []
            for item in todos:
                status = item.get("status", "?")
                mark = {"completed": "x", "in_progress": "~", "pending": " "}.get(status, "?")
                lines.append(f"  [{mark}] {item.get('content', '')}")
            return ok(lines)

        if name == "yolo":
            agent.yolo = not agent.yolo
            if getattr(agent, "tool_context", None) is not None:
                agent.tool_context.yolo = agent.yolo
            return ok(
                [f"automatic approval {'on' if agent.yolo else 'off'}"]
                + ([] if agent.yolo else ["the dashboard cannot answer confirmations, so a tool needing approval may fail"]),
                "success",
            )

        if name == "thinking":
            agent.thinking = not agent.thinking
            return ok([f"reasoning display {'on' if agent.thinking else 'off'}"], "success")

        if name == "ground":
            current = getattr(agent.tool_context, "grounding_enabled", False)
            agent.tool_context.grounding_enabled = not current
            return ok([f"grounding checks {'on' if not current else 'off'}"], "success")

        if name == "effort":
            if not argument:
                return ok([f"effort is {getattr(agent, 'effort', None) or 'unset'}; choose from {', '.join(EFFORT_LEVELS)}"])
            if argument not in EFFORT_LEVELS:
                return ok([f"unknown effort {argument!r}; choose from {', '.join(EFFORT_LEVELS)}"], "error")
            agent.effort = argument
            return ok([f"effort set to {argument}"], "success")

        if name == "max-tokens":
            if not argument:
                return ok([f"max tokens is {agent.max_tokens}"])
            if not argument.isdigit() or int(argument) <= 0:
                return ok([f"{argument!r} is not a positive integer"], "error")
            agent.max_tokens = int(argument)
            return ok([f"max tokens set to {argument}"], "success")

        if name == "fallback-model":
            if not argument:
                return ok([f"fallback model is {session.config.fallback_model or 'unset'}"])
            session.config = _replace_config(session.config, fallback_model=argument)
            agent.fallback_model = argument
            return ok([f"fallback model set to {argument}"], "success")

        if name == "export":
            path = PROMPT_HISTORY_FILE.parent / "exports"
            path.mkdir(parents=True, exist_ok=True)
            target = path / f"session-{session.id}.md"
            target.write_text(_export_markdown(session), encoding="utf-8")
            return ok([f"exported to {target}"], "success")

        if name == "loci":
            return _loci_command(session, argument, agent, ok)

        if name == "mission":
            # The same snapshot the Mission control view renders, as text. `render_lines()` existed
            # with no caller anywhere, so the terminal view the changelog described could not be
            # reached from either front end; this is the way in.
            app = getattr(session, "app", None)
            if app is None:
                return ok(["/mission needs the dashboard's application object; "
                           "use the Mission control view instead"], "warning")
            try:
                from ..mission_control import render_lines, snapshot

                return ok(render_lines(snapshot(app)))
            except Exception as exc:  # noqa: BLE001
                return ok([f"/mission failed: {type(exc).__name__}: {exc}"], "error")

        return ok([f"unknown command /{name} -- try /help"], "error")
    except Exception as exc:  # noqa: BLE001
        return {"handled": True, "output": f"/{name} failed: {type(exc).__name__}: {exc}", "severity": "error", "rebuilt": False}


def _loci_command(session: Any, argument: str, agent: Any, ok) -> dict:
    """`/loci` from the browser, mirroring the CLI's own subcommands.

    Implemented here because the terminal had it and the browser did not, which is how
    `/loci run researcher ...` came back as "unknown command /loci" -- a command the documentation
    and the panel both lead you to try. Where a subcommand genuinely needs a terminal it says so
    rather than failing obscurely.

    `run` executes the turn through the same agent-facing tool the model uses, so the run slot, the
    cross-process lease, the preflight refusal and the retry policy all apply identically. It does
    not open a new session the way the Loci panel does: the CLI prints the answer inline, and a
    slash command that silently replaced the current transcript would be a surprise.
    """
    from .. import loci as loci_module

    parts = argument.split(None, 1)
    verb = parts[0].lower() if parts else "list"
    rest = parts[1].strip() if len(parts) > 1 else ""

    registry = loci_module.get_registry()
    registry.reload()

    if verb in ("list", "ls", ""):
        loci_list = registry.all()
        if not loci_list:
            return ok([
                "No bots yet.",
                "Create one here:  /loci create <name> --role <role> --goal \"<objective>\"",
                "Or pick a ready-made one in the Loci panel of the dashboard.",
            ])
        lines = [f"{len(loci_list)} persistent agent(s):"]
        for locus in sorted(loci_list, key=lambda d: d.name.lower()):
            open_goals = locus.open_goals()
            flag = "" if locus.enabled else "  [disabled]"
            when = ""
            if locus.proactive:
                when = " · wakes" if locus.is_due() else f" · wakes in {locus.seconds_until_due()}s"
            lines.append(f"  {locus.name}  ({locus.role}) — {len(open_goals)} open{when}{flag}")
            nxt = locus.next_goal()
            if nxt is not None:
                lines.append(f"      next: [{nxt.id}] {nxt.title}")
        lines.append("")
        lines.append("  /loci run <name> [message]   give it a turn")
        lines.append("  /loci show <name>            objectives, notes, recent activity")
        return ok(lines)

    if verb == "show":
        if not rest:
            return ok(["Usage: /loci show <name>"], "warning")
        locus = registry.get(rest)
        if locus is None:
            return ok([f"No bot named {rest!r}. Try /loci list"], "error")
        lines = [
            f"{locus.name} — {locus.role}",
            f"  id         : {locus.id}",
            f"  enabled    : {locus.enabled}   proactive: {locus.proactive}",
            f"  workspace  : {locus.workspace_path()}",
            f"  objectives : {len(locus.goals)}",
        ]
        for goal in locus.goals:
            lines.append(f"    [{goal.id}] ({goal.status}) {goal.title}")
            for note in goal.notes[-2:]:
                lines.append(f"        note: {note}")
        problems = locus.preflight()
        if problems:
            lines.extend(["  preflight  :"] + [f"    - {p}" for p in problems])
        else:
            lines.append("  preflight  : clear")
        if locus.activity:
            lines.append("  recent activity:")
            for entry in reversed(locus.activity[-8:]):
                lines.append(f"    [{entry.kind}] {entry.summary[:100]}")
        return ok(lines)

    if verb == "run":
        if not rest:
            return ok(["Usage: /loci run <name> [message]"], "warning")
        head, _, message = rest.partition(" ")
        locus = registry.get(head)
        if locus is None:
            return ok([f"No bot named {head!r}. Try /loci list"], "error")
        prompt = message.strip() or "Take the next concrete step on your first open objective."
        from .. import tool_registry

        text, failed = tool_registry.execute_locus_run({"locus": locus.name, "message": prompt}, agent.tool_context)
        return ok([text], "error" if failed else "success")

    if verb == "create":
        # Parsed rather than assumed: a create that silently ignored a mistyped flag would produce a
        # bot with no objectives, which looks broken and is confusing to debug.
        tokens = rest.split()
        if not tokens:
            return ok(["Usage: /loci create <name> [--role R] [--goal \"...\"] [--instructions \"...\"]"],
                      "warning")
        name = tokens[0]
        role = "generalist"
        goals: list[str] = []
        instructions = ""
        index = 1
        while index < len(tokens):
            flag = tokens[index]
            if flag in ("--role", "--goal", "--instructions") and index + 1 < len(tokens):
                value = tokens[index + 1]
                if flag == "--role":
                    role = value
                elif flag == "--goal":
                    goals.append(value)
                else:
                    instructions = value
                index += 2
                continue
            return ok([f"unexpected argument {flag!r}; expected --role, --goal or --instructions"], "error")
        try:
            locus = registry.create(name, role=role, instructions=instructions, goals=goals)
        except ValueError as exc:
            return ok([str(exc)], "error")
        return ok([f"Created {locus.name} ({locus.role}) with {len(locus.goals)} objective(s).",
                   f"Give it a turn:  /loci run {locus.name}"], "success")

    if verb == "goal":
        tokens = rest.split(None, 1)
        if not tokens:
            return ok(["Usage: /loci goal <name> \"<objective>\"", "       /loci goal <name> <id> --done|--active|--blocked"], "warning")
        locus = registry.get(tokens[0])
        if locus is None:
            return ok([f"No bot named {tokens[0]!r}. Try /loci list"], "error")
        remainder = tokens[1].strip() if len(tokens) > 1 else ""
        if not remainder:
            return ok(["Give an objective, or a goal id with --done/--active/--blocked"], "warning")
        flag = next((f for f in ("--done", "--active", "--blocked", "--pending") if remainder.endswith(f)), "")
        if flag:
            goal_id = remainder[: -len(flag)].strip()
            status = {"--done": "done", "--active": "active", "--blocked": "blocked", "--pending": "pending"}[flag]
            goal = locus.set_goal_status(goal_id, status, "set from the dashboard")
            if goal is None:
                return ok([f"No objective matching {goal_id!r} in {locus.name}"], "error")
            registry.persist(locus)
            return ok([f"{locus.name}: [{goal.id}] {goal.title} -> {status}"], "success")
        goal = locus.add_goal(remainder.strip('"'))
        registry.persist(locus)
        return ok([f"Added [{goal.id}] {goal.title} to {locus.name}."], "success")

    if verb in ("enable", "disable", "on", "off") or verb.startswith("enable") or verb.startswith("disable"):
        # `/loci enable cowork` is how most people will say "start using this bot" -- the CLI's
        # `proactive on|off` reads as a scheduling option rather than a switch, so both are accepted
        # and each does the obvious thing.
        tokens = rest.split(None, 1)
        target = tokens[0].strip() if tokens else ""
        if not target:
            return ok([f"Usage: /loci {verb} <name>"], "warning")
        locus = registry.get(target)
        if locus is None:
            return ok([f"No bot named {target!r}. Try /loci list"], "error")

        trailing = tokens[1].strip().lower() if len(tokens) > 1 else ""
        if verb in ("on", "off") or trailing in ("on", "off"):
            # Explicit on/off: the caller is talking about the schedule, so set proactive.
            wanted = (verb == "on") or trailing == "on"
            locus.proactive = wanted
            registry.persist(locus)
            return ok([f"{locus.name} proactive {'on' if wanted else 'off'}"
                       + (" — it will work on its own interval" if wanted else "")], "success")

        wanted = verb.startswith("enable")
        locus.enabled = wanted
        locus.log("note", f"{'enabled' if wanted else 'disabled'} from the dashboard")
        registry.persist(locus)
        if wanted:
            return ok([f"{locus.name} is enabled. Message it with:  /loci run {locus.name} <message>"],
                      "success")
        return ok([f"{locus.name} is disabled. Nothing will run it until you enable it again."], "success")

    if verb in ("proactive",):
        tokens = rest.split()
        if len(tokens) < 2:
            return ok(["Usage: /loci proactive <name> on|off"], "warning")
        locus = registry.get(tokens[0])
        if locus is None:
            return ok([f"No bot named {tokens[0]!r}. Try /loci list"], "error")
        wanted = tokens[1].lower() in ("on", "true", "yes", "1")
        locus.proactive = wanted
        registry.persist(locus)
        return ok([f"{locus.name} proactive {'on' if wanted else 'off'}"], "success")

    if verb in ("rm", "remove", "delete"):
        # Two steps, in the browser, without an unanswerable dialog.
        #
        # This used to refuse outright and send the user to the panel, because deleting a bot removes
        # its objectives and history and `window.confirm` is the only confirmation a browser gives --
        # which is easy to dismiss by reflex and impossible to automate. So the first call names
        # exactly what would be lost and does nothing; the second, with `--yes`, does it. Nothing is
        # destroyed until the user has read a sentence that says what it is, and the intent is explicit
        # in the command rather than in a click.
        words = rest.split()
        name = words[0] if words else ""
        if not name:
            return ok([f"Usage: /loci {verb} <name> [--yes]"], "warning")
        locus = registry.get(name)
        if locus is None:
            return ok([f"No locus named {name}."], "error")
        confirmed = any(word.lower() in ("--yes", "-y", "yes", "confirm")
                        for word in words[1:])
        open_goals = [g for g in (locus.goals or [])
                      if getattr(g, "status", "") not in ("done", "cancelled")]
        if not confirmed:
            return ok([
                f"Delete {locus.name!r}? This removes the bot, its {len(locus.goals or [])} "
                f"objective(s) ({len(open_goals)} still open) and its activity history. "
                "Its workspace files are left alone.",
                f"Run /loci {verb} {locus.name} --yes to confirm.",
            ], "warning")
        registry.delete(locus.id)
        return ok([
            f"Deleted {locus.name!r} ({len(locus.goals or [])} objective(s), "
            f"{len(open_goals)} open at the time).",
            "Its workspace files were not touched.",
        ], "success")

    if verb in ("chat", "off"):
        # The terminal's `/loci chat` holds a per-terminal switch; a browser session has no such
        # switch, because opening a locus's conversation here is a matter of loading its session -- the
        # Loci panel's "Message…" button does that. Saying so is better than "unknown subcommand".
        return ok([
            f"/loci {verb} is a terminal mode: it points the terminal prompt at a bot until /loci off.",
            "In the browser, use Message… on the bot's card: it opens that bot's own conversation, "
            "which is what /loci chat does in a terminal.",
        ], "warning")

    return ok([
        f"Unknown /loci subcommand {verb!r}.",
        "Try: /loci · /loci list · /loci show <name> · /loci run <name> [message] · "
        "/loci chat <name> (terminal) · /loci enable|disable <name> · /loci create · /loci goal · "
        "/loci proactive · /loci rm <name> --yes",
    ], "error")


def _replace_config(config: Any, **changes: Any):
    import dataclasses

    if dataclasses.is_dataclass(config):
        return dataclasses.replace(config, **changes)
    for key, value in changes.items():
        setattr(config, key, value)
    return config


def _export_markdown(session: Any) -> str:
    lines = [f"# Myriaxon session {session.id}", ""]
    for message in session.messages():
        who = "You" if message["role"] == "user" else "Myriaxon"
        lines.append(f"## {who}")
        lines.append("")
        lines.append(message["text"])
        lines.append("")
    trajectory = session.trajectory()
    if trajectory:
        lines.append("## Tool calls")
        lines.append("")
        for index, event in enumerate(trajectory, 1):
            lines.append(f"{index}. `{event['tool']}`" + (" (failed)" if event["is_error"] else ""))
            lines.append(f"   - args: `{event['args']}`")
        lines.append("")
    return "\n".join(lines)
