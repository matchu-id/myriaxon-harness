"""A searchable tool catalog: keep schemas out of the prompt, keep them findable.

## The gap this closes

Myriaxon sends 18 tool definitions to the model on every request — measured at 13,407 characters, roughly 3,351
tokens, against a ~350-token system prompt. Tool schemas are about ten times the prompt they accompany, and they
are re-sent on every single turn. `tool_registry` already defers seven groups, which cuts the visible set to six
definitions (3,774 chars, ~943 tokens), but what remains is a blunt instrument: a tool is either always visible
or reachable only by guessing a group name.

OpenClaw's Tool Search replaces that with a *searchable catalog* (verified in `src/agents/tool-search-catalog.ts`
and `tool-search-types.ts`): hidden tools stay out of the prompt, three small control tools
(`tool_search`/`tool_describe`/`tool_call`) let the model find and invoke them, and a bounded directory is placed
**above the cache boundary** so an unchanged catalog does not invalidate the prompt prefix.

Three ideas from it are worth copying exactly, and one is worth copying because it is a security decision.

**Fingerprint the descriptors, reuse the snapshot, refresh only the callables.** Their
`rebindCatalogExecutors` runs only when the fingerprint *and* the execution-allow list are unchanged, and returns
`undefined` (forcing a full re-register) unless the id sets are **exactly equal** in size and every existing id
still resolves. When they are, it rebuilds `{...entry, tool}` — the descriptor snapshot is reused and the
callable binding is swapped. Their reason: *"Callable bindings can change while descriptors stay equal."* This is
what keeps the prompt prefix stable across turns.

**Asymmetric fingerprinting of trusted and untrusted schemas.** For an `openclaw` source they hash the raw
`parameters` and `outputSchema`. For `mcp` and `client` sources they use an **opaque per-object identity** from a
WeakMap and never traverse the schema at all, because *"Remote/client schemas may be attacker-sized or lazy
hostile objects."* Hashing an attacker-controlled object means walking it, and walking it is the attack.

**Untrusted schemas are never indexed.** Their MCP and client tools match on name and description only, and are
reported with `input: "unknown"`; calling one requires `tool_describe` first. So a hostile server cannot get its
text into the search index, and cannot claim an input shape the host will trust.

**Bounded, and honest about truncation.** Batch groups that lose candidates set `truncated: true`, *"so an empty
truncated group cannot be mistaken for a query that had no matches."*

## What is dropped

Their BM25 implementation, stemming, intent expansion, embeddings, and the code-mode executor that takes
precedence over tool search. A lexical substring-and-token score over names and descriptions is enough for a
catalog this size, and adding a ranking library to save a few tokens would be a poor trade.
"""

from __future__ import annotations

import hashlib
import json
import re
import weakref
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# Source labels, from their `classifyTool`.
OPENCLAW = "openclaw"
MCP = "mcp"
CLIENT = "client"

# Bounds, from `tool-search-types.ts`.
MAX_RESULTS = 50
MAX_BATCH_QUERIES = 16
MAX_DESCRIPTION_CHARS = 180
METADATA_BUDGET = 2_000
DIRECTORY_CHAR_CAP = 18_000
DEFAULT_LIMIT = 8
MAX_LIMIT = 20


