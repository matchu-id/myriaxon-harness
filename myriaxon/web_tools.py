"""Web access as ordinary tools, for providers that cannot run Anthropic's server-side ones.

`web_search` and `web_fetch` were declared as Anthropic *server* tools: the type string tells
Anthropic's API to run them and return results inline, so the harness never executes them and there
is no local handler. That is fine on Anthropic and wrong everywhere else -- on Netra, DeepSeek,
OpenRouter or a local endpoint the model is offered two tools whose every call comes back as
"Unknown tool", which reads to the model as the harness being broken and to the user as the agent
being unable to research.

Measured: a `researcher` locus spent a turn working around this with `urllib` through a shell and then
asked for real fetch tools, because `run_bash` worked and `web_fetch` did not.

So these are the same tools implemented locally, with the same names and argument shapes. Anthropic
keeps its server-side versions (they are better: the results arrive without a round trip through
this process); every other provider gets these.

Two honesty rules, both load-bearing:

* **`web_fetch` is retrieval and `web_search` is best effort.** Fetching a URL is exact and is what
  a sourced answer should rely on. Search scrapes a public results page that can change without
  notice, so its failures are reported as failures rather than as "no results found" -- the two mean
  very different things to someone deciding whether to trust an answer.
* **Nothing here is claimed to be a search API.** No key is required, which is the point, and it is
  also the limitation.
"""

from __future__ import annotations

import html as _html
import re
import urllib.parse

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Myriaxon/1.0"
)

# Wikipedia's API answers a browser User-Agent with HTTP 403 and a link to its robot policy, and the
# same request with a contact-bearing one returns results. Measured both ways, repeatedly: the UA is
# the whole difference, not rate limiting and not the query parameters.
#
# So there are two identities, used where each belongs. A browser string gets past the sites that
# inspect for one; the contact string is what an API that publishes a policy asks for, and pretending
# to be a browser while ignoring that policy is how a tool gets an IP blocked.
BROWSER_USER_AGENT = USER_AGENT
POLITE_USER_AGENT = "Myriaxon/1.0 (+https://github.com/Ariankhalfani/myriaxon)"

REQUEST_TIMEOUT = 25.0
MAX_FETCH_CHARS = 40_000


# ------------------------------------------------------------------------------------------------
# Search providers that need a key
#
# Scraping a public results page is a fallback, not a foundation: the machine this was built on
# cannot reach DuckDuckGo at all (a certificate whose hostname does not match, so the connection is
# being intercepted), Mojeek answers with a JavaScript captcha, and Startpage returns its own
# marketing pages. What is left is Wikipedia -- an encyclopedia, which answers "what is Python" and
# nothing about this week.
#
# So a key is supported for three providers with real free tiers, checked in this order. With none
# configured the scrapers still run and the result says which backends were tried, rather than
# pretending a thin search is a complete one.
#
# Keys are read from the environment or from `~/.myriaxon/search.json`, so a key never has to be
# written into a command line where it would land in a shell history.
SEARCH_PROVIDERS: tuple[dict[str, str], ...] = (
    {
        "name": "brave",
        "env": "BRAVE_API_KEY",
        "label": "Brave Search API",
        "url": "https://api.search.brave.com/res/v1/web/search",
        "free": "free tier: 2,000 queries/month",
        "docs": "https://brave.com/search/api/",
    },
    {
        "name": "tavily",
        "env": "TAVILY_API_KEY",
        "label": "Tavily",
        "url": "https://api.tavily.com/search",
        "free": "free tier: 1,000 credits/month",
        "docs": "https://tavily.com/",
    },
    {
        "name": "serper",
        "env": "SERPER_API_KEY",
        "label": "Serper (Google results)",
        "url": "https://google.serper.dev/search",
        "free": "free tier: 2,500 queries",
        "docs": "https://serper.dev/",
    },
)

SEARCH_CONFIG_FILE = "search.json"


def _search_config() -> dict:
    """Keys from `~/.myriaxon/search.json`, if it exists. Never raises."""
    try:
        import json

        from . import constants

        path = constants.CONFIG_DIR / SEARCH_CONFIG_FILE
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def configured_search_provider() -> tuple[dict[str, str], str] | None:
    """The first provider with a key available, and the key. None when none is configured."""
    import os

    file_config = _search_config()
    for provider in SEARCH_PROVIDERS:
        value = (os.environ.get(provider["env"]) or "").strip()
        if not value:
            value = str(file_config.get(provider["name"]) or "").strip()
        if value:
            return provider, value
    return None


