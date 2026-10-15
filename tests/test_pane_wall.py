"""The pane wall: several loci side by side, and the API that feeds it.

The wall is a VIEW, so most of it is JavaScript and cannot be tested here. What can be tested is the contract
underneath it, and that contract has one property worth pinning:

    ONE REQUEST CARRIES EVERY VISIBLE TRANSCRIPT

Nine panes polling independently would be nine requests on a timer, each re-reading `loci.json` and rebuilding
the whole roster -- so the cost of watching three agents work together would scale with how closely you
watched. The server accepts `?transcripts=a,b,c` for exactly that reason, and these tests keep it from quietly
becoming a loop of single-transcript calls.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


class TestTheWallIsReachable:
    """A room nobody can open is not a feature. These assert the wiring, not the pixels."""

    @pytest.fixture(scope="class")
    @classmethod
    def markup(cls) -> str:
        from myriaxon.dashboard import ui

        return ui.INDEX_HTML

    def test_the_nav_offers_the_wall(self, markup):
        assert 'data-ui="wall"' in markup

    def test_the_rail_maps_it_to_a_dialog(self, markup):
        """`RAIL_DIALOGS` is the map the rail clicks through. An unmapped entry silently does nothing."""
        assert "wall: \"wallbtn\"" in markup or "wall: 'wallbtn'" in markup

    def test_the_dialog_and_grid_exist(self, markup):
        for needle in ('id="wallmodal"', 'id="wallgrid"', 'id="wallroster"'):
            assert needle in markup, f"{needle} is missing, so the wall cannot render"

    def test_three_columns_are_offered(self, markup):
        """The 3x3 requirement is a control, not just a CSS rule."""
        for cols in ("1", "2", "3"):
            assert f'data-wall-cols="{cols}"' in markup

    def test_every_referenced_id_is_declared(self, markup):
        """`$("id")` on an id that does not exist is a silent no-op in a browser, and a broken control in use.

        This is not hypothetical: the first version of the wall wired a Clear button whose markup had a
        different id, and the button would have done nothing at all with no error anywhere.
        """
        import re

        declared = set(re.findall(r'id="([^"]+)"', markup))
        referenced = set(re.findall(r'\$\("([^"]+)"\)', markup))
        # Ids the page creates while running, which a regex over the source cannot see. Listed explicitly
        # rather than pattern-matched, so the exemption is a decision with a reason instead of a wildcard that
        # would hide the next genuine mistake:
        #   * `buffering-msg` is set via `wrap.id = "buffering-msg"` before being appended, then looked up.
        #   * `wallbadge` is a rail badge id, owned by the badge helper rather than the wall markup.
        runtime_ids = {"buffering-msg", "wallbadge"}
        dangling = sorted(referenced - declared - runtime_ids)
        assert dangling == [], f"referenced but never declared: {dangling}"

    def test_an_id_created_at_runtime_really_is_created(self, markup):
        """The exemption above is only honest if the id is actually assigned somewhere."""
        assert 'wrap.id = "buffering-msg"' in markup

    def test_cross_agent_lines_are_from_the_agents_layer(self, markup):
        """The join between two agents' conversations comes from the transparency feed's AGENTS layer."""
        assert "layer !== 6" in markup


class TestTheMultiTranscriptApi:
    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MYRIAXON_CONFIG_DIR", str(tmp_path / "cfg"))
        from myriaxon.config import Config
        from myriaxon.dashboard.server import DashboardApp

        return DashboardApp(tmp_path, config=Config(api_key="x", base_url="http://127.0.0.1:1",
                                                    model="m", provider="openai_compat"))

    def test_the_payload_carries_a_transcripts_map(self, app):
        """Absent when nobody asked, present-but-empty when asked for nothing -- both are a stable shape."""
        assert app._loci_payload().get("transcripts") == {}

    def test_no_argument_at_all_does_not_raise(self, app):
        """Called with no second argument from `/api/config`, so a missing default would break the whole page.

        Measured before the fix: `TypeError: 'NoneType' object is not iterable`, which took the entire config
        payload down to a 500 and left the dashboard unusable rather than merely missing a feature.
        """
        assert app._loci_payload().get("loci") is not None

    def test_an_unknown_locus_yields_an_empty_list_not_an_error(self, app):
        """A pane whose locus was deleted must render as empty rather than break the wall."""
        result = app._loci_payload("", ["no-such-locus-exists"])
        assert result["transcripts"]["no-such-locus-exists"] == []

    def test_names_are_deduplicated(self, app):
        """A locus in two panes is one read, because the answer is the same conversation."""
        result = app._loci_payload("", ["ghost", "ghost", "ghost"])
        assert list(result["transcripts"]) == ["ghost"]

    def test_the_single_transcript_form_still_works(self, app):
        """The roster's own chat pane uses it, so the wall must not have replaced it."""
        result = app._loci_payload("no-such-locus-exists", None)
        assert "transcript" in result and "transcript_session" in result

    def test_a_blank_name_is_skipped(self, app):
        result = app._loci_payload("", ["", "  ", "ghost"])
        assert list(result["transcripts"]) == ["ghost"]


class TestTheWallDoesNotDuplicateTheRosterRead:
    def test_both_forms_share_one_read_helper(self):
        """`/api/loci` and `/api/loci?transcripts=` must not grow two interpretations of a transcript.

        Two readers of the same session would eventually disagree -- about role mapping, about which messages
        count as prose -- and the roster and the wall would show different conversations for one agent.
        """
        import inspect

        from myriaxon.dashboard import server

        source = inspect.getsource(server)
        assert "def _read_transcript(" in source, "the shared reader is gone"
        # Called from exactly one place, which is what makes it shared rather than duplicated.
        assert source.count("_read_transcript(") == 3, (
            "expected one definition and two call sites (single + wall); "
            f"found {source.count('_read_transcript(')}"
        )
