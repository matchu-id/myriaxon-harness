"""Cache accounting and the status line's numbers.

Cache reads were never being read from either provider, so a session with a 99% hit rate looked
identical to one paying full price for every token. That is the difference between "this long
session is cheap" and "this long session is expensive", and the harness could not tell you which.

The arithmetic here is small and easy to get subtly wrong, which is why it is pinned: the hit rate
has a denominator that can be chosen two ways, and only one of them is meaningful.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon.usage import UsageTotals, cache_fields, format_usage_summary  # noqa: E402


class _Obj:
    """A stand-in for a provider usage object: attributes are set per test."""

    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class TestCacheFields(unittest.TestCase):
    def test_anthropic_shape(self) -> None:
        usage = _Obj(cache_read_input_tokens=900, cache_creation_input_tokens=100)
        self.assertEqual(cache_fields(usage), (900, 100))

    def test_openai_shape_reports_reads_only(self) -> None:
        """OpenAI-compatible APIs do not bill cache writes separately. Reporting 0 for writes is
        the truth about what they tell us, not an assumption that nothing was written."""
        usage = _Obj(prompt_tokens_details=_Obj(cached_tokens=750))
        self.assertEqual(cache_fields(usage), (750, 0))

    def test_a_provider_that_reports_nothing_gives_zero(self) -> None:
        self.assertEqual(cache_fields(_Obj()), (0, 0))

    def test_nonsense_values_do_not_raise(self) -> None:
        """A provider returning a string where a number belongs must not break a turn's accounting."""
        usage = _Obj(cache_read_input_tokens="not a number")
        self.assertEqual(cache_fields(usage), (0, 0))

    def test_a_partial_anthropic_shape(self) -> None:
        """Some proxies forward only one of the two fields."""
        self.assertEqual(cache_fields(_Obj(cache_read_input_tokens=500)), (500, 0))
        self.assertEqual(cache_fields(_Obj(cache_creation_input_tokens=500)), (0, 500))


class TestUsageTotals(unittest.TestCase):
    def test_accumulates_across_requests(self) -> None:
        totals = UsageTotals()
        totals.add(1000, 200, 900, 100)
        totals.add(500, 100, 400, 0)
        self.assertEqual(totals.request_count, 2)
        self.assertEqual(totals.input_tokens, 1500)
        self.assertEqual(totals.output_tokens, 300)
        self.assertEqual(totals.total_tokens, 1800)
        self.assertEqual(totals.cache_read_tokens, 1300)
        self.assertEqual(totals.cache_write_tokens, 100)

    def test_the_old_two_argument_call_still_works(self) -> None:
        """Call sites outside this module pass tokens only; adding parameters must not break them."""
        totals = UsageTotals()
        totals.add(10, 5)
        self.assertEqual(totals.total_tokens, 15)
        self.assertEqual(totals.cache_read_tokens, 0)

    def test_none_is_treated_as_zero(self) -> None:
        totals = UsageTotals()
        totals.add(None, None, None, None)
        self.assertEqual(totals.request_count, 1)
        self.assertEqual(totals.total_tokens, 0)

    def test_hit_rate_uses_the_denominator_that_means_something(self) -> None:
        """Reads over (reads + uncached input).

        Against input alone the rate can exceed 100% -- 1300 cached reads on 1500 input tokens would
        read as 86% one way and 46% the other, and only the second says "of everything that could
        have been cached, this much was".
        """
        totals = UsageTotals()
        totals.add(1000, 0, 900, 0)
        self.assertAlmostEqual(totals.cache_hit_rate, 900 / 1900, places=6)

    def test_a_genuine_full_hit_is_one_hundred_percent(self) -> None:
        totals = UsageTotals()
        totals.add(0, 50, 2000, 0)
        self.assertAlmostEqual(totals.cache_hit_rate, 1.0, places=6)

    def test_no_traffic_is_zero_not_an_error(self) -> None:
        self.assertEqual(UsageTotals().cache_hit_rate, 0.0)

    def test_the_summary_mentions_cache_only_when_there_is_cache(self) -> None:
        plain = UsageTotals()
        plain.add(100, 20)
        self.assertNotIn("Cache:", format_usage_summary(plain, "p", "m"))

        cached = UsageTotals()
        cached.add(100, 20, 4000, 0)
        text = format_usage_summary(cached, "p", "m")
        self.assertIn("Cache:", text)
        self.assertIn("read", text)

    def test_an_empty_session_says_so(self) -> None:
        self.assertIn("No usage", format_usage_summary(UsageTotals(), "p", "m"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