def search_backends() -> list[str]:
    """The names a search will actually try, in order. Used by the result text and by checks."""
    names = []
    provider = configured_search_provider()
    if provider:
        names.append(provider[0]["name"])
    names.extend(("wikipedia", "duckduckgo", "marginalia"))
    return names


def _search_brave(query: str, limit: int, key: str) -> list[dict[str, str]]:
    with _client() as client:
        response = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": str(min(limit, 20))},
            headers={"Accept": "application/json", "X-Subscription-Token": key},
        )
    if response.status_code == 401:
        raise RuntimeError("Brave rejected the API key (401)")
    if response.status_code == 429:
        raise RuntimeError("Brave rate limit reached (429)")
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:80]}")
    payload = response.json()
    results = []
    for item in ((payload.get("web") or {}).get("results") or [])[:limit]:
        results.append({
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "snippet": _strip_html(str(item.get("description") or "")),
        })
    return [r for r in results if r["url"]]


def _search_tavily(query: str, limit: int, key: str) -> list[dict[str, str]]:
    with _client() as client:
        response = client.post(
            "https://api.tavily.com/search",
            json={"api_key": key, "query": query, "max_results": min(limit, 20)},
            headers={"Content-Type": "application/json"},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:80]}")
    payload = response.json()
    results = []
    for item in (payload.get("results") or [])[:limit]:
        results.append({
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "snippet": _strip_html(str(item.get("content") or ""))[:400],
        })
    return [r for r in results if r["url"]]


def _search_serper(query: str, limit: int, key: str) -> list[dict[str, str]]:
    with _client() as client:
        response = client.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": min(limit, 20)},
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:80]}")
    payload = response.json()
    results = []
    for item in (payload.get("organic") or [])[:limit]:
        results.append({
            "title": str(item.get("title") or ""),
            "url": str(item.get("link") or ""),
            "snippet": _strip_html(str(item.get("snippet") or "")),
        })
    return [r for r in results if r["url"]]


_KEYED_SEARCH = {
    "brave": _search_brave,
    "tavily": _search_tavily,
    "serper": _search_serper,
}


class WebToolUnavailable(RuntimeError):
    """A web tool cannot run because the package it needs is absent.

    httpx is an optional extra now that the provider path uses `http.client`, so this is an expected state on a
    minimal install rather than a bug -- which is why it carries the install command in its message.
    """


