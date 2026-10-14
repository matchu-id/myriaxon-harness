"""Verify every objective item in docs/GAP-PLAN.md is actually present, by measurement.

Each check names the artefact that must exist and the verifier that must assert it. This is the evidence pass for
the objective, so it reads the live code rather than trusting the plan's own status markers.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
sys.path.insert(0, str(ROOT))

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(label)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def run(rel: str) -> tuple[int, str]:
    done = subprocess.run([sys.executable, str(ROOT / rel)], capture_output=True, text=True, timeout=900)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def summary(text: str) -> str:
    found = re.search(r"(\d+) passed, (\d+) failed", text)
    return f"{found.group(1)} passed / {found.group(2)} failed" if found else "NO SUMMARY"


print("=== P1.1 — trigger state persisted, `changed` kind, 16 KB cap ===")
conditions = read("myriaxon/conditions.py")
check("TriggerState exists", "class TriggerState" in conditions)
check("KIND_CHANGED exists", 'KIND_CHANGED = "changed"' in conditions)
check("MAX_STATE_BYTES is 16 KB", "MAX_STATE_BYTES = 16 * 1024" in conditions)
check("the cap is REFUSED, not truncated", "raise ValueError(problem)" in conditions)
check("commit/discard semantics exist", "def commit" in conditions and "def to_dict" in conditions)
check("from_dict tolerates junk", "def from_dict" in conditions and "isinstance(data, dict)" in conditions)
check("a dry run returns the state UNCHANGED",
      "(changed if commit else state)" in conditions,
      "returning a mutated state with commit=False would consume the change it was asked about")
loci = read("myriaxon/loci.py")
check("Locus owns the state", "trigger_state: dict" in loci)
check("commit is wired into end_run", "self.commit_trigger_state(succeeded=report.ok)" in loci)
check("private staging fields never serialize",
      'del data[name]' in loci or "if k.startswith" in loci)
rc, out = run("tools/verify_always_on.py")
check("verifier asserts it", rc == 0 and "13b" in read("tools/verify_always_on.py"), summary(out))

print()
print("=== P1.2 — `once`, completing only on a SUCCESSFUL run ===")
check("Condition.once exists", "once: bool = False" in conditions)
check("it is applied in end_run", 'self.condition.get("once")' in loci)
check("and gated on success", "if report.ok and self.condition" in loci,
      "a failed run must leave the watcher armed")
check("it is exposed in the tool schema", '"once"' in loci)
check("verifier covers the failed-run case",
      "13i. `once`" in read("tools/verify_always_on.py"),
      "the `once` semantics need a verifier, not just an inline experiment")

print()
print("=== P1.3 — silent-failure window reduced, limit pinned ===")
check("unresolvable_program exists", "def unresolvable_program" in conditions)
check("it is called before running", conditions.index("missing = unresolvable_program(self.command)")
      < conditions.index("completed = subprocess.run", conditions.index("def _evaluate_command")))
check("observe is checked too", "missing = unresolvable_program(self.observe)" in conditions)
check("it stays silent on ambiguous shapes", "SHELL_BUILTINS" in conditions
      and 'if any(token in raw for token in ("|", "&&"' in conditions)
check("the REMAINING limit is pinned",
      "13h. THE REMAINING LIMIT" in read("tools/verify_always_on.py"),
      "a limit that is not pinned can be silently reintroduced")

print()
print("=== P1.4 — loci --status shows condition, verdict, watchlist ===")
check("the condition block is rendered", "wakes on a condition:" in loci)
check("the verdict is rendered", "last check:" in loci)
check("the ERROR state is loud", '!! last check FAILED TO RUN' in loci)
check("the watchlist is rendered", "watchlist: {len(items)} item(s)" in loci)
check("an age is rendered", "def _relative_age" in loci)
rc, out = run("tools/verify_locus_status.py")
check("verifier passes", rc == 0, summary(out))

print()
print("=== W1.1 — LoC metric + tools/ audit ===")
loc = read("tools/count_loc.py")
check("check_lines column exists", '"check_lines": 0' in loc)
check("check paths are classified", "def is_check_path" in loc)
check("verify_ is a check prefix", '"verify_", "check_", "audit_", "probe_", "inventory_"' in loc)
check("_archive is excluded", '"_archive"' in loc)
check("the audit script exists", (ROOT / "tools/audit_tools.py").is_file())
check("the orphan classifier exists", (ROOT / "tools/classify_orphans.py").is_file())
check("the archive exists with a README", (ROOT / "tools/_archive/README.md").is_file())
archived = len(list((ROOT / "tools/_archive").glob("*.py")))
check("one-shots were archived", archived >= 30, f"{archived} files")
check("the one-shot wirers were deleted",
      not (ROOT / "tools/wire_heartbeat_tools.py").exists()
      and not (ROOT / "tools/wire_runtime_context.py").exists())

print()
print("=== W1.2 — doctor validates loci, conditions, guardrails ===")
health = read("myriaxon/health.py")
check("a locus-conditions check exists", "def _check_dot_conditions" in health)
check("a guardrails check exists", "def _check_guardrails" in health)
check("both are REGISTERED", "core/locus-conditions" in health and "core/guardrails" in health)
check("registration is asserted, not just existence",
      "core/locus-conditions" in read("tools/verify_doctor.py"))
rc, out = run("tools/verify_doctor.py")
check("verifier passes", rc == 0, summary(out))

print()
print("=== W1.3 — docs metric no longer strips markdown headings ===")
check("non_blank exists", "def non_blank" in loc)
check("docs use non_blank", 'result["doc_lines"] += non_blank(path)' in loc)
check("the reason is recorded", "every markdown HEADING" in loc.replace("\n", " ")
      or "HEADING also starts with" in loc)

print()
print("=== W1.4 — the stale handover is refreshed ===")
handover = read("docs/HANDOVER.md")
check("the reading order is stated", "READ THIS ORDER FIRST" in handover)
check("it says it is stale", "STALE" in handover)
check("the old 'Netra only' claim is corrected",
      "The old \"Netra only\" restriction applied to a past benchmarking task" in handover)
check("it points at the plan and the matrix",
      "GAP-PLAN.md" in handover and "CAPABILITY_MATRIX.md" in handover)

print()
print("=== P2.x — the four declines carry measurements ===")
plan = read("docs/GAP-PLAN.md")
for item in ("P2.1", "P2.2", "P2.3", "P2.4"):
    section_start = plan.index(f"### {item}")
    section_end = plan.find("\n### ", section_start + 1)
    section = plan[section_start: section_end if section_end != -1 else len(plan)]
    lowered = section.lower()
    # Each decline must carry EITHER a measurement with a re-open condition, OR -- for the research task that has no
    # measurement to make -- an explicit statement of the bar that would have to be met.
    has_evidence = ("re-open" in lowered or "the bar" in lowered or "the measurement" in lowered)
    check(f"{item} states its evidence or its bar",
          has_evidence and len(section) > 700,
          f"{len(section)} chars, re-open={'re-open' in lowered}, bar={'the bar' in lowered}")

print()
print("=== the plan's own status markers are consistent ===")
check("no P1 item is still marked Open", "⏳ Open" not in plan,
      "a plan that says Open after the work is done contradicts itself")
check("four P1 sections exist", len(re.findall(r"^### P1\.", plan, re.M)) == 4)
check("each P1 section has a Status line",
      len(re.findall(r"^### P1\.", plan, re.M)) == len(re.findall(r"Status: ✅", plan)))

print()
print("=== the matrix is updated with re-measured counts ===")
matrix = read("docs/CAPABILITY_MATRIX.md")
check("§6 no longer lists trigger state as a gap",
      "**Open gap — see §9 #11.**" not in matrix)
check("§9 row 11 is Closed", "| 11 |" in matrix and "✅ **Closed**" in matrix)
check("the check_lines distinction is documented",
      "check_lines" in matrix or "Verifier / check lines" in matrix,
      "the metric change must be visible in the matrix, not only in the script")
# Read the ratio the matrix actually states, rather than hard-coding one. The first version of this check looked
# for a literal "2.35" and started failing the moment the figure was re-measured to 2.33 -- a checker that has to be
# edited every time a number moves is testing the number, not the claim. The claim is that the matrix states a
# protection ratio at all, and that it is the one `count_loc.py` computes.
ratio_shown = re.findall(r"Source per unit of protection\*\* \| \*\*([\d.]+)\*\*", matrix)
check("a protection ratio is stated", bool(ratio_shown), str(ratio_shown))
if ratio_shown:
    loc_out = subprocess.run([sys.executable, str(ROOT / "tools" / "count_loc.py")],
                             capture_output=True, text=True, timeout=900).stdout
    # The protection block prints `harness  src/tests  src/(tests+checks)  checks  tests`. The first regex here
    # expected the FIVE-column *size* table instead and matched nothing, so the check failed against a correct
    # document -- a checker bug that looked exactly like a documentation bug.
    measured = None
    for line in loc_out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Myriaxon"):
            parts = stripped.split()
            # ['Myriaxon', '5.58', '2.33', '100', '43']
            if len(parts) >= 5 and parts[1].replace(".", "").isdigit() and parts[2].replace(".", "").isdigit():
                measured = parts[2]
                break
    check("it matches what count_loc.py measures",
          measured == ratio_shown[0],
          f"matrix says {ratio_shown[0]}, counter says {measured or 'not found'}")
check("it links to the plan", "GAP-PLAN.md" in matrix)
check("the docs-heading correction is recorded", "118 of" in matrix or "markdown **heading**" in matrix)

print()
print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
for label in FAIL:
    print(f"  FAILED: {label}")
raise SystemExit(1 if FAIL else 0)
