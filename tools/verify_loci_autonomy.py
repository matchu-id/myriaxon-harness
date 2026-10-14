#!/usr/bin/env python3
"""Verify Loci's extended expressions and its autonomy.

## The two properties that took the most work, and why they are asserted here

**Tick-size invariance.** Autonomy whose behaviour depends on how often the host polls it is not autonomous, it is a
function of the host's loop. It took four rewrites to get right, each wrong differently, and every wrong version
looked plausible:

  1. threshold-after-addition -- every poll fired, 288 changes in 24 hours;
  2. plus a cooldown -- no effect, because it was compared against a timestamp that crept while the clock jumped;
  3. solve for the crossing time, one per poll -- right arithmetic, but a coarse tick skipped everything between its
     endpoints (594 at a 1s tick against 0 at a 300s tick);
  4. loop over crossings inside one poll -- still tick-dependent, because a firing could be returned-but-dropped, and
     `poll` returned only the last one.

The fix was to stop letting the caller's cadence into the simulation: `poll` sub-steps on a fixed grid, and every
firing goes into a buffer that `run_for` drains. The assertion below is the property itself -- identical sequences
from a 1-second tick to a 3600-second one -- which is worth more than any of the four intermediate tests.

**No fake randomness.** A seeded jitter was added so the character would feel less mechanical, and it changed
nothing: four seeds produced byte-identical sequences and identical pressures, because the delay was applied to a
timestamp that the grid-determined crossing then cancelled. The parameter is gone. A test that asserts "the same seed
gives the same result" would have passed on that code and proved nothing; this file asserts the opposite -- that
there IS no seed, so nobody can add back a knob that does nothing.
"""

from __future__ import annotations

import io
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import loci_autonomy as A  # noqa: E402
from myriaxon import loci_character as C  # noqa: E402
from myriaxon import loci_expressions_more as M  # noqa: E402
from myriaxon import loci_svg  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""), flush=True)


def session(*, hour: float = 9.0, personality=None, step: float = 20.0, seconds: float = 86400.0):
    """A character driven from a fixed clock. No `rng`: there is none to pass."""
    clock = [1_700_000_000.0]
    engine = A.Autonomy(clock=lambda: clock[0])
    engine.hour = hour
    if personality is not None:
        engine.personality = personality
    return engine, engine.run_for(seconds, step=step)