def _client(user_agent: str | None = None):
    """An httpx client, so TLS behaves the same way it does for providers.

    Chosen over urllib deliberately: PowerShell 5.1's TLS stack and Python's default context have
    both failed against hosts that httpx reaches.

    The earlier version of this docstring ended "and this codebase already depends on httpx". That
    stopped being true when `direct_http_client.py` moved to `http.client`, so the web tools are now
    what keeps the dependency -- which is why it is an optional extra and why the import below has to
    advise rather than raise a bare ModuleNotFoundError.
    """
    try:
        import httpx
    except ImportError as exc:
        raise WebToolUnavailable(
            "The web tools need httpx, which is an optional extra. Install it with "
            "`pip install myriaxon[web]`, or `pip install httpx`."
        ) from exc

    return httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={
            "User-Agent": user_agent or BROWSER_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


def _strip_html(document: str) -> str:
    """Readable text from HTML, without a parser dependency.

    Removes script, style, nav and footer content first -- those are the parts that turn a page into
    thousands of characters of noise and push the actual answer past whatever the caller will read.
    """
    text = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", document)
    text = re.sub(r"(?is)<(nav|footer|aside|form)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    # Block elements become newlines so paragraphs do not run together into one line.
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section|article|blockquote)>", "\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _page_title(document: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", document)
    return _html.unescape(match.group(1)).strip() if match else ""


def web_fetch(url: str, max_chars: int = MAX_FETCH_CHARS) -> tuple[str, bool]:
    """Fetch a URL and return readable text. Returns (text, is_error)."""
    target = str(url or "").strip()
    if not target:
        return "web_fetch needs a url", True
    if not target.lower().startswith(("http://", "https://")):
        target = "https://" + target

    try:
        with _client() as client:
            response = client.get(target)
    except Exception as exc:  # noqa: BLE001
        return (f"web_fetch could not reach {target}: {type(exc).__name__}: {exc}\n"
                "The URL may be wrong, the host may be down, or this machine may have no route to "
                "it. This is a failure to fetch, not evidence that the page does not exist."), True

    if response.status_code >= 400:
        return (f"web_fetch got HTTP {response.status_code} from {target}.\n"
                f"{response.text[:400]}"), True

    content_type = response.headers.get("content-type", "")
    body = response.text
    if "html" in content_type.lower() or body.lstrip()[:1] == "<":
        title = _page_title(body)
        text = _strip_html(body)
        header = f"{target}\n" + (f"{title}\n" if title else "")
    else:
        text = body
        header = f"{target} ({content_type or 'unknown type'})\n"

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    note = (f"\n\n[truncated at {max_chars:,} characters of {len(response.text):,} fetched]"
            if truncated else "")
    return f"{header}\n{text}{note}", False


def _search_duckduckgo(query: str, limit: int) -> list[dict[str, str]]:
    """Results from DuckDuckGo's HTML endpoint.

    A public results page, not an API: no key, and no contract. Parsed defensively and allowed to
    return nothing, because a parse failure must not be reported as "no results".

    Note the GET: POSTing here answers `405 Method Not Allowed`, and a 400 carrying that JSON error
    is easy to mistake for a block. Measured.
    """
    with _client() as client:
        response = client.get("https://html.duckduckgo.com/html/", params={"q": query})
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")
    return _parse_duckduckgo(response.text, limit)


def _parse_duckduckgo(document: str, limit: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    # Each result is an anchor with class result__a; the snippet lives in result__snippet.
    pattern = re.compile(
        r'(?is)<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
        r'(?:.*?class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>)?'
    )
    for match in pattern.finditer(document):
        href, title_html, snippet_html = match.group(1), match.group(2), match.group(3) or ""
        url = _clean_ddg_url(href)
        if not url:
            continue
        title = _strip_html(title_html)
        snippet = _strip_html(snippet_html)
        if not title:
            continue
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def _clean_ddg_url(href: str) -> str:
    """DuckDuckGo wraps results in a redirect; unwrap to the real destination."""
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href or href.startswith("/l/"):
        parsed = urllib.parse.urlparse(href if href.startswith("http") else "https://duckduckgo.com" + href)
        target = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return urllib.parse.unquote(target)
    return href if href.startswith("http") else ""


def _search_wikipedia(query: str, limit: int) -> list[dict[str, str]]:
    """Wikipedia's documented search API.

    The one reliable backend on the machine this was built on, and a real API rather than a scrape:
    it needs no key, it is documented, and it honours a contact-bearing User-Agent. It only knows an
    encyclopedia, so it is a fallback for general questions, not a replacement for web search.
    """
    with _client(POLITE_USER_AGENT) as client:
        response = client.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query", "list": "search", "srsearch": query,
                "format": "json", "formatversion": "2", "srlimit": str(limit),
            },
        )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:80]}")
    payload = response.json()
    hits = ((payload.get("query") or {}).get("search") or [])
    results: list[dict[str, str]] = []
    for hit in hits[:limit]:
        title = str(hit.get("title") or "")
        if not title:
            continue
        results.append({
            "title": title,
            "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
            "snippet": _strip_html(str(hit.get("snippet") or "")),
        })
    return results


def _search_marginalia(query: str, limit: int) -> list[dict[str, str]]:
    """Marginalia, an independent index that permits scripted access.

    Result *titles* are not reliably extractable from its markup, so the links are reported without
    them -- a URL with no title is still enough to fetch, and inventing a title from the URL would
    be presenting a guess as a fact.
    """
    with _client() as client:
        response = client.get("https://search.marginalia.nu/search", params={"query": query})
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for href in re.findall(r'href="(https?://[^"]+)"', response.text):
        if "marginalia" in href or href in seen:
            continue
        seen.add(href)
        results.append({"title": "", "url": href, "snippet": ""})
        if len(results) >= limit:
            break
    return results


