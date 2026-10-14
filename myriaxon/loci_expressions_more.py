"""Expressions beyond the spec's fourteen, kept in a separate module on purpose.

## Why these are not simply added to the manifest

`loci_character` publishes the spec, and the spec's own reasoning is that too many moods makes a character unreadable
-- a face only works if a reader can tell it apart from the others at a glance. So the published set stays the
contract, and these live beside it with an explicit rule about what happens when a surface has not implemented one.

## The fallback rule, which is the point of this module

A caller drawing an extension face on a surface that only knows the published set gets the CLOSEST published face,
chosen here and named in `NEAREST_SPEC`. Falling back to `idle` instead would show a contented face for `bored` or
`dozy`, which is worse than showing an approximate one: it would be actively wrong rather than merely imprecise.

## The tier excludes anything published, and filters rather than remembers

Measured: `curious` and `working` were extensions until the published spec grew them, at which point the merged set
stopped satisfying "spec plus extras" and the collision check failed. A hand-maintained exclusion list would collide
again the next time a face is published, so the tier filters instead -- a name is never in two places, and the merge
identity holds however either side grows.
"""

from __future__ import annotations

from .loci_character import Expression

# Nineteen faces, each seven slots in `loci_character.SLOT_ORDER`:
#   S = star, W = window, L = left eye, R = right eye, C = cheek, M = mouth, N = lantern
#
# Every ASCII twin is ONE ASCII character per slot, because the width guard substitutes slot by slot and a
# two-character twin would shift the frame. Every mood is one of the seven the manifest defines.
_RAW_EXPRESSIONS: dict[str, Expression] = {
    "bored": Expression(
        "bored", "Nothing to do", "dim",
        ("✦", "▫", "─", "─", "‿", "‿", "◌"),
        ("*", "#", "-", "-", "u", "u", "."),
    ),
    "eager": Expression(
        "eager", "Ready", "bright",
        ("✦", "▫", "◎", "◎", "‿", "▽", "◉"),
        ("*", "#", "O", "O", "u", "v", "@"),
    ),
    "puzzled": Expression(
        "puzzled", "Not sure", "alert",
        ("✦", "▫", "◉", "◔", "‿", "~", "◍"),
        ("*", "#", "o", "c", "u", "~", "O"),
    ),
    "waking": Expression(
        "waking", "Waking up", "dim",
        ("✦", "▫", "─", "─", "‿", "○", "◌"),
        ("*", "#", "-", "-", "u", "0", "."),
    ),
    "dozy": Expression(
        "dozy", "Sleepy", "dim",
        ("✦", "▫", "─", "─", "‿", "‿", "◌"),
        ("*", "#", "-", "-", "u", "u", "."),
    ),
    "mindful": Expression(
        "mindful", "Checking in", "care",
        ("✦", "▫", "◉", "◉", "‿", "‿", "◍"),
        ("*", "#", "o", "o", "u", "u", "O"),
        persistent=True,
    ),
    "relieved": Expression(
        "relieved", "Glad that is over", "warm",
        ("✦", "▫", "‿", "‿", "‿", "‿", "◍"),
        ("*", "#", "u", "u", "u", "u", "O"),
    ),
    "steady": Expression(
        "steady", "Still here", "calm",
        ("✦", "▫", "◉", "◉", "‿", "─", "◍"),
        ("*", "#", "o", "o", "u", "-", "O"),
    ),
    "waiting": Expression(
        "waiting", "Waiting", "calm",
        ("✦", "▫", "◉", "◉", "‿", "◦", "◍"),
        ("*", "#", "o", "o", "u", ".", "O"),
    ),
    "shy": Expression(
        "shy", "Oh, hello", "warm",
        ("✦", "▫", "◉", "◉", "‿", "‿", "◍"),
        ("*", "#", "o", "o", "u", "u", "O"),
    ),
    "amused": Expression(
        "amused", "That is funny", "bright",
        ("✦", "▫", "◉", "◉", "‿", "▽", "◍"),
        ("*", "#", "o", "o", "u", "v", "O"),
    ),
    "abashed": Expression(
        "abashed", "My mistake", "dim",
        ("✦", "▫", "◔", "◔", "‿", "︵", "◌"),
        ("*", "#", "c", "c", "u", "v", "."),
    ),
    "agreeing": Expression(
        "agreeing", "Agreed", "warm",
        ("✦", "▫", "◉", "◉", "‿", "‿", "◍"),
        ("*", "#", "o", "o", "u", "u", "O"),
    ),
    "surprised": Expression(
        "surprised", "Oh!", "alert",
        ("✦", "▫", "◎", "◎", "‿", "○", "◍"),
        ("*", "#", "O", "O", "u", "0", "O"),
    ),
    "protective": Expression(
        "protective", "I have got this", "care",
        ("✦", "▫", "◉", "◉", "‿", "─", "◍"),
        ("*", "#", "o", "o", "u", "-", "O"),
        persistent=True,
    ),
    "tender": Expression(
        "tender", "Take your time", "care",
        ("✦", "▫", "◉", "◉", "‿", "︵", "◍"),
        ("*", "#", "o", "o", "u", "v", "O"),
        persistent=True,
    ),
    "satisfied": Expression(
        "satisfied", "That worked", "bright",
        ("✦", "▫", "◉", "◉", "‿", "▽", "◍"),
        ("*", "#", "o", "o", "u", "v", "O"),
    ),
}


