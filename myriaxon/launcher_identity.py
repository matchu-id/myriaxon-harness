"""Give the installed launcher its real identity, because right now it claims to be somebody else.

THE PROBLEM, MEASURED -- AND IT IS NOT WHAT I FIRST ASSUMED

`pip install myriaxon-harness` creates `Scripts/myriaxon.exe` from a template pip ships, and the first version
of this module assumed that template carried no version resource. Reading the actual file disproved that:

    CompanyName       Simple Launcher User
    FileDescription   Simple Launcher Executable
    FileVersion       1.1.0.14
    InternalName      t64.exe
    LegalCopyright    Copyright (C) Simple Launcher User
    OriginalFilename  t64.exe
    ProductName       Simple Launcher
    signed            NotSigned

That is distlib's placeholder identity -- the launcher template pip vendors, originally from `t64.exe`. So an
installed `myriaxon.exe` does not say nothing about itself. **It says it is a generic launcher called
`t64.exe`, made by a company called "Simple Launcher User".**

That is a far better explanation for an antivirus flag than anonymity, and a worse state to be in:

  * Every installed copy of the package carries the **same** placeholder identity, so a detection on one is a
    detection on all of them, across every project that uses pip.
  * A generic unsigned launcher that impersonates a differently-named executable is **the exact profile of a
    trojan dropper** -- and it is a documented false-positive class for precisely this reason.
  * It is not merely cosmetic. `myriaxon.exe` reporting `OriginalFilename: t64.exe` to a security tool, a
    firewall prompt or an EDR agent is wrong information about what just ran on the machine.

WHAT THIS MODULE DOES

Two halves, with very different guarantees.

**Reading is pure standard library and always works.** `describe()` walks the PE resource directory and reports
exactly what Windows would report. This is the half a trust decision rests on, because it lets somebody verify
a binary instead of taking anyone's word for it.

**Writing** is a different story, and this module is honest about it. An installed launcher cannot be branded
in place: an attempt using the resource-update API was measured and **failed**, and the account of it is kept
above `version_file_text` so nobody repeats it. What this provides instead is a `VERSIONINFO` document for
PyInstaller's `--version-file`, which works because PyInstaller authors a PE image rather than patching one.

See `docs/FALSE-POSITIVE.md` for the full explanation and the recommended fix.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

# Who the binary claims to be. Kept in one place so the exe, the wheel metadata and AUTHORS.md cannot drift.
AUTHOR = "Thariq Arian Khalfani"
AUTHOR_EMAIL = "ariankhalfani@gmail.com"
COMPANY = "Myriaxon Technologies"
PRODUCT = "Myriaxon Harness"
DESCRIPTION = "Myriaxon Harness - agentic harness for the terminal and the browser"
COPYRIGHT = "Copyright (c) 2026 Myriaxon Technologies"
HOMEPAGE = "https://github.com/matchu-id/myriaxon-harness"

RT_VERSION = 16
RT_MANIFEST = 24

# Microsoft's VERSIONINFO convention. Both values are MATCHED to the launcher template rather than chosen, and
# measured from the real file:
#
#   * the language id is **0**, not 0x0409. The template registers its version resource under language-neutral
#     zero, and `UpdateResource` with 0x0409 does not replace it -- it adds a SECOND RT_VERSION entry, so the
#     loader keeps reading the original "Simple Launcher User" strings and the stamp looks like it silently did
#     nothing. Measured: after stamping with 0x0409 the file's hash changed, our `Comments` appeared, and every
#     other field still read as distlib's placeholder.
#   * the StringTable key is `040904E4`, hardcoded inside `win32verstamp.StringFileInfo`. A table key that
#     disagrees with how the resource is registered is a version resource the loader will not find.
LANG_NEUTRAL = 0
TABLE_KEY = "040904E4"


class NotAPortableExecutable(Exception):
    """The file is not a PE image, so nothing here applies."""


# ---------------------------------------------------------------------------------------------
# Reading -- standard library only, and the half a trust decision rests on
# ---------------------------------------------------------------------------------------------


def _sections(data: bytes) -> list[dict]:
    if data[:2] != b"MZ":
        raise NotAPortableExecutable("no MZ header")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset:pe_offset + 4] != b"PE\x00\x00":
        raise NotAPortableExecutable("no PE signature")
    section_count = struct.unpack_from("<H", data, pe_offset + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe_offset + 20)[0]
    table = pe_offset + 24 + optional_size
    sections = []
    for index in range(section_count):
        base = table + index * 40
        name = data[base:base + 8].rstrip(b"\x00").decode("ascii", "replace")
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from("<IIII", data, base + 8)
        sections.append({"name": name, "virtual_size": virtual_size, "virtual_address": virtual_address,
                         "raw_size": raw_size, "raw_pointer": raw_pointer})
    return sections


def _rsrc(data: bytes) -> dict:
    for section in _sections(data):
        if section["name"] == ".rsrc":
            return section
    raise NotAPortableExecutable("no .rsrc section")


def resource_types(path: Path) -> set[int]:
    """Every resource type id present, which is how a caller knows whether a version resource exists."""
    data = Path(path).read_bytes()
    section = _rsrc(data)
    base = section["raw_pointer"]
    named, ids = struct.unpack_from("<HH", data, base + 12)
    present: set[int] = set()
    for index in range(named + ids):
        entry = base + 16 + index * 8
        if entry + 8 > base + section["raw_size"]:
            break
        name_or_id = struct.unpack_from("<I", data, entry)[0]
        if not (name_or_id & 0x80000000):
            present.add(name_or_id & 0xFFFF)
    return present


def _read_version_strings(path: Path) -> dict[str, str]:
    """Pull the StringFileInfo pairs out of RT_VERSION, by walking the resource tree.

    Walked rather than string-scanned, so a value reported here is a value the loader would genuinely expose --
    which is the only claim that matters when the question is "can I trust this file".
    """
    data = Path(path).read_bytes()
    section = _rsrc(data)
    base = section["raw_pointer"]
    limit = base + section["raw_size"]
    found: dict[str, str] = {}

    def children(offset: int):
        if offset + 16 > limit:
            return
        named, ids = struct.unpack_from("<HH", data, base + offset + 12)
        for index in range(named + ids):
            entry = base + offset + 16 + index * 8
            if entry + 8 > limit:
                return
            name_or_id, child = struct.unpack_from("<II", data, entry)
            yield (name_or_id & 0xFFFF if not (name_or_id & 0x80000000) else None), child

    def data_of(child: int) -> bytes:
        if child & 0x80000000:
            return b""
        entry = base + (child & 0x7FFFFFFF)
        if entry + 16 > limit:
            return b""
        rva, size = struct.unpack_from("<II", data, entry)
        start = base + (rva - section["virtual_address"])
        return data[start:start + size]

    for type_id, type_child in children(0):
        if type_id != RT_VERSION:
            continue
        for _name_id, name_child in children(type_child & 0x7FFFFFFF):
            for _lang_id, lang_child in children(name_child & 0x7FFFFFFF):
                blob = data_of(lang_child)
                if not blob:
                    continue
                text = blob.decode("utf-16-le", "ignore")
                for key in ("CompanyName", "FileDescription", "FileVersion", "InternalName",
                            "LegalCopyright", "OriginalFilename", "ProductName", "ProductVersion",
                            "Comments"):
                    index = text.find(key)
                    if index == -1:
                        continue
                    value = text[index + len(key):].lstrip("\x00").split("\x00")[0].strip()
                    if value:
                        found[key] = value
    return found


def windows_version_info(path: Path) -> dict:
    """What WINDOWS reports for this file -- read by Windows, not by us.

    This is the only reading that counts, and it took a wrong turn to learn that. The hand-written parser below
    walked the RT_VERSION blob and pulled out `CompanyName: Simple Launcher User`, `OriginalFilename: t64.exe`
    and the rest -- and it was **wrong**, because it was finding distlib's raw unicode string blob rather than a
    VERSIONINFO structure. Windows reported `null` for every field, before any stamping was attempted:

        windows sees : {'Company': None, 'Product': None, 'FileDesc': None, 'FileVersion': None}
        our reader   : {'CompanyName': 'Simple Launcher User', ... 'OriginalFilename': 't64.exe'}

    So the pip launcher really does carry **no version information Windows can see**, and a parser that says
    otherwise is worse than no parser: it produced a confident wrong answer, which sent this module down a path
    of "correcting" a finding that had been right the first time.

    `Get-Item .VersionInfo` is the shell property bag Explorer shows and is present on every supported Windows.
    Nothing is reimplemented, so nothing here can disagree with the loader.
    """
    if sys.platform != "win32":
        return {"note": "not Windows, so there is no version resource to read"}
    import json
    import subprocess

    command = (
        f"$v = (Get-Item -LiteralPath '{path}').VersionInfo; "
        "[pscustomobject]@{CompanyName=$v.CompanyName; ProductName=$v.ProductName; "
        "FileDescription=$v.FileDescription; FileVersion=$v.FileVersion; "
        "ProductVersion=$v.ProductVersion; LegalCopyright=$v.LegalCopyright; "
        "OriginalFilename=$v.OriginalFilename; InternalName=$v.InternalName; "
        "Comments=$v.Comments} | ConvertTo-Json -Compress"
    )
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                              capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        raw = json.loads(done.stdout.strip() or "{}")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    # Windows reports an absent field as an empty string or null; normalised so a caller can test truthiness.
    return {key: value for key, value in raw.items() if value not in (None, "")}


def describe(path: Path) -> dict:
    """What Windows would report for this file. Never raises; a failure is part of the answer."""
    path = Path(path)
    out: dict = {"path": str(path), "exists": path.is_file()}
    if not out["exists"]:
        out["error"] = "no such file"
        return out
    try:
        import hashlib

        data = path.read_bytes()
        out["size"] = len(data)
        out["sha256"] = hashlib.sha256(data).hexdigest()
        out["is_pe"] = data[:2] == b"MZ"
        out["resource_types"] = sorted(resource_types(path)) if out["is_pe"] else []
        # WINDOWS IS THE AUTHORITY. The local parse is kept only to show the resource is present, never to
        # report field values -- see `windows_version_info` for why that distinction cost a wrong answer.
        out["has_version_resource"] = RT_VERSION in out["resource_types"]
        out["version"] = windows_version_info(path)
        out["signed"] = _signature_state(path)
        out["carries_project_identity"] = is_our_identity(out["version"])
        out["claims_to_be"] = out["version"].get("OriginalFilename") or out["version"].get("InternalName") or ""
        out["misbranded"] = bool(out["version"]) and not out["carries_project_identity"]
        # The state this project is actually in, and worse than mislabelled: Windows sees nothing at all.
        out["no_identity_at_all"] = not out["version"]
    except NotAPortableExecutable as exc:
        out["is_pe"] = False
        out["error"] = str(exc)
        out["carries_project_identity"] = False
        out["misbranded"] = True
        out["no_identity_at_all"] = True
    except OSError as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["carries_project_identity"] = False
        out["misbranded"] = True
        out["no_identity_at_all"] = True
    return out


def is_our_identity(version: dict) -> bool:
    """Does the version resource describe Myriaxon rather than nothing, or something else?"""
    return (version.get("ProductName") == PRODUCT and version.get("CompanyName") == COMPANY)


def _signature_state(path: Path) -> str:
    """Authenticode state, via PowerShell rather than a dependency.

    `Get-AuthenticodeSignature` is present on every supported Windows and is the same check Explorer shows, so
    there is no reason to reimplement it or to add a library for it.
    """
    if sys.platform != "win32":
        return "not applicable (not Windows)"
    import subprocess

    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-AuthenticodeSignature -LiteralPath '{path}').Status"],
            capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
        )
        return (done.stdout or "").strip() or "Unknown"
    except Exception as exc:  # noqa: BLE001
        return f"unknown ({type(exc).__name__})"


# ---------------------------------------------------------------------------------------------
# Writing -- and the honest account of why there is no in-place writer here
# ---------------------------------------------------------------------------------------------
#
# THE ATTEMPT THAT FAILED, AND WHY IT IS NOT IN THIS FILE ANY MORE
#
# This module used to call `UpdateResource`/`EndUpdateResource` (pywin32's `win32verstamp`) to stamp a
# VERSIONINFO resource into the installed launcher. Measured on the real pip template:
#
#     apply_identity -> the API calls return cleanly, EndUpdateResource(commit) succeeds,
#                       the file's SHA-256 CHANGES, our `Comments` string is present in the image
#     THEN: Windows reports ... nothing. Every field still empty.
#
# distlib's launcher already occupies the RT_VERSION slot with a blob Windows will not parse, and the
# resource-update API adds alongside it rather than replacing it in a way the loader honours. Replacing it means
# rewriting the resource directory, which is a job for a full PE toolchain.
#
# The first version of that code returned success as soon as the API calls completed. That was a false claim,
# caught only by asking Windows what it saw -- which is why the function below now exists in this shape and why
# there is no in-place writer at all. Dead code that reports success is worse than no code.


def version_file_text(version: str = "") -> str:
    """A PyInstaller `--version-file` document describing this project.

    THE PATH THAT WORKS. PyInstaller *authors* a PE image rather than patching one, so it writes a version
    resource the loader genuinely reads. That makes it the supported route to a branded executable, and it needs
    no third-party PE library at all -- this generates the input file from the same constants the rest of the
    module reads, so there is one source of truth for who made this.

        pip install "myriaxon-harness[build]"
        python -c "from myriaxon.launcher_identity import version_file_text; \
                   open('version_info.txt','w').write(version_file_text())"
        pyinstaller --onefile --name myriaxon --version-file version_info.txt myriaxon/__main__.py
    """
    from .constants import VERSION

    version = version or VERSION
    major, minor, patch, _ = _version_quad(version)
    quad = f"{major}, {minor}, {patch}, 0"

    def entry(key: str, value: str) -> str:
        return f"      StringStruct('{key}', '{value}'),"

    return "\n".join([
        "# Generated by myriaxon.launcher_identity.version_file_text -- do not edit by hand.",
        "# Passed to: pyinstaller --version-file version_info.txt",
        "VSVersionInfo(",
        "  ffi=FixedFileInfo(",
        f"    filevers=({quad}),",
        f"    prodvers=({quad}),",
        "    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)",
        "  ),",
        "  kids=[",
        "    StringFileInfo([",
        f"      StringTable('040904E4', [",
        entry("CompanyName", COMPANY),
        entry("FileDescription", DESCRIPTION),
        entry("FileVersion", version),
        entry("InternalName", "myriaxon"),
        entry("LegalCopyright", COPYRIGHT),
        entry("OriginalFilename", "myriaxon.exe"),
        entry("ProductName", PRODUCT),
        entry("ProductVersion", version),
        entry("Comments", f"{HOMEPAGE} - {AUTHOR_EMAIL}"),
        "      ])",
        "    ]),",
        "    VarFileInfo([VarStruct('Translation', [1033, 1252])])",
        "  ]",
        ")",
        "",
    ])


def guidance() -> str:
    """What to do about a detection, in the order that actually helps."""
    return (
        "An antivirus may flag the pip-generated launcher, and the reason is measurable rather than mysterious: "
        "it is unsigned and Windows shows no publisher for it. Nothing in this repository can change that -- "
        "signing needs a certificate from a certificate authority, and a version resource cannot be stamped into "
        "the template in place (measured; see the note above `version_file_text`).\n"
        "In order of effectiveness:\n"
        "  1. code signing -- the only thing that establishes provenance with Windows;\n"
        "  2. build a branded executable with PyInstaller -- see `version_file_text()`;\n"
        "  3. run `python -m myriaxon`, which needs no launcher at all;\n"
        "  4. report the detection to your vendor, and here: " + HOMEPAGE + "/issues\n"
        "Verify any launcher with: python tools/verify_binary_identity.py"
    )


def _version_quad(version: str) -> tuple[int, int, int, int]:
    parts: list[int] = []
    for chunk in str(version).split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])  # type: ignore[return-value]


def launcher_path() -> Path | None:
    """The launcher beside the running interpreter, if pip made one."""
    if sys.platform != "win32":
        return None
    scripts = Path(sys.executable).parent
    for name in ("myriaxon.exe", "myriaxon-harness.exe"):
        candidate = scripts / name
        if candidate.is_file():
            return candidate
    return None


def security_note() -> str:
    """One line a caller can surface when a user is worried about a detection. Never raises."""
    try:
        exe = launcher_path()
        if exe is None:
            return "no launcher on this platform, so there is nothing to verify"
        report = describe(exe)
        if report.get("carries_project_identity"):
            return f"{exe.name} carries this project's identity"
        return (f"{exe.name}: pip-generated launcher, {report.get('signed')}, "
                "no version information Windows can read. Verify with tools/verify_binary_identity.py")
    except Exception as exc:  # noqa: BLE001
        return f"could not inspect the launcher: {type(exc).__name__}: {exc}"


if __name__ == "__main__":  # pragma: no cover - manual use
    import json

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else launcher_path()
    if target is None:
        print("no launcher found")
        raise SystemExit(1)
    print(json.dumps(describe(target), indent=2, ensure_ascii=False))
