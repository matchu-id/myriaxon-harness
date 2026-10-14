#!/usr/bin/env python3
"""Verify output-schema enforcement.

The claim under test is that a DECLARED shape is held to, and that an undeclared one is not silently reported as
validated. The second half matters as much as the first: "checked and fine" and "nothing to check against" are
different statements.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# See the note in verify_loci_character: rich is absent and there is no network, so a documented test double stands in
# and says so, rather than letting the import line kill the run before any assertion.
if importlib.util.find_spec("rich") is None:
    sys.path.insert(0, str(REPO / "tools" / "_testdoubles"))
    print("  [NOTE] rich is absent; using the test double in tools/_testdoubles.", flush=True)

from myriaxon import output_schema as O  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    print("=== 1. a conforming value passes ===")
    schema = {"type": "object", "required": ["path", "bytes"],
              "properties": {"path": {"type": "string"}, "bytes": {"type": "integer"}}}
    verdict = O.check_tool_result('{"path": "/a/b", "bytes": 12}', schema)
    check("it is checked", verdict.checked)
    check("it passes", verdict.ok, verdict.render())
    check("the rendering says so", "matches" in verdict.render(), verdict.render())

    print()
    print("=== 2. THE FIX: a missing required key is reported ===")
    verdict = O.check_tool_result('{"path": "/a/b"}', schema)
    check("it fails", not verdict.ok, verdict.render())
    check("the missing key is named",
          any("bytes" in v.render() for v in verdict.violations), verdict.render())
    check("the path is the root", verdict.violations[0].path == "", verdict.violations[0].render())

    print()
    print("=== 3. a wrong type is reported as a type mismatch ===")
    verdict = O.check_tool_result('{"path": "/a/b", "bytes": "twelve"}', schema)
    check("it fails", not verdict.ok)
    check("the field is named", any("bytes" in v.render() for v in verdict.violations), verdict.render())
    check("expected and actual are both shown",
          any("integer" in v.render() and "string" in v.render() for v in verdict.violations),
          verdict.render())

    print()
    print("=== 4. every violation is reported, not just the first ===")
    # A tool author fixing one field per round trip is the failure mode a single-error validator produces.
    verdict = O.check_tool_result('{"bytes": "x"}', schema)
    check("more than one violation", len(verdict.violations) >= 2, verdict.render())

    print()
    print("=== 5. a type mismatch does NOT cascade into nonsense ===")
    # Once the type is wrong, every deeper check is meaningless. Reporting "missing key 'x'" for a value that is
    # a string would be a confusing secondary error.
    verdict = O.check_tool_result('"just a string"', schema)
    check("exactly one violation", len(verdict.violations) == 1, verdict.render())
    check("it is the type mismatch", "expected object" in verdict.violations[0].render(),
          verdict.violations[0].render())

    print()
    print("=== 6. THE FIX: bool is NOT accepted where an integer is required ===")
    # `bool` subclasses `int` in Python, so an unguarded isinstance check accepts True for an integer field.
    # A validator that accepts `True` where it asked for a number is decorative.
    int_schema = {"type": "integer"}
    check("an int passes", O.check(5, int_schema) == [])
    check("a bool FAILS", O.check(True, int_schema) != [],
          "bool is a subclass of int; accepting it would make the check decorative")
    check("and the violation says boolean",
          "boolean" in O.check(True, int_schema)[0].render(), O.check(True, int_schema)[0].render())
    check("a bool is fine where boolean is required", O.check(True, {"type": "boolean"}) == [])
    check("a float fails an integer schema", O.check(1.5, int_schema) != [])
    check("an int fails a string schema", O.check(5, {"type": "string"}) != [])

    print()
    print("=== 7. nested structures are walked ===")
    nested = {
        "type": "object",
        "properties": {
            "files": {"type": "array", "items": {
                "type": "object", "required": ["name"],
                "properties": {"name": {"type": "string"}},
            }},
        },
    }
    verdict = O.check_tool_result('{"files": [{"name": "a"}, {"nope": 1}]}', nested)
    check("the nested failure is found", not verdict.ok, verdict.render())
    check("the path names the array index",
          any("files[1]" in v.render() for v in verdict.violations), verdict.render())

    print()
    print("=== 8. constraints are enforced ===")
    check("minLength is enforced", O.check("ab", {"type": "string", "minLength": 3}) != [])
    check("maxLength is enforced", O.check("abcd", {"type": "string", "maxLength": 3}) != [])
    check("minimum is enforced", O.check(1, {"type": "integer", "minimum": 5}) != [])
    check("maximum is enforced", O.check(9, {"type": "integer", "maximum": 5}) != [])
    check("enum is enforced", O.check("c", {"type": "string", "enum": ["a", "b"]}) != [])
    check("a permitted enum value passes", O.check("a", {"type": "string", "enum": ["a", "b"]}) == [])
    check("minItems is enforced", O.check([1], {"type": "array", "minItems": 2}) != [])
    check("maxItems is enforced", O.check([1, 2, 3], {"type": "array", "maxItems": 2}) != [])

    print()
    print("=== 9. THE DISTINCTION: no schema is NOT the same as validated ===")
    verdict = O.check_tool_result("anything", {})
    check("ok is true, because nothing failed", verdict.ok)
    check("but checked is FALSE", not verdict.checked,
          "'validated' and 'nothing to validate against' are different statements")
    check("the rendering says so", "no output schema" in verdict.render(), verdict.render())
    check("a None schema behaves the same", not O.check_tool_result("x", None).checked)

    print()
    print("=== 10. THE FIX: a violation is ATTACHED, not substituted ===")
    # The common failure is a partially correct result, and discarding it to prove a point makes the model's next
    # step harder rather than safer.
    annotated = O.annotate('{"path": "/a"}', O.check_tool_result('{"path": "/a"}', schema))
    check("the original text survives", annotated.startswith('{"path": "/a"}'), annotated[:60])
    check("the violation is appended", "[output-schema]" in annotated, annotated[:160])
    check("it names the missing field", "bytes" in annotated)
    check("it tells the model what to do", "unreliable" in annotated, annotated[-200:])
    check("a passing result is NOT annotated",
          O.annotate("fine", O.check_tool_result("fine", {"type": "string"})) == "fine",
          "annotating a correct result would be noise")
    check("an unchecked result is NOT annotated",
          O.annotate("fine", O.check_tool_result("fine", {})) == "fine")

    print()
    print("=== 11. coercion parses JSON when the text is JSON ===")
    check("an object parses", O.coerce('{"a": 1}') == {"a": 1})
    check("an array parses", O.coerce("[1, 2]") == [1, 2])
    check("null parses", O.coerce("null") is None)
    check("a bare word stays a string", O.coerce("hello") == "hello")
    check("malformed JSON stays a string", O.coerce("{oops") == "{oops")
    check("an empty string stays", O.coerce("") == "")
    check("whitespace is trimmed for the decision", O.coerce('  {"a": 1}  ') == {"a": 1})
    check("a non-string passes through", O.coerce(5) == 5)

    print()
    print("=== 12. a schema of type string accepts a plain text result ===")
    verdict = O.check_tool_result("just some text", {"type": "string"})
    check("it passes", verdict.ok, verdict.render())

    print()
    print("=== 13. the schema is read from either provider shape ===")
    check("a top-level output_schema is found",
          O.output_schema_of({"output_schema": {"type": "string"}}) == {"type": "string"})
    check("a nested function shape is found",
          O.output_schema_of({"function": {"output_schema": {"type": "string"}}}) == {"type": "string"})
    check("`returns` is accepted as a synonym",
          O.output_schema_of({"returns": {"type": "string"}}) == {"type": "string"})
    check("a definition with none returns empty", O.output_schema_of({"name": "x"}) == {})
    check("a non-dict returns empty", O.output_schema_of("nope") == {})
    check("an empty schema is treated as none",
          O.output_schema_of({"output_schema": {}}) == {},
          "an empty schema declares nothing, so it must not report itself as validated")

    print()
    print("=== 14. WIRING: the live tool registry declares no output schemas yet ===")
    # Reported, not asserted as a requirement. Whether a tool declares its output shape is a policy choice, and
    # this states the current answer so the matrix can quote a measured number rather than a guess.
    from myriaxon import tool_registry as TR

    declared = []
    for definition in TR.build_tool_defs():
        name = TR._def_name(definition)
        if O.output_schema_of(definition):
            declared.append(name)
    check("the registry can be scanned without error", True,
          f"{len(declared)} of {len(TR.build_tool_defs())} tools declare an output schema: {declared or 'none'}")
    check("enforcement is opt-in, so adding it breaks nothing",
          all(not O.check_tool_result("anything", O.output_schema_of(d)).checked
              for d in TR.build_tool_defs()),
          "only a DECLARED schema is enforced; an undeclared one is reported as unchecked")

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
