#!/usr/bin/env python3
"""Static checks that find the bugs a test only finds when it happens to run that exact line.

A name that does not exist is invisible until the line executes. This project has been bitten by it again and again:
`time` used in a function that only runs under `myriaxon loci --watch`, `tools` in the interactive locus delete,
`_session_cls` in every hosted-MCP connection, `loci_module` in a lease release, a function defined twice so that the
later one silently replaced the earlier. Each was found by reading the code, because no test went down that path.
These checks read ALL of the code, every time:

  1. every module in the package imports;
  2. every name a function uses is defined (pyflakes), the check that would have caught all of the above that are NameErrors;
  3. every `from .x import y` names something that exists, including the imports inside functions that rarely run;
  4. nothing is defined twice in the same scope;
  5. no source file contains a control character (an escape sequence mangled on its way into the file, such as a form
     feed in the middle of a comment).

pyflakes is a development dependency (`pip install -e ".[dev]"`). Without it, check 2 is reported as skipped, never as passed.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "myriaxon"
sys.path.insert(0, str(REPO))
# Importing the package must not read or write the real configuration directory.
os.environ.setdefault("MYRIAXON_CONFIG_DIR", tempfile.mkdtemp(prefix="myx-names-"))

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def skip(label: str, why: str) -> None:
    SKIP.append(label)
    print(f"  [SKIP] {label}\n         {why}")


def source_files() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def module_name(path: Path) -> str:
    parts = list(path.relative_to(REPO).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def parse(path: Path) -> ast.Module:
    # utf-8-sig: a byte-order mark at the start of a file is harmless to Python and must not read as a syntax error.
    return ast.parse(path.read_text(encoding="utf-8-sig"), str(path))


# --------------------------------------------------------------------------------------------------------------------
def check_imports(files: list[Path]) -> None:
    print("=== 1. every module in the package imports ===")
    broken: list[str] = []
    skipped: list[str] = []
    for path in files:
        name = module_name(path)
        if name.endswith("__main__"):
            continue  # runs the program when executed; its logic is covered by the packaging and e2e checks
        try:
            importlib.import_module(name)
        except ImportError as exc:
            missing = getattr(exc, "name", "") or ""
            if missing.split(".")[0] == "myriaxon":
                broken.append(f"{name}: {type(exc).__name__}: {exc}")
            else:
                # An optional third-party package that is not installed. Reported, not counted as a pass or a failure.
                skipped.append(f"{name} needs {missing or exc}")
        except Exception as exc:  # noqa: BLE001
            broken.append(f"{name}: {type(exc).__name__}: {exc}")
    check(f"all {len(files)} modules import", not broken, "; ".join(broken[:5]))
    if skipped:
        skip(f"{len(skipped)} module(s) need an optional package that is not installed", "; ".join(skipped[:5]))


# --------------------------------------------------------------------------------------------------------------------
def check_undefined_names(files: list[Path]) -> None:
    print()
    print("=== 2. every name a function uses is defined ===")
    try:
        from pyflakes import checker, messages  # type: ignore
    except ImportError:
        skip("undefined names", "pyflakes is not installed (pip install -e \".[dev]\")")
        return
    fatal = (messages.UndefinedName, messages.UndefinedLocal, messages.UndefinedExport)
    problems: list[str] = []
    for path in files:
        tree = parse(path)
        result = checker.Checker(tree, filename=str(path))
        for message in result.messages:
            if isinstance(message, fatal):
                problems.append(f"{path.relative_to(REPO)}:{message.lineno}: {message.message % message.message_args}")
    check("no undefined names anywhere in the package", not problems, "\n         ".join(problems[:12]))


# --------------------------------------------------------------------------------------------------------------------
def first_party_target(path: Path, node: ast.ImportFrom) -> str | None:
    """The absolute module an `ImportFrom` points at, if it is inside this package."""
    package = module_name(path) if path.name == "__init__.py" else module_name(path).rpartition(".")[0]
    if node.level:
        parts = package.split(".")
        if node.level - 1 > len(parts) - 1:
            return None
        base = parts[: len(parts) - (node.level - 1)]
        target = ".".join(base + ([node.module] if node.module else []))
    else:
        target = node.module or ""
    return target if target == "myriaxon" or target.startswith("myriaxon.") else None


def check_import_targets(files: list[Path]) -> None:
    print()
    print("=== 3. every `from .x import y` names something that exists ===")
    missing: list[str] = []
    checked = 0
    for path in files:
        for node in ast.walk(parse(path)):  # the whole tree, so imports inside rarely-run functions count
            if not isinstance(node, ast.ImportFrom):
                continue
            target = first_party_target(path, node)
            if target is None:
                continue
            try:
                module = importlib.import_module(target)
            except ImportError as exc:
                missing.append(f"{path.relative_to(REPO)}:{node.lineno}: cannot import module {target} ({exc})")
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                checked += 1
                if hasattr(module, alias.name):
                    continue
                try:
                    if importlib.util.find_spec(f"{target}.{alias.name}") is not None:
                        continue
                except (ImportError, ValueError):
                    pass
                missing.append(f"{path.relative_to(REPO)}:{node.lineno}: {target} has no {alias.name!r}")
    check(f"all {checked} first-party imports resolve", not missing, "\n         ".join(missing[:12]))


# --------------------------------------------------------------------------------------------------------------------
_REPLACEMENT_DECORATORS = {"setter", "getter", "deleter", "register", "overload"}


def _is_variant(node: ast.AST) -> bool:
    """A def that is meant to sit beside another of the same name: a property setter, an overload, a singledispatch case."""
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name in _REPLACEMENT_DECORATORS:
            return True
    return False


def check_duplicates(files: list[Path]) -> None:
    print()
    print("=== 4. nothing is defined twice in the same scope ===")
    duplicates: list[str] = []

    def scan(body: list[ast.stmt], where: str, path: Path) -> None:
        seen: dict[str, int] = {}
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in seen and not _is_variant(node):
                    duplicates.append(f"{path.relative_to(REPO)}:{node.lineno}: {where}{node.name} is already defined "
                                      f"at line {seen[node.name]}; the later one silently replaces it")
                seen.setdefault(node.name, node.lineno)
                if isinstance(node, ast.ClassDef):
                    scan(node.body, f"{node.name}.", path)

    for path in files:
        scan(parse(path).body, "", path)
    check("no function or class is defined twice", not duplicates, "\n         ".join(duplicates[:12]))


# --------------------------------------------------------------------------------------------------------------------
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def check_control_characters() -> None:
    print()
    print("=== 5. no source file contains a stray control character ===")
    roots = [PACKAGE, REPO / "tests"]
    paths = [p for root in roots if root.is_dir() for p in root.rglob("*.py") if "__pycache__" not in p.parts]
    paths += sorted((REPO / "tools").glob("verify_*.py"))
    paths += [p for p in (REPO / "tools" / "run_checks.py", REPO / "tools" / "_artifacts.py") if p.is_file()]
    offenders: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            if _CONTROL.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()[:70]!r}")
    check(f"none of {len(paths)} files has one", not offenders, "\n         ".join(offenders[:12]))


# --------------------------------------------------------------------------------------------------------------------
def main() -> int:
    files = source_files()
    check_imports(files)
    check_undefined_names(files)
    check_import_targets(files)
    check_duplicates(files)
    check_control_characters()
    print()
    note = f", {len(SKIP)} skipped" if SKIP else ""
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed{note} ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
