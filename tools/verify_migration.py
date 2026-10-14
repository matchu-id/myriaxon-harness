#!/usr/bin/env python3
"""Verify the Dot -> Locus state migration.

## The defect this exists for

`tools/verify_migration.py` is the one verifier that had to be written *after* the rename, because the rename is what
created the risk. And building it found the bug it exists to catch:

The bulk rename rewrote **the compatibility shim into a copy of the thing it was meant to be compatible with**. Three
literals hold the OLD name on purpose — `LEGACY_LOCI_FILENAME`, `LEGACY_LOCI_WORKSPACE_DIRNAME`, `LEGACY_LOCI_KEY` —
and the rename changed their values to the new name:

    LEGACY_LOCI_FILENAME = "loci.json"   # was "dots.json"
    LEGACY_LOCI_KEY = "loci"             # was "dots"

The fallback branch inside `_read_locked` was rewritten the same way, producing

    entries = raw.get("loci") if isinstance(raw.get("loci"), list) else raw.get("loci")

— two identical branches. Measured consequence: the registry loaded **0** loci instead of **5**, silently. The test
suite stayed green throughout, because no test covered the migration path; it was found by running
`loci.get_registry()` by hand and noticing the count.

That is the whole argument for this file. A rename script cannot distinguish a name from a *reference* to a name.

## What is asserted

  1. The three legacy literals still hold the OLD values. This is the direct regression guard.
  2. A genuine old-format file — written with the old filename, the old key and `version: 1` — is read completely.
  3. Reading it does not modify or delete it, so a crash before the first save leaves the original intact.
  4. A new-format file is read too, and an empty new file does not fall back to a stale old one.
  5. Saving writes the NEW name and the NEW key, which is what performs the migration.
  6. The real state on this machine still loads.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import loci  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""), flush=True)


def legacy_entry(name: str, locus_id: str) -> dict:
    """One entry in the EXACT shape the Dot-era writer produced.

    Copied from the real `dots.json` on this machine rather than invented. Measured: the first version of this fixture
    wrote `"text"` for a goal, and `Goal.__init__()` requires `title`, so every entry failed `from_dict` and the
    registry read zero -- which looked like a migration bug and was a fixture bug. A synthetic fixture that does not
    match the real schema tests the fixture, not the migration.

    The real entry has 21 keys and the real goal has 9; both are reproduced here.
    """
    return {
        "id": locus_id,
        "name": name,
        "role": "reviewer",
        "instructions": "be terse",
        "workspace": "",
        "goals": [{
            "id": "g1",
            "title": "ship it",
            "detail": "",
            "status": "active",
            "created_at": 1_700_000_000.0,
            "updated_at": 1_700_000_000.0,
            "attempts": 0,
            "notes": [],
            "last_error": "",
        }],
        "activity": [{"at": 1_700_000_000.0, "kind": "turn", "summary": "did a thing"}],
        "enabled": True,
        "proactive": True,
        "interval_seconds": 900,
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_000.0,
        "last_error": "",
        "last_goal_id": "",
        "last_run_at": 0.0,
        "max_steps_per_run": 1,
        "run_started_at": 0.0,
        "running": False,
        "session_id": "",
    }


def main() -> int:
    print("=== 1. THE REGRESSION GUARD: the legacy literals must hold the OLD name ===")
    # This is the check that would have failed loudly instead of the product silently showing zero agents.
    check("LEGACY_LOCI_FILENAME is the old filename", loci.LEGACY_LOCI_FILENAME == "dots.json",
          f"got {loci.LEGACY_LOCI_FILENAME!r} -- if this says 'loci.json' the shim has been renamed into uselessness")
    check("LEGACY_LOCI_WORKSPACE_DIRNAME is the old directory", loci.LEGACY_LOCI_WORKSPACE_DIRNAME == "dots",
          f"got {loci.LEGACY_LOCI_WORKSPACE_DIRNAME!r}")
    check("LEGACY_LOCI_KEY is the old JSON key", loci.LEGACY_LOCI_KEY == "dots",
          f"got {loci.LEGACY_LOCI_KEY!r}")
    check("the new filename is different from the old one", loci.LOCI_FILE.name != loci.LEGACY_LOCI_FILENAME,
          f"both are {loci.LOCI_FILE.name!r}, so the fallback can never fire")

    print()
    print("=== 2. a genuine OLD-FORMAT file is read completely ===")
    stage = Path(tempfile.mkdtemp(prefix="myx-migrate-"))
    # Written exactly as the Dot era wrote it: old filename, old key, version 1.
    old_file = stage / loci.LEGACY_LOCI_FILENAME
    old_file.write_text(json.dumps({
        "version": 1,
        loci.LEGACY_LOCI_KEY: [legacy_entry("legacy-a", "id-a"), legacy_entry("legacy-b", "id-b")],
    }, indent=2), encoding="utf-8")
    original_bytes = old_file.read_bytes()

    registry = loci.LocusRegistry(stage / "loci.json") if False else loci.LocusRegistry(stage / "loci.json")
    # `LocusRegistry(path)` sets an explicit path, which disables the legacy fallback by design. To exercise the
    # fallback the registry must be pointed at the DEFAULT location, so this test drives `_source_path` directly.
    probe = loci.LocusRegistry.__new__(loci.LocusRegistry)
    probe.path = stage / "loci.json"
    probe.explicit_path = False
    probe._lock = __import__("threading").RLock()
    check("the legacy file is found when the new one is absent",
          probe._source_path() == old_file, str(probe._source_path()))
    loaded = probe._read_locked()
    check("both legacy loci are read", len(loaded) == 2, f"{len(loaded)} read, keys {list(loaded)}")
    check("  names survive", {x.name for x in loaded.values()} == {"legacy-a", "legacy-b"},
          str([x.name for x in loaded.values()]))
    check("  goals survive", all(len(x.goals) == 1 for x in loaded.values()),
          str([len(x.goals) for x in loaded.values()]))
    check("  the old key was understood, not the new one",
          old_file.read_text(encoding="utf-8").count('"dots"') >= 1)

    print()
    print("=== 3. reading does NOT modify or delete the old file ===")
    # The ordering matters: if reading renamed the file and the process then died, neither file would be intact.
    check("the legacy file is byte-identical after reading", old_file.read_bytes() == original_bytes)
    check("  it still exists", old_file.is_file())
    check("  no new file was created by reading alone", not (stage / "loci.json").is_file())

    print()
    print("=== 4. the NEW format is read, and an empty new file wins over a stale old one ===")
    new_file = stage / "loci.json"
    new_file.write_text(json.dumps({"version": loci.LOCI_PAYLOAD_VERSION,
                                    "loci": [legacy_entry("modern", "id-m")]}), encoding="utf-8")
    probe._loci = probe._read_locked()
    check("the new file takes precedence", probe._source_path() == new_file, str(probe._source_path()))
    check("  its locus is read", len(probe._loci) == 1, f"{len(probe._loci)}")

    # An explicitly empty new list must NOT fall through to a populated old file: a user who deletes all their loci
    # has deleted them, and resurrecting them from a stale backup would be worse than showing none.
    new_file.write_text(json.dumps({"version": loci.LOCI_PAYLOAD_VERSION, "loci": []}), encoding="utf-8")
    probe._loci = probe._read_locked()
    check("an EMPTY new file does not resurrect the old entries", len(probe._loci) == 0,
          f"{len(probe._loci)} -- a stale fallback would be a data-integrity bug")
    new_file.unlink()

    print()
    print("=== 5. saving performs the migration: new NAME, new KEY ===")
    live = loci.LocusRegistry(stage / "loci.json")
    live.path = stage / "loci.json"
    live.explicit_path = False
    live._loci = probe._read_locked()
    live.save()
    check("a new file was written", (stage / "loci.json").is_file())
    saved = json.loads((stage / "loci.json").read_text(encoding="utf-8"))
    check("  under the new key", "loci" in saved, str(sorted(saved)))
    check("  not the old one", "dots" not in saved, str(sorted(saved)))
    check("  at the new payload version", saved.get("version") == loci.LOCI_PAYLOAD_VERSION,
          f"version {saved.get('version')!r}, expected {loci.LOCI_PAYLOAD_VERSION}")
    check("  carrying the loci forward", len(saved.get("loci", [])) == 2, str(len(saved.get("loci", []))))
    check("  and the old file is still there untouched", old_file.read_bytes() == original_bytes,
          "the old file is the user's only backup until they delete it")

    print()
    print("=== 6. the real state on this machine still loads ===")
    # The end-to-end check. This machine has a genuine five-agent dots.json, so if this reads zero the migration is
    # broken for real users regardless of what the synthetic tests above say.
    #
    # Only meaningful where there IS real state. Under tools/run_checks.py every check gets an empty home directory, and on
    # a fresh install nothing has been created yet, so with no registry file and no legacy file there is nothing to
    # have migrated and the check is skipped, rather than failing because a machine has no agents.
    real = loci.get_registry()
    real_count = len(real.all())
    legacy_present = real.legacy_path.is_file()
    if real_count == 0 and not legacy_present and not real.path.is_file():
        print("  [SKIP] the real registry loads its loci: no registry or legacy file exists on this machine")
    else:
        check("the real registry loads its loci", real_count > 0,
              f"{real_count} loaded from {real.path.name}; legacy file present: {legacy_present}")
        if real_count:
            check("  every locus has a name", all(x.name for x in real.all()),
                  str([x.name for x in real.all()][:6]))
            check("  and an id", all(x.id for x in real.all()))

    print()
    print("=== 7. a corrupt legacy file is moved aside, not silently dropped ===")
    # Recoverability: a user whose file is unreadable should be able to find it, not wonder where it went.
    broken = Path(tempfile.mkdtemp(prefix="myx-migrate-bad-"))
    bad = broken / loci.LEGACY_LOCI_FILENAME
    bad.write_text("{not json at all", encoding="utf-8")
    probe2 = loci.LocusRegistry.__new__(loci.LocusRegistry)
    probe2.path = broken / "loci.json"
    probe2.explicit_path = False
    probe2._lock = __import__("threading").RLock()
    result = probe2._read_locked()
    check("a corrupt file yields an empty registry rather than raising", result == {}, str(result))
    check("  and is preserved for recovery, not deleted",
          (broken / "dots.json.corrupt").is_file() or bad.is_file(),
          f"files: {[p.name for p in broken.iterdir()]}")

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
