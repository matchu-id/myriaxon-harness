"""Probe why bun cannot create its node_modules symlink layout in the opencode tree.

bun reported 1333 packages installed and 1047 symlink failures. This distinguishes the three candidate causes:

  1. the workspace sandbox refuses symlink creation;
  2. the OneDrive filesystem refuses it;
  3. bun needs a privilege Windows withholds (unprivileged symlink creation).

The answer decides whether opencode can be installed here at all, which is the difference between "not yet scored"
and "not scorable on this host".
"""

import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon")
OCD = ROOT / "opencode-dev-src" / "opencode-dev"


def try_symlink(label: str, directory: Path, target: Path) -> None:
    probe = directory / "myx-symlink-probe"
    try:
        if probe.exists() or probe.is_symlink():
            probe.unlink()
    except OSError:
        pass
    try:
        os.symlink(target, probe)
        print(f"  {label:<34} CREATED")
        try:
            probe.unlink()
        except OSError:
            pass
    except OSError as exc:
        print(f"  {label:<34} FAILED  {type(exc).__name__}: {str(exc)[:90]}")


print("=== 1. symlink creation, three locations ===")
tmp = Path(tempfile.mkdtemp())
(tmp / "target.txt").write_text("x", encoding="utf-8")
try_symlink("temp dir (outside OneDrive)", tmp, tmp / "target.txt")
try_symlink("repo root (OneDrive)", ROOT, ROOT / "package.json")
try_symlink("opencode tree (OneDrive)", OCD, OCD / "package.json")

print()
print("=== 2. is Developer Mode / SeCreateSymbolicLink enabled? ===")
# Windows withholds unprivileged symlink creation unless Developer Mode is on. A hardlink needs no privilege, so a
# working hardlink alongside a failing symlink is the signature of that specific cause.
probe = tmp / "myx-hardlink-probe"
try:
    os.link(tmp / "target.txt", probe)
    print("  hardlink in temp dir              CREATED (so the filesystem supports links)")
    probe.unlink()
except OSError as exc:
    print(f"  hardlink in temp dir              FAILED {exc}")

print()
print("=== 3. can bun link at all? (its own error text) ===")
bun = ROOT / ".tooling" / "bun" / "bun-windows-x64" / "bun.exe"
if bun.is_file():
    done = subprocess.run([str(bun), "--version"], capture_output=True, text=True, timeout=60)
    print(f"  bun {done.stdout.strip()} at {bun}")
    # A bare `bun link`-style operation in a scratch package, to get bun's OWN diagnostic.
    scratch = Path(tempfile.mkdtemp())
    (scratch / "package.json").write_text('{"name":"probe","version":"1.0.0"}', encoding="utf-8")
    (scratch / "index.js").write_text("module.exports = 1;", encoding="utf-8")
    dep = scratch / "dep"
    dep.mkdir()
    (dep / "package.json").write_text('{"name":"dep","version":"1.0.0"}', encoding="utf-8")
    (dep / "index.js").write_text("module.exports = 2;", encoding="utf-8")
    (scratch / "package.json").write_text(
        '{"name":"probe","version":"1.0.0","dependencies":{"dep":"file:./dep"}}', encoding="utf-8")
    done = subprocess.run([str(bun), "install", "--ignore-scripts"], cwd=str(scratch),
                          capture_output=True, text=True, timeout=180)
    print(f"  minimal `bun install` exit: {done.returncode}")
    for line in ((done.stdout or "") + (done.stderr or "")).splitlines()[-6:]:
        print(f"    {line[:130]}")
    link = scratch / "node_modules" / "dep"
    print(f"  node_modules/dep present: {link.exists() or link.is_symlink()}")
else:
    print("  bun not found")