def _hash_value(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# Opaque per-object identities for untrusted schemas. A WeakMap so the identity dies with the object and cannot
# leak, and a counter so two structurally identical objects still get different ids -- they are different objects,
# and the point is that we did not look inside either.
_untrusted_ids: "weakref.WeakKeyDictionary[Any, str]" = weakref.WeakKeyDictionary()
_untrusted_counter = 0
_untrusted_lock = threading.Lock()


def untrusted_schema_fingerprint(schema: Any) -> str:
    """An opaque identity for a schema we must not traverse.

    Their comment names the reason directly: remote and client schemas "may be attacker-sized or lazy hostile
    objects." Hashing one means walking it, and walking it is the attack -- a lazy object can run arbitrary code
    on attribute access, and an attacker-sized one can be a denial of service. Identity is enough: if it is not
    the same object, treat it as changed.
    """
    global _untrusted_counter
    if schema is None:
        return "none"
    try:
        with _untrusted_lock:
            existing = _untrusted_ids.get(schema)
            if existing is not None:
                return existing
            _untrusted_counter += 1
            identity = f"object:{_untrusted_counter}"
            try:
                _untrusted_ids[schema] = identity
            except TypeError:
                # Not weak-referenceable (a dict, a list). Fall back to a type-and-length identity, which is
                # still cheap and still refuses to look inside.
                return f"opaque:{type(schema).__name__}:{len(schema) if hasattr(schema, '__len__') else '?'}"
            return identity
    except Exception:
        return f"opaque:{type(schema).__name__}"


@dataclass
class CatalogEntry:
    """One tool in the catalog.

    `direct_visible` means the tool is *also* offered directly, so the model need not search for it. Their
    `isDirectVisibleCatalogTool` grants that only to a tool resolving to a trusted source -- *"an MCP lookalike
    must never become a direct delivery or core-coding tool"* -- which is the security half of this field.
    """

    id: str
    source: str
    name: str
    description: str
    source_name: str = ""
    parameters: Any = None
    output_schema: Any = None
    direct_visible: bool = False
    tool: Any = None

    @property
    def trusted(self) -> bool:
        return self.source == OPENCLAW

    def searchable_text(self) -> str:
        """What a query matches against.

        For a trusted tool the parameter names are included, because a model searching for "token" should find
        the tool that takes one. For an untrusted tool the schema is **never** traversed, so only the name and
        description are indexed -- their behaviour, and the reason a hostile server cannot seed the index.
        """
        parts = [self.name, self.description]
        if self.trusted and isinstance(self.parameters, dict):
            properties = self.parameters.get("properties")
            if isinstance(properties, dict):
                parts.extend(str(key) for key in properties)
        return " ".join(part for part in parts if part).lower()


@dataclass
class SearchHit:
    entry: CatalogEntry
    score: float

    def render(self) -> str:
        visibility = " (already available directly)" if self.entry.direct_visible else ""
        return f"  {self.entry.name:<28} {self.entry.description[:80]}{visibility}"


@dataclass
class SearchResult:
    hits: list[SearchHit] = field(default_factory=list)
    truncated: bool = False
    total_considered: int = 0

    def render(self) -> str:
        if not self.hits:
            # A truncated empty result is NOT the same as no matches, and saying so is the whole point of the
            # flag: their comment is that this stops "an empty truncated group being mistaken for a query that
            # had no matches".
            return "  no matches" + (" (the result was truncated)" if self.truncated else "")
        lines = [hit.render() for hit in self.hits]
        if self.truncated:
            lines.append("  ... more matches were dropped to stay within the result budget")
        return "\n".join(lines)


def build_entries(
    tool_defs: Iterable[dict],
    *,
    source: str = OPENCLAW,
    source_name: str = "",
    direct_names: Iterable[str] = (),
) -> list[CatalogEntry]:
    """Turn tool definitions into catalog entries.

    Entries are sorted by name so the rendered directory is stable across turns -- an unstable order would change
    the prompt prefix and defeat the caching this exists to protect.
    """
    direct = set(direct_names)
    entries: list[CatalogEntry] = []
    for definition in tool_defs:
        name = str(definition.get("name") or "")
        if not name:
            continue
        entries.append(CatalogEntry(
            id=f"{source}:{source_name or 'core'}:{name}",
            source=source,
            source_name=source_name,
            name=name,
            description=str(definition.get("description") or ""),
            parameters=definition.get("input_schema"),
            output_schema=definition.get("output_schema"),
            direct_visible=name in direct,
        ))
    entries.sort(key=lambda entry: entry.name)
    return entries


def catalog_fingerprint(entries: list[CatalogEntry]) -> str:
    """A stable fingerprint over the descriptor *snapshot*.

    Trusted sources contribute their raw schemas; untrusted ones contribute only an opaque identity. The result
    is sorted and joined, so two orderings of the same catalog fingerprint the same.
    """
    parts = []
    for entry in entries:
        if entry.trusted:
            schema_part = _hash_value(entry.parameters) + _hash_value(entry.output_schema)
        else:
            schema_part = (
                untrusted_schema_fingerprint(entry.parameters)
                + untrusted_schema_fingerprint(entry.output_schema)
            )
        parts.append(f"{entry.id}\x1f{entry.description}\x1f{entry.direct_visible}\x1f{schema_part}")
    return hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()


def rebind_executors(
    existing: list[CatalogEntry], current: list[CatalogEntry]
) -> list[CatalogEntry] | None:
    """Reuse the descriptor snapshot, swapping only the callable bindings.

    Returns `None` to force a full re-register. Conditions, from their implementation:

    * the id sets must be **exactly equal in size** and every existing id must resolve in the new set. A subset
      match would silently drop or duplicate a tool.
    * the descriptors are taken as unchanged, which the caller has already established by comparing fingerprints.

    Two distinct reuse cases, and conflating them is a bug:

    * **nothing changed at all** -- every callable is already reference-identical, and the *new* list is returned
      untouched. Returning the old list here would discard any non-descriptor state the caller attached to the
      new entries, which is why their implementation returns `currentEntries` in this branch.
    * **only the callables changed** -- the descriptor snapshot is reused and the callable is taken from the new
      entry, which is their `{...entry, tool}` line. Their comment for it: *"Callable bindings can change while
      descriptors stay equal."*
    """
    if len(existing) != len(current):
        return None
    by_id = {entry.id: entry for entry in current}
    if len(by_id) != len(current):
        return None
    for old in existing:
        if old.id not in by_id:
            return None

    # Nothing changed: hand back the caller's own list rather than the cached one. This is the branch that keeps
    # an unchanged catalog byte-identical without discarding the new objects.
    if all(old.tool is by_id[old.id].tool for old in existing):
        return current

    # Descriptors from the snapshot, callables from the new entries.
    return [
        CatalogEntry(
            id=old.id, source=old.source, name=old.name, description=old.description,
            source_name=old.source_name, parameters=old.parameters,
            output_schema=old.output_schema, direct_visible=old.direct_visible,
            tool=by_id[old.id].tool,
        )
        for old in existing
    ]


# Words that carry no discriminating power in a query like "remember what happened in an earlier session".
# Without this, "a" and "in" match almost every description and inflate every score towards the best, which is
# what made a single query unlock five tool groups. Their BM25 would suppress these through IDF; a stopword list
# plus the document-frequency weighting in `search` is the cheap equivalent.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "does", "for", "from", "get",
    "had", "has", "have", "how", "i", "if", "in", "into", "is", "it", "its", "me", "my", "not", "of",
    "on", "or", "so", "that", "the", "their", "them", "then", "there", "these", "they", "this", "to",
    "up", "use", "used", "using", "was", "we", "what", "when", "where", "which", "who", "will", "with",
    "you", "your",
})


