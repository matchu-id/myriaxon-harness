"""Brand consistency: the design tokens and the surfaces that render them must agree.

The palette used to live in four unrelated places -- the Rich theme, the banner's colour
cycle, the prompt_toolkit REPL style, and the dashboard's CSS -- each holding its own copy of
the same hex strings. A brand change had to be applied four times, and missing one produced a
UI that was quietly two brands at once. That is not a hypothetical: the dashboard referenced a
`--mono` variable that was never defined, so the file and diff panels fell back to the
proportional font and nobody noticed, because an undefined CSS variable is not an error.

These tests are the guard. They fail if the dashboard's declared palette drifts from
``myriaxon.brand``, if any rule references a variable the page never defines, or if the
identity loses the properties that make it recognisable.

They are deliberately offline and instant: no server, no browser, no network.
"""

from __future__ import annotations

import re
import unittest

from myriaxon import brand
from myriaxon.dashboard.ui import INDEX_HTML

# The two blocks that declare the palette, and the aliases that bridge the pre-rebrand names.
_TOKEN_BLOCK = re.compile(
    r':root,\s*html\[data-theme="dark"\]\s*\{(?P<dark>.*?)\}\s*'
    r'html\[data-theme="light"\]\s*\{(?P<light>.*?)\}',
    re.DOTALL,
)


def _declared(block: str) -> dict[str, str]:
    """Every ``--name: value`` pair in a CSS block, including several-per-line declarations."""
    return {
        name: value.strip()
        for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block)
    }


class TestDashboardPaletteMatchesBrand(unittest.TestCase):
    """The dashboard is a string with no build step, so the tokens are mirrored by hand.
    This is what stops the mirror from going stale."""

    @classmethod
    def setUpClass(cls) -> None:
        match = _TOKEN_BLOCK.search(INDEX_HTML)
        assert match is not None, "the dark/light token blocks were not found in INDEX_HTML"
        cls.dark = _declared(match.group("dark"))
        cls.light = _declared(match.group("light"))
        cls.all_declared = _declared(INDEX_HTML)

    def test_signal_colour_matches_the_brand(self) -> None:
        self.assertEqual(self.dark["--signal"].lower(), brand.SIGNAL.lower())

    def test_light_signal_is_the_readable_variant(self) -> None:
        """Signal green on white fails contrast, so the light theme must not reuse it."""
        self.assertEqual(self.light["--signal"].lower(), brand.SIGNAL_LIGHT.lower())
        self.assertNotEqual(self.light["--signal"].lower(), brand.SIGNAL.lower())

    def test_ink_steps_match_the_brand(self) -> None:
        self.assertEqual(self.dark["--ink"].lower(), brand.INK.lower())
        self.assertEqual(self.dark["--ink-2"].lower(), brand.INK_2.lower())
        self.assertEqual(self.dark["--ink-3"].lower(), brand.INK_3.lower())
        self.assertEqual(self.light["--ink"].lower(), brand.INK_LIGHT.lower())

    def test_lines_and_text_match_the_brand(self) -> None:
        self.assertEqual(self.dark["--line"].lower(), brand.LINE.lower())
        self.assertEqual(self.dark["--line-2"].lower(), brand.LINE_2.lower())
        self.assertEqual(self.dark["--text"].lower(), brand.TEXT.lower())
        self.assertEqual(self.dark["--text-2"].lower(), brand.TEXT_2.lower())
        self.assertEqual(self.dark["--text-3"].lower(), brand.TEXT_3.lower())

    def test_supporting_hues_match_the_brand(self) -> None:
        for name, value in (
            ("--blue", brand.BLUE),
            ("--amber", brand.AMBER),
            ("--violet", brand.VIOLET),
            ("--red", brand.RED),
        ):
            with self.subTest(token=name):
                self.assertEqual(self.dark[name].lower(), value.lower())

    def test_type_stacks_match_the_brand(self) -> None:
        for token, stack in (
            ("--display", brand.FONT_DISPLAY),
            ("--ui", brand.FONT_UI),
            ("--mono", brand.FONT_MONO),
        ):
            with self.subTest(token=token):
                self.assertEqual(self.all_declared[token], stack)

    def test_no_fetched_webfonts(self) -> None:
        """The page must work air-gapped. A font CDN would also leak that the tool is running."""
        self.assertNotIn("fonts.googleapis", INDEX_HTML)
        self.assertNotIn("fonts.gstatic", INDEX_HTML)
        self.assertNotIn("@import", INDEX_HTML)


