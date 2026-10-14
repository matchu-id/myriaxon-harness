"""End-to-end verification: a real agent turn, offline, with no API key.

This is the test that would have caught the release-blocking defects. Every one of them lived in
the turn path -- a variable that was never assigned, a module that was never imported, an
argument derived from the wrong message -- and every one of them was invisible to a unit suite
that checks the pieces in isolation. So this drives the whole path for real:

    build a provider config -> preload the SDK -> resolve the config -> build the agent
    -> send a prompt -> stream the response -> dispatch a tool call -> feed the result back
    -> produce a final answer -> persist the session

The provider is a local OpenAI-compatible server (``tools/mock_provider.py``) that scripts a
tool call followed by a text answer, so the tool round trip is exercised rather than simulated.

It also covers the same path through the dashboard's session layer, because that is a second
implementation of the turn and the two have drifted apart before.

Run:  python tools/e2e_check.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

# `rich` is genuinely absent from this machine and there is no network to install it. The guarded modules here are
# `theme` and `tool_registry`, which need only `Console` to import -- the TURN itself is real either way, over real
# HTTP against the scripted provider, so what this harness proves about the provider path is unaffected. What is NOT
# proven in this mode is anything about how output is RENDERED, which is why the note is printed rather than assumed.
if importlib.util.find_spec("rich") is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "_testdoubles"))
    print("  [NOTE] rich is absent; using the test double. The turn is real; the RENDERING is not.", flush=True)

from mock_provider import RunningProvider  # noqa: E402
from myriaxon import tool_registry  # noqa: E402  (needs sys.path set above)

# Point every piece of state at a scratch directory BEFORE importing the package, so the real
# config, memory database and session file are never touched.
SCRATCH = Path(tempfile.mkdtemp(prefix="mx-e2e-"))
os.environ["MYRIAXON_CONFIG_DIR"] = str(SCRATCH)

results: list[tuple[str, bool, str]] = []

# Output goes to a file as well as stdout. Measured reason: this environment can swallow a
# child process's stdout entirely (a run that plainly completed left a zero-byte capture), and a
# verification tool whose report can vanish is worse than no tool. The file is always written.
REPORT_FILE = REPO / "bench-out" / "e2e-report.txt"
_report_lines: list[str] = []


def _emit(line: str) -> None:
    _report_lines.append(line)
    try:
        print(line, flush=True)
    except Exception:
        pass


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, bool(ok), detail))


def report() -> int:
    for label, ok, detail in results:
        suffix = f"  — {detail}" if detail and not ok else ""
        _emit(f"  {'PASS' if ok else 'FAIL'}  {label}{suffix}")
    failed = [r for r in results if not r[1]]
    _emit("")
    _emit(f"{len(results) - len(failed)}/{len(results)} end-to-end checks passed")
    try:
        REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        REPORT_FILE.write_text("\n".join(_report_lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    return 1 if failed else 0


def make_config(base_url: str):
    from myriaxon.config import Config

    return Config(provider="openai_compat", api_key="test-key", base_url=base_url, model="myriaxon-test-model")


def main() -> int:
    from myriaxon.cli import _build_agent
    from myriaxon.settings import load_settings
    from myriaxon.theme import get_console

    console = get_console(quiet=True)
    workdir = SCRATCH / "workspace"
    workdir.mkdir(parents=True, exist_ok=True)
    os.chdir(workdir)

    # The script: write a file, then answer. Two requests, so the tool result must be fed back.
    script = [
        ("tool", "call_1", "write_file", {"path": "hello.txt", "content": "written by the e2e check\n"}),
        ("text", "Done. I wrote hello.txt."),
    ]

    with RunningProvider(script) as provider:
        config = make_config(provider.base_url)

        # ---- the agent is constructible through the real production path -----------------
        # `_build_agent` is what `main()` calls, so this exercises provider dispatch, the client
        # factory, profile detection and tool-set assembly together -- not a hand-wired
        # constructor that could pass while the real path is broken.
        try:
            agent = _build_agent(
                config,
                config.model,
                console,
                True,            # yolo
                None,            # mcp_bridge
                [],              # mcp_tool_defs
                [],              # url_mcp_servers
                {},              # discovered_skills
                {},              # discovered_subagent_types
                {},              # loaded_hooks
                "",              # project_instructions
                None,            # scheduler
                load_settings(),
                mode="auto",
            )
            check("agent builds through _build_agent", agent is not None)
        except Exception as exc:
            import traceback

            check("agent builds through _build_agent", False, repr(exc))
            traceback.print_exc()
            return report()

        if agent is None:
            return report()

        check("the agent speaks the OpenAI-compatible protocol", type(agent).__name__ == "OpenAIAgent",
              type(agent).__name__)
        check("a model profile was detected", getattr(agent, "profile", None) is not None)
        check("the tool list is non-empty", len(agent.tools or []) > 0, str(len(agent.tools or [])))
        tool_names = _tool_names(agent.tools)
        check("write_file is offered", "write_file" in tool_names, str(sorted(tool_names)[:6]))
        check("no delegation tools in single-agent mode", not ({"task", "parallel_tasks"} & tool_names))
        # The locus tools are DEFERRED, not removed: they are withheld to save ~700 tokens on every
        # request and loaded by `request_tools`. The property worth asserting is therefore
        # reachability, not presence -- asserting presence would re-break the saving, and asserting
        # nothing would let the group become unreachable without anyone noticing.
        check("the unlock tool is offered", "request_tools" in tool_names, str(sorted(tool_names)))
        check("locus tools are withheld but loaded on request",
              not ({"locus_list", "locus_create"} & tool_names)
              and {"locus_list", "locus_create"} <= _tool_names(
                  tool_registry.apply_tool_visibility(
                      agent.all_tools, {"loci"})))

        # ---- a real turn -----------------------------------------------------------------
        try:
            agent.run_turn("write hello.txt and tell me when done")
            check("run_turn completes without raising", True)
        except Exception as exc:
            import traceback

            check("run_turn completes without raising", False, repr(exc))
            traceback.print_exc()
            return report()

        final = (agent.last_assistant_text() or "").strip()
        check("a final answer was produced", bool(final), repr(final[:80]))
        check("the answer is the scripted one", "hello.txt" in final, repr(final[:120]))

        # ---- the tool actually ran -------------------------------------------------------
        written = workdir / "hello.txt"
        check("the tool call created the file on disk", written.is_file())
        if written.is_file():
            check("the file has the requested content", "written by the e2e check" in written.read_text(encoding="utf-8"))

        # The model script's second response is the answer, so the tool result must have been
        # sent back -- otherwise the server would never have been asked twice.
        check("the tool result was fed back (two requests)", len(provider.provider.requests) == 2,
              f"{len(provider.provider.requests)} request(s)")

        first = provider.provider.requests[0] if provider.provider.requests else {}
        check("the first request carried the user's prompt",
              any("write hello.txt" in json.dumps(m) for m in first.get("messages", [])))
        check("the first request declared tools", bool(first.get("tools")))
        check("the first request is a streaming request", first.get("stream") is True)

        second = provider.provider.requests[1] if len(provider.provider.requests) > 1 else {}
        check("the second request carried a tool result",
              any(m.get("role") == "tool" for m in second.get("messages", [])),
              str([m.get("role") for m in second.get("messages", [])]))

        # ---- the character follows the REAL turn ------------------------------------------
        # Asserted HERE rather than on a fake agent, and that distinction is the point: everything below drives the
        # same `for_agent` / `before_turn` / `after_turn` sequence the CLI does, against the agent that just ran a
        # genuine turn through the provider. A character that works on a double and not here would still be unwired.
        from myriaxon import character as character_module

        character = character_module.for_agent(agent)
        character.begin()
        check("Loci reacted to the turn", character.expression == "happy", character.expression)
        character.before_turn(typing=True)
        check("  typing shows listening", character.expression == "listening", character.expression)
        character.before_turn()
        check("  sending shows thinking", character.expression == "thinking", character.expression)
        # `run_turn` already happened above, so the transcript on the agent is the REAL one -- tool_calls plus a
        # `role: "tool"` message in the OpenAI shape. This is what proves `_did_work` reads this host's protocol
        # rather than the Anthropic one it was first written against.
        character.after_turn(agent)
        check("a turn that used tools shows cheering", character.expression == "cheering", character.expression)
        # The three channels the user actually sees, rendered rather than merely inspected.
        fragment = character.status_fragment()
        check("the status line carries the mood", bool(fragment.strip()), repr(fragment))
        prefix = character.prompt_prefix()
        check("the prompt prefix is renderable", bool(prefix.strip()) and "\n" not in prefix, repr(prefix))
        ascii_character = character_module.Character()
        ascii_character._terminal = type(character.terminal)(color=0, unicode=False, columns=80)
        ascii_character.enabled = True
        check("  and an ASCII session still shows a character", "^" in ascii_character.prompt_prefix(),
              repr(ascii_character.prompt_prefix()))

        # ---- conversation state ----------------------------------------------------------
        roles = [m.get("role") for m in agent.messages]
        check("the transcript has the user turn", "user" in roles, str(roles))
        check("the transcript has the tool round trip", "tool" in roles, str(roles))
        check("the transcript ends with the assistant", roles and roles[-1] == "assistant", str(roles))

        # ---- usage accounting ------------------------------------------------------------
        # The mock provider does not send a usage chunk (real endpoints do, with
        # stream_options.include_usage), so the totals must exist and stay at zero rather than
        # the attribute being absent or the accounting raising.
        usage = getattr(agent, "usage_totals", None)
        check("a usage accumulator exists", usage is not None)
        check("usage totals are readable",
              usage is not None and hasattr(usage, "input_tokens") and hasattr(usage, "output_tokens"),
              type(usage).__name__ if usage is not None else "missing")

        # ---- the same path through the dashboard's session layer -------------------------
        _check_dashboard_turn(workdir, provider)

    # The report is the point of this script, and it was not being returned: `main()` ended without
    # calling `report()`, so a clean run printed nothing, wrote no report file, and exited 0 even
    # when individual checks had failed. The comment above `report()` says a verification tool whose
    # output can vanish is worse than no tool -- which is exactly what it had become. The three
    # early error paths always returned it, so the gap was only on the success path, where a reader
    # would least suspect it.
    return report()


def _tool_names(tools) -> set[str]:
    names = set()
    for tool in tools or []:
        if "function" in tool and isinstance(tool["function"], dict):
            names.add(tool["function"].get("name", ""))
        elif "name" in tool:
            names.add(tool["name"])
        elif tool.get("type") == "custom" and isinstance(tool.get("custom"), dict):
            names.add(tool["custom"].get("name", ""))
    return {n for n in names if n}


def _check_dashboard_turn(workdir: Path, provider) -> None:
    """Drive the dashboard's session implementation as well.

    Separate from the CLI path on purpose: the dashboard builds its own agent and runs turns on
    a worker thread, so a fix applied to one has historically not reached the other. This
    constructs a real session, not a mock, so a change to the agent factory it uses is caught.
    """
    try:
        from myriaxon.dashboard.session import Session, SessionManager
    except Exception as exc:
        check("dashboard session layer imports", False, repr(exc))
        return
    check("dashboard session layer imports", True)

    try:
        config = make_config(provider.base_url)
        manager = SessionManager(workspace=workdir)
        session = Session("e2e-session", workdir, config=config)
        check("dashboard session constructs through the real class", session is not None)
        created = manager.create(config, mode="auto_edit")
        check("dashboard can create a session", created is not None)
        check("dashboard lists the sessions it holds", len(manager.list()) >= 1, str(len(manager.list())))
    except Exception as exc:
        check("dashboard session constructs through the real class", False, repr(exc))
        return

    # The agent the dashboard would actually run. `_ensure_agent` is the dashboard's own builder --
    # a different code path from the CLI's, which is why it is exercised rather than assumed.
    try:
        created._ensure_agent()
        agent = created.agent
        check("the dashboard builds an agent on demand", agent is not None)
        if agent is not None:
            names = _tool_names(getattr(agent, "tools", []))
            check("the dashboard agent exposes tools", len(names) > 0, str(len(names)))
            check("the dashboard agent can write files", "write_file" in names, str(sorted(names)[:5]))
            check("the dashboard agent runs without a confirmation prompt",
                  getattr(agent.tool_context, "yolo", False) is True,
                  "no terminal exists to answer a prompt, so it must auto-approve")
            # Same as above: deferred, so reachability is the property, not presence.
            check("the dashboard agent can load the locus tools",
                  "request_tools" in names
                  and {"locus_list", "locus_create"} <= _tool_names(
                      tool_registry.apply_tool_visibility(
                          getattr(agent, "all_tools", agent.tools), {"loci"})),
                  str(sorted(names)))
            # The dashboard has no terminal, so plan mode is the only thing standing between a model and an
            # unattended edit. Asserted here rather than only in verify_plan_mode: that verifier proves the RULE,
            # and this proves the rule is reachable from the context the dashboard actually builds.
            from myriaxon import plan_mode as plan_mode_module
            from myriaxon.constants import MODE_PLAN

            dashboard_ctx = agent.tool_context
            dashboard_ctx.mode = MODE_PLAN
            verdict = plan_mode_module.check_for_context("write_file", dashboard_ctx)
            check("the dashboard context honours plan mode",
                  not verdict.allowed and "plan mode" in verdict.reason,
                  verdict.reason[:90])
    except Exception as exc:
        check("the dashboard builds an agent on demand", False, repr(exc))


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil

        shutil.rmtree(SCRATCH, ignore_errors=True)
