# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone Myriaxon executable.

Build with::

    pip install "myriaxon-harness[build]"
    pyinstaller myriaxon.spec --noconfirm

Three things this spec has to get right, and the third was missing until it mattered.

1. **No absolute paths.** The entry script was pinned to a developer's own checkout
   (`C:/Users/<someone>/Documents/...`), so the spec could not build anywhere else. It is
   resolved from this file's location instead.
2. **Package data is bundled explicitly.** Myriaxon ships data that is not Python and will
   therefore never be picked up by import analysis: the built-in skill, the IQB-14 task
   suite, and the brand SVGs. Without them the frozen binary starts, then silently has no
   built-in skills and a benchmark suite with zero tasks -- both of which degrade quietly
   rather than raising, so the failure is easy to miss.
3. **A VERSION RESOURCE, which is the whole reason a standalone build is worth making.**

   This spec produced an executable with no version information at all, which left it in the
   same state as the pip launcher it replaces: unsigned, and showing no publisher to Windows.
   That is the profile an antivirus flags -- so a build from this spec was as likely to be
   flagged as the launcher, and the point of building one is to be *less* likely.

   `myriaxon.launcher_identity.version_file_text()` generates the resource from the same
   constants the rest of the project reads, so the executable cannot claim a different company
   or product than the wheel metadata does.

   The install line above was wrong too -- it said `myriaxon[build]`, the pre-public
   distribution name -- so anyone following these instructions failed at the first step.
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

SPEC_DIR = Path(SPECPATH).resolve()
PACKAGE_DIR = SPEC_DIR / "myriaxon"

# --- the version resource -------------------------------------------------------------------
# Generated rather than committed as a separate literal: a hand-maintained file is one more thing
# that goes stale silently, and the whole failure being fixed here is metadata that disagreed with
# reality. Written beside the spec because PyInstaller wants a path.
version_file = None
try:
    sys.path.insert(0, str(SPEC_DIR))
    from myriaxon.launcher_identity import version_file_text

    version_file = SPEC_DIR / "build" / "version_info.txt"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(version_file_text(), encoding="utf-8")
    print(f"[spec] version resource written: {version_file}", file=sys.stderr)
except Exception as exc:  # a missing identity must not fail the build, but it must be LOUD
    print(f"[spec] WARNING: no version resource will be embedded ({type(exc).__name__}: {exc}). "
          "The executable will show no publisher to Windows -- see docs/FALSE-POSITIVE.md.",
          file=sys.stderr)

datas = []
binaries = []
hiddenimports = []

# Third-party packages that load data files or import submodules dynamically. collect_all is
# used rather than listing hidden imports because each of these ships templates, certificates
# or lazily-imported backends that analysis alone will not find.
for package in ("anthropic", "openai", "google.genai", "rich", "prompt_toolkit", "httpx", "pygments"):
    try:
        collected_datas, collected_binaries, collected_hidden = collect_all(package)
    except Exception as exc:  # a genuinely optional SDK must not fail the whole build
        print(f"[spec] skipping {package}: {exc}", file=sys.stderr)
        continue
    datas += collected_datas
    binaries += collected_binaries
    hiddenimports += collected_hidden

# --- Myriaxon's own non-code assets, in the layout the code expects at runtime -----------
# skills.py resolves `Path(__file__).parent / "builtin_skills"`, bench/__main__.py resolves
# `Path(__file__).parent / "tasks"`, and brand/assets is served by the dashboard.
for source, target in (
    (PACKAGE_DIR / "builtin_skills", "myriaxon/builtin_skills"),
    (PACKAGE_DIR / "bench" / "tasks", "myriaxon/bench/tasks"),
    (PACKAGE_DIR / "brand" / "assets", "myriaxon/brand/assets"),
):
    if source.is_dir():
        datas.append((str(source), target))
    else:
        print(f"[spec] warning: expected asset directory is missing: {source}", file=sys.stderr)

a = Analysis(
    [str(PACKAGE_DIR / "__main__.py")],
    pathex=[str(SPEC_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PIL", "pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="myriaxon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # THE POINT OF THE BUILD. `version_file` is None when the generator was unavailable, in
    # which case the warning above already said so and the executable is built unbranded rather
    # than not built at all.
    version=str(version_file) if version_file else None,
)