class TestEveryCssVariableResolves(unittest.TestCase):
    """An undefined CSS variable is not an error -- it resolves to nothing and the element
    silently loses its styling. That is how the previous design shipped code panels in the
    wrong font. This asserts it cannot happen again."""

    def test_no_rule_references_an_undefined_variable(self) -> None:
        style = INDEX_HTML.split("<style>", 1)[1].split("</style>", 1)[0]
        used = set(re.findall(r"var\((--[a-z0-9-]+)", style))
        declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", style))
        undefined = sorted(used - declared)
        self.assertEqual(undefined, [], f"CSS variables used but never declared: {undefined}")

    def test_the_whole_page_resolves_including_inline_styles(self) -> None:
        used = set(re.findall(r"var\((--[a-z0-9-]+)", INDEX_HTML))
        declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", INDEX_HTML))
        self.assertEqual(sorted(used - declared), [])

    def test_legacy_aliases_are_still_emitted(self) -> None:
        """The markup and script reference the pre-rebrand names in hundreds of places; the
        aliases are what let the palette change without rewriting every rule."""
        for token in ("--bg", "--panel", "--border", "--muted", "--accent", "--ok", "--warn", "--err"):
            with self.subTest(token=token):
                self.assertIn(f"{token}:", INDEX_HTML)


class TestDashboardMark(unittest.TestCase):
    """The mark's structural invariants, and proof the old logogram is gone."""

    def test_mark_is_present_and_labelled(self) -> None:
        self.assertIn('class="mark"', INDEX_HTML)
        self.assertIn('viewBox="0 0 200 200"', INDEX_HTML)
        self.assertIn('role="img"', INDEX_HTML)
        self.assertIn('aria-label="Myriaxon"', INDEX_HTML)

    def test_mark_has_eight_rays(self) -> None:
        """Four cardinal diamonds and four diagonal needles: the myriad paths."""
        self.assertGreaterEqual(INDEX_HTML.count("<polygon"), 8)

    def test_the_logo_is_declared_once_and_cloned_everywhere(self) -> None:
        """One hand-written drawing, cloned into each placement.

        Asserted as "one declaration, N renders" rather than "exactly eight polygons in the file":
        Mission control draws a second, smaller mark as the centre of its connection diagram, so a
        fixed file-wide count would break every time a surface wants the mark -- and the real
        invariant, that the artwork is declared once, would go untested.
        """
        self.assertIn('id="marktemplate"', INDEX_HTML)
        self.assertEqual(INDEX_HTML.count('id="marktemplate"'), 1)
        # Each declaration carries exactly the eight rays, so any drawing is a whole mark.
        for block in INDEX_HTML.split("<svg")[1:]:
            if "polygon" in block.split("</svg>")[0]:
                polygons = block.split("</svg>")[0].count("<polygon")
                self.assertEqual(polygons % 8, 0, "a mark is drawn with a partial set of rays")
        self.assertGreaterEqual(INDEX_HTML.count('class="markslot"'), 2)
        self.assertEqual(INDEX_HTML.count('class="mark"'), 1, "only the template declares the mark")

    def test_mark_survives_both_themes_from_one_drawing(self) -> None:
        self.assertIn('fill="currentColor"', INDEX_HTML)
        self.assertNotIn("mark-light", INDEX_HTML)
        self.assertNotIn("mark-dark", INDEX_HTML)

    def test_old_logogram_colours_are_gone(self) -> None:
        for stale in ("#e8232a", "#ffc400", "#ec0f8c", "#17b3ab"):
            with self.subTest(colour=stale):
                self.assertNotIn(stale, INDEX_HTML, f"the previous logo's {stale} survives")


class TestIdentityProperties(unittest.TestCase):
    """The properties that make the identity recognisable, asserted where they are defined."""

    def test_dashboard_title(self) -> None:
        self.assertIn("<title>Myriaxon</title>", INDEX_HTML)

    def test_favicon_is_inline_so_it_cannot_404(self) -> None:
        self.assertIn('rel="icon"', INDEX_HTML)
        self.assertIn("data:image/svg+xml", INDEX_HTML)

    def test_theme_storage_key_is_branded(self) -> None:
        self.assertIn("myriaxon-theme", INDEX_HTML)
        self.assertNotIn('"ioh-theme"', INDEX_HTML)

    def test_terminal_mark_is_two_braille_rows(self) -> None:
        self.assertEqual(len(brand.MARK_GLYPH_LINES), 2)
        for line in brand.MARK_GLYPH_LINES:
            # Braille block plus the hair space used to keep cells from fusing.
            self.assertTrue(all(ch == "\u200a" or "\u2800" <= ch <= "\u28ff" for ch in line))

    def test_wordmark_renders_every_letter_of_the_name(self) -> None:
        rows = brand.wordmark_rows("MYRIAXON")
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row.strip() for row in rows), "a wordmark row is blank")
        for letter in "MYRIAXON":
            self.assertIn(letter, brand._WORDMARK_GLYPHS)

    def test_both_themes_define_every_palette_role(self) -> None:
        """A missing role in one theme means an element silently renders unstyled there."""
        light_block = re.search(r'html\[data-theme="light"\]\s*\{(.*?)\}', INDEX_HTML, re.DOTALL)
        assert light_block is not None
        light = _declared(light_block.group(1))
        for role in ("--ink", "--ink-2", "--ink-3", "--line", "--text", "--signal", "--signal-ink"):
            with self.subTest(role=role):
                self.assertIn(role, light, f"{role} is missing from the light theme")


if __name__ == "__main__":
    unittest.main(verbosity=2)