def _terms(query: str) -> list[str]:
    return [term for term in re.split(r"\W+", (query or "").lower()) if term and term not in _STOPWORDS]


def score(entry: CatalogEntry, terms: list[str], document_frequency: dict[str, int] | None = None) -> float:
    """A lexical score: exact name match beats a name prefix, which beats a description hit.

    Each term's contribution is divided by how many entries it matches, so a word that appears in every
    description contributes almost nothing and a distinctive word dominates. Without that weighting, "in" and
    "a" pull every tool's score towards the best, and a relative cut-off cannot tell a strong match from noise.

    Deliberately still simple. A real BM25 index over a few dozen tools would rank marginally better and cost a
    dependency, a tokeniser and a stemming table.
    """
    name = entry.name.lower()
    description = entry.description.lower()
    total = 0.0
    for term in terms:
        # `1 + df` rather than `df`, so a term matching nothing is not a division by zero and a term matching one
        # entry is not scaled up by an arbitrary constant.
        weight = 1.0 / (1.0 + (document_frequency or {}).get(term, 1))
        subtotal = 0.0
        if name == term:
            subtotal += 10.0
        elif name.startswith(term):
            subtotal += 6.0
        elif term in name:
            subtotal += 4.0
        if term in description:
            subtotal += 2.0
        # Only a trusted entry indexed its parameter names, so this cannot fire for an untrusted one.
        if entry.trusted and isinstance(entry.parameters, dict):
            properties = entry.parameters.get("properties")
            if isinstance(properties, dict) and any(term in str(key).lower() for key in properties):
                subtotal += 1.0
        total += subtotal * weight
    return total


def search(
    entries: list[CatalogEntry],
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    source_filter: str = "",
) -> SearchResult:
    """Find tools matching a query.

    `source_filter` exists to enforce the rule that a call must not reach an untrusted tool through the catalog
    without the model having described it first: a caller can restrict a search to trusted sources.
    """
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    terms = _terms(query)

    considered = [e for e in entries if not source_filter or e.source == source_filter]
    result = SearchResult(total_considered=len(considered))
    if not terms:
        return result

    # How many entries each term matches, computed once. This is the IDF weight.
    haystacks = [entry.searchable_text() for entry in considered]
    document_frequency = {
        term: sum(1 for text in haystacks if term in text) for term in terms
    }

    scored = [(e, score(e, terms, document_frequency)) for e in considered]
    matched = [(entry, value) for entry, value in scored if value > 0]
    matched.sort(key=lambda pair: (-pair[1], pair[0].name))

    if len(matched) > MAX_RESULTS:
        matched = matched[:MAX_RESULTS]
        result.truncated = True
    if len(matched) > limit:
        matched = matched[:limit]
        result.truncated = True

    result.hits = [SearchHit(entry=entry, score=value) for entry, value in matched]
    return result