def main() -> int:
    merged = M.all_expressions(C.EXPRESSIONS)

    print("=== 1. the extended set is valid and additive ===")
    # The spec's fourteen are asserted BY NAME rather than by count.
    #
    # The count was pinned at fourteen before the manifest gained \happy\ -- which verify_loci_e2e and the dashboard
    # both require, so it is part of the live contract. Asserting the NAMES is the stronger check and the one that
    # was always meant: a total cannot tell a well-formed new face from a malformed one, and it breaks the moment a
    # legitimate face is added while saying nothing when one is CORRUPTED.
    spec_missing = [n for n in C.SPEC_NAMES if n not in C.EXPRESSIONS]
    check("the spec set is untouched at 14", not spec_missing, str(spec_missing))
    check("  and nothing was removed or renamed",
          all(C.EXPRESSIONS[n].id == n for n in C.SPEC_NAMES if n in C.EXPRESSIONS))
    check("the extension adds faces", len(M.EXTRA_EXPRESSIONS) >= 15, str(len(M.EXTRA_EXPRESSIONS)))
    # The identity is "published plus extensions, with nothing lost and nothing counted twice".
    #
    # This used to read `14 + len(...)`, which broke as soon as the manifest gained faces the spec does not
    # define -- `happy` is required by verify_loci_e2e and the dashboard, and `listening_back` is an internal
    # variant. Fourteen was never the property; the property is that the merge is exactly the union of the two
    # sets, however large either side is. Stated this way it cannot drift.
    check("the merged set is spec + extras",
          len(merged) == len(C.EXPRESSIONS) + len(M.EXTRA_EXPRESSIONS),
          f"merged={len(merged)} published={len(C.EXPRESSIONS)} extras={len(M.EXTRA_EXPRESSIONS)}")
    check("  and no name collides",
          not (set(M.EXTRA_EXPRESSIONS) & set(C.EXPRESSIONS)),
          str(sorted(set(M.EXTRA_EXPRESSIONS) & set(C.EXPRESSIONS))))
    check("every face has seven slots",
          all(len(e.uni) == 7 and len(e.ascii_glyphs) == 7 for e in merged.values()),
          str([k for k, e in merged.items() if len(e.uni) != 7 or len(e.ascii_glyphs) != 7]))
    check("every ASCII twin is really ASCII",
          all(all(ord(ch) < 128 for ch in e.ascii_glyphs) for e in merged.values()),
          str([k for k, e in merged.items() if any(ord(c) >= 128 for c in e.ascii_glyphs)]))
    check("every face names a defined mood",
          all(e.mood in C.MOODS for e in merged.values()),
          str([k for k, e in merged.items() if e.mood not in C.MOODS]))
    check("every face has a label", all(e.label for e in merged.values()))
    check("ids match their keys", all(k == e.id for k, e in merged.items()))
    check("the merge is non-destructive on a clash",
          M.all_expressions({"curious": C.EXPRESSIONS["idle"]})["curious"] is C.EXPRESSIONS["idle"],
          "the published spec must win over an extension of the same name")

    print()
    print("=== 2. every extra face renders, at every tier, in both glyph sets ===")
    for name in sorted(M.EXTRA_EXPRESSIONS):
        for terminal in (C.Terminal(color=C.T0_PLAIN, unicode=True, columns=100),
                         C.Terminal(color=C.T0_PLAIN, unicode=False, columns=100)):
            for tier in ("hero", "small", "mini", "signal"):
                drawn = C.render(name, terminal, size=tier)
                if not drawn:
                    check(f"{name}/{tier} renders", False, "empty")
    check("all extra faces render at every tier and glyph set", True, f"{len(M.EXTRA_EXPRESSIONS)} faces x 4 tiers")
    # The frame must not shift for any of them either.
    plain = C.Terminal(color=C.T0_PLAIN, unicode=True, columns=100)
    widths = {n: {len(line) for line in C.render_hero(n, plain).splitlines()[:9]} for n in merged}
    check("every face renders at one width", len({w for s in widths.values() for w in s}) == 1,
          str(sorted({w for s in widths.values() for w in s})))

    print()
    print("=== 3. every extra face has SVG, and it is valid ===")
    svg_ids = loci_svg.coverage()[0]
    missing = sorted(set(M.EXTRA_EXPRESSIONS) - svg_ids)
    # Extras are drawn by the spec's part table when they are close enough; only a genuinely new face needs new parts.
    # So the assertion is that every extra has SOME definition, falling back explicitly rather than silently.
    for name in sorted(missing):
        check(f"{name} falls back to a spec face for SVG", M.nearest_spec(name) in svg_ids,
              f"nearest = {M.nearest_spec(name)!r}")
    for name in sorted(set(M.EXTRA_EXPRESSIONS) & svg_ids):
        try:
            ET.fromstring(loci_svg.render_svg(name))
        except ET.ParseError as exc:
            check(f"{name} SVG is valid", False, str(exc))
    check("every extra face either has SVG or an explicit fallback", True,
          f"{len(set(M.EXTRA_EXPRESSIONS) & svg_ids)} with parts, {len(missing)} with a fallback")
    check("every fallback target exists in the spec",
          all(M.nearest_spec(k) in C.EXPRESSIONS for k in M.EXTRA_EXPRESSIONS))

    print()
    print("=== 4. THE PROPERTY: the sequence does not depend on the tick size ===")
    _, reference = session(step=20.0)
    reference_sequence = [c.expression for c in reference]
    check("a simulated day produces autonomous changes", len(reference) > 100, f"{len(reference)} changes")
    check("  and uses more than one expression", len(set(reference_sequence)) > 2,
          str(sorted(set(reference_sequence))))
    for step in (1.0, 5.0, 20.0, 60.0, 300.0, 3600.0):
        _, run = session(step=step)
        check(f"a {step:g}s tick gives the identical sequence",
              [c.expression for c in run] == reference_sequence,
              f"{len(run)} changes vs {len(reference)}")

    print()
    print("=== 5. there is no randomness, and no knob that pretends otherwise ===")
    # Asserted negatively, because the earlier code had a seeded `rng` that changed nothing. A test saying "the same
    # seed gives the same result" passed on it and proved nothing at all.
    engine = A.Autonomy()
    check("the engine has no rng parameter", not hasattr(engine, "rng"),
          "a knob that does nothing is worse than no knob")
    check("  and no jitter setting", not hasattr(engine, "jitter_seconds"))
    _, again = session(step=20.0)
    check("two runs of the same day are identical", [c.expression for c in again] == reference_sequence)

    print()
    print("=== 6. personality changes the character ===")
    # Compared over a SHORT window. A whole day saturates every motivation, so every personality ends up firing the
    # same set and the comparison passes trivially -- which is how the first version of this check proved nothing.
    short = {}
    for name in ("steady", "calm", "curious", "warm", "lively", "diligent"):
        _, run = session(personality=A.personality_for(name), seconds=1800.0)
        short[name] = [c.expression for c in run]
        check(f"{name} produces changes", bool(run), f"{len(run)}")
    check("personalities do NOT all behave identically", len({tuple(v) for v in short.values()}) > 1,
          str({k: v[:5] for k, v in short.items()}))
    check("an unknown preset falls back rather than raising", isinstance(A.personality_for("nope"), A.Personality))
    check("a preset is copied, not shared",
          A.personality_for("warm") is not A.personality_for("warm"),
          "a caller mutating one must not corrupt the preset")
    check("weights are clamped so no trait is absolute",
          A.Personality(social=-5).weight("social") >= 0.25 and A.Personality(social=99).weight("social") <= 4.0)

    print()
    print("=== 7. the circadian rhythm actually does something ===")
    night_rest = 0
    day_rest = 0
    for hour, target in ((23.0, "night"), (9.0, "day")):
        _, run = session(hour=hour)
        count = sum(1 for c in run if c.motivation == "rest")
        if target == "night":
            night_rest = count
        else:
            day_rest = count
    check("late evening rests more than mid-morning", night_rest > day_rest, f"23h={night_rest} 9h={day_rest}")
    check("  and daytime still rests sometimes", day_rest > 0, str(day_rest))
    check("the hour can be forced, so 3am is testable", A.Autonomy(hour=3.0)._circadian() > 0,
          str(A.Autonomy(hour=3.0)._circadian()))
    check("  and midday is quiet", A.Autonomy(hour=12.0)._circadian() == 0.0,
          str(A.Autonomy(hour=12.0)._circadian()))

    print()
    print("=== 8. THE GUARD RAILS ===")
    _, run = session()
    check("autonomy NEVER chooses crisis", not any(c.expression == "crisis" for c in run),
          "a character that drifts into the safety face would frighten someone who is fine")
    check("  nor celebrate", not any(c.expression == "celebrate" for c in run),
          "celebrate is rationed to a real milestone; a timer is not one")
    check("  nor any expression outside the merged set",
          all(c.expression in merged for c in run))
    check("every behaviour is a legal expression",
          all(v in merged for v in A.BEHAVIOURS.values()), str(A.BEHAVIOURS))
    check("  and none is forbidden", not (set(A.BEHAVIOURS.values()) & A.NEVER_AUTONOMOUS))

    held = A.Autonomy()
    held.hold()
    check("a hold stops every autonomous change", not held.poll(now=1e12).fired, held.poll(now=1e12).reason)
    held.hold()
    held.release()
    check("  one release does not clear two holds", not held.poll(now=1e12).fired)
    held.release()
    check("  the second release clears it", isinstance(held.poll(now=1e12), A.AutonomousChoice))
    off = A.Autonomy()
    off.enabled = False
    check("disabled stops every autonomous change", not off.poll(now=1e12).fired)

    print()
    print("=== 9. it never raises, and never runs away ===")
    for label, engine in (("the default engine", A.Autonomy()),
                          ("a held engine", A.Autonomy(holds=1)),
                          ("a disabled engine", A.Autonomy(enabled=False)),
                          ("a saturated engine", A.Autonomy())):
        if label.startswith("a saturated"):
            for motivation in engine.motivations.values():
                motivation.pressure = 99.0
        try:
            result = engine.poll(now=1e12)
            check(f"survives {label}", isinstance(result, A.AutonomousChoice))
        except Exception as exc:  # noqa: BLE001
            check(f"survives {label}", False, f"{type(exc).__name__}: {exc}")
    # A week of nothing must not become a week of simulated feelings.
    engine = A.Autonomy()
    engine.last_poll = 1_700_000_000.0
    engine.poll(now=1_700_000_000.0 + 7 * 86400)
    check("a week's gap is not replayed", engine.last_poll > 1_700_000_000.0 + 6 * 86400,
          "it resumes from the present, which is what a person expects")
    check("the change log is bounded", len(A.Autonomy().log) <= 64)

    print()
    print("=== 10. `drain` is how a headless run collects everything ===")
    engine, _ = session(seconds=600.0)
    drained = engine.drain()
    check("drain returns a list", isinstance(drained, list))
    check("  and empties the buffer", engine.drain() == [])
    # The bug this exists for: `poll` returns only the LAST firing, so a coarse step used to look quieter than a fine
    # one. Driven just under the catch-up ceiling, so what is being tested is the buffer rather than the ceiling.
    #
    # The first version drained AFTER `run_for`, which had already drained -- so it measured zero and looked like a
    # broken engine. `run_for` is the collecting API; `drain` is for a host that polls directly.
    engine = A.Autonomy()
    coarse = engine.run_for(engine.max_catch_up_seconds * 0.5,
                            step=engine.max_catch_up_seconds * 0.5)
    check("a single coarse poll still reports every change it contained", len(coarse) > 3,
          f"{len(coarse)} from one {engine.max_catch_up_seconds * 0.5:.0f}s window")
    # And a host calling `poll` directly gets the buffer instead.
    direct = A.Autonomy()
    direct.poll(now=direct.last_poll + direct.max_catch_up_seconds * 0.5)
    check("  a direct poll leaves the same events on the buffer", len(direct.drain()) == len(coarse),
          f"{len(direct.drain())} vs {len(coarse)}")
    # And the ceiling itself, asserted separately so the two behaviours cannot be confused.
    engine = A.Autonomy()
    engine.poll(now=engine.last_poll + engine.max_catch_up_seconds * 4)
    check("  a window past the ceiling is not replayed in full",
          len(engine.drain()) <= int(engine.max_catch_up_seconds / engine.granularity) + 1,
          "a character that was not running did not have those feelings")

    print()
    print("=== 11. status is inspectable ===")
    engine, _ = session(seconds=600.0)
    status = engine.status()
    for key in ("enabled", "held", "personality", "hour", "circadian", "pressures", "excess", "fired"):
        check(f"status reports {key}", key in status, str(sorted(status)))
    check("  the pressures are numbers", all(isinstance(v, (int, float)) for v in status["pressures"].values()))
    check("a choice explains itself", bool(A.AutonomousChoice(expression="x", motivation="social",
                                                             reason="because").render()))

    print()
    print("=== 12. the CLI can show it ===")
    surface = (REPO / "myriaxon" / "loci_surface.py").read_text(encoding="utf-8")
    check("the sheet knows about the extended faces",
          "all_expressions" in surface or "SHEET_ORDER" in surface,
          "a face nobody can list is a face nobody can use")

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
