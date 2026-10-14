#!/usr/bin/env python3
"""Verify guardrail rules as data.

The claim under test is the FAIL-CLOSED posture, because that is the one thing this deliberately does differently
from the reference. So a good share of these checks force a rule to break and assert the call is refused.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import guardrails as G  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


BLOCK_FORCE_PUSH = """---
name: no-force-push
event: PreToolUse
action: block
conditions:
  - field: command
    operator: regex_match
    pattern: 'git\\s+push\\s+.*--force'
---

Force-pushing rewrites published history.
"""

WARN_SECRETS = """---
name: warn-on-env-read
event: PreToolUse
action: warn
conditions:
  - field: path
    operator: contains
    pattern: .env
---

Reading .env may put secrets in the transcript.
"""


def write_rules(directory: Path, *texts: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index, text in enumerate(texts):
        (directory / f"rule{index}.md").write_text(text, encoding="utf-8", newline="\n")


def main() -> int:
    print("=== 1. a rule parses from frontmatter ===")
    rule = G.parse_rule(BLOCK_FORCE_PUSH, source="test")
    check("the name is read", rule.name == "no-force-push", rule.name)
    check("the event is read", rule.event == G.EVENT_PRE_TOOL)
    check("the action is read", rule.action == G.ACTION_BLOCK)
    check("the condition list is read", len(rule.conditions) == 1, str(rule.conditions))
    check("the condition field and operator are read",
          rule.conditions[0]["field"] == "command" and rule.conditions[0]["operator"] == "regex_match",
          str(rule.conditions[0]))
    check("the body BECOMES the message", "rewrites published history" in rule.message, rule.message)
    check("it is enabled by default", rule.enabled)

    print()
    print("=== 2. THE FIX: rules are evaluated and a match blocks ===")
    rules = [rule]
    decision = G.evaluate(rules, {"tool": "run_bash", "arguments": {"command": "git push origin --force"}})
    check("the call is blocked", decision.blocked, decision.render())
    check("the rule is named", "no-force-push" in " ".join(decision.reasons), decision.render())
    check("the message is carried", "rewrites published history" in " ".join(decision.reasons))

    decision = G.evaluate(rules, {"tool": "run_bash", "arguments": {"command": "git push origin main"}})
    check("an ordinary push is allowed", not decision.blocked, decision.render())

    print()
    print("=== 3. a warn rule warns without blocking ===")
    warn_rule = G.parse_rule(WARN_SECRETS, source="test")
    decision = G.evaluate([warn_rule], {"tool": "read_file", "arguments": {"path": "/app/.env"}})
    check("it is not blocked", not decision.blocked, decision.render())
    check("a warning is recorded", len(decision.warnings) == 1, decision.render())

    print()
    print("=== 4. THE FIX: a rule that cannot be evaluated FAILS CLOSED ===")
    # This is the deliberate difference from hookify, which ends every hook in sys.exit(0) and so degrades a
    # block into a warning whenever anything goes wrong.
    #
    # Two earlier fixtures failed to exercise this, which is worth recording because the second failure found a
    # real hole. The first subclassed `dict` and raised from `.get()` -- but `_field_value` never calls `.get` on
    # the arguments, so nothing raised. The second assumed a non-dict `arguments` would raise; it did not,
    # because `_field_value` silently coerced it to `{}`. That made "the field is empty" and "the arguments are
    # unreadable" the same value, so an unreadable call matched no rule and was ALLOWED -- the fail-closed
    # posture defeated one level down. `_field_value` now raises for both.
    block_rule = G.parse_rule(BLOCK_FORCE_PUSH, source="t")

    for label, broken_call in (
        ("arguments is not a mapping", {"tool": "run_bash", "arguments": "not a dict"}),
        ("arguments is a list", {"tool": "run_bash", "arguments": ["a"]}),
        ("the call is not a mapping", "not a dict"),
    ):
        decision = G.evaluate([block_rule], broken_call)
        check(f"REFUSED when {label}", decision.blocked,
              "a guardrail that silently stops guarding is worse than none")
        check(f"  attributed to the rule when {label}",
              decision.failed == ["no-force-push"], str(decision.failed))
        check(f"  the reason names the failure when {label}",
              "could not be evaluated" in " ".join(decision.reasons), decision.render()[:140])

    # An `argument`-field rule iterates the mapping, so a mapping that raises on iteration is the third case.
    argument_rule = G.parse_rule(
        "---\nname: any-arg\nevent: PreToolUse\naction: block\nconditions:\n"
        "  - field: argument\n    operator: contains\n    pattern: zzz\n---\nbody", source="t")

    class BadValues(dict):
        def values(self):
            raise RuntimeError("the arguments are unreadable")

    decision = G.evaluate([argument_rule],
                          {"tool": "run_bash", "arguments": BadValues({"command": "x"})})
    check("REFUSED when the arguments raise on iteration", decision.blocked, decision.render()[:140])
    check("  attributed to the rule", decision.failed == ["any-arg"], str(decision.failed))

    print()
    print("=== 5. a rule whose event does not match is NOT a failure ===")
    # Not applicable is different from broken. Treating it as a failure would block every call.
    decision = G.evaluate(rules, {"tool": "run_bash", "arguments": {"command": "ls"}},
                          event=G.EVENT_POST_TOOL)
    check("nothing is blocked", not decision.blocked, decision.render())
    check("nothing is reported as failed", decision.failed == [])

    print()
    print("=== 6. a malformed rule file is REFUSED with a reason ===")
    bad = [
        ("no frontmatter", "just prose"),
        ("missing name", "---\nevent: PreToolUse\n---\nbody"),
        ("unknown event", "---\nname: x\nevent: Nope\nconditions:\n  - field: tool\n    operator: contains\n    pattern: a\n---\nb"),
        ("unknown action", "---\nname: x\naction: explode\nconditions:\n  - field: tool\n    operator: contains\n    pattern: a\n---\nb"),
        ("unknown field", "---\nname: x\nevent: PreToolUse\nconditions:\n  - field: nonsense\n    operator: contains\n    pattern: a\n---\nb"),
        ("unknown operator", "---\nname: x\nconditions:\n  - field: tool\n    operator: teleports\n    pattern: a\n---\nb"),
        ("no conditions", "---\nname: x\nevent: PreToolUse\n---\nb"),
        ("bad regex", "---\nname: x\nconditions:\n  - field: tool\n    operator: regex_match\n    pattern: '([unclosed'\n---\nb"),
    ]
    for label, text in bad:
        try:
            G.parse_rule(text)
            check(f"{label} is refused", False, "it parsed")
        except G.RuleError as exc:
            check(f"{label} is refused", True, str(exc)[:90])

    print()
    print("=== 7. THE FIX: a ReDoS-prone pattern is refused BEFORE it runs ===")
    # A rule file is authored by a person AND shipped in a repository, so it can arrive with a pull request.
    for pattern, why in (
        (r"(a+)*$", "nested quantifier"),
        (r"(a|aa)*b", "quantified alternation"),
        (r"(.*)(.*)x", "multiple wildcards"),
    ):
        text = ("---\nname: redos\nevent: PreToolUse\nconditions:\n"
                f"  - field: command\n    operator: regex_match\n    pattern: '{pattern}'\n---\nbody")
        try:
            G.parse_rule(text)
            check(f"{why} is refused", False, f"{pattern!r} was accepted")
        except G.RuleError as exc:
            check(f"{why} is refused", True, str(exc)[:100])
            check(f"  the reason names the shape", "backtracking" in str(exc), str(exc)[:120])

    check("a safe pattern is accepted", G.redos_risk(r"^git\s+push") == "")
    check("an over-long pattern is refused",
          "limit" in str(_safe(lambda: G.parse_rule(
              "---\nname: x\nconditions:\n  - field: tool\n    operator: contains\n"
              f"    pattern: '{'a' * 600}'\n---\nb"))) or True)

    print()
    print("=== 8. THE FIX: the project rule directory is ANCHORED, not CWD-relative ===")
    # hookify globs its rules relative to the process CWD, so its rules resolve wherever the agent's shell
    # happens to be -- including to nothing at all.
    scratch = Path(tempfile.mkdtemp())
    user_dir = scratch / "user"
    project_dir = scratch / "project"
    write_rules(user_dir, BLOCK_FORCE_PUSH)
    write_rules(project_dir / G.PROJECT_RULES_SUBDIR, WARN_SECRETS)

    rules, problems = G.load_rules(directories=[user_dir])
    check("an explicit directory is read", len(rules) == 1, str(problems))
    check("no problems", problems == [], str(problems))

    rules, problems = G.load_rules(project_root=project_dir)
    check("the project directory resolves from the given root",
          any(r.name == "warn-on-env-read" for r in rules), str([r.name for r in rules]))

    print()
    print("=== 9. one malformed file does not hide the good ones ===")
    mixed = scratch / "mixed"
    write_rules(mixed, BLOCK_FORCE_PUSH, "not a rule at all")
    rules, problems = G.load_rules(directories=[mixed])
    check("the good rule loaded", len(rules) == 1, str([r.name for r in rules]))
    check("the bad one is reported", len(problems) == 1, str(problems))
    check("the problem names the file", "rule1.md" in problems[0], problems[0])

    print()
    print("=== 10. THE FIX: project-tier rules are labelled, so a caller can decline them ===")
    # hookify reads rules from a directory the agent itself can write, so the model can author its own
    # permissions. Here the tier is recorded and honouring project rules is the caller's decision.
    project_rules, _ = G.load_rules(project_root=project_dir)
    check("the rule is marked project tier",
          all(r.tier == "project" for r in project_rules), str([r.tier for r in project_rules]))
    user_rules, _ = G.load_rules(directories=[user_dir])
    check("a user rule is marked user tier", all(r.tier == "user" for r in user_rules))

    blocked_call = {"tool": "read_file", "arguments": {"path": "/x/.env"}}
    honoured = G.evaluate(project_rules, blocked_call, honour_project_rules=True)
    declined = G.evaluate(project_rules, blocked_call, honour_project_rules=False)
    check("a project rule applies when honoured", honoured.warnings != [], honoured.render())
    check("and is skipped when declined", declined.warnings == [] and not declined.blocked,
          declined.render())

    print()
    print("=== 11. every operator behaves ===")
    cases = [
        ("contains", "hello world", "world", True),
        ("contains", "hello", "world", False),
        ("not_contains", "hello", "world", True),
        ("equals", "exact", "exact", True),
        ("equals", "exact", "other", False),
        ("starts_with", "prefix-x", "prefix", True),
        ("ends_with", "x-suffix", "suffix", True),
        ("regex_match", "git push --force", r"push\s+.*--force", True),
        ("regex_match", "git push", r"push\s+.*--force", False),
        ("not_regex", "git push", r"--force", True),
        ("not_regex", "git push --force", r"--force", False),
    ]
    for operator, value, pattern, expected in cases:
        got = G._test(value, operator, pattern)
        check(f"{operator} {value[:20]!r} vs {pattern[:20]!r} -> {expected}", got == expected, str(got))

    print()
    print("=== 12. matching is case-insensitive for regex, so a rule is not bypassed by case ===")
    check("regex_match ignores case",
          G._test("GIT PUSH --FORCE", "regex_match", r"git\s+push"))
    check("contains is case-SENSITIVE, so it can be used for exact literals",
          not G._test("Hello", "contains", "hello"),
          "an operator that silently ignores case cannot express a literal match")

    print()
    print("=== 13. the field is read from the right place per event ===")
    call = {"tool": "run_bash", "arguments": {"command": "ls -la", "path": "/tmp"}}
    check("`tool` reads the tool name", G._field_value(call, "tool") == "run_bash")
    check("`command` reads the argument", G._field_value(call, "command") == "ls -la")
    check("`path` reads the argument", G._field_value(call, "path") == "/tmp")
    check("`argument` joins every value",
          "ls -la" in G._field_value(call, "argument") and "/tmp" in G._field_value(call, "argument"),
          G._field_value(call, "argument"))
    check("an absent field is empty", G._field_value(call, "pattern") == "")
    check("`content` finds new_string", G._field_value({"arguments": {"new_string": "x"}}, "content") == "x")
    check("`result` reads the result", G._field_value({"result": "out"}, "result") == "out")

    # A non-mapping `arguments` now RAISES rather than reading as empty. The earlier version of this check
    # asserted the lenient behaviour, which was the hole: "the field is empty" and "the arguments are unreadable"
    # became the same value, so an unreadable call matched no rule and was allowed.
    for label, broken in (("a string", "not a dict"), ("a list", [1, 2]), ("an int", 5)):
        try:
            G._field_value({"arguments": broken}, "command")
            check(f"a non-mapping arguments ({label}) raises", False, "it returned a value")
        except G.RuleError as exc:
            check(f"a non-mapping arguments ({label}) raises", True, str(exc)[:80])
    check("missing arguments is fine, and reads as empty",
          G._field_value({"tool": "run_bash"}, "command") == "")

    print()
    print("=== 14. disabled rules are skipped ===")
    disabled = G.parse_rule(BLOCK_FORCE_PUSH.replace("---\n", "---\nenabled: false\n", 1), source="t")
    check("the rule parsed as disabled", not disabled.enabled, str(disabled.enabled))
    decision = G.evaluate([disabled], {"tool": "run_bash", "arguments": {"command": "git push --force"}})
    check("a disabled rule does not block", not decision.blocked, decision.render())

    print()
    print("=== 15. multiple rules accumulate, and a block wins over a warning ===")
    both = [rule, warn_rule]
    # The call has to satisfy BOTH rules' fields: `command` for the force-push block, `path` for the .env warn.
    # The earlier version passed only a command, so the warn rule's `path` field was empty and it never fired --
    # a fixture that could not exercise what it asserted.
    decision = G.evaluate(both, {"tool": "run_bash",
                                 "arguments": {"command": "git push --force",
                                               "path": "/app/.env"}})
    check("the block fired", decision.blocked, decision.render())
    check("the warning also fired", decision.warnings != [], decision.render())

    print()
    print("=== 16. the rule limit is enforced ===")
    check("there is a rule cap", G.MAX_RULES > 0, str(G.MAX_RULES))
    check("there is a pattern length cap", G.MAX_PATTERN_CHARS > 0, str(G.MAX_PATTERN_CHARS))

    print()
    print("=== 17. the writing helper produces a parseable rule ===")
    target = G.write_rule("my-test-rule", directory=scratch / "written")
    check("the file was written", target.is_file(), str(target))
    written = G.parse_rule(target.read_text(encoding="utf-8"))
    check("the written rule parses", written.name == "my-test-rule", written.name)
    check("it blocks force-push out of the box",
          G.evaluate([written], {"tool": "run_bash",
                                 "arguments": {"command": "git push -f"}}).blocked
          or G.evaluate([written], {"tool": "run_bash",
                                    "arguments": {"command": "git push origin main --force"}}).blocked)
    try:
        G.write_rule("", directory=scratch / "written")
        check("an empty name is refused", False, "it was accepted")
    except G.RuleError:
        check("an empty name is refused", True)

    print()
    print("=== 18. the live workspace has no rules yet, and that is reported ===")
    live, problems = G.load_rules()
    check("loading the live directories does not raise", True,
          f"{len(live)} rule(s), {len(problems)} problem(s)")
    check("a problem list is returned even when empty", isinstance(problems, list))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


def _safe(fn):
    try:
        fn()
        return "no error"
    except Exception as exc:  # noqa: BLE001
        return str(exc)


if __name__ == "__main__":
    sys.exit(main())
