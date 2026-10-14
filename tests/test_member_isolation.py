"""Level 4.12: per-member mount enforcement.

A Locus runs confined to its own workspace, and that confinement is what lets it act without asking for
approval -- there is nobody to ask. So the confinement has to be REAL, and the tests below press the ways a
path check of this kind is normally defeated.

THE GAP THIS FOUND, by measurement rather than review:

    board inside l1 workspace: NO

The swarm board and the collective memory live under the config directory, which is outside every member's
workspace, so `_resolve` refused them and a confined Locus could not use the board AT ALL. The board shipped
working in an unconfined session and was unreachable in the confined one it exists for.

THE FIX IS AN ALLOWLIST, AND THE OBVIOUS ALTERNATIVES ARE BOTH WORSE:

  * widening the boundary to the config directory would expose `sessions/`, `audit.jsonl`, `config.json` and
    every PEER's workspace -- three confinements destroyed to solve one problem;
  * mounting the board inside each workspace would give each member a private copy, so two members would share
    nothing while appearing to share everything.

An explicit named grant does neither, and the whole set of exceptions stays visible in one place.

`run_bash` is a separate boundary and this file says so rather than implying the path check covers it: the
mount is the only thing confining a shell, and a path check over file tools cannot see a command at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A config dir, a swarm dir, and two loci with separate workspaces."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("MYRIAXON_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("MYRIAXON_SWARM_DIR", str(cfg / "swarm"))

    from myriaxon import loci, locus_swarm, tools

    alpha = loci.Locus(id="alpha", name="alpha", role="engineer")
    beta = loci.Locus(id="beta", name="beta", role="engineer")
    yield {
        "tools": tools, "swarm": locus_swarm, "cfg": cfg,
        "alpha": alpha, "beta": beta,
        "w1": alpha.workspace_path(), "w2": beta.workspace_path(),
        "board": locus_swarm.swarm_root(),
    }
    # Every test leaves the process as it found it: grants are process-global, so a leak here would make a
    # later test pass for the wrong reason.
    tools.grant_shared("swarm", None)
    tools.grant_shared("member-workspaces", None)


class TestMembersAreIsolated:
    def test_two_members_get_different_workspaces(self, env):
        assert env["w1"] != env["w2"]

    def test_one_workspace_is_not_inside_the_other(self, env):
        assert not str(env["w1"]).startswith(str(env["w2"]))
        assert not str(env["w2"]).startswith(str(env["w1"]))

    def test_a_member_can_reach_its_own_workspace(self, env):
        tools = env["tools"]
        token = tools.confine_to(env["w1"])
        try:
            assert tools._resolve(str(env["w1"] / "note.txt")).name == "note.txt"
        finally:
            tools._LOCUS_WORKSPACE.reset(token)

    def test_a_member_cannot_reach_a_peers_workspace(self, env):
        tools = env["tools"]
        env["w2"].mkdir(parents=True, exist_ok=True)
        (env["w2"] / "secret.txt").write_text("peer's work", encoding="utf-8")
        token = tools.confine_to(env["w1"])
        try:
            with pytest.raises(tools.WorkspaceEscape):
                tools._resolve(str(env["w2"] / "secret.txt"))
        finally:
            tools._LOCUS_WORKSPACE.reset(token)


class TestTheConfineIsNotDefeated:
    def test_traversal_is_caught(self, env):
        """The usual way a confinement like this is defeated."""
        tools = env["tools"]
        token = tools.confine_to(env["w1"])
        try:
            with pytest.raises(tools.WorkspaceEscape):
                tools._resolve(str(env["w1"] / ".." / ".." / "elsewhere.txt"))
        finally:
            tools._LOCUS_WORKSPACE.reset(token)

    def test_a_symlink_out_of_the_tree_is_caught(self, env, tmp_path):
        """`resolve()` follows symlinks before the check, which is why it is applied first."""
        tools = env["tools"]
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = env["w1"] / "link.txt"
        env["w1"].mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available to this account")
        token = tools.confine_to(env["w1"])
        try:
            with pytest.raises(tools.WorkspaceEscape):
                tools._resolve(str(link))
        finally:
            tools._LOCUS_WORKSPACE.reset(token)


class TestTheBoardIsReachableOnlyByGrant:
    def test_the_board_is_outside_every_workspace(self, env):
        """The measurement that found the gap. Asserted so a change to the layout is noticed."""
        assert not str(env["board"]).startswith(str(env["w1"]))

    def test_without_a_grant_the_board_is_refused(self, env):
        """Which is what left a confined Locus unable to coordinate at all."""
        tools = env["tools"]
        token = tools.confine_to(env["w1"])
        try:
            with pytest.raises(tools.WorkspaceEscape):
                tools._resolve(str(env["board"] / "board"))
        finally:
            tools._LOCUS_WORKSPACE.reset(token)

    def test_with_a_grant_the_board_is_reachable(self, env):
        tools = env["tools"]
        tools.grant_shared("swarm", env["board"])
        token = tools.confine_to(env["w1"])
        try:
            assert tools._resolve(str(env["board"] / "board"))
        finally:
            tools._LOCUS_WORKSPACE.reset(token)

    def test_the_grant_does_not_open_the_rest_of_the_config_dir(self, env):
        """The reason this is an allowlist and not a widened boundary."""
        tools = env["tools"]
        tools.grant_shared("swarm", env["board"])
        (env["cfg"] / "audit.jsonl").write_text("{}", encoding="utf-8")
        (env["cfg"] / "config.json").write_text("{}", encoding="utf-8")
        (env["cfg"] / "sessions").mkdir(exist_ok=True)
        token = tools.confine_to(env["w1"])
        try:
            for name in ("audit.jsonl", "config.json", "sessions"):
                with pytest.raises(tools.WorkspaceEscape):
                    tools._resolve(str(env["cfg"] / name))
        finally:
            tools._LOCUS_WORKSPACE.reset(token)

    def test_the_grant_does_not_open_a_peer_workspace(self, env):
        tools = env["tools"]
        tools.grant_shared("swarm", env["board"])
        env["w2"].mkdir(parents=True, exist_ok=True)
        (env["w2"] / "peer.txt").write_text("x", encoding="utf-8")
        token = tools.confine_to(env["w1"])
        try:
            with pytest.raises(tools.WorkspaceEscape):
                tools._resolve(str(env["w2"] / "peer.txt"))
        finally:
            tools._LOCUS_WORKSPACE.reset(token)

    def test_a_granted_directory_does_not_grant_its_siblings(self, env):
        tools = env["tools"]
        tools.grant_shared("swarm", env["board"])
        sibling = env["cfg"] / "swarm-sibling"
        sibling.mkdir(exist_ok=True)
        token = tools.confine_to(env["w1"])
        try:
            with pytest.raises(tools.WorkspaceEscape):
                tools._resolve(str(sibling / "x"))
        finally:
            tools._LOCUS_WORKSPACE.reset(token)


class TestGrantsAreAuditableAndRevocable:
    def test_the_grants_are_listable_by_name(self, env):
        """A named grant is one a reader can check; a widened boundary is not."""
        tools = env["tools"]
        tools.grant_shared("swarm", env["board"])
        grants = tools.shared_grants()
        assert "swarm" in grants and str(env["board"]) in grants["swarm"]

    def test_revoking_removes_it(self, env):
        tools = env["tools"]
        tools.grant_shared("swarm", env["board"])
        tools.grant_shared("swarm", None)
        assert "swarm" not in tools.shared_grants()

    def test_a_revoked_grant_is_refused_again(self, env):
        """Grants are process-global, so one left standing outlives the run it was made for."""
        tools = env["tools"]
        tools.grant_shared("swarm", env["board"])
        tools.grant_shared("swarm", None)
        token = tools.confine_to(env["w1"])
        try:
            with pytest.raises(tools.WorkspaceEscape):
                tools._resolve(str(env["board"] / "board"))
        finally:
            tools._LOCUS_WORKSPACE.reset(token)

    def test_granting_none_for_an_unknown_name_is_harmless(self, env):
        env["tools"].grant_shared("never-granted", None)
        assert "never-granted" not in env["tools"].shared_grants()

    def test_the_refusal_names_the_shared_directories_available(self, env):
        """A refusal that lists what IS reachable is one an agent can act on."""
        tools = env["tools"]
        tools.grant_shared("swarm", env["board"])
        token = tools.confine_to(env["w1"])
        try:
            with pytest.raises(tools.WorkspaceEscape) as caught:
                tools._resolve(str(env["cfg"] / "config.json"))
            assert "Shared directories you CAN reach" in str(caught.value)
        finally:
            tools._LOCUS_WORKSPACE.reset(token)


class TestUnconfinedIsUnaffected:
    def test_no_boundary_means_no_refusal(self, env):
        """An interactive session is not confined, and must not start being refused."""
        assert env["tools"]._resolve(str(env["cfg"] / "config.json"))

    def test_confined_workspace_reports_the_active_boundary(self, env):
        tools = env["tools"]
        assert tools.confined_workspace() is None
        token = tools.confine_to(env["w1"])
        try:
            assert tools.confined_workspace() == env["w1"].resolve()
        finally:
            tools._LOCUS_WORKSPACE.reset(token)
        assert tools.confined_workspace() is None


class TestTheShellIsADifferentBoundary:
    def test_the_path_check_does_not_see_a_shell_command(self, env):
        """Asserted so nobody reads `_resolve` as covering `run_bash`.

        A path check examines an ARGUMENT. A shell command is a string that can name any path at all, so the
        mount is the only thing confining it -- which is why the run loop mounts the member's own workspace
        rather than the project root, and why that was a real reported bug when it was wrong.
        """
        import inspect

        assert "command" not in inspect.signature(env["tools"]._resolve).parameters
        assert list(inspect.signature(env["tools"]._resolve).parameters) == ["path_str"]

    def test_the_shell_cwd_is_the_member_workspace(self, env):
        """The mount, not the check, is what confines a shell."""
        import inspect

        from myriaxon import tools as tools_module

        source = inspect.getsource(tools_module)
        assert "sandbox_cwd = _LOCUS_WORKSPACE.get()" in source
        assert "mount_cwd = str(sandbox_cwd)" in source
