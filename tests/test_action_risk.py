"""Action risk: what an action would cost if it went wrong. Level 5.6.

Level 3 escalates when it is DONE; Level 5 escalates when the next action is dangerous. That needs a
classifier, and the point of this one is that it classifies by BLAST RADIUS rather than by pattern.

`constants.RISKY_BASH_PATTERNS` already exists and labels itself *"a best-effort heuristic for Auto mode's
safety check -- NOT a security boundary"*. It is a regex over one tool's command text, so it sees `rm -rf` and
nothing else: it cannot distinguish a write inside the workspace from a write over the owner's documents, and
it has no opinion about any tool that is not a shell. Blast radius generalises because it is a fact about the
ACTION rather than a pattern in its text.

THE SHELL DECISION IS THE INTERESTING ONE. A shell command is HIGH because it is UNBOUNDED, not because it is
necessarily dangerous: `ls` is harmless, `rm -rf /` is not, and nothing in the text reliably separates them.
So it escalates unless the caller says the workspace is already a boundary (`confined=True`), in which case
the sandbox contains it and the verdict drops to MEDIUM.

WHAT THIS IS NOT, asserted so nobody mistakes it for more: it decides whether to ASK, and asking is a social
control. A caller that ignores the verdict has ignored it. The isolation is the container and the mount flags.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import action_risk as ar  # noqa: E402


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    (root / "sub").mkdir(parents=True)
    return root


class TestReadsAreLow:
    def test_a_read_is_low_and_does_not_ask(self, workspace):
        risk = ar.classify("read_file", {"path": "src/app.py"}, workspace=workspace)
        assert risk.level == ar.LOW and risk.should_ask is False

    def test_searching_is_low(self, workspace):
        assert ar.classify("grep_search", {"pattern": "x"}, workspace=workspace).level == ar.LOW

    def test_reading_peer_memory_is_low(self, workspace):
        """Reading what peers learned has no blast radius."""
        assert ar.classify("memory_recall", {"query": "x"}, workspace=workspace).level == ar.LOW


class TestWorkspaceWritesAreMedium:
    def test_a_write_inside_the_workspace_is_medium(self, workspace):
        risk = ar.classify("write_file", {"path": str(workspace / "out.svg")}, workspace=workspace)
        assert risk.level == ar.MEDIUM and risk.should_ask is False

    def test_a_relative_path_resolves_against_the_workspace(self, workspace):
        risk = ar.classify("write_file", {"path": "sub/a.md"}, workspace=workspace)
        assert risk.level == ar.MEDIUM

    def test_editing_is_medium(self, workspace):
        assert ar.classify("edit_file", {"path": str(workspace / "a.py")},
                           workspace=workspace).level == ar.MEDIUM

    def test_a_workspace_write_is_not_irreversible(self, workspace):
        """Reversible by editing again, which is what keeps it below HIGH."""
        assert ar.classify("write_file", {"path": str(workspace / "a.md")},
                           workspace=workspace).irreversible is False


class TestWritesOutsideAreHigh:
    def test_a_write_outside_the_workspace_escalates(self, workspace, tmp_path):
        outside = tmp_path / "elsewhere" / "important.docx"
        risk = ar.classify("write_file", {"path": str(outside)}, workspace=workspace)
        assert risk.level == ar.HIGH and risk.should_ask is True
        assert "outside the workspace" in risk.reason

    def test_it_is_marked_irreversible(self, workspace, tmp_path):
        outside = tmp_path / "elsewhere.txt"
        assert ar.classify("write_file", {"path": str(outside)},
                           workspace=workspace).irreversible is True

    def test_a_traversal_path_is_caught(self, workspace):
        """A relative path that escapes is the case a superficial check misses."""
        risk = ar.classify("write_file", {"path": "../../secrets.txt"}, workspace=workspace)
        assert risk.level == ar.HIGH

    def test_credentials_are_protected_even_inside_the_workspace(self, workspace):
        risk = ar.classify("write_file", {"path": str(workspace / ".env")}, workspace=workspace)
        assert risk.level == ar.HIGH and "credentials" in risk.reason

    def test_git_internals_are_protected(self, workspace):
        risk = ar.classify("write_file", {"path": str(workspace / ".git" / "config")},
                           workspace=workspace)
        assert risk.level == ar.HIGH

    def test_a_key_file_is_protected(self, workspace):
        assert ar.classify("write_file", {"path": str(workspace / "id_rsa")},
                           workspace=workspace).level == ar.HIGH


class TestTheShellDecision:
    def test_an_unconfined_shell_escalates(self, workspace):
        """Unbounded is the reason, and it is stated as such."""
        risk = ar.classify("run_bash", {"command": "ls"}, workspace=workspace, confined=False)
        assert risk.level == ar.HIGH
        assert "unbounded" in risk.reason

    def test_a_confined_shell_does_not(self, workspace):
        risk = ar.classify("run_bash", {"command": "ls"}, workspace=workspace, confined=True)
        assert risk.level == ar.MEDIUM and risk.should_ask is False

    def test_a_destructive_command_escalates_even_when_confined(self, workspace):
        """The sandbox bounds WHERE it acts, not whether the act is a catastrophe."""
        risk = ar.classify("run_bash", {"command": "rm -rf /tmp/x"}, workspace=workspace, confined=True)
        assert risk.level == ar.HIGH and risk.irreversible is True
        assert "recursive force-delete" in risk.reason

    @pytest.mark.parametrize("command,expected", [
        ("Remove-Item -Recurse -Force C:\\x", "recursive delete"),
        ("git push --force origin main", "force push"),
        ("mkfs.ext4 /dev/sda1", "filesystem creation"),
        ("shutdown -h now", "machine power control"),
        ("dd if=/dev/zero of=/dev/sda", "raw device write"),
    ])
    def test_named_destruction_is_recognised(self, command, expected, workspace):
        risk = ar.classify("run_bash", {"command": command}, workspace=workspace, confined=True)
        assert risk.level == ar.HIGH and expected in risk.reason


class TestOutboundAndSpendingTools:
    def test_a_web_fetch_is_medium(self, workspace):
        """Read-only but not confined: it leaves the machine."""
        risk = ar.classify("web_fetch", {"url": "https://x"}, workspace=workspace)
        assert risk.level == ar.MEDIUM

    def test_running_a_locus_escalates(self, workspace):
        """It spends tokens with nobody watching."""
        assert ar.classify("locus_run", {"locus": "sales"}, workspace=workspace).level == ar.HIGH

    def test_spawning_a_subagent_escalates(self, workspace):
        assert ar.classify("spawn_subagent", {}, workspace=workspace).level == ar.HIGH

    def test_an_mcp_tool_escalates(self, workspace):
        """An MCP server's tools are not under this codebase's control."""
        assert ar.classify("mcp__figma__create", {}, workspace=workspace).level == ar.HIGH


class TestUnknownToolsAreNotAssumedSafe:
    def test_an_unclassified_tool_escalates(self, workspace):
        """The cost of asking is one prompt; the cost of an irreversible mistake is not recoverable."""
        risk = ar.classify("brand_new_tool", {}, workspace=workspace)
        assert risk.level == ar.HIGH
        assert "not classified" in risk.reason

    def test_an_empty_tool_name_escalates(self, workspace):
        assert ar.classify("", {}, workspace=workspace).level == ar.HIGH


class TestTheOverride:
    def test_a_caller_that_knows_better_is_taken_at_its_word(self, workspace):
        """Present so the classifier can be corrected rather than worked around."""
        override = ar.Risk(level=ar.LOW, reason="the owner already approved this exact action")
        assert ar.classify("run_bash", {"command": "rm -rf /"}, override=override).level == ar.LOW


class TestTheEscalationMessage:
    def test_it_names_the_action_and_the_reason(self, workspace):
        risk = ar.classify("write_file", {"path": str(workspace.parent / "x.docx")},
                           workspace=workspace)
        message = ar.escalate_message(risk, "write_file")
        assert "write_file" in message and risk.reason[:20] in message

    def test_it_says_an_irreversible_action_cannot_be_undone(self, workspace):
        risk = ar.classify("write_file", {"path": str(workspace.parent / "x.docx")},
                           workspace=workspace)
        assert "cannot be undone" in ar.escalate_message(risk, "write_file")

    def test_it_offers_both_legitimate_ways_forward(self, workspace):
        """A refusal that only says no leaves a model to guess, and it will guess permissively."""
        risk = ar.Risk(level=ar.HIGH, reason="x", irreversible=False)
        message = ar.escalate_message(risk, "run_bash")
        assert "ask the owner to confirm" in message
        assert "narrower action" in message


class TestThresholdComparisons:
    def test_at_least_orders_the_levels(self):
        assert ar.at_least(ar.Risk(level=ar.HIGH), ar.HIGH) is True
        assert ar.at_least(ar.Risk(level=ar.MEDIUM), ar.HIGH) is False
        assert ar.at_least(ar.Risk(level=ar.HIGH), ar.MEDIUM) is True

    def test_the_verdict_serialises_for_a_dashboard(self):
        payload = ar.classify("read_file", {}).to_dict()
        assert set(payload) == {"level", "reason", "irreversible", "should_ask"}


class TestItIsNotASandbox:
    def test_it_only_advises_and_cannot_prevent(self, workspace):
        """Asserted so nobody mistakes this for the isolation layer.

        The verdict says whether to ASK. The isolation is the container and the mount flags, and a caller that
        ignores this has ignored it.
        """
        risk = ar.classify("run_bash", {"command": "rm -rf /"}, workspace=workspace, confined=False)
        assert risk.should_ask is True
        # Nothing here performs, blocks or reverts the action -- there is no such API, deliberately.
        assert not hasattr(ar, "prevent")
        assert not hasattr(ar, "enforce")
