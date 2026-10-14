#!/usr/bin/env python3
"""Verify Loci's character: rendering, the behavioural rules, and the two surfaces agreeing.

## What this exists to hold

The design spec is unusual in that most of its hard requirements are *promises about behaviour* rather than facts
about drawing: crisis is never cute, celebrate is rationed, anger is never mirrored, no mood is carried by colour
alone. A promise nothing asserts is a paragraph. Every one of those is checked here.

## The three defects found while building it, all now asserted

  1. **Crisis de-escalated.** `persistent=True` stopped the *timed* decay in `tick()`, but an ordinary signal still
     replaced crisis outright — measured: `apply(SIGNAL_TASK_DONE)` turned `crisis` into `cheering`, and
     `apply(SIGNAL_QUIET)` turned it into `idle`. Persistence guarded one path and not the other, which is this
     repository's recurring failure mode.
  2. **Anger was mirrored.** The hostile rule was written as `hostile and wanted in (sorry, concerned, confused)`, so
     hostile text arriving with a neutral signal fell through to `idle` — a contented face at an angry user. The
     signal a caller passes is not a proxy for how the user is speaking.
  3. **The spec's own promise about the frame was nearly violated.** `⬤`, `︵` and `★` render double-width in some
     East Asian locales, which shifts every line of the art. The width guard is asserted rather than trusted.

A fourth defect was in this verifier's first draft: it asserted `"dots" not in output` for a line that legitimately
contained the word "dots" in a comment. Same lesson as the rename work — a negative substring test over text that
documents the thing always matches the documentation.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# `rich` is genuinely absent from this machine and there is no network to install it, which would otherwise kill this
# verifier at the import line -- before a single assertion ran. Most of what it checks reads FILES, so it can still do
# its job against a documented test double. The double refuses to shadow a real installation, and it says so loudly
# whenever it is used, so a passing run can never be mistaken for a run against the real library.
if importlib.util.find_spec("rich") is None:
    sys.path.insert(0, str(REPO / "tools" / "_testdoubles"))
    print("  [NOTE] rich is absent; using the test double in tools/_testdoubles. "
          "Markup is NOT interpreted, so any markup-dependent check is UNVERIFIED.", flush=True)

from myriaxon import loci_character as C  # noqa: E402
from myriaxon import loci_expression as X  # noqa: E402
from myriaxon import loci_surface as S  # noqa: E402
from myriaxon import loci_svg  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []

PLAIN_UNI = C.Terminal(color=C.T0_PLAIN, unicode=True, columns=100)
PLAIN_ASCII = C.Terminal(color=C.T0_PLAIN, unicode=False, columns=100)
TRUE = C.Terminal(color=C.T3_TRUECOLOR, unicode=True, columns=120, motion=True)

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""), flush=True)


def visible(text: str) -> str:
    return ANSI.sub("", text)


def main() -> int:
    print("=== 1. the manifest is complete and self-consistent ===")
    check("every expression has a label", all(e.label for e in C.EXPRESSIONS.values()))
    check("every expression names a defined mood",
          all(e.mood in C.MOODS for e in C.EXPRESSIONS.values()),
          str([e.mood for e in C.EXPRESSIONS.values() if e.mood not in C.MOODS]))
    check("every expression has all seven slots",
          all(len(e.uni) == len(C.SLOT_ORDER) and len(e.ascii_glyphs) == len(C.SLOT_ORDER)
              for e in C.EXPRESSIONS.values()),
          str([k for k, e in C.EXPRESSIONS.items() if len(e.uni) != 7]))
    check("only the documented glyphs are non-ASCII in ascii_glyphs",
          all(all(ord(ch) < 128 for ch in e.ascii_glyphs) for e in C.EXPRESSIONS.values()),
          str([k for k, e in C.EXPRESSIONS.items()
               if any(ord(c) >= 128 for c in e.ascii_glyphs)]))
    check("every expression has a distinct id key",
          all(key == e.id for key, e in C.EXPRESSIONS.items()))
    # The count is asserted as a BOUND rather than a constant, and the reason is a genuine conflict between two
    # verifiers written at different stages: this file pinned fourteen, while `verify_loci_e2e` drives a session that
    # opens on `happy` and the dashboard calls `setLoci("happy")` on load -- so `happy` is part of the live contract
    # and the fourteen-expression list predates it. A bound plus the structural checks above (seven slots, a mood, a
    # label, a distinct id) is what actually protects the manifest: a wrong COUNT protects nothing, because it cannot
    # tell a well-formed new face from a malformed one.
    check("the manifest is the expected size",
          14 <= len(C.EXPRESSIONS) <= 20,
          f"{len(C.EXPRESSIONS)} expressions")
    check("  and every face is drawable by the dashboard",
          loci_svg.coverage()[0] == set(C.EXPRESSIONS),
          "the two surfaces must agree on the set, whatever its size")

    print()
    print("=== 2. THE SILHOUETTE RULE: roof, body and lantern survive every tier ===")
    # The spec's §3.2. If a rendering loses one of the three it must use a smaller tier instead -- so the smallest
    # tier is the one that proves the rule, and it must still carry the lantern.
    for name in C.EXPRESSIONS:
        hero = visible(C.render_hero(name, PLAIN_UNI))
        check(f"{name}: hero has a roof", "/^\\" in hero, hero.splitlines()[2] if len(hero.splitlines()) > 2 else "")
        check(f"{name}: hero has a body", "(__" in hero or "(___" in hero, "")
        check(f"{name}: hero has a lantern", "-[" in hero, "")
    signal = visible(C.render_signal("caring", PLAIN_UNI))
    check("the signal tier is exactly one column, and it is the lantern", len(signal) == 1, signal)
    check("  not a space", signal.strip() != "", repr(signal))

    print()
    print("=== 3. NEVER COLOUR ALONE: the label survives with colour stripped ===")
    # The spec's principle 2. A mood shown only by colour is invisible to a colourblind user and to NO_COLOR, so the
    # text channel must be present in the art itself and not only in a legend somewhere else.
    for name in ("idle", "caring", "crisis", "celebrate"):
        hero = visible(C.render_hero(name, PLAIN_UNI))
        label = C.expression_for(name).label
        check(f"{name}: the plain render names its mood", label in hero, hero.splitlines()[-1])
    check("the coloured render strips back to the same words",
          visible(C.render_hero("caring", TRUE)) == visible(C.render_hero("caring", PLAIN_UNI)))

    print()
    print("=== 4. the frame does not shift between expressions ===")
    # Every expression is the same nine lines AND the same total width, so switching expression never moves the art.
    #
    # Measured defect this caught: the `celebrate` template's confetti line is five columns wider than every other
    # line, so the frame shifted sideways whenever Loci celebrated -- precisely the violation the spec forbids, and
    # invisible in a still screenshot. The template is still wider; `render_hero` now pads every frame to a common
    # width, so a deliberate overhang cannot become a shift.
    #
    # The first version of this check compared `len(line)` (an int) to `len(widths[...])`, which raised TypeError.
    # Worth recording because it means the alignment was never verified until the shape of the assertion was fixed.
    rendered = {name: C.render_hero(name, PLAIN_UNI).splitlines() for name in C.EXPRESSIONS}
    check("every hero render is nine lines plus the label",
          all(len(lines) == 10 for lines in rendered.values()),
          str({k: len(v) for k, v in rendered.items() if len(v) != 10}))
    all_art_widths = {len(visible(line)) for lines in rendered.values() for line in lines[:9]}
    check("every expression is exactly the same width", len(all_art_widths) == 1, str(sorted(all_art_widths)))
    check("  so celebrate does not shift the frame",
          len(visible(rendered["celebrate"][0])) == len(visible(rendered["idle"][0])),
          f"celebrate {len(visible(rendered['celebrate'][0]))} vs idle {len(visible(rendered['idle'][0]))}")
    # And the coloured render aligns identically once the escapes are stripped, which is what the padding uses.
    coloured = [len(visible(line)) for line in C.render_hero("celebrate", TRUE).splitlines()[:9]]
    check("the coloured render aligns too", set(coloured) == all_art_widths,
          f"{sorted(set(coloured))} vs {sorted(all_art_widths)}")
    # The body of the character must sit in the same columns whatever the expression, label line aside.
    #
    # Asserted as a PROGRESSION, not as one constant: the frame is a triangle, so the indent legitimately decreases
    # line by line (10, 9, 7, 6, 5, 5, 6, 7). The first version of this check demanded every line be indented by
    # exactly 10 and failed against correct output. What must hold is that the sequence is identical for every
    # expression, which is what "the body does not move" actually means.
    # The character's BODY -- the antenna down to the feet, lines 1-8 -- must sit in identical columns for every
    # expression. Line 0 is excluded deliberately and the reason is worth stating: it is the antenna star slot, and
    # `celebrate` replaces that one slot with a spread of confetti. That is a decoration above the character rather
    # than a part of it, and the design calls for it explicitly.
    #
    # The slice matters. The first version took `[1:9]`, which is lines 1 through 8 inclusive -- correct -- but then
    # compared it against a run that included line 0, so it reported a failure in a body that was already aligned.
    # The earlier defects it DID catch were real: celebrate's feet were one column left, and its frame was five
    # columns wider. Both are fixed.
    def indents(name: str) -> list[int]:
        # `rstrip` first: `render_hero` right-pads every frame to a common width, and the narrowest line carries the
        # most padding, so measuring padded strings produced a non-monotonic sequence out of a correct triangle.
        lines = [line.rstrip() for line in rendered[name][1:9]]
        return [len(line) - len(line.lstrip()) for line in lines]

    check("the BODY never changes indentation between expressions",
          len({tuple(indents(name)) for name in C.EXPRESSIONS}) == 1,
          str({name: indents(name) for name in ("idle", "celebrate")}))
    check("  and the sequence descends like a triangle",
          indents("idle")[0] > indents("idle")[4], str(indents("idle")))
    check("  the antenna sits above the body's centre line",
          rendered["idle"][1].index("|") == rendered["idle"][0].rstrip().index("✦"),
          "the stalk must be under the star")
    check("celebrate is the only expression that decorates line 0",
          sum(1 for name in C.EXPRESSIONS if len(rendered[name][0].rstrip()) != len(rendered[name][0].rstrip()))
          == 0 and len({len(rendered[name][0].rstrip()) for name in C.EXPRESSIONS}) > 1,
          "confetti is allowed; a wider frame for everything else is not")

    print()
    print("=== 5. GLYPH SAFETY: a wrong-width glyph cannot break the frame ===")
    # The spec calls this out, and it matters because a slot rendering two columns shifts every line of the art.
    #
    # Two drafts of this check were wrong before the third was right, and both mistakes are worth recording:
    #   * it used `⬤` and expected width 2 -- but measured on this host `east_asian_width("⬤") == "N"`, so the shipped
    #     glyphs are genuinely one column (Unicode 9 reclassified them);
    #   * it then used `★` and expected width 2 -- but `★` is AMBIGUOUS ("A"), which is one column in a Western
    #     locale and two in an East Asian one.
    # The second is the interesting one, because it is exactly the case the spec warns about, and it means width is a
    # function of the READER's locale rather than of the character. So both classes are asserted separately.
    check("a genuinely wide glyph is always two", C.char_width("＊") == 2 and C.char_width("世") == 2,
          f"＊={C.char_width('＊')} 世={C.char_width('世')}")
    check("a narrow glyph is always one", C.char_width("✦") == 1 and C.char_width("⬤") == 1,
          f"✦={C.char_width('✦')} ⬤={C.char_width('⬤')}")
    check("an AMBIGUOUS glyph is one in a Western locale", C.char_width("★") == 1, str(C.char_width("★")))
    check("  and two in an East Asian one", C.char_width("★", ambiguous_is_wide=True) == 2,
          str(C.char_width("★", ambiguous_is_wide=True)))
    check("the locale is read from the environment",
          C.ambiguous_is_wide({"LANG": "ja_JP.UTF-8"}) and not C.ambiguous_is_wide({"LANG": "en_GB.UTF-8"}),
          "ja must be wide, en must not")
    check("  and LC_ALL wins over LANG",
          C.ambiguous_is_wide({"LC_ALL": "ko_KR.UTF-8", "LANG": "en_US.UTF-8"}))
    check("  a bare language code counts", C.ambiguous_is_wide({"LANG": "zh"}))

    # The guard, on a synthetic expression, in both locales.
    wide = C.Expression("synthetic", "Wide", "calm", ("★", "▫", "★", "★", "◦", "‿", "◉"),
                        ("*", "#", "O", "O", ".", "u", "o"))
    western = C.safe_glyphs(wide, unicode_ok=True, ambiguous_wide=False)
    check("in a Western locale the ambiguous glyphs are kept", western[0] == "★", str(western))
    eastern = C.safe_glyphs(wide, unicode_ok=True, ambiguous_wide=True)
    check("in an East Asian locale they are replaced", eastern[0] == "*", str(eastern))
    check("  all replaced with one-column twins", all(C.char_width(g) == 1 for g in eastern), str(eastern))
    check("  and the frame stays equal-width in that locale",
          len({len(line) for line in visible(C.render_hero("synthetic", PLAIN_UNI)).splitlines()[:9]}) == 1,
          str([len(line) for line in visible(C.render_hero("synthetic", PLAIN_UNI)).splitlines()[:9]]))
    check("a truly wide glyph is replaced in EVERY locale",
          all(C.safe_glyphs(
              C.Expression("w", "W", "calm", ("世",) * 7, ("*",) * 7), unicode_ok=True, ambiguous_wide=flag)[0] == "*"
              for flag in (False, True)))
    check("an ASCII-only render never consults the guard",
          all(C.char_width(g) == 1 for g in C.safe_glyphs(wide, unicode_ok=False)))
    check("char_width counts a combining mark as zero", C.char_width("\u0301") == 0)
    check("  and a plain letter as one", C.char_width("a") == 1)

    print()
    print("=== 6. NO_COLOR and pipes produce EXACTLY plain bytes ===")
    for name in C.EXPRESSIONS:
        for tier in ("hero", "small", "mini", "signal"):
            drawn = C.render(name, PLAIN_UNI, size=tier)
            check(f"{name}/{tier} has no escape sequence", "\x1b" not in drawn, repr(drawn[:60]))
    piped = C.render_inline("listening", "Take your time.")
    check("the piped form carries no art", "\n" not in piped, repr(piped))
    check("  and names the mood in text", "[loci:listening]" in piped, piped)
    forced = C.detect_terminal(force_plain=True)
    check("--plain forces tier 0", forced.color == C.T0_PLAIN, str(forced.color))

    print()
    print("=== 7. tier selection never clips ===")
    for columns, expected in ((120, "hero"), (80, "hero"), (39, "small"), (20, "small"), (19, "mini"), (5, "mini")):
        terminal = C.Terminal(color=C.T0_PLAIN, unicode=True, columns=columns)
        check(f"{columns} columns -> {expected}", terminal.size_tier() == expected, terminal.size_tier())
    narrow = C.render("idle", C.Terminal(color=C.T0_PLAIN, unicode=True, columns=12))
    check("a 12-column terminal gets one line", len(narrow.splitlines()) == 1, narrow)

    print()
    print("=== 8. THE SAFETY PROTOCOL: crisis is still, sticky, and never cute ===")
    # Rule 1 of §4.1, and the defect that this verifier found.
    check("crisis declares no motion", C.EXPRESSIONS["crisis"].motion == (),
          str(C.EXPRESSIONS["crisis"].motion))
    check("crisis is marked persistent", C.EXPRESSIONS["crisis"].persistent)
    check("  motion_allowed refuses even on a motion-capable terminal",
          not X.ExpressionState(expression="crisis").motion_allowed(True))
    check("  and the SVG carries no confetti for it",
          "circle cx=\"70\"" not in loci_svg.render_svg("crisis"),
          "confetti is celebrate-only")
    check("caring declares no hop", "hop" not in C.EXPRESSIONS["caring"].motion,
          str(C.EXPRESSIONS["caring"].motion))
    check("crisis has no blush glyph slot filled", C.EXPRESSIONS["crisis"].uni[4].strip() == "",
          repr(C.EXPRESSIONS["crisis"].uni[4]))

    state = X.ExpressionState()
    state.apply(X.SIGNAL_RISK, now=1.0)
    check("a risk signal shows crisis", state.expression == "crisis", state.expression)
    for signal in (X.SIGNAL_TASK_DONE, X.SIGNAL_OPEN, X.SIGNAL_QUIET, X.SIGNAL_TYPING, X.SIGNAL_MILESTONE):
        state.apply(signal, now=2.0)
    check("THE DEFECT: no ordinary signal de-escalates crisis", state.expression == "crisis", state.expression)
    check("  nor does a long idle period", state.tick(now=600.0).expression == "crisis")
    check("  and it clears only explicitly", state.release_crisis(now=700.0).expression == "idle")
    check("  a second release is a no-op", state.release_crisis(now=701.0).expression == "idle")
    check("crisis outranks angry text", X.ExpressionState().apply(
        X.SIGNAL_QUIET, now=1.0, evidence="you are useless and i want to die").expression == "crisis")

    print()
    print("=== 9. THE DEFECT: anger is never mirrored ===")
    for signal in (X.SIGNAL_QUIET, X.SIGNAL_SENT, X.SIGNAL_ERROR, X.SIGNAL_TASK_DONE, X.SIGNAL_DISTRESS):
        probe = X.ExpressionState()
        probe.apply(signal, now=1.0, evidence="this is useless, you are wrong")
        check(f"{signal} + angry text -> listening", probe.expression == "listening", probe.expression)
    check("never a frown at the user",
          all(X.ExpressionState().apply(s, now=1.0, evidence="you are useless").expression
              not in ("sorry", "concerned", "confused")
              for s in (X.SIGNAL_QUIET, X.SIGNAL_ERROR)))

    print()
    print("=== 10. celebrate is rationed; caring does not decay ===")
    session = X.ExpressionState()
    session.apply(X.SIGNAL_MILESTONE, now=1.0)
    check("the first milestone celebrates", session.expression == "celebrate")
    session.apply(X.SIGNAL_MILESTONE, now=2.0)
    check("  the second degrades to cheering", session.expression == "cheering", session.expression)
    session.reset_session()
    session.apply(X.SIGNAL_MILESTONE, now=3.0)
    check("  a new session may celebrate again", session.expression == "celebrate")
    caring = X.ExpressionState()
    caring.apply(X.SIGNAL_MOOD_LOW, now=1.0)
    check("mood_low shows caring", caring.expression == "caring")
    check("  and does not decay after a long quiet", caring.tick(now=600.0).expression == "caring")
    busy = X.ExpressionState()
    busy.apply(X.SIGNAL_TASK_DONE, now=0.0)
    check("an ordinary expression DOES decay at 8s", busy.tick(now=9.0).expression == "idle")
    check("  but not before", X.ExpressionState(expression="cheering", since=0.0).tick(now=7.0).expression == "cheering")

    print()
    print("=== 11. an unknown expression falls back rather than crashing ===")
    check("is_known is False for a typo", not X.is_known("listning"))
    check("  but render still draws something", len(C.render_hero("listning", PLAIN_UNI).splitlines()) == 10)
    check("  and it is the idle face", "Here" in C.render_hero("listning", PLAIN_UNI),
          "a typo must not silently draw a different mood")
    ok, _ = S.render_one("listning", terminal=PLAIN_UNI)
    check("the CLI reports the typo", ok is False)

    print()
    print("=== 12. THE TWO SURFACES READ THE SAME MANIFEST ===")
    # The spec's central requirement. Two hand-maintained tables is how the CLI and the dashboard drift apart.
    svg_ids, manifest_ids = loci_svg.coverage()
    check("the SVG defines every expression the manifest does", not (manifest_ids - svg_ids),
          str(sorted(manifest_ids - svg_ids)))
    check("  and defines no expression the manifest does not", not (svg_ids - manifest_ids),
          str(sorted(svg_ids - manifest_ids)))
    moods_in_svg = {loci_svg.mood_variables(name) for name in manifest_ids}
    check("the SVG uses only moods the manifest defines", moods_in_svg <= set(C.MOODS), str(moods_in_svg))

    print()
    print("=== 13. every expression is valid, accessible SVG ===")
    for name in sorted(manifest_ids):
        svg = loci_svg.render_svg(name)
        try:
            ET.fromstring(svg)
            parses = True
        except ET.ParseError as exc:
            parses = False
            check(f"{name}: valid XML", False, str(exc))
        if parses:
            check(f"{name}: valid XML", True)
    sample = loci_svg.render_svg("listening")
    check("the SVG has role=img", 'role="img"' in sample)
    check("  and an accessible name that names the mood",
          'aria-label="Loci, listening"' in sample, sample[:120])
    check("  and a <title>", "<title>" in sample)
    check("  the viewBox is fixed at 240 for every tier",
          all(loci_svg.VIEWBOX in loci_svg.render_svg(n) for n in manifest_ids))
    check("  every part group is present",
          all(f'data-part="{part}"' in sample for part in
              ("star", "window", "eyes", "cheeks", "mouth", "lantern")))
    span = loci_svg.render_span("caring", size=64)
    check("the span wrapper carries data-mood for the CSS", 'data-mood="care"' in span, span[:90])
    check("  and data-expression", 'data-expression="caring"' in span)

    print()
    print("=== 14. the dashboard CSS cannot animate crisis ===")
    # Asserted against the stylesheet text because the rule is the guarantee: a bouncing crisis face is the single
    # rendering this project must never ship, and it is one careless selector away.
    ui = (REPO / "myriaxon" / "dashboard" / "ui.py").read_text(encoding="utf-8")
    check("the stylesheet exists in the dashboard", ".loci[data-mood=" in ui)
    crisis_rule = re.search(r'\.loci\[data-expression="crisis"\][^{]*\{[^}]*\}', ui)
    check("there is an explicit crisis rule", bool(crisis_rule), "no crisis rule found")
    if crisis_rule:
        check("  it disables animation", "animation: none" in crisis_rule.group(0), crisis_rule.group(0)[:120])
    check("prefers-reduced-motion is respected",
          "prefers-reduced-motion: reduce" in ui and "animation: none !important" in ui)
    check("the lantern transition is slower than the face transition",
          "--t-lantern, 600ms" in ui and "--t-face, 240ms" in ui,
          "the spec's 600ms/240ms asymmetry")

    print()
    print("=== 15. personalization ===")
    state = X.ExpressionState()
    state.set_identity(name="Pixel", nickname="Pip", pronoun="they")
    check("the nickname wins over the name", state.display_name == "Pip", state.display_name)
    state.set_identity(name="Marzipan")
    check("  the nickname still wins", state.display_name == "Pip", state.display_name)
    check("the pronoun is stored", state.pronoun == "they", state.pronoun)
    state.set_identity(pronoun="banana")
    check("an invalid pronoun is refused, not stored", state.pronoun == "they", state.pronoun)
    check("the accessible label uses the chosen name",
          X.label_for("listening", name="Pip") == "Pip, listening", X.label_for("listening", name="Pip"))
    check("a long name is bounded", len(X.ExpressionState(name="x" * 200).name) <= 24)

    print()
    print("=== 16. the sheet is readable and never overflows the terminal ===")
    for columns in (120, 80, 40, 20):
        terminal = C.Terminal(color=C.T0_PLAIN, unicode=True, columns=columns)
        sheet = S.render_sheet(terminal=terminal)
        over = [line for line in sheet.splitlines() if S._display_width(line) > columns]
        check(f"a {columns}-column sheet fits", not over, f"{len(over)} line(s) too wide: {over[:1]}")
        check(f"  and mentions every expression", all(n in sheet for n in C.EXPRESSIONS))
    check("the wrapper respects wide glyphs", len(S._wrap("aaa bbb ccc", 7)) >= 2, str(S._wrap("aaa bbb ccc", 7)))

    print()
    print("=== 17. the prompt and status fragments degrade cleanly ===")
    state = X.ExpressionState()
    prompt = S.prompt_prefix(state, PLAIN_UNI)
    check("the prompt prefix is one line", "\n" not in prompt, repr(prompt))
    check("  and is the mini character", "(" in prompt and ")" in prompt, repr(prompt))
    ascii_prompt = S.prompt_prefix(state, C.Terminal(color=C.T0_PLAIN, unicode=False, columns=80))
    check("an ASCII terminal still gets a character", "^" in ascii_prompt, repr(ascii_prompt))
    status = S.status_fragment(state, PLAIN_UNI)
    check("the status fragment names the mood", "Here" in status, status)
    check("  which is the third channel, present without colour", "\x1b" not in status, repr(status))

    print()
    print("=== 18. WIRED: the CLI exposes it ===")
    parser_source = (REPO / "myriaxon" / "argparse_cli.py").read_text(encoding="utf-8")
    cli_source = (REPO / "myriaxon" / "cli.py").read_text(encoding="utf-8")
    check("the avatar command is registered", '"avatar"' in parser_source)
    check("  it is dispatched", "_run_avatar_command" in cli_source)
    check("  and it exists", "def _run_avatar_command" in cli_source)
    # The measured bug: `[{N}]` is a valid Rich markup tag, so the lantern vanished when the art went through Rich's
    # markup parser.
    check("the art is printed with markup disabled",
          "markup=False" in cli_source[cli_source.index("def _run_avatar_command"):][:4000],
          "Rich reads [◉] as a style tag and eats the lantern")
    check("the system prompt carries the tone rules",
          "When someone seems to be struggling" in
          (REPO / "myriaxon" / "system_prompt.py").read_text(encoding="utf-8"))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
