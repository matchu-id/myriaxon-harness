"""Set up the `cowork` dot: a design collaborator wired to Figma and to the poster designer.

Run this once. It is idempotent -- running it again updates the instruction text rather than
creating a second dot.

    .venv\\Scripts\\python.exe tools\\setup_cowork.py
    .venv\\Scripts\\python.exe tools\\setup_cowork.py --figma-token figd_xxxxxxxxxxxx

WHAT THIS CAN AND CANNOT DO, stated plainly:

* The dot's *designing* works today, with no credentials. `design_poster` composes a real poster
  from typography and the brand palette and writes SVG and PNG. Verified on this machine.
* The dot's *Figma* connection needs a personal access token that only you can create
  (Figma -> Settings -> Security -> Personal access tokens). Without it the MCP entry is not
  written at all: a server entry with a placeholder token connects and then fails on every call,
  which is worse than an honest absence. Pass `--figma-token` when you have one.
* Figma itself is reachable from here -- `api.figma.com` answers 401 "Missing credentials" rather
  than timing out -- so the token is the only missing piece.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import constants  # noqa: E402
from myriaxon import dots  # noqa: E402
from myriaxon import mcp_catalog  # noqa: E402

DOT_NAME = "cowork"

# The standing brief. Written as instructions to a competent colleague rather than as a prompt:
# this text is prepended to every turn the dot takes, so anything vague here is paid for on every
# run, and anything untrue here becomes a confident wrong answer.
INSTRUCTIONS = """\
You are the design partner for this workspace. Your job is to turn a rough request into artwork
that someone would actually put on a wall or in a feed.

How to work:
- Lead with the design, not with questions. If the request is clear enough to attempt, attempt it,
  then show the result and offer one specific alternative. Ask only when a wrong guess would waste
  real work (for example: which product, which language, which event date).
- Use `design_poster` to produce the artwork. It composes real typography and the Myriaxon palette
  and writes SVG and PNG. Read its result: if it warns that the copy overruns the page, cut a block
  and run it again rather than accepting an over-full poster.
- One idea per poster. A short title beats a sentence; a sentence beats a paragraph. If the user
  gives you a paragraph, reduce it to the one line that carries the meaning and say what you cut.
- Prefer `portrait` for a feed, `story` for a phone screen, `wide` for a slide, `a4` for print.
  Say which you chose and why in one clause.
- Keep the house colours unless asked otherwise: the accent is the mint `#7CF5C8` on the ink
  ground `#0A0C11`. `theme: "light"` and `theme: "signal"` exist for variety; do not mix themes
  within one set.
- Write the files into the workspace and report the paths. Never claim you produced an image you
  did not write.

Figma:
- If the Figma MCP tools are available (they appear as `mcp__figma__*`), you may read a file's
  structure, frames and text styles to keep a poster consistent with an existing design. Read
  before you write, and quote the frame or style name you took a decision from, so the user can
  check your reasoning.
- If they are not available, say so once, plainly, and continue with `design_poster` alone. Do not
  describe Figma content you cannot see, and do not pretend a design came from Figma.
- Never write to a Figma file. Read it. Your output is a poster in this workspace; a designer
  decides what enters their own document.

Accountability:
- When you finish an objective, mark it done and state exactly which files you wrote.
- If you cannot finish because you need a decision only the user can make -- a brand colour, a
  legal line, a date -- mark the objective blocked and say precisely what you need.
