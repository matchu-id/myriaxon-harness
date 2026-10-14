"""Output-schema enforcement: a tool that declares a result shape has that shape checked.

## The gap this closes

Myriaxon validates tool *input* carefully — required arguments, JSON types, actionable error messages. It trusts
tool *output* completely. So a tool that declares it returns `{path, bytes}` can return `"oops"`, `None`, or a
dict missing `bytes`, and the only thing that notices is the model, which has no way to know the shape was
promised rather than merely implied.

DeepSeek's `defineTool` compiles the author's schema *and separately* compiles and enforces a canonical OUTPUT
schema against every successful body (verified in `core/tools/src/schema.ts`). Their reason for doing both is
that a declared shape is a contract the harness can hold the tool to, and the model should be able to rely on it.

## What enforcement does when it fails

Two available postures, and the choice matters:

* **replace the result with an error.** Honest, and loses whatever the tool did manage to produce.
* **keep the result and attach the violation.** The model gets the data *and* knows it is malformed, which is
  usually what it wants: a tool returning a dict with one missing key still carries useful context.

This module takes the second as the default and offers the first. Attaching rather than replacing, because the
common failure is a *partially* correct result and throwing it away to prove a point makes the model's next step
harder, not safer. The violation is stated in a form the model can act on, in the same spirit as
`coach_argument_error`.

## Why enforcement is opt-in per tool

A schema that was written carelessly, or one on a legacy tool, would start failing every call the moment this
became global — turning a *documentation* improvement into an outage. A tool declares
`output_schema`, and only a declared schema is enforced, so the blast radius of adding this is exactly the set of
tools whose authors asked for it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# The JSON Schema subset enforced on output. Deliberately the same vocabulary the input validator uses, so a tool
# author learns one set of rules.
_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}

# `bool` is a subclass of `int` in Python, so an `integer` field would accept `True` without this. A schema that
# says integer and receives `True` is not satisfied, and silently accepting it is the kind of looseness that makes
# a validator decorative.
_NUMERIC_EXCLUSIONS = {"integer": bool, "number": bool}


@dataclass
class Violation:
    """One way a result failed its declared schema."""

    path: str
    problem: str

    def render(self) -> str:
        return f"{self.path or '(root)'}: {self.problem}"


@dataclass
class Verdict:
    """The outcome of checking one result against one schema."""

    ok: bool
    violations: list[Violation] = field(default_factory=list)
    checked: bool = False

    def render(self) -> str:
        if not self.checked:
            return "no output schema declared"
        if self.ok:
            return "result matches its declared schema"
        lines = [f"the tool's result does not match its declared schema ({len(self.violations)} problem(s)):"]
        for violation in self.violations[:12]:
            lines.append(f"  - {violation.render()}")
        if len(self.violations) > 12:
            lines.append(f"  ... and {len(self.violations) - 12} more")
        lines.append(
            "The result above is still shown, because a partial result is usually more use than none. Treat "
            "the listed fields as unreliable and say so rather than relying on them."
        )
        return "\n".join(lines)


def _type_name(value: Any) -> str:
    for name, python_type in _TYPES.items():
        if name in ("integer", "number") and isinstance(value, bool):
            return "boolean"
        if isinstance(value, python_type):
            return name
    return type(value).__name__


def check(value: Any, schema: Any, path: str = "") -> list[Violation]:
    """Validate `value` against a JSON Schema subset. Returns every violation, in walk order.

    Reports *all* problems rather than the first, matching the input validator: a tool author fixing one field at
    a time, one round trip each, is the failure mode a single-error validator produces.
    """
    violations: list[Violation] = []
    if not isinstance(schema, dict) or not schema:
        return violations

    expected = schema.get("type")
    if expected:
        allowed = _TYPES.get(str(expected))
        if allowed is not None:
            excluded = _NUMERIC_EXCLUSIONS.get(str(expected))
            if excluded is not None and isinstance(value, excluded):
                violations.append(Violation(path, f"expected {expected}, got {_type_name(value)}"))
                return violations
            if not isinstance(value, allowed):
                violations.append(Violation(
                    path, f"expected {expected}, got {_type_name(value)}"
                ))
                # A type mismatch makes every deeper check meaningless, so it returns here rather than
                # producing a cascade of confusing secondary errors.
                return violations

    if "enum" in schema and value not in schema["enum"]:
        violations.append(Violation(path, f"expected one of {schema['enum']!r}, got {value!r}"))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            violations.append(Violation(path, f"shorter than minLength {schema['minLength']}"))
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            violations.append(Violation(path, f"longer than maxLength {schema['maxLength']}"))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            violations.append(Violation(path, f"below minimum {schema['minimum']}"))
        if "maximum" in schema and value > schema["maximum"]:
            violations.append(Violation(path, f"above maximum {schema['maximum']}"))

    if isinstance(value, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                violations.append(Violation(path, f"missing required key {key!r}"))
        properties = schema.get("properties") or {}
        if isinstance(properties, dict):
            for key, subschema in properties.items():
                if key in value:
                    violations.extend(check(value[key], subschema, f"{path}.{key}" if path else key))

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                violations.extend(check(item, items, f"{path}[{index}]"))
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            violations.append(Violation(path, f"fewer than minItems {schema['minItems']}"))
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            violations.append(Violation(path, f"more than maxItems {schema['maxItems']}"))

    return violations


def coerce(text: str) -> Any:
    """Best-effort parse of a tool's text result into a value for validation.

    A tool returns a string. If it looks like JSON, it is parsed so the schema can be checked against the
    structure the tool meant; otherwise the string itself is the value, which is correct for a schema of
    `{"type": "string"}` and a violation for anything else.

    Never raises: a tool returning invalid JSON is a violation, not a crash in the validator.
    """
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    if not stripped:
        return text
    if stripped[0] in "{[" or stripped in ("null", "true", "false"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return text
    return text


def check_tool_result(text: str, schema: Any, *, tool_name: str = "") -> Verdict:
    """Check a tool's text result against its declared output schema.

    `schema` being falsy means the tool declared none, and the verdict says `checked=False` rather than `ok=True`.
    The distinction is load-bearing: "this result was validated" and "there was nothing to validate against" are
    different statements, and a caller that cannot tell them apart will overstate what was checked.
    """
    if not schema:
        return Verdict(ok=True, checked=False)
    value = coerce(text)
    violations = check(value, schema)
    return Verdict(ok=not violations, violations=violations, checked=True)


def annotate(text: str, verdict: Verdict) -> str:
    """Append a violation report to the result text.

    Appends rather than replaces: the common failure is a partially correct result, and discarding it to prove a
    point makes the model's next step harder rather than safer.
    """
    if not verdict.checked or verdict.ok:
        return text
    return f"{text}\n\n[output-schema] {verdict.render()}"


def output_schema_of(tool_def: dict) -> dict:
    """A tool definition's declared output schema, in either provider shape.

    Both `output_schema` and `returns` are accepted because the two are in use across the codebase and a tool
    author should not have to know which one this module reads.
    """
    if not isinstance(tool_def, dict):
        return {}
    for candidate in (tool_def, tool_def.get("function")):
        if not isinstance(candidate, dict):
            continue
        for key in ("output_schema", "returns"):
            schema = candidate.get(key)
            if isinstance(schema, dict) and schema:
                return schema
    return {}
