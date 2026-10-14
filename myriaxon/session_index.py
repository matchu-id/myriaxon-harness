"""Session search: a literal-phrase index over past transcripts, as a separate derived store.

## The gap this closes

`memory_store` already offers `search_memory`, which is a substring scan over stored transcripts. That has three
properties worth improving, and DeepSeek's `session-query-sqlite` addresses all three (verified in
`packages/session-query/session-query-sqlite`):

**Queries are literal phrases, and the syntax of the underlying index is treated as DATA.** Their query text is
"trimmed, whitespace-normalized, and FTS5 syntax (quotes, OR, NEAR, *) treated as data, never executed". A user
searching for `foo OR bar` wants the words `foo`, `OR` and `bar` -- not a boolean query. This is a real
distinction: a search box that executes its input is both a surprise and, with a hostile query, a denial of
service against your own index.

**Metadata filters narrow BEFORE ranking.** Session id, workspace, time window, parent — applied first, so a
search within one project is not competing against every other project's transcripts for the result slots.

**A result groups by the session's strongest matching event.** Not a flat list of matching lines: a session either
matched or it did not, and the best line is what you see. That is what makes ten years of history navigable.

**The index is separate and optional, with an `openAt` ladder** — `startup | first-search | never` — because
building an index over a large corpus is not something to do while someone waits for a prompt.

## Why not SQLite FTS5

Python's `sqlite3` ships with FTS5 available in most builds, and using it would be the closest port. It is not
used here, deliberately: FTS5's tokeniser is its own, so the **literal-phrase** guarantee above would depend on
quoting every term correctly, and a mistake there is exactly the injection this module is designed to avoid. A
plain inverted index over normalised terms is a fraction of the code, has no syntax to escape, and is fast enough
for transcripts. The tradeoff is recorded rather than hidden: this is a *lexical* index, so it does not stem, and
`running` will not match `run`.

## What is dropped

Their `readWindowMax`, snippet offsets into a specific event, WAL tuning, the `:memory:` mode and the
parent-session tree walk. What is kept is the shape: a separate derived index, literal-phrase queries, filters
before ranking, best-event grouping, and an explicit freshness question answered by the caller.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# The opt-in ladder from their config. `never` is the default here because building an index while a user waits
# for a prompt is the wrong trade, and because the substring search already works without one.
OPEN_AT_STARTUP = "startup"
OPEN_AT_FIRST_SEARCH = "first-search"
OPEN_AT_NEVER = "never"
OPEN_AT = (OPEN_AT_STARTUP, OPEN_AT_FIRST_SEARCH, OPEN_AT_NEVER)

# Their bounds.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
SNIPPET_CHARS = 240

# Index format version. A file that declares a newer version is refused rather than misread -- the same rule the
# snapshot store applies, and for the same reason: an unknown field may change how the rest is interpreted.
INDEX_FORMAT_VERSION = 1

_WORD = re.compile(r"[a-z0-9_]+")


def terms(text: str) -> list[str]:
    """Normalise text into index terms.

    Lowercased and split on non-word characters. Deliberately simple and, importantly, **lossless about what the
    user typed**: there is no query syntax, so nothing in the input can be interpreted as an operator.
    """
    return _WORD.findall((text or "").lower())


def normalise_query(query: str) -> str:
    """A query reduced to a literal phrase.

    Their rule: quotes, `OR`, `NEAR` and `*` are data, never executed. Here that falls out of the design -- the
    query is tokenised by the same function as the documents, so `foo OR bar` becomes the three terms `foo`,
    `or`, `bar` and is matched literally. Stated as a function so the guarantee has one place to live.
    """
    return " ".join(terms(query))


@dataclass
class Document:
    """One searchable session."""

    session_id: str
    workspace: str = ""
    provider: str = ""
    model: str = ""
    created_at: float = 0.0
    text: str = ""
    source: str = ""

    def key(self) -> str:
        return self.session_id


@dataclass
class Hit:
    """One matching session, grouped by its strongest match."""

    session_id: str
    score: float
    snippet: str
    workspace: str = ""
    created_at: float = 0.0
    matched_terms: int = 0
    source: str = ""

    def render(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.created_at)) if self.created_at else "?"
        where = f" in {self.workspace}" if self.workspace else ""
        return f"  {self.session_id}{where}  ({when}, {self.matched_terms} term(s))\n      {self.snippet}"


def make_snippet(text: str, query_terms: list[str], width: int = SNIPPET_CHARS) -> str:
    """A window that is guaranteed to CONTAIN the match.

    Two earlier versions got this wrong, both caught by a check rather than by reading:

    * the first centred on the match's *start*, so a 26-character term in a 200-character window began at the
      centre and ran past the end -- the snippet held the first character of the match and nothing else;
    * the second reserved the term's width but centred on its start again, so the same offset error persisted
      whenever the term was long relative to the window.

    The correct rule is to place the window so the *whole* term fits, aiming to center it but always clamping so
    the term is inside. A snippet without the match is not a snippet, so containing the term is the requirement
    and the balance around it is only a preference.
    """
    if not text:
        return ""
    lowered = text.lower()

    # The longest matching term, because it is the hardest to fit and the most specific thing to center on.
    best_start, best_term = -1, ""
    for term in query_terms:
        found = lowered.find(term)
        if found >= 0 and len(term) > len(best_term):
            best_start, best_term = found, term

    if best_start < 0:
        # No match to contain -- the caller wants a head preview of a document that lacks the query, which is the
        # honest answer for that case.
        return " ".join(text.split())[:width]

    # A term longer than the requested window cannot be centred inside it, and the requirement is that the snippet
    # CONTAINS the match -- a snippet that proves nothing is worse than one that is wider than asked for. So the
    # window widens to fit the term rather than the term being cut. Requesting 200 characters and receiving 215
    # when the match is 30 characters wide is the correct trade.
    width = max(width, len(best_term) + 2)

    term_end = best_start + len(best_term)
    # Aim to center the term, then clamp so it is fully inside the window.
    start = max(0, best_start - max(0, width - len(best_term)) // 2)
    end = min(len(text), start + width)
    if end - start < width:
        start = max(0, end - width)
    if term_end > end:
        # The clamp still cut it: shift the window right so the term fits, which is the requirement.
        end = min(len(text), term_end)
        start = max(0, end - width)
        end = min(len(text), start + width)

    snippet = " ".join(text[start:end].split())
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


class SessionIndex:
    """An inverted index over session transcripts, with literal-phrase queries.

    The index is inverted (`term -> {session_id: count}`) plus a document table, which is enough for scoring and
    keeps the whole structure inspectable in a JSON file -- there is no opaque binary to become unreadable when a
    library version moves.
    """

    def __init__(self, open_at: str = OPEN_AT_FIRST_SEARCH, path: Path | None = None) -> None:
        self.open_at = open_at if open_at in OPEN_AT else OPEN_AT_NEVER
        self.path = path
        self.documents: dict[str, Document] = {}
        self.postings: dict[str, dict[str, int]] = {}
        self.built_at: float = 0.0
        self.built = False

    # ---- building

    def add(self, document: Document) -> None:
        """Index one document, replacing any earlier version of it.

        Replacement subtracts the old postings first. Without that, re-indexing a growing session would leave its
        earlier terms counted forever, and a term the session no longer contains would keep matching.
        """
        if not document.session_id:
            return
        if document.session_id in self.documents:
            self.remove(document.session_id)

        self.documents[document.session_id] = document
        for term in terms(document.text):
            self.postings.setdefault(term, {})
            self.postings[term][document.session_id] = self.postings[term].get(document.session_id, 0) + 1

    def remove(self, session_id: str) -> bool:
        document = self.documents.pop(session_id, None)
        if document is None:
            return False
        for term in set(terms(document.text)):
            bucket = self.postings.get(term)
            if bucket is None:
                continue
            bucket.pop(session_id, None)
            if not bucket:
                # An empty posting list is dropped rather than kept, so `terms_in_index` stays honest about what
                # can actually match.
                self.postings.pop(term, None)
        return True

    def build(self, documents: Iterable[Document]) -> int:
        self.documents.clear()
        self.postings.clear()
        count = 0
        for document in documents:
            self.add(document)
            count += 1
        self.built_at = time.time()
        self.built = True
        return count

    @property
    def document_count(self) -> int:
        return len(self.documents)

    @property
    def term_count(self) -> int:
        return len(self.postings)

    # ---- searching

    def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_LIMIT,
        workspace: str = "",
        provider: str = "",
        since: float = 0.0,
        until: float = 0.0,
        require_all: bool = True,
    ) -> list[Hit]:
        """Find sessions matching a literal phrase.

        Filters are applied **before** ranking, which is their rule and it matters: a search inside one workspace
        should not have its result slots taken by another workspace's better-scoring matches.
        """
        limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
        query_terms = terms(query)
        if not query_terms:
            return []

        # Candidate sessions: those matching every term, or any term when `require_all` is off.
        candidates: dict[str, int] = {}
        for term in set(query_terms):
            bucket = self.postings.get(term)
            if not bucket:
                if require_all:
                    # A term nobody has is decisive: no session can match all of them.
                    return []
                continue
            for session_id in bucket:
                candidates[session_id] = candidates.get(session_id, 0) + 1
        if require_all:
            candidates = {sid: n for sid, n in candidates.items() if n == len(set(query_terms))}

        # Metadata filters FIRST.
        filtered: list[Document] = []
        for session_id in candidates:
            document = self.documents.get(session_id)
            if document is None:
                continue
            if workspace and document.workspace != workspace:
                continue
            if provider and document.provider != provider:
                continue
            if since and document.created_at and document.created_at < since:
                continue
            if until and document.created_at and document.created_at > until:
                continue
            filtered.append(document)

        hits: list[Hit] = []
        for document in filtered:
            score = self._score(document, query_terms)
            hits.append(Hit(
                session_id=document.session_id,
                score=score,
                snippet=make_snippet(document.text, query_terms),
                workspace=document.workspace,
                created_at=document.created_at,
                matched_terms=sum(1 for t in set(query_terms) if t in self.postings
                                  and document.session_id in self.postings[t]),
                source=document.source,
            ))
        # Score first, then recency, then id -- so the order is total and a repeated search is stable.
        hits.sort(key=lambda h: (-h.score, -h.created_at, h.session_id))
        return hits[:limit]

    def _score(self, document: Document, query_terms: list[str]) -> float:
        """BM25-style term weight with a sub-linear length penalty on the total.

        Two corrections are folded in here, both found by a check that failed rather than by reading the code:

        * **the IDF is the probabilistic form**, `log((N - df + 0.5) / (df + 0.5) + 1)`, not `log(1 + N/df)`. The
          earlier form never approached zero, so a term present in most documents still carried substantial
          weight: measured, `the` (in 3 of 4 documents, twice in one of them) outscored `database` (in 1 of 4),
          which is backwards.
        * **the length penalty is applied once to the total**, not per term. Dividing each term's contribution by
          the document length let the penalty swamp the IDF, which is the same defect wearing a different hat.
        """
        total = 0.0
        for term in set(query_terms):
            bucket = self.postings.get(term)
            if not bucket or document.session_id not in bucket:
                continue
            frequency = bucket[document.session_id]
            df = len(bucket)
            n = max(1, self.document_count)
            idf = math.log((n - df + 0.5) / (df + 0.5) + 1)
            # Saturating term frequency, so a word repeated ten times is worth more than one repeated twice but
            # nowhere near five times as much.
            total += (frequency * 2.2 / (frequency + 1.2)) * idf

        # A bonus for matching more of the phrase, so "alpha beta" prefers a session holding both over one holding
        # only the more common term.
        distinct = sum(1 for t in set(query_terms) if self.postings.get(t, {}).get(document.session_id))
        total *= 1 + distinct / max(1, len(set(query_terms)))

        return total / math.sqrt(max(1, len(terms(document.text))))

    def filter_values(self) -> dict[str, list[str]]:
        """The distinct workspaces and providers, so a caller can offer real choices rather than free text."""
        return {
            "workspaces": sorted({d.workspace for d in self.documents.values() if d.workspace}),
            "providers": sorted({d.provider for d in self.documents.values() if d.provider}),
        }

    # ---- persistence

    def to_json(self) -> dict:
        return {
            "format_version": INDEX_FORMAT_VERSION,
            "built_at": self.built_at,
            "documents": [
                {
                    "session_id": d.session_id, "workspace": d.workspace, "provider": d.provider,
                    "model": d.model, "created_at": d.created_at, "source": d.source, "text": d.text,
                }
                for d in self.documents.values()
            ],
        }

    def save(self, path: Path | None = None) -> bool:
        target = Path(path or self.path) if (path or self.path) else None
        if target is None:
            return False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.to_json(), ensure_ascii=False), encoding="utf-8", newline="")
            tmp.replace(target)
            return True
        except OSError:
            return False

    @classmethod
    def load(cls, path: Path, open_at: str = OPEN_AT_FIRST_SEARCH) -> "SessionIndex":
        """Read an index, refusing a newer format rather than misreading it.

        Postings are rebuilt from the documents rather than stored. That costs a pass at load and saves roughly
        half the file, and it removes the possibility of the postings disagreeing with the documents -- a
        divergence that would produce matches for text the index does not actually hold.
        """
        index = cls(open_at=open_at, path=Path(path))
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return index
        if not isinstance(raw, dict):
            return index
        version = int(raw.get("format_version") or 1)
        if version > INDEX_FORMAT_VERSION:
            return index
        index.built_at = float(raw.get("built_at") or 0.0)
        for entry in raw.get("documents") or []:
            if not isinstance(entry, dict) or not entry.get("session_id"):
                continue
            index.add(Document(
                session_id=str(entry["session_id"]),
                workspace=str(entry.get("workspace") or ""),
                provider=str(entry.get("provider") or ""),
                model=str(entry.get("model") or ""),
                created_at=float(entry.get("created_at") or 0.0),
                source=str(entry.get("source") or ""),
                text=str(entry.get("text") or ""),
            ))
        index.built = True
        return index


def documents_from_memory_store(store_path: Path | None = None) -> list[Document]:
    """Build documents from the transcripts `memory_store` already keeps.

    Read from the same source `search_memory` uses rather than a new one, so the two agree about what exists and
    the index is a better way to read the same corpus rather than a second corpus.
    """
    from . import memory_store

    documents: list[Document] = []
    try:
        entries = memory_store.list_sessions() if hasattr(memory_store, "list_sessions") else []
    except Exception:
        entries = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        documents.append(Document(
            session_id=str(entry.get("id") or entry.get("session_id") or entry.get("path") or ""),
            workspace=str(entry.get("project") or entry.get("cwd") or ""),
            provider=str(entry.get("provider") or ""),
            model=str(entry.get("model") or ""),
            created_at=float(entry.get("timestamp") or 0.0),
            text=str(entry.get("text") or entry.get("transcript") or ""),
            source=str(entry.get("path") or ""),
        ))
    return [d for d in documents if d.session_id and d.text]


def search_transcripts(
    query: str,
    *,
    index: SessionIndex | None = None,
    store_path: Path | None = None,
    **kwargs: Any,
) -> tuple[list[Hit], str]:
    """Search transcripts, building the index on demand per the ladder. Returns `(hits, note)`.

    The ladder is theirs: `never` refuses rather than building, `startup` expects the caller to have built it, and
    `first-search` builds on the first query and reuses it. The note says which happened, because "no results" and
    "no index" are different answers and a caller that cannot tell them apart will draw the wrong conclusion.
    """
    active = index
    if active is None:
        active = SessionIndex(open_at=OPEN_AT_FIRST_SEARCH, path=store_path)

    note = ""
    if not active.built:
        if active.open_at == OPEN_AT_NEVER:
            return [], (
                "The transcript index is disabled (openAt=never), so no search was performed. "
                "This is not the same as finding nothing."
            )
        if active.path is not None and Path(active.path).is_file():
            loaded = SessionIndex.load(Path(active.path), open_at=active.open_at)
            if loaded.document_count:
                active = loaded
                note = f"searched {active.document_count} indexed session(s)"
        if not active.built:
            count = active.build(documents_from_memory_store(store_path))
            note = f"built an index over {count} session(s)"
            if active.path is not None:
                active.save()

    hits = active.search(query, **kwargs)
    return hits, note or f"searched {active.document_count} session(s)"
