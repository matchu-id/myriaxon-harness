"""Explicit context caching for Vertex AI Gemini, with full observability and an off switch.

What this buys, concretely: a long system prompt and a large tool catalogue are re-sent on EVERY turn. On
a 4,000-token prompt that is 4,000 input tokens per request at full price. Vertex lets that prefix be
stored once as a `cachedContent` resource and referenced by name, after which the repeated part is billed
at the cache rate instead of the input rate. The saving is proportional to how much of the prompt repeats
and how many turns you take -- and the cost to hold the cache is charged per hour, not per use.

Why it is BUILT rather than enabled by default, and why "cost whatever" is still not a reason to switch
it on silently:

  * a cache is a RESOURCE WITH A LIFETIME. It is billed while it exists, so an idle session holding a
    cache is paying for nothing. It has to be created deliberately, expired deliberately, and be
    inspectable.
  * the minimum token floor is real. Vertex refuses to cache a prefix below the model's minimum, so a
    small prompt cannot be cached and a silent attempt would just add a failing API call per turn.
  * a cache that no longer matches the prompt is WORSE THAN NO CACHE: the request misses, the prefix is
    re-sent at full price, and there is an extra round trip.

So the design is: an explicit enable, an explicit TTL, a named resource, and a panel that shows whether a
cache exists, how big it is, when it expires, and whether requests are actually hitting it.

Nothing here is assumed about the API shape. Every request and response key used below was read from the
live `cachedContents` endpoint before being written down.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import vertex_auth as va

# Vertex's own floor for a cacheable prefix. Below this the API refuses the create call outright.
#
# MEASURED, not looked up: an attempt with a 2,762-token prefix was refused with
# "The cached content is of 2762 tokens. The minimum token count to start explicit caching is 4096."
# My first guess here was 2048, which would have let the harness attempt a cache it could never create
# and report the failure as if it were a transient error. The number is quoted from the API's own
# message so the constant and the refusal cannot drift apart.
MIN_CACHE_TOKENS = 4096

# The TTL the API applies when a caller does not choose one. Short on purpose: an unexpired cache costs
# money per hour, and a default that lingers is a default that bills.
DEFAULT_TTL_SECONDS = 1800


@dataclass
class CachedContent:
    """One cache resource, as the API describes it."""

    name: str = ""
    model: str = ""
    display_name: str = ""
    create_time: str = ""
    expire_time: str = ""
    total_tokens: int = 0
    # Seconds until expiry, computed locally from `expire_time` so a clock skew on the server does not
    # make the panel disagree with reality.
    seconds_until_expiry: float = 0.0


@dataclass
class CacheStats:
    """What the dashboard shows. Counts, never guesses."""

    enabled: bool = False
    active: list[CachedContent] = field(default_factory=list)
    created: int = 0
    hits: int = 0
    misses: int = 0
    tokens_served_from_cache: int = 0
    last_error: str = ""
    last_created_tokens: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "active": [
                {"name": c.name, "model": c.model, "tokens": c.total_tokens,
                 "expires_in_s": int(c.seconds_until_expiry), "display_name": c.display_name}
                for c in self.active
            ],
            "created": self.created,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hit_rate, 4),
            "tokens_served_from_cache": self.tokens_served_from_cache,
            "last_error": self.last_error,
            "last_created_tokens": self.last_created_tokens,
        }


# Process-wide, because the dashboard and the agent both read it and a per-instance copy would show two
# different truths depending on who asked.
_stats = CacheStats()

# name -> the system-instruction text the cache was built from, so a changed prompt can be detected
# instead of silently missing every request.
_signatures: dict[str, str] = {}


def stats() -> CacheStats:
    return _stats


def enabled() -> bool:
    """Whether explicit caching is switched on.

    Default OFF. See the module docstring: a cache bills for its lifetime, so it is opted into rather
    than inherited, and an operator who turns it on can see exactly what it costs.
    """
    return (os.environ.get("MYRIAXON_VERTEX_CACHE", "") or "").strip().lower() in ("1", "true", "yes")


def ttl_seconds() -> int:
    try:
        return max(60, int(os.environ.get("MYRIAXON_VERTEX_CACHE_TTL") or DEFAULT_TTL_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS


def _headers() -> dict:
    return {"Authorization": f"Bearer {va.access_token()}", "Content-Type": "application/json"}


def _request(path: str, *, payload: dict | None = None, method: str = "GET") -> tuple[int, Any]:
    url = f"https://{va.host()}/v1/{path}"
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in _headers().items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw).get("error") or {}
            return exc.code, f"{parsed.get('status','')}: {str(parsed.get('message',''))[:300]}"
        except json.JSONDecodeError:
            return exc.code, raw[:300]
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def list_caches(refresh: bool = True) -> list[CachedContent]:
    """Every live cache on this project, newest expiry last.

    Reads the API rather than a local record, because a cache can be deleted from anywhere -- another
    process, the console, or a colleague -- and a panel that listed only what THIS process created would
    report a cache that no longer exists and hide the ones that do.
    """
    if not refresh:
        return list(_stats.active)
    status, body = _request(f"projects/{va.project()}/locations/{va.location()}/cachedContents")
    if status != 200 or not isinstance(body, dict):
        _stats.last_error = f"listing caches failed: HTTP {status} {str(body)[:160]}"
        return list(_stats.active)

    now = time.time()
    found: list[CachedContent] = []
    for entry in body.get("cachedContents") or []:
        name = entry.get("name", "")
        usage = entry.get("usageMetadata") or {}
        expire = entry.get("expireTime") or ""
        seconds = _seconds_until(expire, now)
        found.append(CachedContent(
            name=name,
            model=str(entry.get("model", "")),
            display_name=entry.get("displayName", ""),
            create_time=entry.get("createTime", ""),
            expire_time=expire,
            total_tokens=int(usage.get("totalTokenCount") or 0),
            seconds_until_expiry=seconds,
        ))
    _stats.active = found
    return found


def _seconds_until(expire_time: str, now: float) -> float:
    """Seconds until an RFC3339 timestamp. Returns 0 when it cannot be parsed.

    Parsed locally rather than trusting a server-supplied countdown, and 0 rather than a large number on
    failure -- an unparseable expiry should read as "assume it is gone", because the safe direction for a
    billing resource is to under-claim its remaining life.
    """
    if not expire_time:
        return 0.0
    text = expire_time.replace("Z", "+00:00")
    try:
        from datetime import datetime

        when = datetime.fromisoformat(text)
        return max(0.0, when.timestamp() - now)
    except (TypeError, ValueError):
        return 0.0


def find_matching(model: str, signature: str) -> CachedContent | None:
    """An existing, unexpired cache for this model built from the same prefix text."""
    for entry in list_caches():
        if entry.seconds_until_expiry <= 0:
            continue
        if entry.model and not entry.model.endswith(model):
            continue
        if _signatures.get(entry.name) == signature:
            return entry
    return None


def create(model: str, system_instruction: str, *, tools: list | None = None,
           ttl: int | None = None, display_name: str = "") -> tuple[CachedContent | None, str]:
    """Create a cache for a repeated prefix. Returns (cache, problem).

    `system_instruction` is the repeated part. Tool declarations are cached with it because a tool
    catalogue is usually the single largest fixed block in the prompt, and caching the instruction while
    re-sending the tools would leave most of the saving on the table.
    """
    if not system_instruction.strip():
        return None, "nothing to cache: the system instruction is empty"

    ttl = ttl or ttl_seconds()
    payload: dict[str, Any] = {
        "model": f"projects/{va.project()}/locations/{va.location()}/publishers/google/models/{model}",
        "ttl": f"{ttl}s",
    }
    if system_instruction.strip():
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    if tools:
        payload["tools"] = [{"functionDeclarations": tools}]
    if display_name:
        payload["displayName"] = display_name[:120]

    status, body = _request(f"projects/{va.project()}/locations/{va.location()}/cachedContents",
                            payload=payload, method="POST")
    if status != 200 or not isinstance(body, dict):
        hint = ""
        if status == 400 and "minimum token count" in str(body).lower():
            # Reported as a FINDING rather than a failure, because it usually is one: the measured
            # Myriaxon system prompt is about 1,532 tokens against a 4,096 floor, so caching genuinely
            # cannot help an ordinary session here. Saying "it is not an error to retry" was wrong --
            # retrying changes nothing, and the useful statement is that nothing needs fixing.
            hint = (" This is not a fault and there is nothing to retry: the prompt is smaller than "
                    "Vertex's minimum cacheable size, so caching cannot help it. Turn caching off and "
                    "forget it; the requests are being sent as efficiently as they can be already.")
        _stats.last_error = f"create failed: HTTP {status} {str(body)[:200]}{hint}"
        return None, _stats.last_error

    usage = body.get("usageMetadata") or {}
    created = CachedContent(
        name=body.get("name", ""),
        model=str(body.get("model", "")),
        display_name=body.get("displayName", ""),
        create_time=body.get("createTime", ""),
        expire_time=body.get("expireTime", ""),
        total_tokens=int(usage.get("totalTokenCount") or 0),
        seconds_until_expiry=_seconds_until(body.get("expireTime", ""), time.time()),
    )
    _stats.created += 1
    _stats.last_created_tokens = created.total_tokens
    _stats.last_error = ""
    _signatures[created.name] = _signature(system_instruction, tools)
    list_caches()
    return created, ""


def delete(name: str) -> tuple[bool, str]:
    """Delete a cache. Takes a full resource name or just the trailing id.

    Called with the full name that `list_caches` returned, because that is what an operator sees in the
    panel and copies. A bare id is accepted too since that is what the API's own URLs show.
    """
    if not name:
        return False, "no cache name given"
    full = name if name.startswith("projects/") else (
        f"projects/{va.project()}/locations/{va.location()}/cachedContents/{name}"
    )
    status, body = _request(full, method="DELETE")
    if status not in (200, 204):
        return False, f"delete failed: HTTP {status} {str(body)[:200]}"
    _signatures.pop(name, None)
    list_caches()
    return True, "deleted"


def delete_all() -> tuple[int, list[str]]:
    """Delete every cache on this project. Returns (deleted_count, problems).

    The kill switch. A billing resource needs one, and "delete them one at a time from a panel" is not
    one when the reason you are reaching for it is that something is costing money unexpectedly.
    """
    deleted, problems = 0, []
    for entry in list_caches():
        ok, problem = delete(entry.name)
        if ok:
            deleted += 1
        else:
            problems.append(f"{entry.name}: {problem}")
    return deleted, problems


def _signature(system_instruction: str, tools: list | None) -> str:
    """A stable fingerprint of the cached prefix.

    Hashed rather than stored so a panel reporting on a cache never has to hold the prompt text, which
    can contain user data. The purpose is only to answer "is the thing I would send the thing that is
    cached", and a hash answers exactly that.
    """
    import hashlib

    blob = system_instruction + "\x00" + json.dumps(tools or [], sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def note_usage(cached_tokens: int) -> None:
    """Record what a real response reported, so the panel shows measured hits rather than intentions.

    Counting hits from a local attempt would be a guess: the API decides whether a request matched, and
    `cachedContentTokenCount` is its answer. A request that carried a cache name and matched nothing
    reports 0 and is counted as a MISS, which is the number that reveals a cache that stopped matching.
    """
    if cached_tokens > 0:
        _stats.hits += 1
        _stats.tokens_served_from_cache += int(cached_tokens)
    else:
        _stats.misses += 1


def format_status(stats_obj: CacheStats | None = None, *, refresh: bool = False) -> str:
    """A plain-text report, for the CLI and for an operator who wants it without the dashboard."""
    stats_obj = stats_obj or _stats
    lines = ["Vertex context cache:"]
    lines.append(f"  enabled        : {'yes' if stats_obj.enabled else 'no'}")
    if not stats_obj.enabled:
        lines.append("  turn it on with: MYRIAXON_VERTEX_CACHE=true")
    if refresh:
        list_caches()
    lines.append(f"  live caches    : {len(stats_obj.active)}")
    for entry in stats_obj.active:
        lines.append(f"    {entry.name.split('/')[-1]}  {entry.total_tokens:,} tokens  "
                     f"expires in {int(entry.seconds_until_expiry)}s")
    lines.append(f"  created        : {stats_obj.created}")
    lines.append(f"  cache hits     : {stats_obj.hits}")
    lines.append(f"  cache misses   : {stats_obj.misses}")
    lines.append(f"  hit rate       : {stats_obj.hit_rate:.1%}")
    lines.append(f"  tokens cached  : {stats_obj.tokens_served_from_cache:,}")
    if stats_obj.last_error:
        lines.append(f"  last error     : {stats_obj.last_error}")
    return "\n".join(lines)
