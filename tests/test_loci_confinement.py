"""Autonomy inside an ENFORCED boundary, not a stated one.

The owner chose full autonomy in an isolated environment. Those two words are the whole design, and
each is useless alone:

  * autonomy without a boundary is a grant of the machine;
  * a boundary that is only announced is not a boundary at all.

So both halves are tested here, together: a mutating tool inside the workspace must be ALLOWED without
a prompt (a headless run has nobody to answer one), a path outside it must be REFUSED even though the
tool was auto-approved, and a shell -- which no path check can confine -- must be refused outright
when no real sandbox is available.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402
from rich.console import Console  # noqa: E402

from myriaxon import tool_registry, tools  # noqa: E402
from myriaxon.tool_context import ToolContext  # noqa: E402


def make_ctx(workspace: str = "") -> ToolContext:
    return ToolContext(
        client=None, model="test", max_tokens=100, console=Console(quiet=True),
        yolo=False, tools=[], mode="default", locus_workspace=workspace,
    )


@pytest.fixture()
def workspace(tmp_path):
    made = tmp_path / "locus-workspace"
    made.mkdir()
    return made


def test_a_confined_locus_may_write_without_being_asked(workspace):
    """The prompt is the bug: a headless run that asks gets EOF, retries, and dies."""
    ctx = make_ctx(str(workspace))
    proceed, blocked = tools.confirm_action(
        ctx.console, None, ctx, "write_file", tool_input={"path": str(workspace / "piece.md")}
    )
    assert proceed and blocked is None


def test_a_confined_locus_may_edit_without_being_asked(workspace):
    ctx = make_ctx(str(workspace))
    proceed, blocked = tools.confirm_action(
        ctx.console, None, ctx, "edit_file", tool_input={"path": str(workspace / "x")}
    )
    assert proceed and blocked is None


def test_a_confined_locus_is_refused_a_shell_when_no_sandbox_exists(workspace):
    """A shell is not confined by a path check, so auto-approving it would grant the whole machine."""
    ctx = make_ctx(str(workspace))
    ctx.sandbox_enabled = False
    proceed, blocked = tools.confirm_action(
        ctx.console, None, ctx, "run_bash", command="Get-ChildItem C:\\"
    )
    assert not proceed
    assert blocked and "cannot be confined" in blocked
    # The refusal must say what to do, not only that it is refused.
    assert "Docker" in blocked or "Sandbox" in blocked


def test_an_ordinary_turn_is_unaffected(workspace):
    """No workspace means no confinement: the interactive path keeps its prompts.

    `yolo` is set so this asserts a DEFINITE outcome. Without it the call reaches `Prompt.ask` and
    reads stdin, which under pytest raises -- and in a real unattended run would block forever, which
    is precisely the failure being fixed here. A test that hangs is worse than one that fails.
    """
    ctx = make_ctx("")
    ctx.mode = "default"
    ctx.yolo = True  # the ordinary auto-approve path, so no prompt is reached
    proceed, blocked = tools.confirm_action(
        ctx.console, None, ctx, "run_bash", command="Write-Output hi"
    )
    assert proceed and blocked is None, "an ordinary turn keeps its existing auto-approve behaviour"


def test_an_ordinary_turn_is_never_given_the_locus_refusal(workspace):
    """The confinement refusal must be specific to a confined Locus, not leak into normal turns."""
    ctx = make_ctx("")
    ctx.mode = "default"
    ctx.yolo = True
    _proceed, blocked = tools.confirm_action(
        ctx.console, None, ctx, "run_bash", command="Get-ChildItem C:\\"
    )
    assert blocked is None, "a session turn was refused with a Locus-specific message"


def test_a_path_inside_the_workspace_resolves(workspace):
    token = tools.confine_to(workspace)
    try:
        assert tools._resolve(str(workspace / "sub" / "file.txt")) == (workspace / "sub" / "file.txt")
    finally:
        tools._LOCUS_WORKSPACE.reset(token)


def test_a_workspace_outside_the_project_root_can_still_be_written(tmp_path_factory):
    """The regression that only a REAL Locus could have found.

    Every other test here uses `tmp_path`, and a real Locus's workspace is `~/.myriaxon/loci/<id>`.
    Both are outside the project root, but `tmp_path` happened to be accepted by an older check while
    the real one was not -- so a green suite coexisted with a Locus that could not write anything.
    Measured end to end: it ran 30 minutes, called write_file, got "Blocked: ... is outside the
    project dir", and left an EMPTY workspace.

    This test asserts its own precondition, so it cannot silently stop testing the thing it exists for.
    """
    outside = tmp_path_factory.mktemp("outside-project-root")
    assert not str(outside.resolve()).startswith(str(Path.cwd().resolve())), (
        "meaningless if the temp directory happens to sit inside the project root"
    )
    ctx = make_ctx(str(outside))
    target = outside / "designs" / "piece.svg"
    token = tools.confine_to(outside)
    try:
        result, is_error = tool_registry.dispatch_tool(
            "write_file", {"path": str(target), "content": "<svg/>"}, ctx
        )
    finally:
        tools._LOCUS_WORKSPACE.reset(token)
    assert not is_error, f"a confined Locus could not write inside its own workspace: {result}"
    assert target.exists()


def test_writing_outside_the_workspace_is_refused_even_though_auto_approved(tmp_path_factory):
    """Auto-approval must not become a hole. The boundary is what makes granting it safe."""
    outside = tmp_path_factory.mktemp("outside-project-root-2")
    ctx = make_ctx(str(outside))
    escape_target = outside.parent / "escaped.txt"
    token = tools.confine_to(outside)
    try:
        result, is_error = tool_registry.dispatch_tool(
            "write_file", {"path": str(escape_target), "content": "x"}, ctx
        )
    finally:
        tools._LOCUS_WORKSPACE.reset(token)
    assert is_error, "a confined Locus wrote outside its workspace"
    assert "outside this Locus's workspace" in str(result)
    assert not escape_target.exists(), "the file was created despite the refusal"


def test_parent_traversal_is_refused(workspace):
    """`..` is the obvious escape, and it must be collapsed BEFORE the check, not after."""
    token = tools.confine_to(workspace)
    try:
        with pytest.raises(tools.WorkspaceEscape):
            tools._resolve(str(workspace / ".." / "outside.txt"))
    finally:
        tools._LOCUS_WORKSPACE.reset(token)


def test_an_absolute_path_outside_is_refused(workspace):
    token = tools.confine_to(workspace)
    try:
        with pytest.raises(tools.WorkspaceEscape):
            tools._resolve(str(workspace.parent))
    finally:
        tools._LOCUS_WORKSPACE.reset(token)


def test_the_boundary_is_restored_afterwards(workspace, tmp_path):
    """A stale boundary would make the interactive agent refuse paths it may touch."""
    token = tools.confine_to(workspace)
    tools._LOCUS_WORKSPACE.reset(token)
    assert tools.confined_workspace() is None
    # And an unconfined resolve must accept an outside path again.
    assert tools._resolve(str(tmp_path / "anywhere.txt")).name == "anywhere.txt"


def test_the_refusal_explains_the_confinement_to_the_model(workspace):
    """The message reaches the model as a tool error, so it must be actionable rather than a code."""
    token = tools.confine_to(workspace)
    try:
        with pytest.raises(tools.WorkspaceEscape) as caught:
            tools._resolve(str(workspace.parent / "elsewhere"))
    finally:
        tools._LOCUS_WORKSPACE.reset(token)
    text = str(caught.value)
    assert "outside this Locus's workspace" in text
    assert "confined" in text


def test_nesting_restores_the_outer_boundary(workspace, tmp_path):
    """A locus run inside another confined scope must not widen or lose the outer boundary."""
    outer = tmp_path / "outer"
    outer.mkdir()
    outer_token = tools.confine_to(outer)
    inner_token = tools.confine_to(workspace)
    assert tools.confined_workspace() == workspace.resolve()
    tools._LOCUS_WORKSPACE.reset(inner_token)
    assert tools.confined_workspace() == outer.resolve()
    tools._LOCUS_WORKSPACE.reset(outer_token)