def web_search(query: str, limit: int = 8) -> tuple[str, bool]:
    """Search the web. Returns (text, is_error).

    A failure to search is an error, never an empty result set: telling a model "no results" when
    the search itself failed invites it to conclude the thing does not exist.
    """
    text = str(query or "").strip()
    if not text:
        return "web_search needs a query", True
    limit = max(1, min(int(limit or 8), 20))

    errors: list[str] = []

    # A keyed provider first when one is configured: it is a real search index, and its presence is
    # the difference between "this answers questions" and "this answers questions about
    # encyclopedias". The key-free backends still run afterwards, so removing a key degrades rather
    # than breaks.
    keyed = configured_search_provider()
    # Not annotated: a variable annotation inside a function is never evaluated, so `list[tuple[str,
    # Any]]` would have looked like a type while `Any` was not even imported -- a claim the code does
    # not honour. The names below are self-describing.
    backends = []
    if keyed:
        provider, key = keyed
        handler = _KEYED_SEARCH.get(provider["name"])
        if handler is not None:
            backends.append(
                (provider["name"], lambda q, n, h=handler, k=key: h(q, n, k))
            )
    backends.extend((
        ("wikipedia", _search_wikipedia),
        ("duckduckgo", _search_duckduckgo),
        ("marginalia", _search_marginalia),
    ))

    for name, backend in backends:
        try:
            results = backend(text, limit)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if results:
            lines = [f'Results for "{text}" (via {name}, {len(results)} shown):', ""]
            for index, item in enumerate(results, 1):
                lines.append(f"{index}. {item['title'] or '(no title)'}")
                lines.append(f"   {item['url']}")
                if item.get("snippet"):
                    lines.append(f"   {item['snippet'][:400]}")
                lines.append("")
            lines.append("Fetch a result with web_fetch to read it before relying on it; a snippet "
                         "is not a source.")
            if not keyed:
                # Said once, in the result. A caller who does not know its search is
                # Wikipedia-and-scrapers will over-trust an answer about anything current, and the
                # fix is a one-line environment variable.
                lines.append("")
                lines.append(
                    "Note: no search API key is configured, so this came from the key-free "
                    "backends, which cover reference material well and current events poorly. Set "
                    + " or ".join(p["env"] for p in SEARCH_PROVIDERS)
                    + " (or add one to ~/.myriaxon/search.json) for a real web index."
                )
            return "\n".join(lines), False

    if errors:
        # WHEN EVERY BACKEND FAILED FOR THE SAME OPTIONAL REASON, SAY SO ONCE AND NAME THE INSTALL. Every backend
        # needs httpx, which is now an extra rather than a core dependency, so on a minimal install the whole
        # feature is unavailable -- and "wikipedia: WebToolUnavailable: ..." three times tells the user less than
        # the one line that fixes it. The generic message is kept for the case where the failures are unrelated.
        missing = [e for e in errors if "WebToolUnavailable" in e]
        if missing and len(missing) == len(errors):
            return (f"web_search needs httpx, which is an optional extra and is not installed. "
                    "Install it with pip install myriaxon[web], or pip install httpx.\n"
                    "This is a missing-package failure, not a result of zero: nothing was searched, so do not "
                    "conclude the subject does not exist."), True
        return (f"web_search failed for {text!r}: " + "; ".join(errors) +
                "\n\nThis is a search failure, not a result of zero. Do not conclude the subject "
                "does not exist; try web_fetch on a specific URL instead."), True
    return (f'web_search found nothing for "{text}" on any backend.\n'
            "That is a real empty result. Try different words, or fetch a known URL directly."), False


# ------------------------------------------------------------------------------------------------
# Tool definitions and dispatch
# ------------------------------------------------------------------------------------------------
WEB_SEARCH_TOOL_DEF = {
    "name": "web_search",
    "description": (
        "Search the web and return a list of results with titles, URLs and snippets. Use it for "
        "anything current or external. Snippets are not sources -- call web_fetch on a result to read "
        "it before relying on it. If the search fails, the result says so rather than returning "
        "nothing, and you should not treat a failure as evidence that something does not exist."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "limit": {"type": "integer", "description": "How many results (1-20, default 8)."},
        },
        "required": ["query"],
    },
}

WEB_FETCH_TOOL_DEF = {
    "name": "web_fetch",
    "description": (
        "Fetch a URL and return its readable text. Use it to read a page properly instead of "
        "guessing from a search snippet. HTML is reduced to text: scripts, navigation and footers "
        "are removed. Long pages are truncated with a note saying so."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The absolute URL to fetch."},
        },
        "required": ["url"],
    },
}

# Names this module serves. Used by the registry to route a call here, and to decide whether a
# provider needs them: Anthropic runs its own, everyone else needs these.
LOCAL_WEB_TOOL_NAMES = ("web_search", "web_fetch")


def execute(name: str, tool_input: dict) -> tuple[str, bool]:
    """Dispatch a local web tool call."""
    if name == "web_fetch":
        return web_fetch(str(tool_input.get("url") or ""))
    if name == "web_search":
        return web_search(str(tool_input.get("query") or ""), int(tool_input.get("limit") or 8))
    return f"Unknown web tool: {name}", True
