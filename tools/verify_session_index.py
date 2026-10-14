#!/usr/bin/env python3
"""Verify the session search index.

The claim under test is that a query is matched LITERALLY -- nothing a user types is interpreted as an operator --
and that metadata filters narrow before ranking rather than competing for result slots.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import session_index as SI  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def sample_docs() -> list[SI.Document]:
    return [
        SI.Document(session_id="s1", workspace="/alpha", provider="anthropic",
                    created_at=1_700_000_000.0, source="/t/s1",
                    text="Refactored the authentication module and fixed a token refresh bug."),
        SI.Document(session_id="s2", workspace="/beta", provider="openai",
                    created_at=1_700_100_000.0, source="/t/s2",
                    text="Wrote tests for the authentication module. The token logic needed care."),
        SI.Document(session_id="s3", workspace="/alpha", provider="anthropic",
                    created_at=1_700_200_000.0, source="/t/s3",
                    text="Investigated a slow database query and added an index."),
        SI.Document(session_id="s4", workspace="/gamma", provider="gemini",
                    created_at=1_700_300_000.0, source="/t/s4",
                    text="Discussed OR and NEAR and the * wildcard as literal search text."),
    ]


def main() -> int:
    print("=== 1. a query is matched literally ===")
    check("a phrase normalises to its terms",
          SI.normalise_query("token refresh") == "token refresh")
    check("punctuation is not syntax",
          SI.normalise_query('foo OR bar') == "foo or bar",
          "OR becomes an ordinary term, not a boolean operator")
    check("quotes are data", SI.normalise_query('"token"') == "token")
    check("a wildcard is data", SI.normalise_query("tok*") == "tok")
    check("NEAR is data", SI.normalise_query("alpha NEAR beta") == "alpha near beta")
    check("an empty query has no terms", SI.terms("") == [])
    check("underscores and digits survive",
          SI.terms("snake_case 2x") == ["snake_case", "2x"])

    print()
    print("=== 2. THE FIX: a query full of search syntax finds the DOCUMENT ABOUT that syntax ===")
    # The behaviour a search box must have: searching for operators searches for the words.
    index = SI.SessionIndex()
    index.build(sample_docs())
    hits = index.search("OR NEAR wildcard", require_all=False)
    check("it matched the session that discusses those words",
          "s4" in [h.session_id for h in hits], str([h.session_id for h in hits]))
    check("require_all=True finds it too (all three words are present)",
          [h.session_id for h in index.search("OR NEAR wildcard")] == ["s4"],
          str([h.session_id for h in index.search("OR NEAR wildcard")]))

    print()
    print("=== 3. a term nobody has is decisive ===")
    check("an absent term with require_all returns nothing",
          index.search("authentication zzzznotpresent") == [])
    check("without require_all it still matches on the present term",
          len(index.search("authentication zzzznotpresent", require_all=False)) == 2)

    print()
    print("=== 4. results group by SESSION, not by matching line ===")
    hits = index.search("authentication")
    check("two sessions matched", len(hits) == 2, str([h.session_id for h in hits]))
    check("each hit carries a snippet", all(h.snippet for h in hits))
    check("the snippet contains the matched term",
          all("authentication" in h.snippet.lower() for h in hits),
          str([h.snippet for h in hits]))

    print()
    print("=== 5. THE FIX: a snippet is centred on the match ===")
    # A transcript's opening line is almost never what matched, and a snippet without the match is not a snippet.
    #
    # The needle has to sit FURTHER from the start than the window is wide, or the check proves nothing: the first
    # version used 12 filler repeats with a 200-character window, so the correct centred snippet started at offset
    # 624 and could not reach a match at 628 -- it failed for arithmetic reasons, not because centring was broken.
    filler = "opening filler that is very long and uninteresting. " * 40
    long_text = filler + "THE_NEEDLE_TERM appears here."
    check("the needle is beyond the window width from the start",
          long_text.index("THE_NEEDLE_TERM") > 200,
          f"offset {long_text.index('THE_NEEDLE_TERM')}")

    snippet = SI.make_snippet(long_text, ["the_needle_term"], width=200)
    # Compared case-insensitively: terms are lowercased for indexing while the stored text keeps its case, so a
    # case-sensitive containment check fails on correct output. The earlier version of this check did that and
    # reported a snippet that did contain the match as not containing it.
    check("the snippet contains the match", "the_needle_term" in snippet.lower(), snippet[-70:])
    check("it is bounded", len(snippet) < 260, str(len(snippet)))
    # Only the LEADING marker is asserted. The window ends at the end of the text here, so there is nothing after
    # it to elide -- a trailing "..." would claim text was cut when none was. The earlier version required both
    # markers and failed on correct output.
    check("it is marked as cut from the START", snippet.startswith("..."), snippet[:40])
    check("it is NOT marked as cut at the end, because nothing follows",
          not snippet.endswith("..."), snippet[-40:])
    check("a window with text after it DOES get a trailing marker",
          SI.make_snippet(filler + "MIDDLE_TERM" + filler, ["middle_term"], width=200).endswith("..."),
          "the marker has to be conditional, not always applied")
    check("a head-only window is NOT what was returned",
          not snippet.lstrip(".").lower().startswith("opening filler"),
          "a snippet taken from the start would not contain the match at all")

    # The hard case that a naive centring gets wrong: a term LONGER than half the window. Centring on the match's
    # start puts its tail outside the boundary, so the snippet holds a prefix of the match and nothing usable.
    wide_term = "a_very_long_matched_term_indeed"
    wide_text = filler + wide_term.upper() + " and then more text."
    wide_snippet = SI.make_snippet(wide_text, [wide_term], width=len(wide_term) + 20)
    check("a term wider than the window's margin is still fully included",
          wide_term in wide_snippet.lower(), wide_snippet)
    check("a term wider than the ENTIRE window is still included",
          wide_term in SI.make_snippet(wide_text, [wide_term], width=len(wide_term) - 5).lower(),
          "losing the match would leave a snippet that proves nothing")

    check("a document with no match falls back to the head",
          SI.make_snippet("some text", ["absent"], width=200) == "some text")
    check("an empty document yields an empty snippet", SI.make_snippet("", ["x"]) == "")

    print()
    print("=== 6. THE FIX: metadata filters narrow BEFORE ranking ===")
    # Their rule, and it matters: a search inside one workspace must not lose result slots to another workspace's
    # better-scoring matches.
    everything = index.search("module", require_all=False)
    check("an unfiltered search sees both workspaces",
          len({h.workspace for h in everything}) > 1, str([h.workspace for h in everything]))

    alpha = index.search("module", workspace="/alpha", require_all=False)
    check("filtering to one workspace excludes the other",
          all(h.workspace == "/alpha" for h in alpha), str([h.workspace for h in alpha]))

    # The decisive version: a query whose BEST match is outside the filter must still return the filtered one.
    check("the filtered result is not empty just because the best match was excluded",
          len(alpha) >= 1, str(len(alpha)))
    check("filtering by provider works",
          all(h.session_id != "s2" for h in index.search("module", provider="anthropic",
                                                         require_all=False)),
          str([h.session_id for h in index.search("module", provider="anthropic", require_all=False)]))

    print()
    print("=== 7. a time window filters before ranking too ===")
    recent = index.search("module", since=1_700_150_000.0, require_all=False)
    check("sessions before the window are excluded",
          all(h.session_id != "s1" for h in recent), str([h.session_id for h in recent]))
    older = index.search("module", until=1_700_050_000.0, require_all=False)
    check("sessions after the window are excluded",
          all(h.session_id in ("s1",) for h in older), str([h.session_id for h in older]))

    print()
    print("=== 8. ranking prefers a better match, and is stable ===")
    ranked = index.search("token", require_all=False)
    check("both token sessions are found", len(ranked) == 2, str([h.session_id for h in ranked]))
    check("the order is stable across calls",
          [h.session_id for h in index.search("token", require_all=False)]
          == [h.session_id for h in ranked])
    check("scores are positive", all(h.score > 0 for h in ranked))
    check("matching more of a phrase ranks higher", (
        index.search("authentication token")[0].session_id == "s1"
    ), str([(h.session_id, round(h.score, 3)) for h in index.search("authentication token")]))

    print()
    print("=== 9. IDF and length normalisation are applied ===")
    # "the" appears in three of four documents, so its IDF is low; a rare term should outrank it.
    common = index.search("the", require_all=False)
    rare = index.search("database", require_all=False)
    check("a common term matches many sessions", len(common) >= 3, str(len(common)))
    check("a rare term matches few", len(rare) == 1, str(len(rare)))
    check("the rare match scores higher than any common one",
          rare[0].score > max(h.score for h in common),
          f"rare {rare[0].score:.3f} vs best common {max(h.score for h in common):.3f}")

    print()
    print("=== 10. re-indexing a document REPLACES its postings ===")
    # Without the subtraction, a session's earlier terms would be counted forever and a term it no longer
    # contains would keep matching.
    growing = SI.SessionIndex()
    growing.add(SI.Document(session_id="g1", text="alpha beta gamma"))
    check("the first version is searchable", len(growing.search("alpha")) == 1)
    growing.add(SI.Document(session_id="g1", text="delta only now"))
    check("the old terms no longer match", growing.search("alpha") == [],
          "re-indexing must subtract the previous postings")
    check("the new terms do", len(growing.search("delta")) == 1)
    check("the document count did not grow", growing.document_count == 1, str(growing.document_count))

    print()
    print("=== 11. removing a document drops its empty posting lists ===")
    shrinking = SI.SessionIndex()
    shrinking.build([SI.Document(session_id="r1", text="unique_zzz term")])
    check("the unique term is indexed", "unique_zzz" in shrinking.postings)
    shrinking.remove("r1")
    check("the document is gone", shrinking.document_count == 0)
    check("the empty posting list is dropped", "unique_zzz" not in shrinking.postings,
          "an empty bucket would make term_count claim a term can match when it cannot")
    check("removing a missing document reports so", not shrinking.remove("nope"))

    print()
    print("=== 12. bounds are enforced ===")
    check("the limit is capped at the maximum",
          len(index.search("the", limit=10_000, require_all=False)) <= SI.MAX_LIMIT)
    check("a limit of zero yields at least one", len(index.search("the", limit=0, require_all=False)) >= 1)
    check("an empty query returns nothing", index.search("") == [])
    check("a query of only punctuation returns nothing", index.search("!!! ???") == [])

    print()
    print("=== 13. the index round-trips through JSON ===")
    tmp = Path(tempfile.mkdtemp()) / "index.json"
    index.path = tmp
    check("it saved", index.save())
    loaded = SI.SessionIndex.load(tmp)
    check("every document came back", loaded.document_count == index.document_count,
          f"{loaded.document_count} vs {index.document_count}")
    check("postings were rebuilt", loaded.term_count == index.term_count,
          f"{loaded.term_count} vs {index.term_count}")
    check("search behaves identically after a round-trip",
          [h.session_id for h in loaded.search("authentication")]
          == [h.session_id for h in index.search("authentication")])
    check("the metadata survived", loaded.documents["s1"].workspace == "/alpha")
    check("the file declares its format version",
          json.loads(tmp.read_text(encoding="utf-8"))["format_version"] == SI.INDEX_FORMAT_VERSION)

    print()
    print("=== 14. a newer format is refused rather than misread ===")
    future = Path(tempfile.mkdtemp()) / "future.json"
    future.write_text(json.dumps({"format_version": 99, "documents": [
        {"session_id": "x", "text": "should not be read"}]}), encoding="utf-8")
    refused = SI.SessionIndex.load(future)
    check("the newer index is not loaded", refused.document_count == 0,
          "an unknown field may change how the rest is interpreted")
    check("and nothing matches in it", refused.search("read") == [])

    print()
    print("=== 15. a missing or corrupt index yields an empty one, not an exception ===")
    check("a missing file loads empty", SI.SessionIndex.load(Path("nope-not-here.json")).document_count == 0)
    broken = Path(tempfile.mkdtemp()) / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    check("a corrupt file loads empty", SI.SessionIndex.load(broken).document_count == 0)
    notdict = Path(tempfile.mkdtemp()) / "list.json"
    notdict.write_text("[1,2,3]", encoding="utf-8")
    check("a non-object loads empty", SI.SessionIndex.load(notdict).document_count == 0)

    print()
    print("=== 16. THE FIX: the openAt ladder is honoured, and 'no index' != 'no results' ===")
    disabled = SI.SessionIndex(open_at=SI.OPEN_AT_NEVER)
    hits, note = SI.search_transcripts("anything", index=disabled)
    check("never refuses to build", hits == [])
    check("the note explains WHY it is empty", "disabled" in note, note)
    check("it says this is not the same as finding nothing",
          "not the same as finding nothing" in note, note)

    check("first-search builds on demand",
          SI.SessionIndex(open_at=SI.OPEN_AT_FIRST_SEARCH).open_at == SI.OPEN_AT_FIRST_SEARCH)
    check("an unknown openAt falls back to never",
          SI.SessionIndex(open_at="whenever").open_at == SI.OPEN_AT_NEVER,
          "an unrecognised opt-in must not enable work the caller did not ask for")

    print()
    print("=== 17. filter_values offers real choices ===")
    values = index.filter_values()
    check("workspaces are listed", sorted(values["workspaces"]) == ["/alpha", "/beta", "/gamma"],
          str(values["workspaces"]))
    check("providers are listed", sorted(values["providers"]) == ["anthropic", "gemini", "openai"],
          str(values["providers"]))

    print()
    print("=== 18. the note distinguishes built from loaded ===")
    scratch = Path(tempfile.mkdtemp()) / "auto.json"
    live = SI.SessionIndex(open_at=SI.OPEN_AT_FIRST_SEARCH, path=scratch)
    live.build(sample_docs())
    hits2, note2 = SI.search_transcripts("authentication", index=live)
    check("a built index reports a search", "searched" in note2, note2)
    check("it found the sessions", len(hits2) == 2, str(len(hits2)))

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
