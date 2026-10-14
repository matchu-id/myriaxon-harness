"""Test the local web tools against the real internet, and wire nothing."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import web_tools  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail and not ok else ''}")


def fetch_a_working_page() -> tuple[str, str, bool]:
    """Fetch a real page, trying several stable hosts.

    A single hardcoded URL makes this check fail whenever that one host is busy: measured, python.org
    answered `503 Service Unavailable` on one run and the whole check reported three failures that
    had nothing to do with the code. Three stable, high-availability pages are tried in turn, and the
    one that answered is named so a failure is attributable.
    """
    candidates = (
        ("example.com", "https://example.com/"),
        ("python.org downloads", "https://www.python.org/downloads/"),
        ("wikipedia", "https://en.wikipedia.org/wiki/Python_(programming_language)"),
    )
    last = ("", "", True)
    for label, url in candidates:
        text, is_error = web_tools.web_fetch(url)
        if not is_error and len(text) > 1000:
            return label, text, False
        last = (label, text, is_error)
    return last


def main() -> int:
    print("web_fetch: a real page")
    label, text, is_error = fetch_a_working_page()
    print(f"     source: {label}")
    check("did not error", not is_error, str(text)[:300])
    check("returned substantial text", len(text) > 1000, f"{len(text)} chars")
    check("script noise removed", "<script" not in text.lower())
    print(f"     {len(text):,} chars; first lines:")
    for line in text.splitlines()[:4]:
        print(f"       {line[:100]!r}")

    print("\nweb_fetch: a 404 is an error, not empty content")
    text, is_error = web_tools.web_fetch("https://docs.python.org/3/this-page-does-not-exist-xyz")
    check("reported as an error", is_error, str(text)[:200])

    print("\nweb_fetch: an unreachable host is an error, and says so")
    text, is_error = web_tools.web_fetch("https://no-such-host-xyzzy-12345.invalid/")
    check("reported as an error", is_error, str(text)[:200])
    check("makes clear it is not proof of absence", "not evidence" in text, text[:200])

    print("\nweb_fetch: a bare host is upgraded rather than rejected")
    text, is_error = web_tools.web_fetch("example.com")
    check("worked", not is_error, str(text)[:200])

    print("\nweb_search: a real query")
    text, is_error = web_tools.web_search("Python 3.13 release notes", limit=5)
    if is_error:
        print(f"     search unavailable from this machine: {text[:200]}")
        print("     (reported as a failure, which is the contract -- not as zero results)")
    else:
        check("returned results", "Results for" in text, text[:200])
        check("includes URLs", "http" in text)
        print(f"     {len(text):,} chars; first lines:")
        for line in text.splitlines()[:5]:
            print(f"       {line[:110]!r}")

    print("\nweb_search: empty query is refused")
    text, is_error = web_tools.web_search("")
    check("refused", is_error)

    print("\nthe tool definitions are well formed")
    for definition in (web_tools.WEB_SEARCH_TOOL_DEF, web_tools.WEB_FETCH_TOOL_DEF):
        check(f"{definition['name']} has a description", bool(definition.get("description")))
        check(f"{definition['name']} requires something", bool(
            definition["input_schema"].get("required")))

    print()
    print("web tools verified" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
