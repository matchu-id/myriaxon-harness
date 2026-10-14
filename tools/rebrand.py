"""One-shot source transformer for the Myriaxon rebrand.

This is a *migration tool*, not part of the shipped product. It exists because the audit
found that a naive find-and-replace would corrupt several files:

* ``agent.py``, ``openai_agent.py`` and ``gemini_agent.py`` begin with a UTF-8 BOM.
  A tool that reads and rewrites them as plain ASCII silently drops it, and a tool that
  prepends its own header corrupts the first line.
* The same three files contain UTF-8 -> cp1252 -> UTF-8 mojibake in strings that are
  printed or sent to the model (``IOH-CLI setup Ã¢â‚¬â€ no sign-in``), so the em dash
  ``\u2014`` is stored as ``Ã¢â‚¬â€``. A byte-level replace would preserve the garbage.

So this runs in strict UTF-8 with BOM detection, repairs the mojibake in the same pass,
and writes back as BOM-free UTF-8 with LF line endings. Every rewrite is idempotent and
reported, so the transformation can be reviewed per file rather than trusted.

Usage::

    python tools/rebrand.py --check      # report what would change, write nothing
    python tools/rebrand.py --apply      # do it
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Directories that are build residue, vendored copies, or state -- never transformed.
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", "build", "dist",
    "bench-out", ".bench-tmp", ".iohcode", ".myriaxon", "lighthouse",
    "ioh_code.egg-info", "redteam",
}
SKIP_DIR_PREFIXES = ("pip-",)
SOURCE_SUFFIXES = {".py", ".md", ".toml", ".cmd", ".ps1", ".spec", ".yaml", ".yml",
                   ".js", ".html", ".json", ".txt", ".in", ".cfg"}

# --------------------------------------------------------------------------------------
# Ordered rewrite table. Order matters: longer, more specific patterns first.
# --------------------------------------------------------------------------------------
# Each entry is (pattern, replacement, description). Patterns are literal unless the entry
# sets `regex=True`.
LITERAL_RULES: list[tuple[str, str, str]] = [
    # --- import package and module paths -------------------------------------------------
    ("ioh_code", "myriaxon", "python package name"),
    ("ioh-code", "myriaxon", "distribution / console-script name"),
    ("ioh_code.egg-info", "myriaxon.egg-info", "egg-info directory"),
    # --- state directory -----------------------------------------------------------------
    (".iohcode_write_probe", ".myriaxon_write_probe", "config-dir write probe"),
    (".ioh_settings_probe", ".myriaxon_settings_probe", "settings write probe"),
    (".ioh_upload_probe", ".myriaxon_upload_probe", "uploads write probe"),
    (".ioh_doctor_probe", ".myriaxon_doctor_probe", "doctor write probe"),
    ("ioh-harness-uploads", "myriaxon-uploads", "temp uploads directory"),
    (".iohcode", ".myriaxon", "state directory"),
    (".iohignore", ".myriaxonignore", "search-ignore filename"),
    # --- instructions filename -----------------------------------------------------------
    ("IOH.md", "MYRIAXON.md", "project instructions filename"),
    # --- environment variables -----------------------------------------------------------
    ("IOHCODE_TOOL_NAME", "MYRIAXON_TOOL_NAME", "hook env var"),
    ("IOHCODE_TOOL_IS_ERROR", "MYRIAXON_TOOL_IS_ERROR", "hook env var"),
    ("IOH_CONFIG_DIR", "MYRIAXON_CONFIG_DIR", "config-dir env var"),
    ("IOH_USER_CONFIG_DIR", "MYRIAXON_USER_CONFIG_DIR", "user-config env var"),
    ("IOH_DASHBOARD_VERBOSE", "MYRIAXON_DASHBOARD_VERBOSE", "dashboard env var"),
    ("IOH_GEMINI_PROTOCOL", "MYRIAXON_GEMINI_PROTOCOL", "gemini protocol env var"),
    ("IOH_OPENAI_SDK", "MYRIAXON_OPENAI_SDK", "openai sdk env var"),
    ("IOH_TIMING", "MYRIAXON_TIMING", "timing env var"),
    ("IOH_API_KEY", "MYRIAXON_API_KEY", "api key env var"),
    ("IOH_BASE_URL", "MYRIAXON_BASE_URL", "base url env var"),
    ("IOH_FALLBACK_MODEL", "MYRIAXON_FALLBACK_MODEL", "fallback model env var"),
    ("IOH_PROVIDER", "MYRIAXON_PROVIDER", "provider env var"),
    ("IOH_MODEL", "MYRIAXON_MODEL", "model env var"),
    # --- product name --------------------------------------------------------------------
    ("IOH Harness", "Myriaxon", "dashboard product name"),
    ("IOH-CLI", "Myriaxon", "cli product name"),
    ("the operator-Code", "Myriaxon", "legacy title casing"),
    ("IOHHarness", "Myriaxon", "http server_version"),
    ("IOH logogram", "Myriaxon mark", "mark description"),
    # --- identifiers ---------------------------------------------------------------------
    ("IOH_THEME", "MYRIAXON_THEME", "rich theme identifier"),
    ("IoHPygmentsStyle", "MyriaxonPygmentsStyle", "pygments style class"),
    ("IOHCodeJudge", "MyriaxonJudge", "bench judge class"),
    ("IOHCodeJudge", "MyriaxonJudge", "bench judge class"),
    # --- leftover uppercase references ---------------------------------------------------
    ("IOH", "Myriaxon", "remaining product references"),
]

# Bare "IOH" appears in prose and comments in a few spots; the literal rule above catches
# them. Anything still holding a branded identifier is reported by --check rather than
# guessed at.
OPTIONAL_RE = [
    (re.compile(r"\bioh\b", re.IGNORECASE), "myriaxon", "lowercase product reference"),
]

# UTF-8 -> cp1252 -> UTF-8 mojibake, repaired to the intended character. Each of these was
# observed in the audit; they are corrected here so the rebrand does not have to carry
# corrupted strings forward.
MOJIBAKE_RULES: list[tuple[str, str]] = [
    ("Ã¢â‚¬â€", "—"),   # the double-mangled em dash seen in openai_agent.py
    ("â€”", "—"),
    ("â€“", "–"),
    ("â€™", "’"),
    ("â€˜", "‘"),
    ("â€œ", "“"),
    ("â€\x9d", "”"),
    ("â€¦", "…"),
    ("Â·", "·"),
    ("â†’", "→"),
    ("âœ“", "✓"),
    ("Ã©", "é"),
]


@dataclass
class FileReport:
    path: Path
    rules: dict[str, int] = field(default_factory=dict)
    mojibake: dict[str, int] = field(default_factory=dict)
    had_bom: bool = False
    had_crlf: bool = False
    remaining: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.rules or self.mojibake or self.had_bom)

    def summary(self) -> str:
        bits = []
        if self.rules:
            bits.append(", ".join(f"{k}x{v}" for k, v in sorted(self.rules.items())))
        if self.mojibake:
            bits.append("mojibake:" + ",".join(sorted(self.mojibake)))
        if self.had_bom:
            bits.append("BOM-stripped")
        return "; ".join(bits) or "-"


def read_source(path: Path) -> tuple[str, bool, bool]:
    """Read a source file as UTF-8, reporting BOM and CRLF instead of hiding them."""
    raw = path.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    if had_bom:
        raw = raw[3:]
    had_crlf = b"\r\n" in raw
    text = raw.decode("utf-8", errors="strict")
    return text, had_bom, had_crlf


def transform_text(text: str) -> tuple[str, dict[str, int], dict[str, int]]:
    """Apply the ordered rules, then mojibake repair. Returns (text, counts, mojibake)."""
    counts: dict[str, int] = {}
    for pattern, replacement, description in LITERAL_RULES:
        if pattern == "IOH" and replacement == "Myriaxon":
            continue  # handled last, after every more specific pattern has fired
        occurrences = text.count(pattern)
        if occurrences:
            text = text.replace(pattern, replacement)
            counts[description] = counts.get(description, 0) + occurrences

    # Bare IOH last, so it only sees references the specific rules did not already claim.
    occurrences = len(re.findall(r"\bIOH\b", text))
    if occurrences:
        text = re.sub(r"\bIOH\b", "Myriaxon", text)
        counts["remaining product references"] = occurrences

    mojibake: dict[str, int] = {}
    for bad, good in MOJIBAKE_RULES:
        occurrences = text.count(bad)
        if occurrences:
            text = text.replace(bad, good)
            mojibake[bad] = mojibake.get(bad, 0) + occurrences
    return text, counts, mojibake


def find_remaining(text: str) -> list[str]:
    """Any surviving brand token, for the --check report."""
    hits = []
    for pattern in (r"\bIOH\b", r"ioh_code", r"ioh-code", r"\.iohcode", r"IOH_", r"\bioh\b"):
        found = re.findall(pattern, text)
        if found:
            hits.append(f"{pattern} x{len(found)}")
    return hits


def iter_sources(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in SOURCE_SUFFIXES:
            continue
        parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS for part in parts):
            continue
        if any(part.startswith(SKIP_DIR_PREFIXES) for part in parts):
            continue
        # Never rewrite this tool itself -- its patterns *are* the brand strings.
        if path.name == "rebrand.py":
            continue
        yield path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Myriaxon rebrand transformer")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="report changes, write nothing")
    group.add_argument("--apply", action="store_true", help="apply the changes")
    parser.add_argument("--root", default=str(REPO), help="tree to transform")
    parser.add_argument("--quiet", action="store_true", help="only print totals")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    reports: list[FileReport] = []
    scanned = 0

    for path in iter_sources(root):
        scanned += 1
        text, had_bom, had_crlf = read_source(path)
        new_text, counts, mojibake = transform_text(text)
        report = FileReport(path, counts, mojibake, had_bom, had_crlf)
        if args.check:
            report.remaining = find_remaining(new_text)
        if report.changed:
            reports.append(report)
            if args.apply:
                # Write BOM-free UTF-8 with LF endings: the BOM in the three agent files
                # is what made earlier tooling mis-handle them, and mixed line endings in
                # a single file defeat every line-based tool downstream.
                path.write_bytes(new_text.replace("\r\n", "\n").encode("utf-8"))

    total_edits = sum(sum(r.rules.values()) for r in reports)
    total_moji = sum(sum(r.mojibake.values()) for r in reports)
    bom_files = [r for r in reports if r.had_bom]

    if not args.quiet:
        print(f"{'APPLIED' if args.apply else 'WOULD CHANGE'}: {len(reports)} of {scanned} files")
        for report in reports:
            rel = report.path.relative_to(root)
            print(f"  {rel}\n      {report.summary()}")
            for leftover in report.remaining:
                print(f"      REMAINING: {leftover}")
    print()
    print(f"scanned          : {scanned}")
    print(f"files changed    : {len(reports)}")
    print(f"token rewrites   : {total_edits}")
    print(f"mojibake repairs : {total_moji}")
    print(f"BOM files fixed  : {len(bom_files)}")
    if bom_files and not args.quiet:
        for report in bom_files:
            print(f"    {report.path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
