"""A MINIMAL, TEST-ONLY stand-in for `rich`, so verifiers can import modules that depend on it.

## Read this before trusting anything it produces

This is **not** a reimplementation of `rich` and it must never ship. `rich` is genuinely absent from this machine and
there is no network to install it, which means every verifier that imports `myriaxon.cli` or `myriaxon.dashboard`
dies at the import line before checking anything. This module exists for exactly one purpose: to let those verifiers
reach their own assertions, most of which read FILES rather than call `rich` at all.

## What it deliberately does not do

It does not render, wrap, colour, or parse markup. `Console.print` writes its arguments to the given stream with
markup left visible, which is the OPPOSITE of what real `rich` does -- so any check that depends on markup being
interpreted must be treated as unverified while this is in use. `tools/verify_packaging.py` already asserts the
console script routes through `__main__` *because* the absence of `rich` used to kill `--version`; that check stays
meaningful here, and the fast path is exercised against the real absence.

## How to use it

    sys.path.insert(0, str(REPO / "tools" / "_testdoubles"))

Every verifier that needs it says so in its own output, so a passing run is never mistaken for a run against the real
library.
"""

from __future__ import annotations

import sys
from typing import Any

__all__ = ["Console", "Group", "Live", "Markdown", "Panel", "Prompt", "Text", "Theme",
           "escape", "install"]

__version__ = "0.0.0-testdouble"


def escape(text: Any) -> str:
    """Real `rich` escapes markup brackets. This returns the input unchanged.

    Returning it unchanged is the SAFE direction for the checks here: a verifier looking for a literal string still
    finds it, whereas escaping could hide a bracket a check is searching for.
    """
    return str(text)


class Text:
    """A stand-in that records what was appended and can stringify."""

    def __init__(self, text: str = "", style: str = "", **_: Any) -> None:
        self.plain = str(text)
        self.style = style

    def append(self, text: Any, style: str = "") -> "Text":
        self.plain += str(text)
        return self

    def __str__(self) -> str:
        return self.plain

    def __len__(self) -> int:
        return len(self.plain)


class Panel:
    def __init__(self, renderable: Any = "", **_: Any) -> None:
        self.renderable = renderable

    def __str__(self) -> str:
        return str(self.renderable)


class Prompt:
    """`Prompt.ask`/`Prompt.ask_async` return the default rather than blocking.

    A verifier must never hang waiting for input it cannot provide, so this cannot prompt even in principle.
    """

    @staticmethod
    def ask(_prompt: str = "", *, default: str = "", **_kw: Any) -> str:
        return default

    @staticmethod
    async def ask_async(_prompt: str = "", *, default: str = "", **_kw: Any) -> str:
        return default


class Console:
    """Writes plainly to a stream. No wrapping, no colour, no markup interpretation."""

    def __init__(self, *_, file: Any = None, **kwargs: Any) -> None:
        self.file = file or sys.stdout
        self.width = int(kwargs.get("width") or 80)

    def print(self, *objects: Any, **_kwargs: Any) -> None:
        message = " ".join(str(obj) for obj in objects)
        stream = self.file
        try:
            stream.write(message + "\n")
        except (AttributeError, ValueError):
            sys.stdout.write(message + "\n")

    # The fluent API real rich supports, so a call site that chains still works.
    def rule(self, title: str = "", **_kw: Any) -> None:
        self.print(f"--- {title} ---" if title else "---")

    def log(self, *objects: Any, **_kw: Any) -> None:
        self.print(*objects)

    def input(self, _prompt: str = "") -> str:
        return ""


def install() -> None:
    """Register the double as `rich`, `rich.console`, `rich.text`, `rich.markup`, `rich.panel`, `rich.prompt`.

    Idempotent, and it refuses to shadow a real installation: if `rich` can already be imported, this does nothing and
    says so, so a machine that has the real library is never quietly downgraded.
    """
    try:
        import rich  # noqa: F401

        print("  [testdouble] real rich is present; the double was NOT installed", flush=True)
        return
    except ImportError:
        pass

    import types

    root = types.ModuleType("rich")
    root.__version__ = __version__
    root.escape = escape
    module_console = types.ModuleType("rich.console")
    module_console.Console = Console
    module_text = types.ModuleType("rich.text")
    module_text.Text = Text
    module_markup = types.ModuleType("rich.markup")
    module_markup.escape = escape
    module_panel = types.ModuleType("rich.panel")
    module_panel.Panel = Panel
    module_prompt = types.ModuleType("rich.prompt")
    module_prompt.Prompt = Prompt

    for name, module in (
        ("rich", root), ("rich.console", module_console), ("rich.text", module_text),
        ("rich.markup", module_markup), ("rich.panel", module_panel), ("rich.prompt", module_prompt),
    ):
        sys.modules[name] = module
    root.console = module_console
    root.text = module_text
    root.markup = module_markup
    root.panel = module_panel
    root.prompt = module_prompt

    print("  [testdouble] rich is ABSENT; a minimal test double is standing in. "
          "Markup is NOT interpreted and rendering is NOT real.", flush=True)


class Group:
    """Several renderables printed in order. Real `rich` composes them; this concatenates."""

    def __init__(self, *renderables: Any) -> None:
        self.renderables = renderables

    def __str__(self) -> str:
        return "\n".join(str(item) for item in self.renderables)


class Live:
    """A no-op context manager.

    Real `rich` repaints a region of the terminal continuously. Nothing here repaints, so a caller using `Live` gets
    its output once rather than live -- a real behavioural difference, and precisely why this double must never be
    used outside a verifier that reads files instead of rendering.
    """

    def __init__(self, renderable: Any = None, **_kw: Any) -> None:
        self.renderable = renderable

    def __enter__(self) -> "Live":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def update(self, renderable: Any = None, **_kw: Any) -> None:
        self.renderable = renderable

    def refresh(self) -> None:
        return None

    def stop(self) -> None:
        return None


class Markdown:
    """Holds the markup and returns it unchanged. Real `rich` renders it to terminal styling."""

    def __init__(self, markup: str = "", **_kw: Any) -> None:
        self.markup = markup

    def __str__(self) -> str:
        return self.markup


class Theme:
    """Records the styles it was given. Real `rich` resolves them against a style stack."""

    def __init__(self, *_, **kwargs: Any) -> None:
        self.styles = dict(kwargs)
