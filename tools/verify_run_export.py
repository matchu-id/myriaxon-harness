#!/usr/bin/env python3
"""Verify run export.

## The gap this closes

Opencode has `opencode export` with an optional `--sanitize`; PiHarness exports a run dossier plus a redacted debug
bundle. Myriaxon had nothing — the records existed but scattered across `session_store`, `compaction_audit`,
`checkpoints` and the activity logs, with no supported way to hand someone a run.

For a harness whose claim is "prove it by execution", that matters: an unexportable run is one nobody can review.

## What these checks are mostly about

Two properties, both safety rather than features:

  * **sanitised by default.** The transcript holds whatever the agent read. An export is the artefact most likely to
    leave the machine, so the safe setting must be the default and the unsafe one must be asked for by name.
  * **sanitising is reported, not silent.** A reader who cannot tell redaction happened will read `[redacted]` as
    the real value.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import run_export as R  # noqa: E402
from myriaxon import session_store as SS  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []

SECRET = "sk_live_" "abcdefghijklmnopqrstuvwxyz"
TOKEN = "ghp_01" "2345678901234567890123456789012345"


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def seeded(with_secret: bool = False) -> Path:
    root = Path(tempfile.mkdtemp())
    body = f"the key is {SECRET} and the token is {TOKEN}" if with_secret else "nothing sensitive here"
    SS.save_session(
        "anthropic", "claude-sonnet-4-5", "auto", None, False,
        [{"role": "user", "content": "fix the bug"},
         {"role": "assistant", "content": body},
         {"role": "user", "content": "thanks"}],
        root, session_id="demo-1",
    )
    return root


def main() -> int:
    print("=== 1. a bundle has the sections a reviewer needs ===")
    root = seeded()
    bundle = R.build(root)
    check("metadata is populated", bool(bundle.metadata.get("provider")), str(bundle.metadata)[:110])
    check("  it names the session", bundle.metadata.get("session_id") == "demo-1",
          str(bundle.metadata.get("session_id")))
    check("  and the message count", bundle.metadata.get("message_count") == 3,
          str(bundle.metadata.get("message_count")))
    check("the transcript is included", len(bundle.messages) == 3, str(len(bundle.messages)))
    check("the environment is recorded", "platform" in bundle.environment, str(bundle.environment)[:110])
    check("a bundle version is stamped", bundle.bundle_version == R.BUNDLE_VERSION)
    check("an export time is stamped", bundle.exported_at > 0)

    print()
    print("=== 2. THE DEFAULT IS SANITISED ===")
    # Not a convenience flag. An export is the artefact most likely to be pasted into an issue.
    dirty = seeded(with_secret=True)
    default = R.build(dirty)
    check("sanitize defaults to ON", default.sanitized.applied, default.sanitized.render())
    blob = json.dumps(default.to_dict(), default=str)
    check("  the stripe key is GONE", SECRET not in blob, "found the raw key in the default export")
    check("  the github token is GONE", TOKEN not in blob, "found the raw token in the default export")
    check("  a placeholder is present instead", "redacted" in blob.lower(), blob[:0] or "no placeholder")

    print()
    print("=== 3. sanitising is REPORTED, with the kinds ===")
    # A reader who cannot tell redaction happened reads `[redacted]` as the real value.
    check("it says sanitizing was on", "ON" in default.sanitized.render(), default.sanitized.render())
    check("it counts the replacements", default.sanitized.replacements >= 2,
          str(default.sanitized.replacements))
    check("it names the credential kinds", bool(default.sanitized.labels), str(default.sanitized.labels))
    check("  the stripe key is named as a kind",
          any("stripe" in k for k in default.sanitized.labels), str(sorted(default.sanitized.labels)))
    check("  and the github token as a kind",
          any("github" in k for k in default.sanitized.labels), str(sorted(default.sanitized.labels)))
    rendered = default.to_markdown()
    check("the markdown states it too", "sanitize was ON" in rendered, rendered[:200])

    print()
    print("=== 4. --no-sanitize is explicit and WARNS ===")
    raw = R.build(dirty, sanitize=False)
    check("it is marked as not sanitised", not raw.sanitized.applied)
    check("  and says what that means",
          "may contain credentials" in raw.sanitized.render(), raw.sanitized.render())
    raw_blob = json.dumps(raw.to_dict(), default=str)
    check("  the raw key IS present, as asked", SECRET in raw_blob or TOKEN in raw_blob,
          "sanitize=False must actually not sanitize")
    check("  and the warning is in the file too",
          "may contain credentials" in raw.to_markdown(), raw.to_markdown()[:200])

    print()
    print("=== 5. sanitising covers EVERY section, not just the transcript ===")
    # The failure mode of field-by-field sanitising is a section added later escaping it. This asserts the whole
    # bundle is clean rather than the transcript alone.
    check("the whole JSON is free of the secret",
          SECRET not in json.dumps(default.to_dict(), default=str))
    check("the whole markdown is free of the secret", SECRET not in default.to_markdown())
    check("and free of the token", TOKEN not in default.to_markdown())

    print()
    print("=== 6. a clean run says it found nothing, rather than claiming to have cleaned ===")
    clean = R.build(root)
    check("it reports zero replacements", clean.sanitized.replacements == 0,
          str(clean.sanitized.replacements))
    check("  and says so plainly", "found nothing" in clean.sanitized.render(),
          clean.sanitized.render())

    print()
    print("=== 7. an export is a READ: it never mutates the session ===")
    before = SS.load_session(root)
    R.build(root)
    R.build(root, sanitize=False)
    after = SS.load_session(root)
    check("the session file is unchanged", before == after, "exporting must not alter the run")
    check("  the claim field is untouched",
          before.get(SS.SESSION_CLAIM_FIELD) == after.get(SS.SESSION_CLAIM_FIELD))

    print()
    print("=== 8. a missing session produces a NAMED gap, not a crash or a silent empty bundle ===")
    empty = Path(tempfile.mkdtemp())
    bundle = R.build(empty)
    check("it does not raise", isinstance(bundle, R.RunBundle))
    check("it records the gap", any("no session" in g for g in bundle.gaps), str(bundle.gaps))
    check("  the gap is written into the JSON", bool(bundle.to_dict()["incomplete_because"]))
    check("  and into the markdown", "## Incomplete" in bundle.to_markdown(),
          bundle.to_markdown()[-200:])
    check("the bundle is still usable", bundle.to_json().startswith("{"))

    print()
    print("=== 9. write() produces both formats and refuses nonsense ===")
    ok, message, path = R.write(bundle, empty / "b.json", fmt="json")
    check("json writes", ok, message[:100])
    check("  and parses back", json.loads((empty / "b.json").read_text(encoding="utf-8"))["harness"] == "myriaxon")
    ok, message, path = R.write(bundle, empty / "b.md", fmt="markdown")
    check("markdown writes", ok, message[:100])
    check("  and is readable text", (empty / "b.md").read_text(encoding="utf-8").startswith("# Run export"))
    ok, message, _ = R.write(bundle, empty / "b.txt", fmt="yaml")
    check("an unknown format is REFUSED", not ok, message[:90])
    ok, _message, _path = R.write(bundle, empty, fmt="json")
    check("writing onto a directory is refused", not ok)
    # The default destination must be predictable and must not overwrite.
    ok, message, default_path = R.write(bundle)
    check("a default path is chosen", ok and default_path is not None, message[:100])
    check("  and it is named for the session", "session" in (default_path.name if default_path else ""),
          default_path.name if default_path else "")
    if default_path:
        default_path.unlink(missing_ok=True)

    print()
    print("=== 10. budget: a huge transcript is bounded and SAYS it was bounded ===")
    big = Path(tempfile.mkdtemp())
    SS.save_session("anthropic", "m", "auto", None, False,
                    [{"role": "user", "content": "x" * (R.MAX_CHARS_PER_MESSAGE + 5000)}], big,
                    session_id="big")
    bundle = R.build(big)
    check("the message is truncated", len(bundle.messages[0]["content"]) < R.MAX_CHARS_PER_MESSAGE + 600,
          str(len(bundle.messages[0]["content"])))
    check("  and the bundle says so", any("truncated" in g for g in bundle.gaps), str(bundle.gaps))
    check("  the marker is in the text", "omitted" in bundle.messages[0]["content"])

    many = Path(tempfile.mkdtemp())
    SS.save_session("anthropic", "m", "auto", None, False,
                    [{"role": "user", "content": f"m{i}"} for i in range(R.MAX_MESSAGES + 50)], many,
                    session_id="many")
    bundle = R.build(many)
    check("the transcript is capped", len(bundle.messages) == R.MAX_MESSAGES, str(len(bundle.messages)))
    check("  and the cap is reported", any("truncated to the last" in g for g in bundle.gaps),
          str([g for g in bundle.gaps if "truncated" in g]))
    check("  the TAIL is kept, not the head", bundle.messages[-1]["content"] == f"m{R.MAX_MESSAGES + 49}",
          bundle.messages[-1]["content"])

    print()
    print("=== 11. it never raises, whatever it is handed ===")
    for label, kwargs in (
        ("a nonexistent root", {"project_root": Path(tempfile.gettempdir()) / "no-such-dir-xyz"}),
        ("an unknown session id", {"session_id": "not-a-session"}),
        ("no messages", {"include_messages": False}),
    ):
        try:
            result = R.build(**kwargs)
            check(f"handles {label}", isinstance(result, R.RunBundle),
                  f"{len(result.gaps)} gap(s)")
        except Exception as exc:  # noqa: BLE001
            check(f"handles {label}", False, f"{type(exc).__name__}: {exc}")

    print()
    print("=== 12. WIRED: the CLI exposes it ===")
    # A module nothing calls is a library; this repository has hit that four times.
    cli_source = (REPO / "myriaxon" / "cli.py").read_text(encoding="utf-8")
    parser_source = (REPO / "myriaxon" / "argparse_cli.py").read_text(encoding="utf-8")
    check("the export command is registered", 'add_parser(\n        "export"' in parser_source
          or '"export",' in parser_source, "looked for the subparser")
    check("  it is dispatched", '_run_export_command' in cli_source)
    check("  and it exists", "def _run_export_command" in cli_source)
    check("  the unsafe flag is opt-in", "--no-sanitize" in parser_source)
    check("  and its default is sanitised",
          "not args.no_sanitize" in cli_source, "the default must be the safe one")
    check("  the warning is printed when sanitising is off",
          "warning" in cli_source[cli_source.index("def _run_export_command"):][:2000],
          "an unsanitised export must not be silent")

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
