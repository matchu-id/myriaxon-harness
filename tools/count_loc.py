"""Count lines of code across the four harnesses, with the exclusions stated rather than silent.

Any LoC comparison is misleading unless the boundary is explicit, so this reports what it excluded and why. The
categories are separated because they answer different questions:

  * `source`   -- the code that implements the product
  * `tests`    -- regression protection, which is a different kind of work
  * `docs`     -- prose
  * `snapshots`-- recorded fixtures, which can dominate a tree without being code

Counted as non-blank, non-comment-only lines. That is the standard "logical SLOC" idea, approximated per language:
`//`, `#`, `--`, `/* */` and `<!-- -->` are recognised, and a line is comment-only if it holds nothing else.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")

TREES = {
    "Myriaxon": ROOT,
    "OpenClaw": ROOT / "openclaw-src" / "openclaw-main",
    "DeepSeek": ROOT / "deepseek-harness-master-src" / "deepseek-harness-master",
    "ClaudeCode": ROOT / "claude-code-main-src" / "claude-code-main",
    "PiHarness": ROOT / "pi-harness-main-src" / "pi-harness-main",
    "Opencode": ROOT / "opencode-dev-src" / "opencode-dev",
}

# Directories that are someone else's code, generated, or build output. Excluded, and each with a reason.
EXCLUDED_DIRS = {
    ".git", ".github", ".vscode", ".devcontainer", ".idea",
    "node_modules", "vendor", "third_party", "thirdparty",
    ".venv", ".venv-harbor-linux", ".uv", "__pycache__", ".mypy_cache", ".pytest_cache",
    "dist", "build", "out", "lib", "coverage", ".next", "target",
    "snapshots", "fixtures", "__snapshots__",
    # The third-party reference trees are EXTRACTED INSIDE this repository, so without these the Myriaxon count
    # includes their TypeScript. The first run of this script did exactly that and reported Myriaxon at 5M source
    # lines -- which is why the exclusion list is data rather than prose, and why the report prints what it skipped.
    #
    # It then happened AGAIN, at 10x instead of 70x: extracting pi-harness and opencode without adding them here
    # took Myriaxon from 56,793 source lines to 587,902. Enumerating directory names was the mistake both times --
    # a list has to be edited whenever a tree is added, and the failure is silent and enormous. So the names are
    # kept for readability AND `EXTRACTED_SUFFIX` below catches any future extraction regardless of its name.
    "openclaw-src", "claude-code-main-src", "deepseek-harness-master-src",
    "pi-harness-main-src", "opencode-dev-src",
    # A sibling project living in this working directory. Not Myriaxon's code.
    "certivo-rework", "localisation", "redteam",
    # Tooling and caches created by the comparison work. This was the THIRD time the same mistake inflated the
    # count: installing a package manager to try to run a rival harness added `.npmcache` (322,979 raw lines),
    # `.pnpm-store` (96,804) and `.corepack`, taking Myriaxon from 56,890 to 59,022 source lines and its file count
    # from 1,085 to 1,918. Each time, the fix was to name what is not ours.
    #
    # So the rule is now general rather than enumerated: **an all-lowercase dot-directory at the repository root is
    # not product code.** `.myriaxon/` is the one exception and is named explicitly, because it is the harness's own
    # state and its size is part of the story.
    ".npmcache", ".pnpm-store", ".corepack", ".tooling", ".bun", ".cache",
}

# Asset sizes, in bytes beyond which a directory is a cache rather than source. A cache directory is big; a source
# directory is not. Used only for the dot-directory rule below, so a small `.vscode` is still skipped by name.
ROOT_DOTDIRS_KEEP = {".myriaxon", ".github"}

# Any directory whose name ends this way is a reference tree extracted into the repository. The convention is
# documented in docs/REFERENCE_TREES.md, and the counters enforce it rather than trusting the author to remember:
# a name-based list is exactly the kind of rule that fails silently at 10x magnitude.
EXTRACTED_SUFFIX = "-src"


def is_excluded_dir(name: str, *, at_root: bool = False) -> bool:
    """Whether a directory is not Myriaxon's code.

    Three rules, in order of specificity:

    1. an enumerated name (`node_modules`, `dist`, a reference tree, a sibling project);
    2. the `-src` convention, so a seventh extracted tree cannot inflate the count because someone forgot an edit;
    3. **an all-lowercase dot-directory at the repository root is a cache, not source** -- unless it is
       `.myriaxon/` (the harness's own state) or `.github/`.

    Rule 3 exists because rules 1 and 2 both failed the same way, three times: a new directory appeared and nobody
    remembered to name it. Enumerating is a rule that has to be maintained; this one does not.
    """
    if name in EXCLUDED_DIRS or name.endswith(EXTRACTED_SUFFIX) or name.startswith("claude-code-"):
        return True
    if at_root and name.startswith(".") and name not in ROOT_DOTDIRS_KEEP:
        return name == name.lower()
    return False

# Files whose content is data rather than code.
EXCLUDED_SUFFIXES = (
    ".lock", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".zip", ".gz", ".tar",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".webm", ".wasm", ".node", ".so", ".dll", ".dylib",
    ".exe", ".bin", ".map", ".min.js", ".min.css", ".csv", ".parquet", ".db", ".sqlite",
)

CODE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".rb", ".sh",
    ".ps1", ".c", ".h", ".cpp", ".hpp", ".swift", ".kt", ".cs", ".php", ".lua", ".sql", ".vue", ".svelte",
}
DOC_SUFFIXES = {".md", ".mdx", ".txt", ".rst", ".adoc"}
DATA_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".jsonl"}

# A path segment indicating test code.
TEST_MARKERS = ("test", "tests", "__tests__", "spec", "specs", "e2e", "bench", "benchmarks")

# Filename prefixes indicating ASSERTION code -- a verifier or an audit counter, which is protection of the same
# kind as a test.
#
# Why this exists: `tools/verify_always_on.py` matched none of the TEST_MARKERS, so all the verifier files were
# counted as SOURCE. That single misclassification produced the whole of the misleading "source per test line"
# figure -- Myriaxon's real protection lives in `tools/verify_*.py` (executed assertions), not in `tests/`. The
# honest ratio is product lines over ALL protection lines, so checks get their own column rather than being folded
# into either side.
CHECK_PREFIXES = ("verify_", "check_", "audit_", "probe_", "inventory_")

# Archived one-shot scripts. Excluded because they are finished and unreferenced: they are not part of the product
# and not protection, and leaving them in `source` inflated the count by thousands of lines. Kept on disk rather
# than deleted -- see tools/_archive/README.md for why that choice was made.
EXCLUDED_DIRS = EXCLUDED_DIRS | {"_archive"}

_COMMENT_PREFIXES = ("//", "#", "--", ";", "*", "/*", "*/", "<!--")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def is_test_path(parts: tuple[str, ...], name: str) -> bool:
    lowered = [p.lower() for p in parts]
    stem = name.lower()
    if any(marker in lowered for marker in TEST_MARKERS):
        return True
    return (
        stem.startswith("test_") or stem.endswith("_test.py") or ".test." in stem
        or ".spec." in stem or stem.endswith(".test.ts") or stem.endswith(".test.tsx")
    )


def is_check_path(parts: tuple[str, ...], name: str) -> bool:
    """Whether this is a verifier or an audit -- assertion code, counted separately from both source and tests.

    Checked AFTER `is_test_path`, so a file under `tests/` stays a test whatever it is named.

    Matched by **filename prefix only**, plus a directory that is literally named for checks. The first version
    also matched `audit` and `inventory` as *substring* directory markers, which swept unrelated subtrees into the
    check column -- measured at 98 check files for Myriaxon, far more than the 51 verifier and counter scripts that
    exist. A rule that inflates the very number it was written to correct is worse than the misclassification it
    replaced, so the rule is now narrow and the count is checked against the file list.
    """
    lowered = [p.lower() for p in parts]
    stem = name.lower()
    if any(marker in ("checks", "verifiers") for marker in lowered):
        return True
    return stem.startswith(CHECK_PREFIXES)


def count_file(path: Path) -> tuple[int, int]:
    """Return `(total_lines, code_lines)`.

    `code_lines` excludes blanks and comment-only lines. A line with code and a trailing comment counts as code,
    which is the usual convention and the one that makes the number comparable to a published figure.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0, 0

    # Strip whole-block comments first, so a multi-line /* */ does not count as several code lines.
    stripped = _BLOCK_COMMENT.sub("\n", text)
    stripped = _HTML_COMMENT.sub("\n", stripped)

    total = 0
    code = 0
    for line in stripped.splitlines():
        total += 1
        content = line.strip()
        if not content:
            continue
        if content.startswith(_COMMENT_PREFIXES):
            continue
        code += 1
    return total, code


def non_blank(path: Path) -> int:
    """Non-blank lines, with no comment stripping.

    For prose this is the right unit: a markdown heading is content, not a comment. Kept separate from
    `count_file` so the two conventions cannot be confused at a call site.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if line.strip())


def walk(tree: Path) -> dict:
    """Count one tree, split into source / tests / docs / data.

    Traversal prunes excluded directories as it descends rather than filtering after the fact. Two reasons: it is
    far faster on a 52,000-file tree, and `rglob` on Windows raises `OSError [WinError 1920]` on a broken symlink
    (`lib64` in a virtualenv), which would abort the whole count. Every `stat` is guarded for the same reason --
    a single unreadable path must not lose the measurement.
    """
    result = {
        "source_lines": 0, "source_files": 0,
        "test_lines": 0, "test_files": 0,
        "check_lines": 0, "check_files": 0,
        "doc_lines": 0, "doc_files": 0,
        "data_files": 0,
        "raw_lines": 0, "file_count": 0,
        "by_language": {},
        "skipped_dirs": set(),
        "unreadable": 0,
    }
    if not tree.is_dir():
        return result

    import os

    for current, dirnames, filenames in os.walk(tree, onerror=lambda _e: None):
        # Prune in place: `os.walk` honours the mutation, so an excluded subtree is never entered.
        keep = []
        for directory in dirnames:
            # `at_root` is what lets rule 3 tell a root-level cache from a dot-directory deeper in a real source
            # tree, where the same convention does not hold.
            if is_excluded_dir(directory, at_root=(Path(current) == tree)):
                result["skipped_dirs"].add(directory)
            else:
                keep.append(directory)
        dirnames[:] = keep

        here = Path(current)
        for filename in filenames:
            path = here / filename
            if filename.endswith(EXCLUDED_SUFFIXES):
                continue
            try:
                if not path.is_file():
                    continue
            except OSError:
                result["unreadable"] += 1
                continue

            parts = path.relative_to(tree).parts
            suffix = path.suffix.lower()
            try:
                total, code = count_file(path)
            except OSError:
                result["unreadable"] += 1
                continue
            if not total:
                continue
            result["file_count"] += 1
            result["raw_lines"] += total

            if suffix in CODE_SUFFIXES:
                if is_test_path(parts[:-1], filename):
                    result["test_lines"] += code
                    result["test_files"] += 1
                elif is_check_path(parts[:-1], filename):
                    # A verifier or an audit: assertion code, but not a pytest suite. Counted on its own so the
                    # "source per test line" figure stops treating protection as product.
                    result["check_lines"] += code
                    result["check_files"] += 1
                else:
                    result["source_lines"] += code
                    result["source_files"] += 1
                language = result["by_language"].setdefault(suffix, {"files": 0, "lines": 0})
                language["files"] += 1
                language["lines"] += code
            elif suffix in DOC_SUFFIXES:
                # Prose is counted as NON-BLANK lines, not as comment-stripped code.
                #
                # `_COMMENT_PREFIXES` contains `#`, which is correct for Python and shell -- and every markdown
                # HEADING also starts with `#`. So the old code silently discarded every heading before counting:
                # in docs/CAPABILITY_MATRIX.md that was 118 of 395 lines (30 %). "Docs as a share of source" was
                # therefore comparing a number that had lost its headings against a number with comments AND
                # blanks removed -- two different units, in a row presented as one ratio.
                result["doc_lines"] += non_blank(path)
                result["doc_files"] += 1
            elif suffix in DATA_SUFFIXES:
                result["data_files"] += 1

    result["skipped_dirs"] = sorted(result["skipped_dirs"])
    return result


def main() -> int:
    report = {}
    for name, tree in TREES.items():
        report[name] = walk(tree)

    print(f"  {'harness':<12} {'source':>10} {'tests':>10} {'checks':>9} {'docs':>9} {'files':>8} {'raw':>11}")
    print(f"  {'-'*12} {'-'*10} {'-'*10} {'-'*9} {'-'*9} {'-'*8} {'-'*11}")
    for name, data in report.items():
        print(f"  {name:<12} {data['source_lines']:>10,} {data['test_lines']:>10,} "
              f"{data['check_lines']:>9,} {data['doc_lines']:>9,} {data['file_count']:>8,} "
              f"{data['raw_lines']:>11,}")

    print()
    print("  PROTECTION RATIOS. `checks` is tools/verify_*.py and friends -- assertion code that is not a pytest")
    print("  suite. Counting it as `source` (which this script used to do) is what made Myriaxon look")
    print("  under-protected: its real protection lives in those files, not in tests/.")
    print()
    print(f"    {'harness':<12} {'src/tests':>10} {'src/(tests+checks)':>20}   {'checks':>8} {'tests':>8}")
    print(f"    {'-'*12} {'-'*10} {'-'*20} {'-'*8} {'-'*8}")
    for name, data in report.items():
        tests_only = (data["source_lines"] / data["test_lines"]) if data["test_lines"] else 0.0
        protected = data["test_lines"] + data["check_lines"]
        honest = (data["source_lines"] / protected) if protected else 0.0
        print(f"    {name:<12} {tests_only:>10.2f} {honest:>20.2f}   "
              f"{data['check_files']:>8,} {data['test_files']:>8,}")

    print()
    print("  exclusions applied:")
    for name, data in report.items():
        print(f"    {name:<12} {', '.join(data['skipped_dirs']) or 'none'}   "
              f"({data['data_files']:,} data files not counted)")

    print()
    print("  top languages by lines (source + tests):")
    for name, data in report.items():
        top = sorted(data["by_language"].items(), key=lambda kv: -kv[1]["lines"])[:5]
        rendered = ", ".join(f"{suffix} {v['lines']:,}" for suffix, v in top)
        print(f"    {name:<12} {rendered}")

    out = ROOT / "docs" / "loc-report.json"
    out.write_text(json.dumps(
        {k: {**v, "skipped_dirs": v["skipped_dirs"]} for k, v in report.items()},
        indent=2, default=str,
    ), encoding="utf-8")
    print()
    print(f"  written to {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
