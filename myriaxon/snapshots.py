"""Snapshot testing: pin the prompt, the tool schemas and the transcript, and fail on any drift.

## The gap this closes

Myriaxon's tests check that functions behave. Nothing checked that **what the model is sent** stayed the same.
A tool description reworded by one character, a system-prompt section reordered, a schema field renamed — each of
those changes model behaviour, none of them fails a unit test, and the diff is buried in a file nobody re-reads.

DeepSeek's harness pins all three as expected files (`verified in packages/test-support/session-snapshot/src/`
and `snapshots/sdk/bash-tool/`, whose directory holds `system-prompt.expected.md`,
`tool-schemas.expected.json`, `session.vN.jsonl` and `workspace.expected/`). Four ideas from it are worth
copying exactly, and one is worth copying because it is a hard-won correction.

**The transcript is both replay input and expected output.** The recorded assistant turns are fed back in place
of a live model, so a test runs without an API key, and the transcript the harness produces is compared to the
one that was recorded. One artefact does two jobs, which is why the corpus stays honest.

**A normalizer must be a fixed point of itself.** Their assertion is literally
`expect(scrub(expected)).toBe(expected)`: running the scrubber over already-scrubbed text must change nothing. If
it does not hold, the normalizer is still rewriting real content and a passing test proves less than it appears
to. This is the check most likely to be missing from a home-grown version, and it is one line.

**The tool schemas are pinned by exact equality, including every description.** Their `verifyHeaders()`
reconstructs the request from the pinned prompt and schemas and asserts `toEqual(expected)`. A reworded tool
description fails with a precise diff.

**The workspace is an independent oracle.** Their `workspace.final: true` requires the final tree to equal
`workspace.expected/`; otherwise it must equal the initial tree. Their reasoning, recorded after a refresh
produced a case where seven scenarios passed while *every tool was absent*: **"model prose and tool-result text do
not prove the external effect."** That post-mortem is the reason this module treats the filesystem, not the
transcript, as the authority on whether anything happened.

## What is dropped

The `cordis.yml` composition patches, the vitest configs, the multi-generation `.vN.jsonl` filenames (one
monotonic integer is enough), and the browser cases. Also dropped: their four frozen per-version session codecs,
which exist only because released format generations must never move.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Bumped when the *recorded* format changes. A file newer than this is refused rather than misread -- the same
# rule their session codec applies, and the reason a single integer is enough at this scale.
SNAPSHOT_FORMAT_VERSION = 1

# Files that make up a case.
MANIFEST = "snapshot.json"
PROMPT_EXPECTED = "system-prompt.expected.md"
SCHEMAS_EXPECTED = "tool-schemas.expected.json"
TRANSCRIPT = "session.jsonl"
RESULT_EXPECTED = "result.expected.json"
WORKSPACE_EXPECTED = "workspace.expected"

# Values substituted before recording so a committed case carries no machine-specific path or id.
PLACEHOLDER_CWD = "{{cwd}}"
PLACEHOLDER_SESSION = "{{session}}"
PLACEHOLDER_HOME = "{{home}}"


class SnapshotError(RuntimeError):
    """A case could not be loaded or is inconsistent."""


@dataclass
class Case:
    """One recorded scenario."""

    name: str
    directory: Path
    prompt: str = ""
    schemas: dict[str, Any] = field(default_factory=dict)
    transcript: list[dict] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def checks_workspace(self) -> bool:
        return bool(self.manifest.get("workspace", {}).get("final"))

    def render(self) -> str:
        return (
            f"  {self.name}: {len(self.transcript)} turn(s), "
            f"{len(self.schemas.get('initial', []))} initial tool(s), "
            f"workspace oracle {'on' if self.checks_workspace else 'off'}"
        )


# --------------------------------------------------------------------------- normalisation


class Normalizer:
    """Replaces machine-specific values with stable tokens.

    The crucial property is that it must be **idempotent**: `normalise(normalise(x)) == normalise(x)`. If it is
    not, it is still rewriting content on the second pass, and a passing test proves less than it looks like.
    `assert_fixed_point` makes that a test rather than an assumption -- this is their
    `expect(scrub(expected)).toBe(expected)` check, and it catches the common bug of a normalizer that mangles
    its own output.
    """

    def __init__(self, cwd: str = "", session_id: str = "", home: str = "") -> None:
        self.cwd = cwd
        self.session_id = session_id
        self.home = home

    def scrub(self, value: Any) -> Any:
        """Recursively replace machine-specific values. Order matters: home before cwd, because a cwd under home
        would otherwise be replaced twice and the second replacement would see `{{home}}/...`."""
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, dict):
            return {key: self.scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        return value

    def scrub_text(self, text: str) -> str:
        result = text
        # Longest first, so a nested path is not partially replaced by its parent.
        for real, token in self._replacements():
            if not real:
                continue
            # A Windows path is written both ways in practice: the OS reports backslashes, but a path pasted into
            # a shell command, a JSON config or a git output uses forward slashes. Replacing only the form the OS
            # happens to use left the other variant in the snapshot, which is exactly the leak this function
            # exists to prevent. The forward-slash variant is a distinct string rather than a transformation, so
            # this costs nothing on POSIX where the two are identical.
            result = result.replace(real, token)
            result = result.replace(real.replace("\\", "/"), token)
        return result

    def _replacements(self) -> list[tuple[str, str]]:
        pairs = [(self.home, PLACEHOLDER_HOME), (self.cwd, PLACEHOLDER_CWD),
                 (self.session_id, PLACEHOLDER_SESSION)]
        return sorted(pairs, key=lambda pair: len(pair[0] or ""), reverse=True)

    def assert_fixed_point(self) -> None:
        """Raise unless normalising twice equals normalising once.

        Covers every placeholder in one pass rather than only the ones a case happens to use, because a
        normalizer that is not a fixed point for an unused token will bite the first time a case uses it.
        """
        probe = {
            "a": f"path {self.cwd or '/x'} and {self.home or '/h'} and {self.session_id or 's'}",
            "b": [PLACEHOLDER_CWD, PLACEHOLDER_HOME, PLACEHOLDER_SESSION],
            "c": {"nested": f"{self.cwd or '/x'}/deep"},
        }
        once = self.scrub(probe)
        twice = self.scrub(once)
        if once != twice:
            raise SnapshotError(
                "the normalizer is not a fixed point of itself: normalising an already-normalised value "
                f"changed it.\n  once:  {once}\n  twice: {twice}"
            )


def default_normalizer() -> Normalizer:
    import os

    return Normalizer(
        cwd=os.getcwd(),
        session_id=os.environ.get("MYRIAXON_SESSION_ID", ""),
        home=str(Path.home()),
    )


# --------------------------------------------------------------------------- loading and recording


def case_directory(root: Path, name: str) -> Path:
    return Path(root) / name


def load_case(root: Path, name: str) -> Case:
    """Read a case from disk, refusing anything the format cannot represent.

    A case whose manifest declares a newer format is refused rather than read: an unknown field may change how
    the rest is interpreted, and silently resuming a case that means something different is worse than not
    running it.
    """
    directory = case_directory(root, name)
    if not directory.is_dir():
        raise SnapshotError(f"no such snapshot case: {directory}")

    manifest: dict[str, Any] = {}
    manifest_path = directory / MANIFEST
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SnapshotError(f"{manifest_path} is not valid JSON: {exc}") from exc
    version = int(manifest.get("format_version", SNAPSHOT_FORMAT_VERSION))
    if version > SNAPSHOT_FORMAT_VERSION:
        raise SnapshotError(
            f"{manifest_path} declares format v{version}, but this harness reads up to "
            f"v{SNAPSHOT_FORMAT_VERSION}. Refusing to guess at the newer format."
        )

    case = Case(name=name, directory=directory, manifest=manifest)

    prompt_path = directory / PROMPT_EXPECTED
    case.prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""

    schemas_path = directory / SCHEMAS_EXPECTED
    if schemas_path.is_file():
        try:
            case.schemas = json.loads(schemas_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SnapshotError(f"{schemas_path} is not valid JSON: {exc}") from exc

    transcript_path = directory / TRANSCRIPT
    if transcript_path.is_file():
        case.transcript = read_jsonl(transcript_path)

    result_path = directory / RESULT_EXPECTED
    if result_path.is_file():
        try:
            case.result = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SnapshotError(f"{result_path} is not valid JSON: {exc}") from exc

    return case


def read_jsonl(path: Path) -> list[dict]:
    """One JSON object per line. A malformed line raises rather than being skipped: a case with a dropped turn
    would replay as a different conversation and still compare equal to itself."""
    entries: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entries.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            raise SnapshotError(f"{path} line {number} is not valid JSON: {exc}") from exc
    return entries


def write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- comparison


@dataclass
class Difference:
    what: str
    expected: str
    actual: str

    def render(self) -> str:
        return f"  {self.what}\n      expected: {self.expected[:400]}\n      actual:   {self.actual[:400]}"


@dataclass
class Comparison:
    ok: bool
    differences: list[Difference] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines: list[str] = []
        for note in self.notes:
            lines.append(f"  note: {note}")
        for difference in self.differences:
            lines.append(difference.render())
        return "\n".join(lines) if lines else "  identical"


def compare_text(what: str, expected: str, actual: str) -> Difference | None:
    if expected == actual:
        return None
    return Difference(what=what, expected=expected, actual=actual)


def compare_schemas(expected: dict, actual: dict) -> list[Difference]:
    """Compare tool-schema sets field by field, so a changed description is reported as *that*.

    A whole-object diff would be one unreadable blob. Their `verifyHeaders()` exists precisely because a reworded
    tool description is the change most likely to slip through a test suite, and naming the tool and the field is
    what makes the failure actionable.
    """
    differences: list[Difference] = []

    for section in ("initial", "changes"):
        want = expected.get(section)
        got = actual.get(section)
        if want == got:
            continue
        if want is None:
            differences.append(Difference(f"tool schemas: unexpected '{section}' section",
                                          "(absent)", json.dumps(got, indent=2)[:400]))
            continue
        if got is None:
            differences.append(Difference(f"tool schemas: missing '{section}' section",
                                          json.dumps(want, indent=2)[:400], "(absent)"))
            continue
        differences.extend(_diff_schema_list(section, want, got))

    return differences


def _diff_schema_list(section: str, want: Any, got: Any) -> list[Difference]:
    differences: list[Difference] = []
    want_list = want if isinstance(want, list) else []
    got_list = got if isinstance(got, list) else []

    def name_of(entry: Any) -> str:
        if isinstance(entry, dict):
            return str(entry.get("name") or (entry.get("function") or {}).get("name") or "?")
        return "?"

    want_by_name = {name_of(entry): entry for entry in want_list}
    got_by_name = {name_of(entry): entry for entry in got_list}

    for name in sorted(set(want_by_name) - set(got_by_name)):
        differences.append(Difference(
            f"tool schemas [{section}]: {name} was REMOVED",
            json.dumps(want_by_name[name], indent=2)[:400], "(absent)",
        ))
    for name in sorted(set(got_by_name) - set(want_by_name)):
        differences.append(Difference(
            f"tool schemas [{section}]: {name} was ADDED",
            "(absent)", json.dumps(got_by_name[name], indent=2)[:400],
        ))
    for name in sorted(set(want_by_name) & set(got_by_name)):
        if want_by_name[name] != got_by_name[name]:
            differences.append(Difference(
                f"tool schemas [{section}]: {name} CHANGED",
                json.dumps(want_by_name[name], sort_keys=True, indent=2)[:400],
                json.dumps(got_by_name[name], sort_keys=True, indent=2)[:400],
            ))
    return differences


def compare_transcript(expected: list[dict], actual: list[dict]) -> list[Difference]:
    """Compare turn by turn so a divergence points at the turn it happened on.

    Length is checked first and separately: a transcript that stopped early and one that took a different branch
    are different failures, and reporting "turn 3 differs" when there is no turn 3 is misleading.
    """
    differences: list[Difference] = []
    if len(expected) != len(actual):
        differences.append(Difference(
            "transcript length",
            f"{len(expected)} turn(s)",
            f"{len(actual)} turn(s)",
        ))
    for index, (want, got) in enumerate(zip(expected, actual), start=1):
        if want != got:
            differences.append(Difference(
                f"transcript turn {index}",
                json.dumps(want, sort_keys=True, ensure_ascii=False, indent=2)[:500],
                json.dumps(got, sort_keys=True, ensure_ascii=False, indent=2)[:500],
            ))
    return differences


def compare_workspace(expected_dir: Path, actual_dir: Path) -> list[Difference]:
    """Compare two directory trees by relative path and content.

    This is the independent oracle. Their post-mortem 0002 recorded a refresh that made seven scenarios pass while
    every tool was absent; nothing in the transcript revealed it, and the committed workspace tree did. Model prose
    and tool-result text are claims about effects; the file tree is the effect.
    """
    differences: list[Difference] = []
    if not expected_dir.is_dir():
        return [Difference("workspace", f"{expected_dir} (missing)", str(actual_dir))]

    expected_files = _tree(expected_dir)
    actual_files = _tree(actual_dir)

    for relative in sorted(set(expected_files) - set(actual_files)):
        differences.append(Difference(f"workspace: {relative} is MISSING",
                                      expected_files[relative][:300], "(absent)"))
    for relative in sorted(set(actual_files) - set(expected_files)):
        differences.append(Difference(f"workspace: {relative} is UNEXPECTED",
                                      "(absent)", actual_files[relative][:300]))
    for relative in sorted(set(expected_files) & set(actual_files)):
        if expected_files[relative] != actual_files[relative]:
            differences.append(Difference(
                f"workspace: {relative} differs",
                expected_files[relative][:300], actual_files[relative][:300],
            ))
    return differences


def _tree(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    if not root.is_dir():
        return files
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        try:
            files[relative] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            files[relative] = f"<binary, {path.stat().st_size} bytes>"
    return files


# --------------------------------------------------------------------------- running


@dataclass
class Outcome:
    case: str
    ok: bool
    comparison: Comparison
    mode: str = "replay"

    def render(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        lines = [f"  [{mark}] {self.case} ({self.mode})"]
        if not self.ok:
            lines.append(self.comparison.render())
        return "\n".join(lines)


def check_case(
    case: Case,
    *,
    prompt_builder: Callable[[], str],
    schema_builder: Callable[[], dict],
    transcript_runner: Callable[[Case], list[dict]] | None = None,
    normalizer: Normalizer | None = None,
    workspace_dir: Path | None = None,
) -> Outcome:
    """Compare a live harness against a recorded case.

    The normalizer is checked for the fixed-point property first. Doing it first matters: if the normalizer is
    rewriting content, every later comparison is comparing two things that were both altered, and a pass would
    mean nothing.
    """
    normalizer = normalizer or default_normalizer()
    differences: list[Difference] = []
    notes: list[str] = []

    try:
        normalizer.assert_fixed_point()
    except SnapshotError as exc:
        return Outcome(case=case.name, ok=False, mode="setup",
                       comparison=Comparison(ok=False, differences=[Difference("normalizer", "-", str(exc))]))

    if case.prompt:
        difference = compare_text(
            "system prompt",
            case.prompt,
            normalizer.scrub_text(prompt_builder()),
        )
        if difference:
            differences.append(difference)
    else:
        notes.append("no pinned system prompt in this case")

    if case.schemas:
        differences.extend(compare_schemas(case.schemas, normalizer.scrub(schema_builder())))
    else:
        notes.append("no pinned tool schemas in this case")

    if case.transcript and transcript_runner is not None:
        differences.extend(compare_transcript(case.transcript, normalizer.scrub(transcript_runner(case))))
    elif not case.transcript:
        notes.append("no pinned transcript in this case")

    if case.checks_workspace and workspace_dir is not None:
        expected_dir = case.directory / WORKSPACE_EXPECTED
        differences.extend(compare_workspace(expected_dir, workspace_dir))
        notes.append("workspace tree is the oracle for this case")
    elif case.checks_workspace:
        differences.append(Difference(
            "workspace", "the manifest requires the workspace oracle", "no workspace directory was supplied",
        ))

    return Outcome(
        case=case.name,
        ok=not differences,
        comparison=Comparison(ok=not differences, differences=differences, notes=notes),
    )


def discover(root: Path) -> list[str]:
    """Case names, sorted, for every directory under `root` holding a manifest."""
    base = Path(root)
    if not base.is_dir():
        return []
    return sorted(
        child.name for child in base.iterdir()
        if child.is_dir() and (child / MANIFEST).is_file()
    )


def run_all(root: Path, **kwargs: Any) -> list[Outcome]:
    """Run every case under `root`, reporting an unloadable one as a failure rather than aborting.

    A corrupt case must not stop the suite. The failure mode being avoided: one malformed file means no case runs,
    so the suite looks broken rather than the case, and the natural response is to delete the file. Found by a
    test that deliberately planted a malformed transcript.
    """
    outcomes: list[Outcome] = []
    for name in discover(root):
        try:
            case = load_case(root, name)
        except SnapshotError as exc:
            outcomes.append(Outcome(
                case=name,
                ok=False,
                mode="load",
                comparison=Comparison(ok=False, differences=[
                    Difference(what="the case could not be loaded", expected="a readable case", actual=str(exc)),
                ]),
            ))
            continue
        outcomes.append(check_case(case, **kwargs))
    return outcomes
