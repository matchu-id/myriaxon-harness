"""Count features across the four harnesses from each tree's OWN registration convention.

File counts are not features. This looks for the specific artefact each project uses to declare a user-facing
capability, so the numbers mean the same kind of thing:

  * **Myriaxon** -- read from the live registry (tools, CLI commands, roles), never by counting files.
  * **OpenClaw** -- `<name>-tool.ts` per tool; `extensions/<name>/` per extension.
  * **DeepSeek**  -- `tool-<name>` packages; `@dsh/tool-*` names; `SKILL.md` per skill.
  * **Claude Code** -- `plugins/<name>/` per plugin, plus its `commands/`, `agents/`, `skills/`, `hooks/`.

Test files are excluded everywhere: a `foo-tool.test.ts` is regression protection, not a tool.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
TREES = {
    "Myriaxon": ROOT,
    "OpenClaw": ROOT / "openclaw-src" / "openclaw-main",
    "DeepSeek": ROOT / "deepseek-harness-master-src" / "deepseek-harness-master",
    "ClaudeCode": ROOT / "claude-code-main-src" / "claude-code-main",
    "PiHarness": ROOT / "pi-harness-main-src" / "pi-harness-main",
    "Opencode": ROOT / "opencode-dev-src" / "opencode-dev",
}

EXCLUDED = {
    "node_modules", ".git", "dist", "build", "lib", "vendor", "target",
    ".venv", ".uv", ".venv-harbor-linux",
    # Reference trees extracted into this repository. The `-src` names are also covered by `is_excluded` below --
    # enumerating names alone failed twice (once at 70x, once at 10x) because a list must be edited whenever a tree
    # is added, and the failure is silent.
    "openclaw-src", "claude-code-main-src", "deepseek-harness-master-src",
    "pi-harness-main-src", "opencode-dev-src",
    "certivo-rework", "localisation", "__pycache__", ".pytest_cache", ".mypy_cache",
}

# The convention that marks an extracted reference tree: see docs/REFERENCE_TREES.md. Enforced as a rule rather
# than left to the list above, so a seventh tree cannot inflate a count because someone forgot an edit.
EXTRACTED_SUFFIX = "-src"


def is_excluded(name: str) -> bool:
    """Whether a directory is not Myriaxon's code."""
    return name in EXCLUDED or name.endswith(EXTRACTED_SUFFIX)

# Exclusions that apply only at a specific tree's ROOT.
#
# A bare directory name is not a safe exclusion across four trees: `jobs` is Myriaxon's benchmark-artefact
# directory AND a legitimate DeepSeek package (`packages/jobs/tool-jobs`), so excluding it globally silently
# dropped a real tool -- measured at 24 instead of 25. Scoping by root-relative path removes the collision rather
# than trading one miscount for another.
ROOT_EXCLUDED = {
    "Myriaxon": {"jobs"},
    "OpenClaw": set(),
    "DeepSeek": set(),
    "ClaudeCode": set(),
}

# NOT excluded, deliberately: `snapshots`, `fixtures`, `__snapshots__`. They were in the first version of this
# list and they hid `packages/session-query/tool-session-query`, because `snapshots` is a *generic* name that also
# appears deep inside legitimate feature trees. Recorded fixtures are excluded by WHERE the artefact is declared
# (a `tool-*` directory under `packages/`), which states the rule instead of guessing from a directory name.

# Path segments that mark recorded output, used only where an artefact has no declaring location of its own.
FIXTURE_SEGMENTS = {"snapshots", "__snapshots__", "fixtures"}

# Directories whose contents are documentation, RFCs or agent scratch rather than registrable capability. Excluded
# from the instruction-file count: DeepSeek's `.agents/notes` is an RFC system ("durable proposals and decision
# records written by agents"), so counting its files as instruction surfaces would inflate the number with prose
# that instructs nothing at runtime.
PROSE_DIRS = {"docs", "doc", "website", "notes", "archived", "implemented", "proposed", "benchmarks"}