# Extension -> the published face that reads closest, for a surface that has not implemented the extension.
#
# Chosen by MEANING rather than by which glyphs happen to match, because a reader is being shown the fallback AS a
# substitute: `bored` must look subdued rather than contented, and `protective` must look caring.
NEAREST_SPEC: dict[str, str] = {
    "bored": "sleepy",
    "eager": "cheering",
    "puzzled": "confused",
    "waking": "sleepy",
    "dozy": "sleepy",
    "mindful": "caring",
    "relieved": "happy",
    "steady": "idle",
    "waiting": "idle",
    "shy": "happy",
    "amused": "cheering",
    "abashed": "sorry",
    "agreeing": "happy",
    "surprised": "confused",
    "protective": "caring",
    "tender": "caring",
    "satisfied": "cheering",
}


def published_names() -> tuple[str, ...]:
    """The published faces, by name. A function so the import stays lazy and the module cycle stays open."""
    from .loci_character import EXPRESSIONS as _published  # noqa: PLC0415

    return tuple(_published)


# The tier EXCLUDES anything the published set defines. See the module docstring for the measurement behind this.
EXTRA_EXPRESSIONS: dict[str, Expression] = {
    name: expression
    for name, expression in _RAW_EXPRESSIONS.items()
    if name not in set(published_names())
}


def all_expressions(published: dict[str, Expression] | None = None) -> dict[str, Expression]:
    """The published faces plus the extensions.

    The PUBLISHED set wins a name clash, which is why this takes the published set as an argument rather than only
    importing it: an extension must never be able to silently redefine part of the contract, and the merge has to be
    checkable against a synthetic clash to prove it. `verify_loci_autonomy` passes one.
    """
    if published is None:
        from .loci_character import EXPRESSIONS as published  # noqa: PLC0415 - deliberate late binding

    merged = dict(EXTRA_EXPRESSIONS)
    merged.update(published)
    return merged


def nearest_spec(name: str) -> str:
    """The closest published face for an extension, or `idle` for anything unknown.

    Always returns a name that EXISTS in the published set, so a caller can draw the result without a second check.
    """
    from .loci_character import EXPRESSIONS as published  # noqa: PLC0415

    chosen = NEAREST_SPEC.get(name)
    if chosen in published:
        return chosen
    return "idle"


def is_extension(name: str) -> bool:
    """Whether a name is one of the extensions rather than part of the published contract."""
    return name in EXTRA_EXPRESSIONS


def coverage() -> tuple[set[str], set[str]]:
    """`(extensions, published faces)` -- the two sets, so a caller can see the overlap rather than assume it."""
    return set(EXTRA_EXPRESSIONS), set(published_names())
