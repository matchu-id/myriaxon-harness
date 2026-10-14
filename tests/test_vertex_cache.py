"""Vertex context caching: the pure logic, plus the two numbers that were measured rather than assumed.

Two facts in this module were WRONG when first written and are now pinned by tests, because both would
have caused a silent cost problem rather than a visible error:

  * the minimum cacheable prefix is **4096 tokens**. The first attempt used 2048 -- a guess -- and the API
    refused with "The cached content is of 2762 tokens. The minimum token count to start explicit caching
    is 4096." A wrong floor means the harness offers caching for a prompt it can never cache.
  * a cache bills for its LIFETIME. So it is off by default, every cache is listed from the API rather
    than from local memory, and a delete-everything path exists, because the moment an operator wants to
    stop paying is not the moment to require four clicks per resource.

The live round trip (create -> generate with `cachedContent` -> `cachedContentTokenCount` 5,522 of 5,524
-> delete) was verified against the real API before this file existed. What is tested here is the logic
that decides WHETHER to spend that money, since that is the part a live test cannot cover.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import vertex_cache as vc  # noqa: E402


class TestTheMeasuredConstants:
    def test_the_minimum_is_the_apis_own_number(self):
        """4096, quoted from the API's refusal message. Not 2048, which was a guess."""
        assert vc.MIN_CACHE_TOKENS == 4096

    def test_the_default_ttl_is_short_because_a_cache_bills_for_its_lifetime(self):
        assert vc.DEFAULT_TTL_SECONDS <= 3600, "an hour is the ceiling for a default that costs money"


class TestItIsOffUnlessAsked:
    def test_disabled_by_default(self, monkeypatch):
        """A billing resource must be opted into, not inherited."""
        monkeypatch.delenv("MYRIAXON_VERTEX_CACHE", raising=False)
        assert vc.enabled() is False

    def test_each_affirmative_spelling_works(self, monkeypatch):
        for value in ("1", "true", "TRUE", "yes", "Yes"):
            monkeypatch.setenv("MYRIAXON_VERTEX_CACHE", value)
            assert vc.enabled() is True, value

    def test_anything_else_leaves_it_off(self, monkeypatch):
        for value in ("", "0", "false", "no", "maybe"):
            monkeypatch.setenv("MYRIAXON_VERTEX_CACHE", value)
            assert vc.enabled() is False, value

    def test_ttl_is_configurable_and_floored(self, monkeypatch):
        monkeypatch.setenv("MYRIAXON_VERTEX_CACHE_TTL", "900")
        assert vc.ttl_seconds() == 900
        monkeypatch.setenv("MYRIAXON_VERTEX_CACHE_TTL", "1")
        assert vc.ttl_seconds() == 60, "a sub-minute TTL would expire before the first request finished"
        monkeypatch.setenv("MYRIAXON_VERTEX_CACHE_TTL", "nonsense")
        assert vc.ttl_seconds() == vc.DEFAULT_TTL_SECONDS


class TestExpiryParsing:
    def test_an_rfc3339_future_time_converts_to_seconds(self):
        """Deterministic: a FIXED `now` is passed in rather than read from the clock.

        The first version built a timestamp from `time.time() + 600` and asserted a 10-second window
        around it. That is a timing test masquerading as a parsing test -- it passed alone and failed
        intermittently in the full suite, where the machine is busier and the gap between building the
        stamp and measuring it can stretch. `_seconds_until` takes `now` as a parameter precisely so this
        can be exact, and using it removes a source of flakiness that would otherwise be blamed on
        whatever unrelated change happened to be in the same commit.
        """
        from datetime import datetime, timezone

        now = 1_700_000_000.0
        stamp = datetime.fromtimestamp(now + 600, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        assert vc._seconds_until(stamp, now) == 600.0

    def test_a_past_time_reads_as_expired_exactly(self):
        from datetime import datetime, timezone

        now = 1_700_000_000.0
        stamp = datetime.fromtimestamp(now - 60, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        assert vc._seconds_until(stamp, now) == 0.0

    def test_an_unparseable_value_reads_as_expired(self):
        """0, not a large number: the safe direction for a billing resource is to under-claim its life."""
        assert vc._seconds_until("", time.time()) == 0.0
        assert vc._seconds_until("not a date", time.time()) == 0.0

    def test_a_past_time_clamps_to_zero_not_negative(self):
        assert vc._seconds_until("2000-01-01T00:00:00Z", time.time()) == 0.0


class TestSignaturesDecideWhetherToSpend:
    def test_the_same_prefix_and_tools_produce_the_same_signature(self):
        tools = [{"name": "read_file", "parameters": {"type": "object"}}]
        assert vc._signature("hello", tools) == vc._signature("hello", tools)

    def test_a_changed_prefix_changes_the_signature(self):
        """A cache that no longer matches is worse than none: the request misses and pays full price."""
        assert vc._signature("hello", None) != vc._signature("hello there", None)

    def test_a_changed_tool_catalogue_changes_the_signature(self):
        assert vc._signature("x", [{"name": "a"}]) != vc._signature("x", [{"name": "b"}])

    def test_the_signature_does_not_contain_the_prompt(self):
        """A panel reporting on a cache must never have to hold prompt text, which can be user data."""
        secret = "PATIENT-NAME-JOHN-DOE"
        assert secret not in vc._signature(secret, None)


class TestStatsTellTheTruth:
    def test_a_zero_token_response_is_a_miss_not_a_hit(self, monkeypatch):
        """The number that reveals a cache which stopped matching."""
        monkeypatch.setattr(vc, "_stats", vc.CacheStats())
        vc.note_usage(5000)
        vc.note_usage(0)
        assert (vc.stats().hits, vc.stats().misses) == (1, 1)
        assert vc.stats().hit_rate == 0.5
        assert vc.stats().tokens_served_from_cache == 5000

    def test_hit_rate_with_no_requests_is_zero_not_an_error(self, monkeypatch):
        monkeypatch.setattr(vc, "_stats", vc.CacheStats())
        assert vc.stats().hit_rate == 0.0

    def test_the_report_names_the_switch_when_it_is_off(self, monkeypatch):
        monkeypatch.setattr(vc, "_stats", vc.CacheStats(enabled=False))
        assert "MYRIAXON_VERTEX_CACHE" in vc.format_status()

    def test_the_report_is_serialisable_for_the_api(self, monkeypatch):
        import json

        monkeypatch.setattr(vc, "_stats", vc.CacheStats(enabled=True, hits=3, misses=1,
                                                        tokens_served_from_cache=900))
        payload = json.dumps(vc.stats().to_dict())
        assert '"hit_rate": 0.75' in payload


class TestDeletingIsAlwaysPossible:
    def test_delete_without_a_name_is_refused_not_crashed(self):
        ok, problem = vc.delete("")
        assert ok is False and "no cache name" in problem
