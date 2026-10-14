"""Every CLI command must be in the dashboard catalogue -- implemented or explained.

The request was "everything that exists in CLI should be able in dashboard". A command missing from
the catalogue is worse than one marked terminal-only: it answers "unknown command, try /help" and
sends the reader hunting for a typo instead of telling them where the command lives.

This diffs the CLI's completion catalogue against the dashboard's, both ways, and prints the gap.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon.completion import BUILTIN_COMMANDS  # noqa: E402
from myriaxon.dashboard import commands as dash  # noqa: E402

# Commands the CLI handles that are not in `BUILTIN_COMMANDS`. Found by reading the REPL's dispatch,
# not guessed. `/off` is deliberately NOT here: it is not a slash command, it is the argument to
# `/dots off`, and counting it as one made this check report a gap that does not exist.
EXTRA_CLI = {"dots", "mission"}


def main() -> int:
    cli = set(BUILTIN_COMMANDS) | EXTRA_CLI
    implemented = set(dash.COMMAND_HELP)
    refused = set(dash.CLI_ONLY)

    print(f"CLI commands            {len(cli)}")
    print(f"dashboard implements    {len(implemented)}")
    print(f"dashboard explains away {len(refused)}")
    print()

    missing = sorted(cli - implemented - refused)
    print(f"=== in the CLI and in NEITHER dashboard table: {len(missing)} ===")
    for name in missing:
        print(f"    /{name:16} {BUILTIN_COMMANDS.get(name, '(handled in the REPL)')[:66]}")
    if not missing:
        print("    none")

    both = sorted(implemented & refused)
    print(f"\n=== claimed by both tables (ambiguous): {len(both)} ===")
    for name in both:
        print(f"    /{name}")
    if not both:
        print("    none")

    orphan = sorted(refused - cli)
    print(f"\n=== explained away but NOT a CLI command (dead entry): {len(orphan)} ===")
    for name in orphan:
        print(f"    /{name}")
    if not orphan:
        print("    none")

    only_here = sorted(implemented - cli)
    print(f"\n=== implemented in the dashboard only: {len(only_here)} ===")
    for name in only_here:
        print(f"    /{name}")
    if not only_here:
        print("    none")

    # The /dots sub-verbs matter too: the browser must name the ones it can actually run.
    dots_help = dash.COMMAND_HELP.get("dots", "")
    print("\n=== /dots sub-commands named in the dashboard help ===")
    named = [v for v in ("list", "show", "run", "chat", "off", "enable", "disable", "create",
                         "goal", "proactive", "rm") if v in dots_help]
    absent = [v for v in ("list", "show", "run", "chat", "off", "enable", "disable", "create",
                          "goal", "proactive", "rm") if v not in dots_help]
    print(f"    named:  {', '.join(named)}")
    print(f"    absent: {', '.join(absent) or 'none'}")

    problems = len(missing) + len(both) + len(orphan) + len(absent)
    print()
    print("catalogue complete" if not problems else f"{problems} gap(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
