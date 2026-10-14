"""Verify the poster tool is exposed to every provider and actually runs end to end."""

from __future__ import annotations

import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import tool_registry  # noqa: E402
from myriaxon.constants import DEFAULT_MODE  # noqa: E402
from myriaxon.tool_context import ToolContext  # noqa: E402

OUT = REPO / "bench-out" / "posters"


def make_ctx() -> ToolContext:
    """A real ToolContext, because dispatch consults far more of it than a handler does.

    A hand-rolled stub was tried first and failed twice -- on `last_tool_signature`, then on
    `console.print` -- which is the point: the registry touches the context for loop detection,
    policy and progress output, so a double would have to model all of it and would drift. The
    console is real but writes to a buffer, so tool progress does not pollute this report.
    """
    from rich.console import Console

    return ToolContext(client=None, model="check", max_tokens=1024,
                       console=Console(file=io.StringIO(), width=100), yolo=True,
                       tools=[], mode=DEFAULT_MODE)


def main() -> int:
    failures = 0

    print("exposure per provider")
    builds = {
        "anthropic": tool_registry.build_tool_defs,
        "openai": tool_registry.build_tool_defs_openai,
        "gemini": tool_registry.build_tool_defs_gemini,
    }
    for provider, build in builds.items():
        try:
            defs = build()
        except TypeError:
            defs = build(include_task=True)
        names = [d.get("name") or d.get("function", {}).get("name") for d in defs]
        ok = "design_poster" in names
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'}  {provider:10} {len(names):3} tools, design_poster={ok}")

    static = [d.get("name") for d in tool_registry._static_tool_defs()]
    ok = "design_poster" in static
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'}  static tuple contains design_poster")

    print("\ndispatch")
    message, is_error = tool_registry.dispatch_tool(
        "design_poster",
        {
            "title": "Dispatch works",
            "eyebrow": "Tool check",
            "subtitle": "Called through the real registry, not the module directly.",
            "size": "square",
            "theme": "ink",
            "formats": ["svg", "png"],
            "out_dir": str(OUT),
            "stem": "dispatch-check",
        },
        make_ctx(),
    )
    failures += 1 if is_error else 0
    print(f"  {'ok  ' if not is_error else 'FAIL'}  dispatch returned an error: {is_error}")
    for line in message.splitlines():
        print(f"        {line}")

    print("\nvalidation")
    bad, bad_error = tool_registry.dispatch_tool(
        "design_poster", {"title": "Test", "nonsense_field": 1}, make_ctx()
    )
    ok = bad_error and "nonsense_field" in bad
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'}  an unknown field is refused, not ignored")

    empty, empty_error = tool_registry.dispatch_tool("design_poster", {"title": "   "}, make_ctx())
    ok = empty_error
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'}  an empty title is refused")

    print()
    print("poster tool verified" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