"""

OBJECTIVES = [
    "Design a launch poster for Myriaxon in the house palette. Write it to posters/ and report the paths.",
    "Read the Figma file the user names and produce a poster that matches its type and colour. Quote the frame you worked from.",
]

FIGMA_MCP_NAME = "figma-hosted"
FIGMA_MCP_KEY = "figma-hosted"


def configure_figma(token: str) -> tuple[bool, str]:
    """Write the Figma MCP server entry. Returns (changed, message).

    The entry is built by the catalogue's own `resolve()`, not hand-written here: the catalogue
    owns the URL and the token field name, and duplicating them would mean this script kept
    working while the catalogue moved -- producing a config that looks right and connects to
    nothing.
    """
    path = constants.MCP_SERVERS_FILE
    try:
        config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception as exc:  # noqa: BLE001
        return False, f"could not read {path}: {exc}"
    if not isinstance(config, dict):
        config = {}
    config.setdefault("stdio_servers", [])
    config.setdefault("url_servers", [])
    if not isinstance(config["url_servers"], list):
        config["url_servers"] = []

    entry = next((e for e in mcp_catalog.CATALOG if e.key == FIGMA_MCP_KEY), None)
    if entry is None:
        return False, f"the catalogue no longer has a {FIGMA_MCP_KEY!r} server; nothing was written"

    # `needs` for this entry is a token field; resolve() raises with its label if it is missing,
    # which is a better error than a config entry with an empty token.
    try:
        server = mcp_catalog.resolve(entry, {"token": token}, name=FIGMA_MCP_NAME)
    except ValueError as exc:
        return False, f"could not build the Figma entry: {exc}"

    existing = [s for s in config["url_servers"] if s.get("name") == FIGMA_MCP_NAME]
    config["url_servers"] = [s for s in config["url_servers"] if s.get("name") != FIGMA_MCP_NAME]
    config["url_servers"].append(server)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n")
    verb = "updated" if existing else "added"
    return True, f"{verb} the {FIGMA_MCP_NAME!r} MCP server in {path}"


def upsert_dot() -> tuple[dots.Dot, bool]:
    """Create the dot, or bring an existing one up to date. Returns (dot, created)."""
    registry = dots.get_registry()
    registry.load()
    existing = registry.get(DOT_NAME)
    if existing is not None:
        existing.role = "design partner"
        existing.instructions = INSTRUCTIONS
        for objective in OBJECTIVES:
            if not any(g.title == objective for g in existing.goals):
                existing.add_goal(objective)
        # Proactive off: design work needs a person's eye, and a dot that quietly redesigns on a
        # timer spends tokens producing artwork nobody asked for.
        existing.proactive = False
        registry.persist(existing)
        return existing, False

    dot = registry.create(
        DOT_NAME,
        role="design partner",
        instructions=INSTRUCTIONS,
        goals=list(OBJECTIVES),
        proactive=False,
    )
    return dot, True


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up the cowork design dot")
    parser.add_argument("--figma-token", default="", help="Figma personal access token (figd_...)")
    args = parser.parse_args()

    print("Myriaxon — cowork setup")
    print(f"  config dir : {constants.CONFIG_DIR}")
    print(f"  workspace  : {REPO}")
    print()

    # 1. the design tool is what makes the dot useful, so check it before anything else.
    try:
        from myriaxon import poster

        ok = hasattr(poster, "POSTER_TOOL_DEF")
        print(f"  [{'ok' if ok else '!!'}] design_poster tool is registered")
        if not ok:
            print("      the poster module is present but not wired into the tool registry")
        if poster.png_available():
            print("  [ok] PNG output available (Pillow present)")
        else:
            print("  [!!] PNG output unavailable — the dot will write SVG only.")
            print("       install with: pip install pillow   (or: pip install \"myriaxon[poster]\")")
    except Exception as exc:  # noqa: BLE001
        print(f"  [!!] poster module could not be imported: {exc}")

    # 2. Figma, only if a real token was supplied.
    print()
    if args.figma_token:
        if not args.figma_token.startswith("figd_"):
            print("  [!!] that does not look like a Figma personal access token (they start figd_)")
            print("       writing it anyway — change it later in the dashboard's MCP panel")
        changed, message = configure_figma(args.figma_token)
        print(f"  [{'ok' if changed else '!!'}] {message}")
    else:
        print("  [--] Figma not configured: no token supplied.")
        print("       Create one at Figma -> Settings -> Security -> Personal access tokens,")
        print("       then re-run:  python tools/setup_cowork.py --figma-token figd_xxx")
        print("       Until then the dot designs without Figma and says so.")

    # 3. the dot itself.
    print()
    dot, created = upsert_dot()
    verb = "created" if created else "updated"
    print(f"  [ok] dot {dot.name!r} {verb}")
    print(f"       role        : {dot.role}")
    print(f"       objectives  : {len(dot.goals)}")
    for goal in dot.goals:
        print(f"         [{goal.id}] {goal.title}")
    print(f"       proactive   : {dot.proactive} (design work waits for a person's eye)")
    print(f"       workspace   : {dot.workspace_path()}")

    print()
    print("Next:")
    print("  Terminal   myriaxon  then:  /dots run cowork")
    print("  Browser    http://127.0.0.1:8765/  ->  Dots  ->  Run")
    print()
    print("Both run the same agent with the same tools. The dot's objectives are read from disk,")
    print("so a run started in one appears in the other.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
