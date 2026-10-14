#!/usr/bin/env python3
"""Verify the role layer: portable templates that a Locus is created from.

The gap this closes: a Locus was created imperatively and existed only in one machine's `loci.json`. There was no
portable role file, no shipped starting set, and no team composition -- a fresh install had an empty registry and
the answer to "what does a reviewer do here?" was whatever happened to be in that file.

The load-bearing check is section 5: a role that does not say what it hands back must be REFUSED, not installed.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import loci as D  # noqa: E402
from myriaxon import roles as R  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def write_role(directory: Path, name: str, body: str, **frontmatter) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fields = {"name": name, "description": f"the {name} role"}
    fields.update({k: str(v) for k, v in frontmatter.items()})
    text = "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "---\n" + body
    path = directory / f"{name}.md"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


GOOD_BODY = """
## Scope and trigger
Own the artefact. Do not own publication.

## On a task
1. Read the brief.
2. Verify the claim.
3. Check the result is readable.

## Handoff contract
Return the artefact path, the checks performed, and any uncertainty.

## Escalation
Stop and ask when the source is unavailable.
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp())

    print("=== 1. the shipped roles all exist and validate ===")
    available = R.discover_roles()
    check("all four built-in roles are discovered",
          set(available) >= {"coordinator", "researcher", "reviewer", "writer"},
          str(sorted(available)))
    for name, role in sorted(available.items()):
        report = R.validate_role(role)
        check(f"built-in '{name}' passes validation", report.ok,
              "; ".join(report.errors) or "no errors")

    print()
    print("=== 2. every shipped role answers the four questions the format requires ===")
    required = ("scope and trigger", "on a task", "handoff contract", "escalation")
    for name, role in sorted(available.items()):
        lowered = role.body.lower()
        missing = [s for s in required if s.split()[0] not in lowered]
        check(f"'{name}' has all four sections", not missing, f"missing: {missing}")

    print()
    print("=== 3. a role loads from a project directory and overrides the built-in ===")
    project = tmp / "project"
    write_role(project / ".myriaxon" / "roles", "reviewer", GOOD_BODY.replace("Own the artefact", "PROJECT OVERRIDE"))
    project_roles = R.discover_roles(project_root=project)
    check("the project copy won", "PROJECT OVERRIDE" in project_roles["reviewer"].body,
          project_roles["reviewer"].body[:60].replace("\n", " "))
    check("the other built-ins still resolve", "coordinator" in project_roles)

    print()
    print("=== 4. precedence is builtin -> user -> project ===")
    check("the built-in source is inside the package",
          "builtin_roles" in str(available["reviewer"].source))

    print()
    print("=== 5. THE FIX: a role with no handoff contract is REFUSED ===")
    # This is the reason the format exists. A role that does not say what it hands back produces work nobody
    # can accept or reject on its merits, and the user would have no reason to suspect the template.
    incomplete = R.Role(
        name="vague",
        description="does something, probably",
        body="## Scope and trigger\nOwn stuff.\n\n## On a task\n1. Do the thing.\n",
        source=tmp / "vague.md",
    )
    report = R.validate_role(incomplete)
    check("it fails validation", not report.ok)
    check("the missing handoff contract is named",
          any("handoff" in e.lower() for e in report.errors), "; ".join(report.errors))
    check("the missing escalation is named",
          any("escalat" in e.lower() for e in report.errors))
    check("the error explains why it matters",
          any("hands back" in e or "hand back" in e for e in report.errors))

    print()
    print("=== 6. a malformed role cannot create a Locus ===")
    registry = D.reset_registry(tmp / "loci.json")
    locus, report = R.install_role(incomplete, registry)
    check("install returns no Locus", locus is None)
    check("the registry is still empty", registry.all() == [],
          str([d.name for d in registry.all()]))

    print()
    print("=== 7. a valid role installs a Locus with the role's instructions ===")
    good = R.Role(name="reviewer", description="review things", body=GOOD_BODY, source=tmp / "good.md")
    locus, report = R.install_role(good, registry)
    check("a Locus was created", locus is not None, "; ".join(report.errors))
    check("its role is recorded", locus.role == "reviewer", locus.role)
    check("its instructions are the role body", "Handoff contract" in locus.instructions)
    check("it starts with NO goals", locus.goals == [],
          "a role says what kind of work this is, not what this install wants done")
    check("it is not proactive by default", not locus.proactive)
    check("it exists in the registry", registry.get("reviewer") is not None)
    check("the creation is logged with its source", any("role" in a.summary for a in locus.activity))

    print()
    print("=== 8. --goal adds an objective; the role itself carries none ===")
    registry2 = D.reset_registry(tmp / "loci2.json")
    locus2, _ = R.install_role(good, registry2, name="rev2", goals=["review the parser change"])
    check("the objective was attached", len(locus2.goals) == 1, str([g.title for g in locus2.goals]))
    check("the shipped role still has no goals of its own",
          "goals" not in R.role_to_dot_kwargs(good))

    print()
    print("=== 9. naming: installing twice reports a collision instead of crashing ===")
    # The first version of `install_role` let LocusRegistry's ValueError escape, so a second install raised a
    # traceback out of the CLI loop instead of reporting one failed member and continuing.
    duplicate, dup_report = R.install_role(good, registry2, name="rev2")
    check("a duplicate install returns no Locus", duplicate is None)
    check("it reports rather than raising", not dup_report.ok)
    check("the message names the collision",
          any("already exists" in e for e in dup_report.errors), str(dup_report.errors))
    check("the original Locus is untouched", registry2.get("rev2") is not None)
    check("its goal survived", len(registry2.get("rev2").goals) == 1)

    print()
    print("=== 10. the default team installs, and reports per-member failures ===")
    registry3 = D.reset_registry(tmp / "loci3.json")
    created, errors = R.install_team(registry3, available)
    check("the team installed", len(created) == 4, f"created {[d.name for d in created]}, errors {errors}")
    check("no errors for a clean install", errors == [], str(errors))
    check("each member has its own role",
          sorted(d.role for d in created) == ["coordinator", "researcher", "reviewer", "writer"],
          str(sorted(d.role for d in created)))
    again, errors2 = R.install_team(registry3, available)
    check("a second install reports collisions instead of duplicating",
          again == [] and len(errors2) == 4, str(errors2))

    print()
    print("=== 11. a bad role does not prevent the rest of the team ===")
    registry4 = D.reset_registry(tmp / "loci4.json")
    mixed = dict(available)
    mixed["broken"] = incomplete
    created, errors = R.install_team(registry4, mixed, members=["reviewer", "broken", "writer"])
    check("the two good roles installed", len(created) == 2, str([d.name for d in created]))
    check("the bad one is reported by name", any("broken" in e for e in errors), str(errors))

    print()
    print("=== 12. proactive roles become wakable; the rest do not ===")
    registry5 = D.reset_registry(tmp / "loci5.json")
    pro = R.Role(name="watcher", description="watches", body=GOOD_BODY,
                 source=tmp / "p.md", proactive=True, interval_seconds=600)
    locus5, _ = R.install_role(pro, registry5, goals=["watch the deploy"])
    check("a proactive role is installed proactively", locus5.proactive)
    check("its interval is used", locus5.interval_seconds == 600, str(locus5.interval_seconds))
    check("and it is a wake-up candidate", [d.name for d in registry5.headless()] == ["watcher"],
          str([d.name for d in registry5.headless()]))

    print()
    print("=== 13. interval below the Locus floor warns rather than failing ===")
    fast = R.Role(name="fast", description="too fast", body=GOOD_BODY,
                  source=tmp / "f.md", proactive=True, interval_seconds=5)
    report = R.validate_role(fast)
    check("it still validates", report.ok, "; ".join(report.errors))
    check("but warns about the floor", any("30s" in w for w in report.warnings), str(report.warnings))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
