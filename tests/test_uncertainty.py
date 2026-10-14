"""Level 5.5: declared uncertainty -- separating what was CHECKED from what was REASONED.

THE CEILING FIRST, because it caps this level and should not be buried. No harness can make a model feel
doubt. A model that is wrong with confidence produces text indistinguishable from a model that is right with
confidence, and nothing inside a harness reaches the process that generated it. `test_agent_discipline.py`
asserts that limit separately, so nobody tries to fix it by lowering a threshold.

WHAT THIS ACTUALLY BUILDS, and why it is worth building anyway: a model reporting a conclusion writes
`verified` and `assumed` in the same register. "The global endpoint is the bare host" reads identically
whether it was run and observed or guessed from a doc. If the distinction is never RECORDED, a reader cannot
recover it -- and neither can the model on its next turn.

THE REFUSAL IS THE PART THAT MATTERS. A `checked` claim that names nothing it checked is an assertion wearing
the word, and accepting it would make the whole distinction worthless. So `checked` and `inferred` both require
their evidence, and the error says to use `assumed` instead -- because `assumed` is a perfectly good answer,
and an agent that will not proceed without certainty cannot proceed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def uncertainty(tmp_path):
    from myriaxon import uncertainty

    return uncertainty, tmp_path


class TestTheThreeBases:
    def test_checked_requires_naming_what_was_checked(self, uncertainty):
        """The refusal that keeps the label meaningful."""
        module, root = uncertainty
        claim, problem = module.declare("the endpoint is the bare host", module.CHECKED, root=root)
        assert claim is None
        assert "must name" in problem
        assert "assumed" in problem, "the refusal must offer the honest alternative"

    def test_inferred_requires_naming_what_it_came_from(self, uncertainty):
        module, root = uncertainty
        claim, problem = module.declare("the limit is 4096", module.INFERRED, root=root)
        assert claim is None and "inferred from" in problem

    def test_assumed_needs_no_evidence(self, uncertainty):
        """An agent that will not proceed without certainty cannot proceed."""
        module, root = uncertainty
        claim, problem = module.declare("the client will retry", module.ASSUMED, root=root)
        assert problem == "" and claim.basis == module.ASSUMED

    def test_a_checked_claim_with_evidence_is_accepted(self, uncertainty):
        module, root = uncertainty
        claim, problem = module.declare("the endpoint is the bare host", module.CHECKED,
                                        evidence="ran a probe; 200 OK", root=root)
        assert problem == "" and claim.is_evidence is True

    def test_an_unknown_basis_is_refused(self, uncertainty):
        module, root = uncertainty
        claim, problem = module.declare("something", "probably", root=root)
        assert claim is None and "basis must be" in problem

    def test_an_empty_claim_is_refused(self, uncertainty):
        module, root = uncertainty
        assert module.declare("  ", module.ASSUMED, root=root)[0] is None


class TestTheRecordDistinguishesThem:
    def test_unverified_claims_are_counted_separately(self, uncertainty):
        module, root = uncertainty
        module.declare("verified thing", module.CHECKED, evidence="ran it", root=root)
        module.declare("guessed thing", module.ASSUMED, root=root)
        data = module.summary(root=root)
        assert data["checked"] == 1 and data["unverified"] == 1

    def test_the_report_puts_the_unverified_ones_first(self, uncertainty):
        """A reader scanning for what ISN'T established must not have to hunt for it."""
        module, root = uncertainty
        module.declare("a verified fact", module.CHECKED, evidence="ran it", root=root)
        module.declare("an assumption", module.ASSUMED, root=root)
        report = module.format_report(root=root)
        assert report.index("NOT VERIFIED") < report.index("Verified:")

    def test_the_report_carries_the_evidence(self, uncertainty):
        module, root = uncertainty
        module.declare("a fact", module.CHECKED, evidence="ran the probe", root=root)
        assert "ran the probe" in module.format_report(root=root)

    def test_the_report_carries_what_would_settle_it(self, uncertainty):
        """The part that makes the record actionable rather than merely honest."""
        module, root = uncertainty
        module.declare("an assumption", module.ASSUMED,
                       would_confirm="send a 429 and observe", root=root)
        assert "send a 429" in module.format_report(root=root)

    def test_an_empty_record_says_so_plainly(self, uncertainty):
        module, root = uncertainty
        assert "Nothing declared" in module.format_report(root=root)

    def test_inferred_is_not_evidence(self, uncertainty):
        """Reasoning from something else is not observation, and a reader must be able to tell."""
        module, root = uncertainty
        claim, _ = module.declare("the limit is 4096", module.INFERRED,
                                  evidence="the 400 error message", root=root)
        assert claim.is_evidence is False and claim.unverified is True


