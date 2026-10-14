"""A MINIMAL, TEST-ONLY stand-in for `pygments`, so verifiers can import modules that depend on it.

## Same warning as the `rich` double next door

`pygments` is genuinely absent and there is no network. The package imports exactly two things from it, both in
`theme.py`, and both for SYNTAX HIGHLIGHTING: a `Style` base class and the token constants. Nothing about the agent's
behaviour depends on either, so a double lets the real provider path run -- a real turn, over real HTTP, with real
tool dispatch -- while proving nothing whatsoever about how code is coloured.

`tools/e2e_check.py` prints a note whenever this is in use, for exactly that reason.
"""

from __future__ import annotations

from typing import Any

__all__ = ["Style", "token"]

__version__ = "0.0.0-testdouble"


class _Token:
    """A token type. Real pygments has a deep hierarchy; this synthesises children on demand.

    `Keyword.Constant` and `String.Doc` are attribute CHAINS, so a flat namespace of names is not enough. Children are
    created on first access and MEMOISED, which matters: equality is identity, so a style key built once must compare
    equal to the same path looked up later. It does, because the parent caches the child it handed out.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._children: dict[str, "_Token"] = {}

    def __getattr__(self, item: str) -> "_Token":
        # `__getattr__` is only reached when normal lookup fails, so this cannot shadow a real attribute.
        if item.startswith("_"):
            raise AttributeError(item)
        child = self._children.get(item)
        if child is None:
            child = _Token(f"{self.name}.{item}")
            self._children[item] = child
        return child

    def __repr__(self) -> str:
        return f"Token.{self.name}"

    def __str__(self) -> str:
        return self.name


class _TokenNamespace:
    """`pygments.token`, with the names `theme.py` imports plus the usual children."""

    def __init__(self) -> None:
        for name in ("Token", "Text", "Whitespace", "Error", "Other",
                     "Keyword", "Name", "Literal", "String", "Number", "Operator",
                     "Punctuation", "Comment", "Generic", "GenericHeading"):
            setattr(self, name, _Token(name))


class Style:
    """The base class `theme.py` subclasses. It only ever declares `styles` and `background_color`."""

    styles: dict = {}
    background_color = ""

    def __init__(self, **_: Any) -> None:
        pass

    @classmethod
    def style_for_token(cls, _token: Any) -> dict:
        return {}