TEST_SUFFIXES = (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx", "_test.py", ".test.js")
SUPPORT_MARKERS = ("test-support", ".test.", ".spec.", "__tests__")


def walk(tree: Path, root_name: str = ""):
    """Prune as it descends. `root_name` selects the per-tree root exclusions.

    `os.walk` yields `current` as a **string**, so depth is computed by counting separators rather than with
    `Path.relative_to` -- which raises `AttributeError` on a `str`, and would also fail on a path that has since
    been removed.
    """
    root_extra = ROOT_EXCLUDED.get(root_name, set())
    base = str(tree)
    for current, dirnames, filenames in os.walk(base, onerror=lambda _e: None):
        depth = len(Path(current).parts) - len(Path(base).parts)
        keep = []
        for directory in dirnames:
            if is_excluded(directory):
                continue
            # A root exclusion applies only at depth 0, so `Myriaxon/jobs` is skipped while `packages/jobs` is not.
            if depth == 0 and directory in root_extra:
                continue
            keep.append(directory)
        dirnames[:] = keep
        yield Path(current), filenames


def is_test(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered.endswith(TEST_SUFFIXES)
        or any(marker in lowered for marker in SUPPORT_MARKERS)
        or lowered.startswith("test_")
    )


def all_named(tree: Path, predicate, root_name: str = "") -> set[str]:
    """Names of files matching a predicate, excluding tests and support files."""
    found: set[str] = set()
    for _current, filenames in walk(tree, root_name):
        for name in filenames:
            if is_test(name):
                continue
            match = predicate(name)
            if match:
                found.add(match)
    return found


def dirs_named(tree: Path, names: tuple[str, ...], *, root_name: str = "") -> set[str]:
    """Relative paths of directories with one of these names."""
    found: set[str] = set()
    for current, _ in walk(tree, root_name):
        if current.name in names:
            found.add(str(current.relative_to(tree)))
    return found


# --------------------------------------------------------------------------- per-project


def count_skills(tree: Path, root_name: str = "") -> int:
    """Count `SKILL.md` files, excluding recorded fixtures.

    Two corrections are folded in. Directories named `skills` were counted first, which reported 18 for OpenClaw
    against 124 `SKILL.md` files -- a directory can hold any number of skills, and one can hold none, so the
    artefact is the skill. Then fixture trees were included, which added 3 to DeepSeek from
    `snapshots/*/skills/`. Both are now excluded: the artefact is counted, and a fixture is recognised by its path.
    """
    found = 0
    for current, filenames in walk(tree, root_name):
        if "SKILL.md" not in filenames:
            continue
        if any(part in FIXTURE_SEGMENTS for part in current.parts):
            continue
        found += 1
    return found


def count_openclaw(tree: Path, root_name: str = "") -> dict:
    tools = all_named(tree, lambda n: (m := re.fullmatch(r"([a-z0-9][a-z0-9-]*)-tool\.ts", n)) and m.group(1), root_name)
    ext_root = tree / "extensions"
    extensions = {d.name for d in ext_root.iterdir() if d.is_dir()} if ext_root.is_dir() else set()
    mcp = dirs_named(tree, ("mcp", "mcp-servers"), root_name=root_name)
    return {
        "tools": len(tools),
        "extensions": len(extensions),
        "skills": count_skills(tree, root_name),
        "mcp_surfaces": len(mcp),
        "_tool_names": sorted(tools)[:8],
    }


def count_deepseek(tree: Path, root_name: str = "") -> dict:
    """DeepSeek's tools live one per `packages/<area>/tool-<name>/` package.

    Scoped to `packages/` rather than to the whole tree. A whole-tree search for `tool-*` directories finds three
    more under `snapshots/`, which are *recorded fixtures* -- `snapshots/sdk/tool-error-details` is a captured
    transcript, not a tool. Removing `snapshots` from the exclusion list (necessary, because the name also appears
    inside real packages) made those three appear; scoping to where tools are declared is the precise fix, and it
    is better than a directory-name rule because it states *where the artefact lives* rather than guessing from a
    name.
    """
    packages = tree / "packages"
    tool_dirs: set[str] = set()
    for current, _ in walk(packages, root_name):
        if current.name.startswith("tool-"):
            tool_dirs.add(current.name[len("tool-"):])

    pkg_count = len([d for d in packages.iterdir() if d.is_dir()]) if packages.is_dir() else 0

    agents_md = 0
    for current, filenames in walk(tree, root_name):
        if "AGENTS.md" not in filenames:
            continue
        if any(part in PROSE_DIRS for part in current.parts):
            continue
        agents_md += 1

    return {
        "tools": len(tool_dirs),
        "packages": pkg_count,
        "skills": count_skills(tree, root_name),
        "instruction_files": agents_md,
        "_tool_names": sorted(tool_dirs)[:8],
    }


def count_claudecode(tree: Path, root_name: str = "") -> dict:
    plugins = tree / "plugins"
    plugin_dirs = {d.name for d in plugins.iterdir() if d.is_dir()} if plugins.is_dir() else set()

    command_files: set[str] = set()
    agent_files: set[str] = set()
    skill_files: set[str] = set()
    for current, filenames in walk(tree, root_name):
        parts = current.parts
        if "commands" in parts:
            command_files.update(f for f in filenames if f.endswith(".md"))
        if "agents" in parts:
            agent_files.update(f for f in filenames if f.endswith(".md"))
        if "skills" in parts:
            skill_files.update(f for f in filenames if f == "SKILL.md")

    # Hooks are counted from their REGISTRATION, not from files under a `hooks/` directory.
    #
    # The first version counted every file in any path containing `hooks`, which reported 523 -- because those
    # directories contain nested implementation (limits/columns, views/sections, git/probes, a telemetry subtree).
    # A hook is an entry in a `hooks.json`; counting the files that implement one inflates the number ~25x.
    hook_entries = 0
    for current, filenames in walk(tree, root_name):
        if "hooks.json" not in filenames:
            continue
        try:
            import json as _json

            data = _json.loads((current / "hooks.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        hooks = data.get("hooks", data) if isinstance(data, dict) else {}
        if isinstance(hooks, dict):
            for value in hooks.values():
                hook_entries += len(value) if isinstance(value, list) else 1
        elif isinstance(hooks, list):
            hook_entries += len(hooks)

    mods = tree / "mods"
    mod_dirs = {d.name for d in mods.iterdir() if d.is_dir()} if mods.is_dir() else set()
    return {
        "plugins": len(plugin_dirs),
        "commands": len(command_files),
        "agents": len(agent_files),
        "skills": len(skill_files),
        "hooks": hook_entries,
        "mod_groups": len(mod_dirs),
    }


def count_pi_harness(tree: Path, root_name: str = "") -> dict:
    """pi-harness is an Electron desktop app, so its features are declared in different places than the others.

    Counted from its own artefacts:
      * `SKILL.md` files for skills -- the same rule used for OpenClaw and DeepSeek, because a skill is the file;
      * `<name>-service.ts` under `src/main/` for the main-process services a renderer can drive;
      * `<name>.vue` under `src/renderer/` for views;
      * the IPC channel modules that connect the two, which is what makes a desktop harness different from a CLI.
    """
    skill_files = [
        p for p in tree.rglob("SKILL.md")
        if not any(seg in FIXTURE_SEGMENTS for seg in p.relative_to(tree).parts)
    ]
    services = list((tree / "src" / "main").rglob("*-service.ts")) if (tree / "src" / "main").is_dir() else []
    views = list((tree / "src" / "renderer").rglob("*.vue")) if (tree / "src" / "renderer").is_dir() else []
    ipc = list((tree / "src").rglob("*ipc*")) if (tree / "src").is_dir() else []
    agents_md = list(tree.rglob("AGENTS.md"))
    return {
        "skills": len(skill_files),
        "main_services": len(services),
        "renderer_views": len(views),
        "ipc_modules": len(ipc),
        "instruction_files": len(agents_md),
        "kind": "electron desktop app",
    }


def count_opencode(tree: Path, root_name: str = "") -> dict:
    """opencode's tools are declared in a registry module, so that is what is counted.

    Two registries exist: `packages/opencode/src/tool/registry.ts` (the richer, current surface) and
    `packages/core/src/tool/builtins.ts` (a smaller location-scoped set whose own comment lists what is
    "deliberately" still to be ported). The larger is counted, and the difference is reported rather than hidden --
    the same treatment Claude Code's missing CLI surface gets.
    """
    skill_files = [
        p for p in tree.rglob("SKILL.md")
        if not any(seg in FIXTURE_SEGMENTS for seg in p.relative_to(tree).parts)
    ]
    rich_dir = tree / "packages" / "opencode" / "src" / "tool"
    core_dir = tree / "packages" / "core" / "src" / "tool"
    # A tool module is `<name>.ts` that is not a test, not a `.txt` prompt fragment, and not one of the plumbing
    # files (`tool`, `registry`, `schema`, `truncate`, `invalid`). Counting directory entries was the mistake that
    # produced the OpenClaw `hooks: 523` figure, so the rule states what a tool IS.
    plumbing = {"tool", "registry", "schema", "json-schema", "truncate", "truncation-dir",
                "invalid", "index", "types", "helpers", "external-directory"}
    def modules(directory: Path) -> list[str]:
        if not directory.is_dir():
            return []
        return sorted(
            p.stem for p in directory.glob("*.ts")
            if ".test." not in p.name and p.stem not in plumbing
        )
    rich = modules(rich_dir)
    core = modules(core_dir)
    packages = [d for d in (tree / "packages").iterdir() if d.is_dir()] if (tree / "packages").is_dir() else []
    agents_md = list(tree.rglob("AGENTS.md"))
    return {
        "tools": len(rich),
        "tools_core_layer": len(core),
        "packages": len(packages),
        "skills": len(skill_files),
        "instruction_files": len(agents_md),
        "kind": "cli + server + desktop",
    }


def count_myriaxon(root_name: str = "") -> dict:
    import sys

    sys.path.insert(0, str(ROOT))
    from myriaxon import roles, tool_registry as TR
    from myriaxon.argparse_cli import build_parser

    parser = build_parser()
    commands = []
    for action in parser._actions:
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            commands = sorted(action.choices)

    defs = TR.build_tool_defs()
    visible = TR.apply_tool_visibility(defs, set())
    return {
        "tools": len(defs),
        "tools_visible": len(visible),
        "tool_groups": len(TR.DEFERRED_GROUPS),
        "cli_commands": len(commands),
        "roles": len(list((ROOT / "myriaxon" / "builtin_roles").glob("*/ROLE.md"))),
        "skills": len(list((ROOT / "myriaxon" / "builtin_skills").glob("*/SKILL.md"))),
        "verifier_files": len(list((ROOT / "tools").glob("verify_*.py"))),
        "modules": len(list((ROOT / "myriaxon").glob("*.py"))),
        "_command_names": commands,
    }


COUNTERS = {
    "Myriaxon": lambda _t, _n: count_myriaxon(),
    "OpenClaw": count_openclaw,
    "DeepSeek": count_deepseek,
    "ClaudeCode": count_claudecode,
    "PiHarness": count_pi_harness,
    "Opencode": count_opencode,
}


def main() -> int:
    report: dict = {}
    for name, tree in TREES.items():
        report[name] = COUNTERS[name](tree, name)

    rows = [
        ("tools the model can call", "tools"),
        ("CLI commands", "cli_commands"),
        ("built-in roles / agents", "roles"),
        ("skills", "skills"),
        ("plugin / extension packages", "extensions"),
        ("hook attachment points", "hooks"),
    ]
    print(f"  {'feature':<32}" + "".join(f"{n:>13}" for n in TREES))
    print("  " + "-" * 32 + "-" * (13 * len(TREES)))
    for label, key in rows:
        row = f"  {label:<32}"
        for name in TREES:
            value = report[name].get(key)
            row += f"{value:>13,}" if isinstance(value, int) else f"{'-':>13}"
        print(row)

    print()
    print("  per-harness detail:")
    for name, data in report.items():
        extras = {k: v for k, v in data.items() if not k.startswith("_")}
        print(f"    {name:<11} {extras}")

    print()
    print("  samples, to show the counting rule found real things:")
    for name, data in report.items():
        for key, value in data.items():
            if key.startswith("_") and isinstance(value, list) and value:
                print(f"    {name:<11} {key[1:]:<18} {value}")

    out = ROOT / "docs" / "feature-report.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print()
    print(f"  written to {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