def describe(entry: CatalogEntry) -> str:
    """The full definition of one catalog entry, for a model that found it by search.

    This is where an untrusted schema is finally shown -- and `input: "unknown"` is shown when there is no
    trusted schema, rather than a guess. Their rule: an MCP or client tool's schema is never indexed and its
    input shape is reported as unknown until the model asks.
    """
    lines = [f"  {entry.name}", f"      {entry.description}"]
    if entry.source != OPENCLAW:
        lines.append(f"      source: {entry.source}" + (f" ({entry.source_name})" if entry.source_name else ""))
        lines.append("      input: unknown -- this tool comes from an untrusted source and its parameter")
        lines.append("             schema is not inspected by the harness")
    elif isinstance(entry.parameters, dict):
        properties = entry.parameters.get("properties")
        required = set(entry.parameters.get("required") or [])
        if isinstance(properties, dict) and properties:
            lines.append("      parameters:")
            for key, spec in properties.items():
                marker = " (required)" if key in required else ""
                kind = spec.get("type", "?") if isinstance(spec, dict) else "?"
                lines.append(f"        {key}: {kind}{marker}")
        else:
            lines.append("      parameters: none")
    return "\n".join(lines)


def render_directory(entries: list[CatalogEntry], cap: int = DIRECTORY_CHAR_CAP) -> str:
    """A bounded listing of the catalog, for the prompt.

    Sorted by name and capped, and built only from *policy-filtered* entries the caller already chose to expose.
    Their rule for what must not enter it: "User messages, per-turn tool guesses, session identifiers, and
    untrusted MCP or client metadata do not enter the directory." So this renders names and descriptions only --
    never a schema, never a query, never anything user- or server-supplied beyond the name itself.
    """
    lines: list[str] = []
    used = 0
    for entry in sorted(entries, key=lambda e: e.name):
        description = entry.description[:MAX_DESCRIPTION_CHARS]
        line = f"  {entry.name}: {description}"
        if used + len(line) > cap:
            lines.append(f"  ... {len(entries)} tool(s) total; more were omitted to stay within the budget")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines) if lines else "  (no tools)"


class ToolCatalog:
    """The catalog and its reuse state for one run.

    Kept per-run rather than module-level for the same reason their catalog is keyed by run: a count or a
    fingerprint carried across unrelated runs would fire on a coincidence.
    """

    def __init__(self, entries: list[CatalogEntry] | None = None) -> None:
        self.entries: list[CatalogEntry] = list(entries or [])
        self._fingerprint = catalog_fingerprint(self.entries) if self.entries else ""
        self._execution_allow: str = ""
        self._lock = threading.RLock()
        self.catalog_registered = bool(self.entries)
        self.catalog_reused = False

    def apply(self, new_entries: list[CatalogEntry], execution_allow: Any = None) -> bool:
        """Install a new entry set. Returns whether the descriptor snapshot was reused.

        Reuse requires the fingerprint *and* the execution-allow list to be unchanged, and the rebind to
        succeed. Anything else falls back to a full replace, because a stale descriptor presented as current is
        worse than a re-register.
        """
        with self._lock:
            allow = _hash_value(execution_allow) if execution_allow is not None else ""
            new_fingerprint = catalog_fingerprint(new_entries)

            if (self._fingerprint == new_fingerprint and self._execution_allow == allow
                    and self.entries):
                rebound = rebind_executors(self.entries, new_entries)
                if rebound is not None:
                    self.entries = rebound
                    self.catalog_reused = True
                    self.catalog_registered = True
                    return True

            self.entries = list(new_entries)
            self._fingerprint = new_fingerprint
            self._execution_allow = allow
            self.catalog_reused = False
            self.catalog_registered = True
            return False

    def search(self, query: str, *, limit: int = DEFAULT_LIMIT, source_filter: str = "") -> SearchResult:
        with self._lock:
            return search(self.entries, query, limit=limit, source_filter=source_filter)

    def find(self, name: str) -> CatalogEntry | None:
        with self._lock:
            for entry in self.entries:
                if entry.name == name:
                    return entry
        return None

    def directory(self, cap: int = DIRECTORY_CHAR_CAP) -> str:
        with self._lock:
            return render_directory(self.entries, cap)

    def stats(self) -> dict:
        with self._lock:
            by_source: dict[str, int] = {}
            for entry in self.entries:
                by_source[entry.source] = by_source.get(entry.source, 0) + 1
            return {
                "entries": len(self.entries),
                "by_source": by_source,
                "fingerprint": self._fingerprint[:16],
                "reused": self.catalog_reused,
                "direct_visible": sum(1 for e in self.entries if e.direct_visible),
            }
