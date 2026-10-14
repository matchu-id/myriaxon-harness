# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone Myriaxon executable.

Build with::

    pip install "myriaxon[build]"
    pyinstaller myriaxon.spec --noconfirm

Two things this spec has to get right, both of which a previous version got wrong:

1. **No absolute paths.** The entry script was pinned to a developer's own checkout
   (`C:/Users/<someone>/Documents/...`), so the spec could not build anywhere else. It is
   resolved from this file's location instead.
2. **Package data is bundled explicitly.** Myriaxon ships data that is not Python and will
   therefore never be picked up by import analysis: the built-in skill, the IQB-14 task
   suite, and the brand SVGs. Without them the frozen binary starts, then silently has no
   built-in skills and a benchmark suite with zero tasks -- both of which degrade quietly
   rather than raising, so the failure is easy to miss.
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

SPEC_DIR = Path(SPECPATH).resolve()
PACKAGE_DIR = SPEC_DIR / "myriaxon"

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
)
