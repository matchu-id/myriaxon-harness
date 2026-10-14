"""Verify argument-aware permission rules.

    python scripts/check_permission_rules.py

Safety-critical, so the adversarial cases matter more than the happy path:
  * a deny must hold even with yolo on and even when a lower layer says allow
  * an "ask" must keep asking even with yolo on
  * a prefix rule must not match a different command that merely shares the prefix ("git" must not
    authorise "gitleaks")
  * a more specific rule must win over a less specific one in the same layer

Runs entirely offline: it exercises the policy engine and the tool boundary, not a live model.
"""
import sys
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
warnings.filterwarnings("ignore")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from myriaxon.settings import Settings  # noqa: E402

passed, failed = [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + str(detail)) if detail and not condition else ''}")


def settings_with(*layers: dict) -> Settings:
    settings = Settings()
    settings.permission_layers = list(layers)
    return settings


def main() -> int:
    print("=== tool-wide rules still behave as before ===")
    s = settings_with({"run_bash": "deny"})
    check("deny matches the tool", s.resolve_permission("run_bash", {"command": "ls"}) == "deny")
    check("unrelated tool has no opinion", s.resolve_permission("read_file", {"path": "x"}) is None)

    s = settings_with({"*": "allow"})
    check("wildcard applies", s.resolve_permission("anything", {}) == "allow")

    print("\n=== argument-aware rules ===")
    s = settings_with({"run_bash:git *": "allow", "run_bash": "ask"})
    check("git command allowed", s.resolve_permission("run_bash", {"command": "git status"}) == "allow")
    check("git commit allowed", s.resolve_permission("run_bash", {"command": "git commit -m x"}) == "allow")
    check("non-git falls back to the tool rule",
          s.resolve_permission("run_bash", {"command": "rm -rf /"}) == "ask", )

    s = settings_with({"run_bash:git push *": "ask", "run_bash:git *": "allow"})
    check("more specific pattern wins",
          s.resolve_permission("run_bash", {"command": "git push origin main"}) == "ask")
    check("less specific still applies to the rest",
          s.resolve_permission("run_bash", {"command": "git status"}) == "allow")

    print("\n=== a bare word means the command, not a prefix substring ===")
    s = settings_with({"run_bash:git": "allow"})
    check("'git' allows 'git status'", s.resolve_permission("run_bash", {"command": "git status"}) == "allow")
    check("'git' does NOT allow 'gitleaks'",
          s.resolve_permission("run_bash", {"command": "gitleaks detect"}) is None)
    check("'git' does NOT allow a chained command that merely starts with git-like text",
          s.resolve_permission("run_bash", {"command": "github-cli push"}) is None)

    print("\n=== file tools match on path ===")
    s = settings_with({"write_file:*.md": "allow", "write_file": "deny"})
    check("markdown write allowed", s.resolve_permission("write_file", {"path": "docs/x.md"}) == "allow")
    check("other write denied", s.resolve_permission("write_file", {"path": "src/x.py"}) == "deny")

    print("\n=== layer priority is unchanged and absolute ===")
    # permission_layers is ordered lowest -> highest priority, and reversed() is used when resolving.
    system = {"*": "deny"}
    project = {"run_bash": "allow"}
    s = settings_with(project, system)
    check("a higher layer's wildcard beats a lower layer's specific allow",
          s.resolve_permission("run_bash", {"command": "ls"}) == "deny",
          s.resolve_permission("run_bash", {"command": "ls"}))
    s2 = settings_with(system, project)
    check("a higher layer's specific allow beats a lower wildcard deny",
          s2.resolve_permission("run_bash", {"command": "ls"}) == "allow")

    print("\n=== the tool boundary honours the rules ===")
    from myriaxon.tool_context import ToolContext
    from myriaxon import tools as tools_module
    from myriaxon.tools import confirm_action
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    console = Console(quiet=True)
    panel = Panel(Text("x"))

    # The real prompt reads stdin, which does not exist in a scripted run -- it raised EOFError, which
    # is itself proof that an "ask" rule still reaches the prompt. Stub it so the test can also
    # control the answer and assert on both outcomes.
    asked = {"count": 0, "answer": "n"}

    class _StubPrompt:
        @staticmethod
        def ask(*args, **kwargs):
            asked["count"] += 1
            return asked["answer"]

    original_prompt = tools_module.Prompt
    tools_module.Prompt = _StubPrompt

    def context(settings, mode="auto", yolo=True):
        ctx = ToolContext(client=None, model="m", max_tokens=1, console=console, yolo=yolo, tools=[])
        ctx.settings = settings
        ctx.mode = mode
        return ctx

    try:
        # deny must hold with yolo on -- the property that makes a policy engine worth having
        ctx = context(settings_with({"run_bash": "deny"}), yolo=True)
        proceed, blocked = confirm_action(console, panel, ctx, "run_bash", command="rm -rf /", tool_input={"command": "rm -rf /"})
        check("deny blocks even with yolo on", proceed is False and blocked is not None, (proceed, blocked))
        check("the block reason names the policy", blocked and "policy" in blocked.lower(), blocked)
        check("deny never prompts", asked["count"] == 0, asked["count"])

        # allow must bypass the prompt
        asked["count"] = 0
        ctx = context(settings_with({"run_bash:git *": "allow"}), yolo=False)
        proceed, blocked = confirm_action(console, panel, ctx, "run_bash", command="git status",
                                          tool_input={"command": "git status"})
        check("an allow rule bypasses the prompt with yolo off", proceed is True and blocked is None)
        check("allow never prompts", asked["count"] == 0, asked["count"])

        # ask must keep asking even with yolo on
        asked["count"] = 0
        ctx = context(settings_with({"run_bash:git push *": "ask", "run_bash": "allow"}), yolo=True)
        proceed, blocked = confirm_action(console, panel, ctx, "run_bash", command="git push origin main",
                                          tool_input={"command": "git push origin main"})
        check("an ask rule keeps asking even with yolo on", proceed is False, proceed)
        check("it actually showed a prompt", asked["count"] == 1, asked["count"])

        # ...and the user saying yes lets it through
        asked["count"] = 0
        asked["answer"] = "y"
        ctx = context(settings_with({"run_bash:git push *": "ask", "run_bash": "allow"}), yolo=True)
        proceed, blocked = confirm_action(console, panel, ctx, "run_bash", command="git push origin main",
                                          tool_input={"command": "git push origin main"})
        check("answering yes proceeds", proceed is True and blocked is None)
        asked["answer"] = "n"

        # an ask rule must NOT gate a command it does not cover
        asked["count"] = 0
        ctx = context(settings_with({"run_bash:git push *": "ask", "run_bash": "allow"}), yolo=True)
        proceed, blocked = confirm_action(console, panel, ctx, "run_bash", command="git status",
                                          tool_input={"command": "git status"})
        check("a command covered by allow still runs without prompting", proceed is True and asked["count"] == 0)

        print("\n=== the deny message tells the user what to do ===")
        ctx = context(settings_with({"write_file": "deny"}), yolo=True)
        proceed, blocked = confirm_action(console, panel, ctx, "write_file", tool_input={"path": "x"})
        check("deny explains it needs a settings change", blocked and "settings.json" in blocked, blocked)
    finally:
        tools_module.Prompt = original_prompt

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
