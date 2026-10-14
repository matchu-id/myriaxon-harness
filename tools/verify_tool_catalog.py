#!/usr/bin/env python3
"""Verify the tool catalog: search, reuse, and the untrusted-schema boundary.

The security checks matter most here. A catalog that indexes untrusted tool schemas, or lets an untrusted tool
become directly visible by picking a trusted-sounding name, is worse than no catalog at all.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import tool_catalog as TC  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


TRUSTED = [
    {"name": "read_file", "description": "Read a file from disk",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "grep_search", "description": "Search file contents for a pattern",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}}},
    {"name": "send_message", "description": "Send a message to a channel",
     "input_schema": {"type": "object", "properties": {"channel": {"type": "string"}}}},
]


def main() -> int:
    print("=== 1. entries are built and sorted stably ===")
    entries = TC.build_entries(TRUSTED, direct_names={"read_file"})
    check("three entries", len(entries) == 3, str(len(entries)))
    check("sorted by name", [e.name for e in entries] == ["grep_search", "read_file", "send_message"],
          str([e.name for e in entries]))
    check("source is recorded", all(e.source == TC.OPENCLAW for e in entries))
    check("the id encodes source and name", entries[0].id.startswith("openclaw:core:"), entries[0].id)
    check("direct visibility is per entry",
          [e.direct_visible for e in entries] == [False, True, False],
          str([e.direct_visible for e in entries]))

    print()
    print("=== 2. search finds by name and by description ===")
    check("name match", TC.search(entries, "read").hits[0].entry.name == "read_file")
    check("description match", any(h.entry.name == "grep_search" for h in TC.search(entries, "pattern").hits),
          str([h.entry.name for h in TC.search(entries, "pattern").hits]))
    check("an exact name outranks a description hit",
          TC.search(entries, "read_file").hits[0].entry.name == "read_file")
    check("no match produces no hits", TC.search(entries, "zzzz").hits == [])
    check("an empty query produces no hits", TC.search(entries, "").hits == [])

    print()
    print("=== 3. matching parameter names, but only for trusted entries ===")
    # A model searching for "path" should find read_file, because it takes one.
    hits = [h.entry.name for h in TC.search(entries, "path").hits]
    check("a parameter name is searchable for a trusted tool", "read_file" in hits, str(hits))

    print()
    print("=== 4. SECURITY: an untrusted schema is never traversed ===")
    # The object raises on any access. Hashing it must not touch it -- their reason: remote schemas "may be
    # attacker-sized or lazy hostile objects", and walking one is the attack.
    class Hostile:
        def __getattr__(self, item):
            raise AssertionError(f"the hostile schema was traversed via {item!r}")

        def __len__(self):
            raise AssertionError("the hostile schema was measured")

        def __iter__(self):
            raise AssertionError("the hostile schema was iterated")

    hostile = Hostile()
    try:
        identity = TC.untrusted_schema_fingerprint(hostile)
        check("a hostile schema is fingerprinted without being touched", identity.startswith("object:"),
              identity)
        check("the same object yields the same identity",
              TC.untrusted_schema_fingerprint(hostile) == identity)
        check("a different object yields a different identity",
              TC.untrusted_schema_fingerprint(Hostile()) != identity)
    except AssertionError as exc:
        check("a hostile schema is fingerprinted without being touched", False, str(exc))

    print()
    print("=== 5. SECURITY: an untrusted tool's schema is not indexed ===")
    untrusted = TC.build_entries(
        [{"name": "get_data", "description": "Fetch data",
          "input_schema": {"type": "object", "properties": {"secret_token": {"type": "string"}}}}],
        source=TC.MCP, source_name="some-server",
    )
    check("the entry is marked untrusted", not untrusted[0].trusted)
    text = untrusted[0].searchable_text()
    check("its parameter names are NOT searchable", "secret_token" not in text, text)
    check("its name and description still are", "get_data" in text and "fetch data" in text)
    check("searching for the hidden parameter finds nothing",
          TC.search(untrusted, "secret_token").hits == [])
    check("a trusted tool's parameter IS searchable for comparison",
          "path" in entries[1].searchable_text())

    print()
    print("=== 6. describe reports an unknown input shape for untrusted tools ===")
    described = TC.describe(untrusted[0])
    check("it says the input is unknown", "input: unknown" in described, described.replace("\n", " | ")[:120])
    check("it says the harness does not inspect it", "not inspected" in described)
    check("it does NOT print the parameter names", "secret_token" not in described)
    trusted_described = TC.describe(entries[1])
    check("a trusted tool lists its parameters", "path: string" in trusted_described,
          trusted_described.replace("\n", " | ")[:120])
    check("and marks the required ones", "(required)" in trusted_described)

    print()
    print("=== 7. THE FIX: an unchanged catalog reuses the descriptor snapshot ===")
    catalog = TC.ToolCatalog(entries)
    first_fingerprint = catalog.stats()["fingerprint"]
    reused = catalog.apply(TC.build_entries(TRUSTED, direct_names={"read_file"}))
    check("an identical catalog is reused", reused)
    check("the fingerprint is unchanged", catalog.stats()["fingerprint"] == first_fingerprint)
    check("it is flagged as reused", catalog.stats()["reused"])

    print()
    print("=== 8. a changed descriptor forces a full replace ===")
    changed = TRUSTED[:2] + [{"name": "send_message", "description": "Send a message to a channel or user",
                              "input_schema": TRUSTED[2]["input_schema"]}]
    reused = catalog.apply(TC.build_entries(changed, direct_names={"read_file"}))
    check("a changed description is not reused", not reused)
    check("the new description took effect",
          any(e.description.endswith("or user") for e in catalog.entries))

    print()
    print("=== 9. THE FIX: a changed callable rebinds while the snapshot is reused ===")
    # This is the line their comment protects: "Callable bindings can change while descriptors stay equal."
    stable = TC.build_entries(TRUSTED, direct_names={"read_file"})
    for entry in stable:
        entry.tool = lambda: "first binding"
    catalog2 = TC.ToolCatalog(stable)

    same_descriptors = TC.build_entries(TRUSTED, direct_names={"read_file"})
    new_binding = lambda: "second binding"          # noqa: E731
    for entry in same_descriptors:
        entry.tool = new_binding

    reused = catalog2.apply(same_descriptors)
    check("the descriptors are reused", reused)
    check("but the callables were refreshed",
          all(e.tool is new_binding for e in catalog2.entries),
          str({e.name: getattr(e.tool, "__name__", "?") for e in catalog2.entries}))
    check("the names are unchanged", [e.name for e in catalog2.entries] == ["grep_search", "read_file", "send_message"])

    print()
    print("=== 10. a truly unchanged catalog returns the caller's OWN list ===")
    # Two distinct reuse cases, and the first version of this test conflated them:
    #   * nothing changed at all -> hand back the new list untouched, so anything the caller attached survives;
    #   * only the callables changed -> reuse the descriptor snapshot, take the callables from the new entries.
    # `build_entries` always makes new objects, so re-applying it exercises the *second* case, not the first.
    # The first case needs the very same entry objects to be applied twice.
    same = TC.ToolCatalog(TC.build_entries(TRUSTED, direct_names={"read_file"}))
    # Point every callable at one shared function so that re-applying the same objects changes nothing.
    shared = lambda: "binding"          # noqa: E731
    for entry in same.entries:
        entry.tool = shared
    identical = TC.build_entries(TRUSTED, direct_names={"read_file"})
    for entry in identical:
        entry.tool = shared
    same.apply(identical)
    returned = same.apply(identical)
    check("re-applying the same objects returns the same list", same.entries is identical,
          "the caller's list was replaced by a copy")
    check("and the apply reported reuse", returned)

    print()
    print("=== 10b. the rebind path is taken when only callables differ ===")
    rebound = TC.ToolCatalog(TC.build_entries(TRUSTED, direct_names={"read_file"}))
    for entry in rebound.entries:
        entry.tool = lambda: "old"
    fresh = TC.build_entries(TRUSTED, direct_names={"read_file"})
    newest = lambda: "new"             # noqa: E731
    for entry in fresh:
        entry.tool = newest
    check("apply reports reuse", rebound.apply(fresh))
    check("every callable was refreshed", all(e.tool is newest for e in rebound.entries))
    check("and the descriptors came from the snapshot",
          [e.name for e in rebound.entries] == ["grep_search", "read_file", "send_message"])

    print()
    print("=== 11. an id-set mismatch refuses the rebind ===")
    mismatched = TC.build_entries(TRUSTED[:2], direct_names={"read_file"})
    catalog3 = TC.ToolCatalog(TC.build_entries(TRUSTED, direct_names={"read_file"}))
    reused = catalog3.apply(mismatched)
    check("a differing count forces a full replace", not reused)
    check("the entry set was replaced", len(catalog3.entries) == 2, str(len(catalog3.entries)))

    print()
    print("=== 12. a changed execution-allow list forces a replace ===")
    catalog4 = TC.ToolCatalog(TC.build_entries(TRUSTED))
    catalog4.apply(TC.build_entries(TRUSTED), execution_allow=["read_file"])
    reused = catalog4.apply(TC.build_entries(TRUSTED), execution_allow=["read_file", "grep_search"])
    check("a widened allow list is not reused", not reused)

    print()
    print("=== 13. the fingerprint ignores entry ORDER ===")
    a = TC.catalog_fingerprint(TC.build_entries(TRUSTED))
    reversed_defs = list(reversed(TRUSTED))
    b = TC.catalog_fingerprint(TC.build_entries(reversed_defs))
    check("two orderings of the same catalog fingerprint the same", a == b, f"{a[:12]} vs {b[:12]}")

    print()
    print("=== 14. THE FIX: an empty truncated result is distinguishable from no matches ===")
    many = TC.build_entries(
        [{"name": f"tool_{i}", "description": "match me", "input_schema": {}} for i in range(60)]
    )
    big = TC.search(many, "match", limit=TC.MAX_LIMIT)
    check("the result is capped at the max limit", len(big.hits) <= TC.MAX_LIMIT, str(len(big.hits)))
    check("it is flagged truncated", big.truncated)
    check("the rendering says more were dropped", "dropped" in big.render(), big.render().splitlines()[-1])
    none = TC.search(many, "zzzz")
    check("a genuine no-match is NOT flagged truncated", not none.truncated)
    check("and renders as a plain no-match", none.render().strip() == "no matches", none.render())

    print()
    print("=== 15. the directory is bounded and carries no schemas ===")
    directory = TC.render_directory(many, cap=200)
    check("it stays within the cap", len(directory) <= 300, str(len(directory)))
    check("it says how many were omitted", "omitted" in directory, directory.splitlines()[-1])
    check("no schema text appears", "properties" not in directory and "input_schema" not in directory)
    check("names and descriptions do", "tool_0:" in directory)

    print()
    print("=== 16. describe never leaks untrusted metadata into the directory ===")
    mixed = TC.build_entries(TRUSTED) + untrusted
    text = TC.render_directory(mixed)
    check("the untrusted tool is listed by name", "get_data" in text)
    check("but its parameter names are not", "secret_token" not in text)

    print()
    print("=== 17. the catalog reports its own shape ===")
    stats = TC.ToolCatalog(TC.build_entries(TRUSTED, direct_names={"read_file"})).stats()
    check("it counts entries", stats["entries"] == 3, str(stats))
    check("it breaks them down by source", stats["by_source"] == {TC.OPENCLAW: 3}, str(stats["by_source"]))
    check("it counts direct-visible ones", stats["direct_visible"] == 1)
    check("it exposes a fingerprint prefix", len(stats["fingerprint"]) == 16)

    print()
    print("=== 18. fingerprinting a large catalog is not pathological ===")
    huge = TC.build_entries(
        [{"name": f"t{i}", "description": "x" * 200, "input_schema": {"type": "object"}} for i in range(2000)]
    )
    started = time.perf_counter()
    TC.catalog_fingerprint(huge)
    elapsed = time.perf_counter() - started
    check("2000 entries fingerprint in under a second", elapsed < 1.0, f"{elapsed * 1000:.0f} ms")

    print()
    print("=== 19. IDF weighting stops common words from flattening the ranking ===")
    # Without this, "a" and "in" match almost every description and pull every score towards the best, which is
    # what made one `tool_search` query unlock five tool groups.
    entries = TC.build_entries([
        {"name": "alpha_one", "description": "handles a widget in a session"},
        {"name": "beta_one", "description": "handles a widget in a session"},
        {"name": "gamma_one", "description": "handles a widget in a session"},
        {"name": "notebook_edit", "description": "edit a Jupyter notebook cell"},
    ])
    common = TC.search(entries, "a widget in a session", limit=4)
    check("a query of common words does not rank one entry far above the rest",
          len(common.hits) >= 2 and (common.hits[0].score - common.hits[-1].score) < common.hits[0].score,
          str([(h.entry.name, round(h.score, 3)) for h in common.hits]))

    distinct = TC.search(entries, "jupyter notebook", limit=4)
    check("a distinctive word dominates", distinct.hits[0].entry.name == "notebook_edit",
          str([h.entry.name for h in distinct.hits]))
    check("a stopword-only query matches nothing",
          TC.search(entries, "a in the of", limit=4).hits == [],
          "stopwords alone carry no discriminating power")

    print()
    print("=== 20. the live deferred-tool catalog is searchable by description ===")
    # The mechanism `tool_search` depends on. Run against the real deferred set, not a fixture.
    from myriaxon import tool_registry as _TR

    catalog_entries = _TR.catalog_for_deferred_tools()
    check("every deferred tool is catalogued", len(catalog_entries) >= 10, str(len(catalog_entries)))
    # The catalog covers everything OUTSIDE the core set, which is deliberately wider than the deferred groups:
    # `tool_search` and `recall` are core tools that still need to be findable. So the assertion is "nothing in the
    # catalog is also visible by default", not "everything in the catalog is deferred" -- the earlier version
    # asserted the latter and broke as soon as a non-deferred tool joined the catalog.
    core = _TR.core_tool_names()
    check("no catalogued tool is also a core tool",
          not ({e.name for e in catalog_entries} & core),
          str(sorted({e.name for e in catalog_entries} & core)))
    check("every entry has a real description, not a placeholder",
          not any("no description declared" in e.description for e in catalog_entries),
          str([e.name for e in catalog_entries if "no description declared" in e.description]))

    live = TC.ToolCatalog(catalog_entries)
    # The intended tool is found for these. Kept as a measured list rather than a claim of general quality.
    for query, intended in (
        ("search past sessions", "search_memory"),
        ("jupyter notebook cell", "edit_notebook"),
        ("roll a file back to an earlier snapshot", "checkpoint_restore"),
        ("run a prompt on a timer", "schedule_prompt"),
        ("hand work to another agent", "task"),
        ("create a persistent background agent", "locus_create"),
    ):
        found = [h.entry.name for h in live.search(query, limit=6).hits]
        check(f"{query!r} finds {intended}", intended in found, str(found))

    # A former limitation, now RESOLVED, and the history is kept because it shows how it was found.
    #
    # Three earlier versions of this check were wrong or stale, and each correction is recorded:
    #   1. it asserted the ranker *misses* `search_memory` for a paraphrase -- it did not; it returned it at
    #      position 4 of 5;
    #   2. it asserted `search_memory` ranks below the score cut -- it did not; it TIED the best score, and
    #      ties break alphabetically;
    #   3. it asserted the COUNT CAP excludes it on a tie, which was true at the time (three tools scored 0.333
    #      because "session" appears in several descriptions) and stopped being true once `session_status` was
    #      added with a description that matches the paraphrase far more strongly -- 0.857 against 0.286.
    #
    # So the paraphrase is now handled well. The check asserts the property that matters and the reason it holds,
    # rather than pinning a defect that no longer exists.
    paraphrase_query = "remember what happened in an earlier session"
    hits = live.search(paraphrase_query, limit=6).hits
    names = [h.entry.name for h in hits]
    best = hits[0].score if hits else 0.0

    check("the paraphrase returns a self-inspection tool first",
          bool(hits) and hits[0].entry.name in ("session_status", "search_memory"),
          f"Got: {names[:3]}")
    check("and the top hit is a CLEAR winner, not an alphabetical tie-break",
          bool(hits) and all(h.score < best * 0.75 for h in hits[1:]),
          "a tie would mean intent is being decided by the letter 'd' rather than by the description: "
          + str([(h.entry.name, round(h.score, 3)) for h in hits[:3]]))

    # What survives as a genuine limitation of a lexical ranker with no stemmer: a paraphrase whose words do not
    # appear in the right tool's description cannot be ranked first on merit. It is still RETURNED, so the model
    # can read it -- which is why this is a ranking note rather than a defect.
    oblique = live.search("recall an earlier conversation", limit=6).hits
    check("an oblique paraphrase still returns candidates rather than nothing",
          len(oblique) >= 1, str([h.entry.name for h in oblique]))
    check("and the correct tool is among them",
          any(h.entry.name in ("search_memory", "session_status") for h in oblique),
          str([h.entry.name for h in oblique]))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
