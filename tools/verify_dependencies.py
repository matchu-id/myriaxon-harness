#!/usr/bin/env python3
"""Verify the declared dependencies match what the code actually needs.

A `pyproject.toml` that over-declares asks every user to install packages the core path does not use. One that
UNDER-declares installs cleanly and then crashes. Both failures are invisible until someone else installs the
package, which is why they are checked here rather than discovered by a user.

The specific thing this pins: httpx was a core dependency with the comment that it was "the default transport for
every OpenAI-compatible provider". It stopped being true when `direct_http_client.py` moved to `http.client`. So the
check reads the SOURCE and compares it against the metadata, in both directions.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")

    def section(name: str) -> str:
        start = pyproject.index(name)
        end = pyproject.index("\n[", start + 1) if "\n[" in pyproject[start + 1:] else len(pyproject)
        return pyproject[start:end]

    core = section("dependencies = [")
    core = core[:core.index("]")]

    print("=== 1. the core dependency list is what a minimal install needs ===")
    check("rich is a core dependency", '"rich' in core,
          "sixteen modules import it at module level, so without it the CLI does not start")
    check("anthropic is a core dependency", '"anthropic' in core, "the default provider")
    check("pygments is a core dependency", '"pygments' in core)
    check("prompt_toolkit is a core dependency", '"prompt_toolkit' in core)
    check("httpx is NOT a core dependency", '"httpx' not in core,
          "the provider path uses http.client now; the comment claiming otherwise was stale")

    print()
    print("=== 2. the code agrees: no top-level httpx import anywhere ===")
    top_level: list[str] = []
    lazy: list[str] = []
    for path in sorted((REPO / "myriaxon").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if re.match(r"^\s*(from httpx|import httpx)\b", line):
                entry = f"{path.relative_to(REPO)}:{number}"
                (top_level if len(line) - len(line.lstrip()) == 0 else lazy).append(entry)
    check("no module-level httpx import remains", not top_level, str(top_level))
    check("the remaining references are lazy", len(lazy) >= 5, f"{len(lazy)} lazy site(s)")

    print()
    print("=== 3. every lazy site degrades with ADVICE, not a bare ImportError ===")
    # The failure this prevents: `pip install myriaxon` succeeds, and the error arrives later inside a command with no
    # hint about what to install -- which is worse than a hard dependency.
    for relative, name in (("myriaxon/ollama.py", "OllamaUnavailable"),
                           ("myriaxon/web_tools.py", "WebToolUnavailable")):
        text = (REPO / relative).read_text(encoding="utf-8")
        check(f"{relative} defines {name}", f"class {name}" in text,
              "a `raise` of an undefined name is a runtime NameError, not a syntax error -- compiling does not catch it")
        check(f"  {relative} catches ImportError", "except ImportError as exc" in text)
        check(f"  and names the extra", "myriaxon[web]" in text,
              "a message that does not say what to install is a diagnosis, not a fix")

    mcp = (REPO / "myriaxon" / "mcp_client.py").read_text(encoding="utf-8")
    check("mcp_client guards its own import", "except ImportError" in mcp or "return []" in mcp)

    print()
    print("=== 4. the extras are declared, so the advice is followable ===")
    extras = section("[project.optional-dependencies]")
    for extra in ("web", "mcp", "openai", "gemini", "poster", "keychain", "all", "dev"):
        check(f"  the {extra!r} extra exists", re.search(rf"^{extra} = \[", extras, re.M) is not None)
    check("  'web' declares httpx", re.search(r'^web = \[[^\]]*httpx', extras, re.M) is not None,
          "the advice says `pip install myriaxon[web]`, so that extra has to install httpx")
    check("  'mcp' declares httpx too", re.search(r'^mcp = \[[^\]]*httpx', extras, re.M) is not None,
          "the MCP registry search uses it")
    check("  'all' declares httpx", re.search(r"^all = \[[^\]]*httpx", extras, re.M | re.S) is not None,
          "an 'all' that omits a declared extra is not all of them")

    print()
    print("=== 5. the stale comment is GONE, not just the dependency ===")
    # A corrected list with a wrong explanation beside it is how the same mistake comes back.
    check("no claim that httpx is the default transport for providers",
          "httpx is the default transport" not in pyproject,
          "a stale justification is how a removed dependency gets re-added")
    check("the reason for its absence is written down",
          "http.client" in pyproject, "so a later reader knows WHY it is not needed")

    print()
    print("=== 6. rich is honestly declared, not optional ===")
    check("rich is in core, not an extra", '"rich' in core)
    rich_modules = []
    for path in sorted((REPO / "myriaxon").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^(from rich|import rich)", text, re.M):
            rich_modules.append(path.name)
    check("and the count justifies it", len(rich_modules) >= 10,
          f"{len(rich_modules)} module(s) import it at module level")

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