class TestPerRunIsolation:
    def test_a_run_filter_shows_only_that_runs_claims(self, uncertainty):
        module, root = uncertainty
        module.declare("run one claim", module.ASSUMED, run_id="r1", root=root)
        module.declare("run two claim", module.ASSUMED, run_id="r2", root=root)
        assert len(module.load(run_id="r1", root=root)) == 1

    def test_the_summary_can_be_scoped_to_a_run(self, uncertainty):
        module, root = uncertainty
        module.declare("a", module.ASSUMED, run_id="r1", root=root)
        module.declare("b", module.ASSUMED, run_id="r2", root=root)
        assert module.summary(run_id="r1", root=root)["total"] == 1


class TestTheStoreDegrades:
    def test_a_missing_store_reads_as_empty(self, uncertainty):
        module, root = uncertainty
        assert module.load(root=root) == []

    def test_a_torn_line_does_not_lose_the_others(self, uncertainty):
        """Declarations are appended while a run is in flight, so a torn tail is the likely failure."""
        module, root = uncertainty
        module.declare("good one", module.ASSUMED, root=root)
        path = module._store_path(root)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{ not json\n")
        module.declare("good two", module.ASSUMED, root=root)
        texts = [c.text for c in module.load(root=root)]
        assert "good one" in texts and "good two" in texts

    def test_the_store_is_bounded(self, uncertainty, monkeypatch):
        module, root = uncertainty
        monkeypatch.setattr(module, "MAX_CLAIMS", 5)
        for index in range(12):
            module.declare(f"claim {index}", module.ASSUMED, root=root)
        assert len(module.load(root=root)) <= 5


class TestTheTool:
    def test_it_records_through_the_tool_surface(self, uncertainty, monkeypatch):
        module, root = uncertainty
        monkeypatch.setattr(module, "_store_path", lambda _root=None: root / module.STORE_FILENAME)
        text, err = module.execute_declare_uncertainty(
            {"text": "a fact", "basis": "checked", "evidence": "ran it"})
        assert err is False and "checked" in text

    def test_a_bad_declaration_reports_the_reason(self, uncertainty, monkeypatch):
        module, root = uncertainty
        monkeypatch.setattr(module, "_store_path", lambda _root=None: root / module.STORE_FILENAME)
        text, err = module.execute_declare_uncertainty({"text": "a fact", "basis": "checked"})
        assert err is True and "must name" in text

    def test_the_tool_def_is_real_schema(self, uncertainty):
        module, _ = uncertainty
        definition = module.DECLARE_UNCERTAINTY_TOOL_DEF
        assert definition["name"] == "declare_uncertainty"
        assert set(definition["input_schema"]["properties"]["basis"]["enum"]) == set(module.BASES)

    def test_a_one_def_group_exposes_a_tuple(self, uncertainty):
        """The registration machinery expects a tuple; the module names its own surface."""
        module, _ = uncertainty
        assert isinstance(module.DECLARE_UNCERTAINTY_TOOL_DEFS, tuple)
        assert len(module.DECLARE_UNCERTAINTY_TOOL_DEFS) == 1


class TestTheCeilingIsStated:
    def test_the_module_says_what_it_cannot_do(self, uncertainty):
        """A capability that oversells itself is worse than one that admits its limit.

        Asserted on the MEANING rather than one exact sentence: the docstring says "no harness can make a model
        feel doubt", and requiring those precise words would fail the day somebody improved the phrasing --
        which teaches nothing about whether the limit is still stated.
        """
        module, _ = uncertainty
        doc = (module.__doc__ or "").lower()
        assert "cannot be fully built" in doc
        assert "make a model feel doubt" in doc

    def test_the_harness_does_not_guess_a_basis(self, uncertainty):
        """Nothing in the text reveals whether a command was run, so a guess would invent the distinction."""
        module, _ = uncertainty
        assert not hasattr(module, "infer_basis")
        assert not hasattr(module, "detect_basis")
