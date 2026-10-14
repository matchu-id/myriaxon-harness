"""Command-line parsing, kept free of provider SDKs and the REPL stack.

This module exists for one measured reason: `myriaxon.cli` imports ~150 modules (rich, the REPL
surface, all three agent modules, checkpoints, MCP) and costs several seconds to import on a cold
machine, which meant `myriaxon --help` paid the full price just to print text. Argument parsing
needs only constants, so it lives here and the heavy CLI is imported afterwards, only when a real
run is actually going to happen.

Nothing here may import a provider SDK, rich, prompt_toolkit, or any other `myriaxon` module except
`constants`.
"""
import argparse

from .constants import (
    APP_NAME,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_TOKENS_OPENAI_COMPAT,
    DEFAULT_MODE,
    DEFAULT_MODEL,
    EFFORT_LEVELS,
    MODES,
    VERSION,
)

# The profile names are duplicated here instead of importing model_profiles, which pulls in
# dataclasses/pathlib for ~0.5s -- a real cost on a path whose entire purpose is to be cheap. A test
# asserts this tuple stays in sync with model_profiles.PROFILES, so the duplication cannot drift.
PROFILE_CHOICES = ("balanced", "large", "small")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="myriaxon", description=f"{APP_NAME} - a self-contained coding agent.")
    parser.add_argument("-p", "--print", dest="prompt", help="Run one prompt non-interactively and exit.")
    parser.add_argument(
        "-y", "--yolo", action="store_true", help="Skip confirmation for write/edit/bash/notebook tools."
    )
    parser.add_argument(
        "--mode",
        choices=MODES,
        default=DEFAULT_MODE,
        help="Permission mode: manual (confirm everything, default) / auto_edit / plan / auto.",
    )
    parser.add_argument("--reauth", action="store_true", help="Re-run the endpoint/API key/provider setup.")
    parser.add_argument(
        "--vendor",
        choices=["deepseek", "netra", "gemini", "ollama"],
        default=None,
        help="Which vendor to run on for this session: 'deepseek' (DeepSeek's own API), 'netra' "
             "(api.netraruntime.com), 'gemini' (Google -- uses Vertex AI when GOOGLE_CLOUD_PROJECT "
             "and GOOGLE_GENAI_USE_VERTEXAI=true are set, in which case NO API KEY is needed because "
             "the credential is your gcloud login), or 'ollama' (local). If a vendor has no key yet "
             "you are asked for one before the first turn rather than failing with a 401. Saved, so "
             "the next run remembers.",
    )
    parser.add_argument(
        "--ollama",
        action="store_true",
        help="Use the Ollama on this computer, no key needed. Uses (or creates) a model called 'myriaxon' "
             "with the full context window; see `myriaxon ollama`.",
    )
    parser.add_argument("--model", default=None, help=f"Override the model (default: {DEFAULT_MODEL}).")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help=(
            f"Override the response token ceiling (default: the real model's own output cap "
            f"when fetchable, else {DEFAULT_MAX_TOKENS} for anthropic / "
            f"{DEFAULT_MAX_TOKENS_OPENAI_COMPAT} for openai_compat)."
        ),
    )
    parser.add_argument(
        "--effort",
        choices=EFFORT_LEVELS,
        default=None,
        help="Reasoning/response effort level. Supported on both providers; omit for provider default.",
    )
    parser.add_argument(
        "--thinking",
        action="store_true",
        help="Show the model's reasoning/thinking trace as it streams, before the answer.",
    )
    parser.add_argument(
        "--no-compact",
        action="store_true",
        help="Disable automatic context compaction (Anthropic only; on by default).",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Don't offer to resume a previous session saved in this folder.",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Run run_bash inside a network-isolated Docker container instead of directly on the host.",
    )
    parser.add_argument(
        "--profile",
        choices=PROFILE_CHOICES,
        default=None,
        help=(
            "Model capability profile: small (tiny window/weak model) / balanced / large. "
            "Default: detected from the model id and the provider's real context window."
        ),
    )
    parser.add_argument(
        "--agent-mode",
        choices=["single", "multi"],
        default="single",
        help="single (default) = one agent, no delegation tools; multi = also offer task/parallel_tasks.",
    )
    parser.add_argument(
        "--gemini-protocol",
        choices=["auto", "generate", "interactions"],
        default=None,
        help="Which Google protocol the gemini provider speaks; auto picks per model.",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json"],
        default="text",
        help="Output format for -p/--print mode. json emits a single structured object.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("auth", help="Run the endpoint/API key/provider setup and exit.")
    web = subparsers.add_parser("web", help="Serve the Myriaxon dashboard locally and open it.")
    web.add_argument("--host", default="127.0.0.1", help="Interface to bind (default: 127.0.0.1).")
    web.add_argument("--port", type=int, default=8765, help="Port to bind (default: 8765).")
    web.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically.")
    web.add_argument(
        "--workspace",
        default="",
        help="Directory the dashboard is allowed to read, write and upload into (default: cwd).",
    )
    # Repeatable on the subcommand as well as globally, because a subparser does not inherit the
    # parent's options: `myriaxon web --mode plan` was an argparse usage error while
    # `myriaxon --mode plan web` worked, a difference no reader would guess.
    #
    # `SUPPRESS` as the default, not None. A subparser's default is written over the parent's value
    # at parse time, so a plain `default=None` made `myriaxon --mode auto web` come out as None --
    # the global flag was accepted and then discarded. With SUPPRESS the attribute is only set when
    # the flag is actually given, so whichever spelling the user chose survives.
    web.add_argument("--mode", default=argparse.SUPPRESS, choices=MODES,
                     help="Permission mode for dashboard sessions (same as the global --mode).")

    # The bot is the web server's /api/inbound webhook; there is no separate daemon to start. The
    # subcommand exists because that was not discoverable -- the honest answer to "how do I start
    # the bot" is "run a server, put a webhook on it", and both halves need saying.
    bot = subparsers.add_parser(
        "bot",
        help="Create, configure, test or serve a bot (a persistent agent you can message).",
        description=(
            "A bot is a persistent agent: it has a role, standing instructions and objectives that\n"
            "survive restarts, it can read and write files and run commands in a workspace, and it\n"
            "answers from a chat app. Run with no arguments for a guided setup."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bot.add_argument("--list", action="store_true", help="List the ready-made bots and exit.")
    bot.add_argument("--templates", action="store_true",
                     help="List the bots plus what makes them different from a chat window.")
    bot.add_argument("--template", default="", metavar="KEY",
                     help="Create the bot for a template (see --list).")
    bot.add_argument("--goal", action="append", default=[], metavar="TEXT",
                     help="An objective for the new bot (repeatable).")
    bot.add_argument("--proactive", action="store_true",
                     help="Let the bot work on its own interval instead of only when asked.")
    bot.add_argument("--every", type=int, default=0, metavar="SECONDS",
                     help="How often a proactive bot wakes (default: the template's own).")
    bot.add_argument("--allow", action="append", default=[], metavar="+NUMBER",
                     help="A number allowed to drive the bot (repeatable). Empty means nobody.")
    bot.add_argument("--secret", default="", metavar="TEXT",
                     help="Shared secret required in the X-Myriaxon-Secret header.")
    bot.add_argument("--provider", default="", choices=["", "twilio", "meta", "generic"],
                     help="Set the messaging provider.")
    bot.add_argument("--enable", action="store_true", help="Switch inbound messages on.")
    bot.add_argument("--yes", action="store_true", help="Skip the interactive questions.")
    bot.add_argument("--host", default="0.0.0.0", help="Interface to bind (default: 0.0.0.0).")
    bot.add_argument("--port", type=int, default=8765, help="Port to bind (default: 8765).")
    bot.add_argument("--mode", default=argparse.SUPPRESS, choices=MODES,
                     help="Permission mode for sessions this server runs.")
    bot.add_argument("--workspace", default="", help="Workspace the agent may work in (default: cwd).")
    bot.add_argument("--serve", action="store_true",
                     help="Start the server that receives messages, instead of only reporting.")
    bot.add_argument("--self-test", action="store_true",
                     help="Post a synthetic message through the real inbound path and report.")
    bot.add_argument("--no-deliver", action="store_true",
                     help="With --self-test, stop before the network send: no real message is sent.")
    bot.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically.")
    bot.add_argument("--tunnel", action="store_true",
                     help="Also expose the webhook publicly and print the URL to give your provider.")

    # `search` exists because the web-search tool is honest about being thin without a key, and that
    # honesty is only useful if the fix is one command away. Before this, the note told the reader to
    # set an environment variable and left them to it.
    search = subparsers.add_parser("search", help="Check or set the web-search provider key.")
    search.add_argument("--set", default="", metavar="PROVIDER:KEY",
                        help="Store a key, e.g. --set brave:BSAxxxx")
    search.add_argument("--clear", default="", metavar="PROVIDER", help="Remove a stored key.")
    search.add_argument("--test", action="store_true", help="Run a real query and show what came back.")

    # Ollama is the one provider that runs on the user's own machine, so it has to be *found* rather
    # than configured with a key. Running `myriaxon ollama` with no flags reports what is installed and
    # what each model can actually do; `--use` switches to one, which is the step that turns detection
    # into something usable rather than a report nobody acts on.
    ollama = subparsers.add_parser(
        "ollama",
        help="Find Ollama on this machine and report each model's real context window and abilities.",
    )
    ollama.add_argument("--use", default="", metavar="MODEL",
                        help="Switch the saved configuration to this local model.")
    ollama.add_argument("--host", default="", metavar="URL",
                        help="Probe this address instead of the defaults, e.g. http://192.168.1.9:11434")
    ollama.add_argument("--quick", action="store_true",
                        help="Skip the per-model capability probe (fewer requests, less detail).")
    ollama.add_argument("--json", action="store_true", help="Machine-readable output.")
    # `--create` is what makes `ollama run myriaxon` work: it builds an Ollama model called 'myriaxon' from one of the
    # user's own models, with that model's FULL context window (Ollama itself runs a model with 4,096 unless told
    # otherwise) and Myriaxon's persona, then points Myriaxon at it.
    ollama.add_argument("--create", action="store_true",
                        help="Create the Ollama model 'myriaxon' (so `ollama run myriaxon` works) and use it.")
    ollama.add_argument("--from", dest="base", default="", metavar="MODEL",
                        help="With --create: build it from this model (default: the loaded one, else the best one that fits).")
    ollama.add_argument("--name", default="", metavar="NAME",
                        help="With --create: call the new model this instead of 'myriaxon'.")
    ollama.add_argument("--ctx", default="max", metavar="TOKENS",
                        help="With --create: the context window. 'max' (default) is the model's own full window; or a number, "
                             "e.g. 65536 or 64k.")
    ollama.add_argument("--no-use", dest="no_use", action="store_true",
                        help="With --create: only create the model; do not point Myriaxon at it.")
    ollama.add_argument("--no-check", dest="no_check", action="store_true",
                        help="With --create: do not load the model afterwards to check that it runs.")
    ollama.add_argument("--modelfile", action="store_true",
                        help="Print the Modelfile that --create would use and stop (for `ollama create -f`).")
    ollama.add_argument("--remove", action="store_true",
                        help="Delete the model made by --create (never the model it was built from).")

    # Compaction removes information from the conversation. Recording that is only half the fix -- if nobody
    # can read the record, the gap is still open. Codex's most-voted context complaint is that compaction is
    # invisible and uncontrollable, so this command is the control: it shows exactly what was removed, when,
    # and lets the original bytes be recovered by digest.
    audit = subparsers.add_parser(
        "audit",
        help="Show what context compaction removed, and recover it by digest.",
    )
    audit.add_argument("--session", default="", metavar="NAME",
                       help="Inspect this session's log (default: the most recent one).")
    audit.add_argument("--list", action="store_true", help="List sessions that have an audit log.")
    audit.add_argument("--show", default="", metavar="DIGEST",
                       help="Print the full stored content for a digest.")
    audit.add_argument("--limit", type=int, default=40, metavar="N",
                       help="Show at most N entries (default: 40, newest last).")
    audit.add_argument("--json", action="store_true", help="Machine-readable output.")

    # Every other harness asks you to trust what is installed. This one checks. `verify` probes each skill by
    # loading it and each plugin tool by calling it, so "installed" stops being the same claim as "works".
    verify = subparsers.add_parser(
        "verify",
        help="Probe skills, plugin tools and tool definitions; report what actually works.",
    )
    verify.add_argument("--verbose", action="store_true", help="Show warnings as well as errors.")
    verify.add_argument("--json", action="store_true", help="Machine-readable output.")
    verify.add_argument("--manifest", default="", metavar="PATH",
                        help="Also write a capability manifest to this path.")
    verify.add_argument("--tools", action="store_true",
                        help="Include the built-in tool definitions (slower, structural checks only).")
    verify.add_argument("--adopt", action="store_true",
                        help="Mark skills that predate provenance tracking as agent-written and unverified "
                             "(rewrites their frontmatter; the body is untouched).")

    # "Faster" needs a number or it is a claim. This measures cold start, import cost by self time (the
    # cumulative column double-counts and has already produced one wrong figure in this repository), and tool
    # dispatch overhead. `--save`/`--compare` make regressions detectable rather than remembered.
    bench = subparsers.add_parser(
        "bench",
        help="Measure startup, import cost and tool dispatch; save or compare against a baseline.",
    )
    bench.add_argument("--runs", type=int, default=5, metavar="N",
                       help="Cold-start runs to average (default: 5).")
    bench.add_argument("--json", action="store_true", help="Machine-readable output.")
    bench.add_argument("--save", default="", metavar="PATH", help="Write the result as a baseline.")
    bench.add_argument("--compare", default="", metavar="PATH",
                       help="Compare against a saved baseline and report the difference.")

    # The autonomy surface. Without this, a Locus only ever ran inside an interactive REPL, so "always-on"
    # required the owner to be sitting at the terminal -- which is the exact failure the Loci design set out to
    # remove. This is the entry point a cron job, a systemd timer or a container calls.
    #
    # `myriaxon loci` is ONE command with two halves, told apart by whether a verb is given:
    #   * no verb  -> the always-on agents: --run, --watch, --status, --reset (below);
    #   * a verb   -> Loci's own personal stores and companion helpers: win, remember, recall, habit, journal, mood, ...
    # Nested under one subcommand rather than added as a dozen top-level ones, because the top level is already long
    # enough that a new user cannot see the end of it.
    loci_cmd = subparsers.add_parser(
        "loci",
        help="Always-on agents (--run, --watch, --status) and Loci's own stores: wins, memories, habits, a journal, "
             "focus sessions, mood check-ins.",
    )
    # `verb` uses nargs="?" deliberately: `myriaxon loci win "text"` records one and `myriaxon loci win` lists them, which
    # halves the surface for no loss of clarity -- the verb is the noun and the object is optional.
    loci_cmd.add_argument(
        "verb", nargs="?", default="",
        choices=["", "win", "wins", "remember", "recall", "forget", "habit", "habits",
                 "journal", "reflect", "focus", "todo", "today", "plan",
                 "mood", "ground", "reframe", "overwhelmed", "kind", "connect", "rest"],
        help="win, remember, recall, forget, habit, journal, reflect, focus, todo, today, plan, "
             "mood, ground, reframe, overwhelmed, kind, connect, rest. Omit it to run or inspect the always-on agents.",
    )
    loci_cmd.add_argument("subject", nargs="*", default=[],
                          help="With a verb: the text to record, the query to search, or the name to act on.")
    loci_cmd.add_argument("--room", default="", metavar="ROOM",
                          help="With `remember`, which room of the memory palace to file it in.")
    loci_cmd.add_argument("--about", default="", metavar="WHAT",
                          help="With `win`, what the win was about; with `journal`, the prompt used.")
    loci_cmd.add_argument("--day", default="", metavar="YYYY-MM-DD",
                          help="With `habit`, the day to mark (default: today).")
    loci_cmd.add_argument("--days", type=int, default=7, metavar="N",
                          help="With `reflect`, how many days to recap. With `focus`, the session length.")
    loci_cmd.add_argument("--limit", type=int, default=20, metavar="N",
                          help="With a verb: how many entries to list (default: 20).")
    # `todo` takes its action positionally because the shape the design document names is `loci todo add "..."`: the
    # action first and the text second. `done`/`snooze`/`forget` take an id in `subject`.
    loci_cmd.add_argument("--done", action="store_true",
                          help="With `todo`: list what is finished instead of what is open.")
    loci_cmd.add_argument("--all", action="store_true",
                          help="With `todo`: include finished tasks.")
    # Section 8.2's opt-in, as a flag: the analysis does not happen unless the user asks for it, which is what both the
    # mood clause and section 8.5's privacy clause require.
    loci_cmd.add_argument("--patterns", action="store_true",
                          help="With `mood`: reflect on patterns. Off unless asked, because the analysis needs an opt-in.")
    loci_cmd.add_argument("--who", default="", metavar="NAME",
                          help="With `connect`: the person to suggest reaching out to.")
    # The wipe requires this flag. `loci forget` with no id and no `--confirm` refuses rather than guessing, because the
    # dangerous overload needs an argument a caller cannot supply by accident.
    loci_cmd.add_argument("--confirm", action="store_true",
                          help="With `forget` and no id: actually delete every store. Required.")
    loci_cmd.add_argument("--run", action="store_true",
                          help="Run every Locus that is due right now, then exit.")
    loci_cmd.add_argument("--watch", action="store_true",
                          help="Keep running: sleep until the next Locus is due, run it, repeat.")
    loci_cmd.add_argument("--locus", default="", metavar="NAME",
                          help="Restrict to one Locus (default: all due Loci).")
    loci_cmd.add_argument("--status", action="store_true",
                          help="Report which Loci are due and when the next one wakes; run nothing.")
    loci_cmd.add_argument("--reset", default="", metavar="NAME",
                          help="Re-enable a Locus that disabled itself after repeated failures, and clear its "
                               "failure ledger.")
    # Bare `--autonomy` is not allowed to mean "changed something": nargs="?" with a const means the
    # flag alone reports the current mode, and a value sets it. A flag that wrote on its own would
    # make `myriaxon loci --autonomy --locus cowork` a mutation when it reads like a question.
    loci_cmd.add_argument("--autonomy", nargs="?", const="?", default="", metavar="MODE",
                          help="With --locus: set how much of its own direction the Locus has. "
                               "'assisted' works only the objectives you gave it; 'self_directed' may "
                               "also decide and record its own. Bare --autonomy reports the current "
                               "mode and changes nothing.")
    loci_cmd.add_argument("--max-seconds", type=float, default=0.0, metavar="N",
                          help="With --watch, stop after N seconds (0 = run until interrupted).")
    loci_cmd.add_argument("--usage", action="store_true",
                          help="Report recorded token and dollar usage from the ledger, broken down "
                               "by agent. Reads the file every turn appends to; changes nothing.")
    # Self-scheduling is PURE PYTHON and cross-platform, deliberately. An operating-system scheduler
    # (schtasks, cron, systemd) was tried first and removed: it is a second moving part outside the
    # harness, it is platform-specific by construction, and its failure mode is invisible.
    loci_cmd.add_argument("--supervise", action="store_true",
                          help="Run due Loci automatically, in a Python loop, until interrupted. "
                               "Cross-platform: the same code on Windows, macOS and Linux. The "
                               "dashboard runs this same loop on a thread while it is open.")
    loci_cmd.add_argument("--json", action="store_true", help="Machine-readable output.")

    # The instruction layer is injected into every turn, and until now there was no way to see what had loaded.
    # Two separate upstream requests for exactly this were closed "not planned" (Codex #8547), so this is a
    # capability the field does not offer rather than a catch-up.
    instructions = subparsers.add_parser(
        "instructions",
        help="Show which instruction files load, in what order, and what they cost per turn.",
    )
    instructions.add_argument("--json", action="store_true", help="Machine-readable output.")
    instructions.add_argument("--show", action="store_true",
                              help="Print the assembled instruction text as the model receives it.")

    # Roles are the declarative half of Loci: a portable template a Locus is created from, instead of an agent
    # that exists only as imperative state in one machine's loci.json.
    roles = subparsers.add_parser(
        "roles",
        help="List, inspect and install agent roles (portable Locus templates).",
    )
    roles.add_argument("names", nargs="*", metavar="NAME",
                       help="Role name(s) to install. With no names, lists what is available.")
    roles.add_argument("--show", default="", metavar="NAME",
                       help="Print one role's full operating program.")
    roles.add_argument("--install", action="store_true",
                       help="Create a Locus from each named role.")
    roles.add_argument("--team", action="store_true",
                       help="Install the default team (coordinator plus specialists).")
    roles.add_argument("--goal", default="", metavar="TEXT",
                       help="With --install, also give the new Locus this objective.")
    roles.add_argument("--proactive", action="store_true",
                       help="With --install, let the new Locus work unattended.")
    roles.add_argument("--check", action="store_true",
                       help="Validate every role and report structural problems.")
    roles.add_argument("--json", action="store_true", help="Machine-readable output.")

    # The skill lifecycle: proposals staged before they become live, plus the usage telemetry that says which
    # skills are actually earning their place in every system prompt.
    skills_cmd = subparsers.add_parser(
        "skills",
        help="Show skills and their usage; review, apply or reject staged skill proposals.",
    )
    skills_cmd.add_argument("--usage", action="store_true",
                            help="Which skills have actually been invoked, and which never have.")
    skills_cmd.add_argument("--list", action="store_true", help="List staged proposals.")
    skills_cmd.add_argument("--show", default="", metavar="ID", help="Print a proposal's draft.")
    skills_cmd.add_argument("--apply", default="", metavar="ID",
                            help="Write a pending proposal into the live skills root (captures a rollback).")
    skills_cmd.add_argument("--reject", default="", metavar="ID", help="Reject a pending proposal.")
    skills_cmd.add_argument("--quarantine", default="", metavar="ID",
                            help="Quarantine a proposal for later review.")
    skills_cmd.add_argument("--stale", default="", metavar="ID",
                            help="Mark a proposal stale when its target changed underneath it.")
    skills_cmd.add_argument("--rollback", default="", metavar="ID",
                            help="Restore the content captured before an apply.")
    skills_cmd.add_argument("--reason", default="", metavar="TEXT", help="Why, for reject/quarantine/stale.")
    skills_cmd.add_argument("--expect", default="", metavar="HASH",
                            help="Compare-and-swap: fail unless the proposal's revision hash matches.")
    skills_cmd.add_argument("--history", action="store_true", help="Show the proposal event log.")
    skills_cmd.add_argument("--status", default="", metavar="STATUS",
                            help="Filter proposals by status (pending, applied, rejected, quarantined, stale).")
    skills_cmd.add_argument("--json", action="store_true", help="Machine-readable output.")

    # Health checks: inspect state and, where a repair is safe, name what it would touch before doing it.
    doctor = subparsers.add_parser(
        "doctor",
        help="Check local state for problems; --fix repairs what can be repaired safely.",
    )
    doctor.add_argument("--fix", action="store_true", help="Apply repairs that are safe to apply.")
    doctor.add_argument("--dry-run", action="store_true",
                        help="With --fix, show exactly what would change without changing it.")
    doctor.add_argument("--check", default="", metavar="ID",
                        help="Run only this check (e.g. core/config-file).")
    doctor.add_argument("--all", action="store_true",
                        help="Include opt-in checks that a routine pass leaves out.")
    doctor.add_argument("--min-severity", default="info", choices=["info", "warning", "error"],
                        help="Only report findings at or above this severity.")
    doctor.add_argument("--json", action="store_true", help="Machine-readable output.")

    # Session lineage: branch the conversation so a second direction can be explored without losing the first.
    branch = subparsers.add_parser(
        "branch",
        help="List, create or resume session branches (fork a conversation from any point).",
    )
    branch.add_argument("--list", action="store_true", dest="list_branches",
                        help="Show the branch tree for this directory.")
    branch.add_argument("--fork", action="store_true",
                        help="Branch the active session, inheriting its messages.")
    branch.add_argument("--at", type=int, default=None, metavar="N",
                        help="With --fork, inherit only the first N messages (default: all).")
    branch.add_argument("--name", default="", metavar="NAME",
                        help="A label for the new branch.")
    branch.add_argument("--resume", default="", metavar="ID",
                        help="Make an existing branch the active session.")
    branch.add_argument("--json", action="store_true", help="Machine-readable output.")

    # Run export: hand someone the run, as one reviewable artefact.
    export = subparsers.add_parser(
        "export",
        help="Export this run's transcript and records as JSON or Markdown.",
    )
    export.add_argument("--out", default="", metavar="PATH",
                        help="Where to write it (default: run-export-<session>-<timestamp> here).")
    export.add_argument("--format", default="markdown", choices=["markdown", "json"],
                        help="markdown is for a person to read; json is for a tool.")
    export.add_argument("--session", default="", metavar="ID",
                        help="Export a specific session or branch id instead of the active session.")
    export.add_argument("--no-sanitize", action="store_true",
                        help="Do NOT redact credentials. Off by default because an export is the artefact most "
                             "likely to leave this machine.")
    export.add_argument("--no-messages", action="store_true",
                        help="Export only the records (metadata, compaction, checkpoints), not the transcript.")

    # Loci's character. A preview surface, so the expression system is inspectable rather than only visible while
    # something else is happening.
    avatar = subparsers.add_parser(
        "avatar",
        help="Draw Loci, preview every expression, or personalize the character.",
    )
    avatar.add_argument("expression", nargs="?", default="",
                        help="One expression to draw (idle, listening, crisis, ...). Omit for the whole sheet.")
    avatar.add_argument("--size", default="", choices=["", "hero", "small", "mini", "signal"],
                        help="Force a size tier. Default: the largest that fits this terminal.")
    avatar.add_argument("--plain", action="store_true",
                        help="ASCII and no colour, regardless of what the terminal supports.")
    avatar.add_argument("--json", action="store_true", dest="as_json",
                        help="Print the expression manifest, for a tool or the dashboard.")
    avatar.add_argument("--expressions", action="store_true", dest="list_expressions",
                        help="One line per expression: id, mood, label.")
    avatar.add_argument("--check", action="store_true",
                        help="Report which tier this terminal gets, and exit.")
    avatar.add_argument("--name", default="", metavar="NAME",
                        help="Personalize: what the character is called.")
    avatar.add_argument("--nickname", default="", metavar="NICK",
                        help="Personalize: a shorter name, which wins over --name.")
    avatar.add_argument("--pronoun", default="", choices=["", "it", "they", "she", "he"],
                        help="Personalize: the pronoun used about the character.")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)
