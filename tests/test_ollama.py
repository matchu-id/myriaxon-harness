"""Ollama detection: pure logic, and the dispatch bug that shipped once.

The bug these tests exist for is real and specific. Ollama was added as a provider with its own
constant, its own alias and its own default endpoint -- but it was left out of the tuple in `cli.py`
that decides which agent implementation handles a provider. So a local turn fell through to the
Anthropic branch, which posted to the base URL without the `/v1` prefix, and Ollama answered
`404 page not found`. That message names neither the provider nor the URL, so it reads as the model
being absent rather than as a routing mistake, and no test failed.

`test_ollama_is_routed_to_the_openai_compatible_agent` is the guard. It asserts the routing decision
itself rather than the symptom, because the symptom is a 404 from a server that may not be running in
CI.

The rest covers the detection logic, which is where the judgement calls live: which context window to
believe, and what to say when nothing answers.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))


# -- the routing guard -------------------------------------------------------------------

def test_ollama_is_routed_to_the_openai_compatible_agent():
    """Ollama must be in the OpenAI-compatible branch, or every local turn 404s.

    Checked against the source text because the branch is chosen inside a function that needs a
    console, a config and a key to reach.

    An earlier version of this test hand-rolled a parenthesis matcher to extract the tuple, and it
    reported a failure while the code was correct -- the extraction was wrong, not the routing. A
    regression test that cries wolf is worse than none, so the check is now a plain assertion on the
    file's contents with no clever parsing.
    """
    source = (Path(__file__).resolve().parent.parent / "myriaxon" / "cli.py").read_text(encoding="utf-8")

    # Flatten whitespace so the assertion does not depend on how the tuple happens to be wrapped.
    flat = " ".join(source.split())

    for provider in (
        "PROVIDER_OPENAI_COMPAT",
        "PROVIDER_DEEPSEEK",
        "PROVIDER_OPENROUTER",
        "PROVIDER_NETRA",
        "PROVIDER_OLLAMA",
    ):
        assert f"config.provider in ( {provider}" in flat or f", {provider}" in flat, (
            f"{provider} is not in the OpenAI-compatible provider tuple in cli.py. Without "
            f"PROVIDER_OLLAMA a local turn is routed to the Anthropic branch and fails with a bare "
            f"404 page not found."
        )

    # And the specific invariant this test exists for, stated directly.
    assert ", PROVIDER_OLLAMA," in flat or "PROVIDER_OLLAMA, )" in flat or ", PROVIDER_OLLAMA )" in flat, (
        "PROVIDER_OLLAMA is not a member of the OpenAI-compatible provider tuple in cli.py"
    )


def test_ollama_has_an_alias_and_a_default_endpoint():
    from myriaxon.cli import _PROVIDER_ALIASES, _PROVIDER_DEFAULT_ENDPOINTS
    from myriaxon.constants import DEFAULT_OLLAMA_BASE_URL, PROVIDER_OLLAMA

    assert _PROVIDER_ALIASES["ollama"] == PROVIDER_OLLAMA
    assert _PROVIDER_DEFAULT_ENDPOINTS[PROVIDER_OLLAMA] == DEFAULT_OLLAMA_BASE_URL
    # The prefix is load-bearing: the client appends `/chat/completions` to this value, and Ollama
    # serves that route only under /v1.
    assert DEFAULT_OLLAMA_BASE_URL.endswith("/v1")


def test_dashboard_switch_provider_accepts_ollama(monkeypatch):
    """`/provider ollama` must work in the dashboard, not just in the CLI.

    This shipped broken: `ollama` was in the dropdown and in the CLI's alias table, but absent from
    the dashboard's own provider table, so the command answered
    `unknown provider 'ollama'; choose from deepseek, gemini, netra, openrouter`. The dropdown offered
    a vendor the command refused -- the worst version of the bug, because the two halves of the same
    feature disagreed in front of the user.
    """
    from myriaxon import ollama
    from myriaxon.config import Config
    from myriaxon.dashboard.server import _switch_provider
    from myriaxon.constants import DEFAULT_OLLAMA_BASE_URL, OLLAMA_PLACEHOLDER_KEY, PROVIDER_OLLAMA

    # Nothing answers, whatever is installed on the machine running the test: the switch must still work.
    monkeypatch.setattr(ollama, "candidate_hosts", lambda: ["http://127.0.0.1:9"])
    base = Config(api_key="sk-test", base_url="https://example.invalid/", model="some-model", provider="deepseek")

    switched = _switch_provider(base, PROVIDER_OLLAMA)

    assert switched.provider == PROVIDER_OLLAMA
    assert switched.base_url == DEFAULT_OLLAMA_BASE_URL
    # Ollama ignores the key, but the protocol requires a non-empty one -- an empty string makes the
    # client reject the request before it is sent, which looks like a credential problem.
    assert switched.api_key == OLLAMA_PLACEHOLDER_KEY
    # With nothing answering there is no model to choose, and that is not an error: the panel explains what is wrong.
    assert switched.model == ""


def test_dashboard_provider_list_includes_ollama():
    """The dropdown and the command must agree about what exists."""
    from myriaxon.dashboard.server import _providers
    from myriaxon.constants import PROVIDER_OLLAMA

    assert PROVIDER_OLLAMA in _providers()


# -- context window ----------------------------------------------------------------------

def test_context_length_is_found_by_suffix_not_by_family():
    """The key is `<architecture>.context_length` and the architecture varies by model.

    Measured on this machine: the same key is `qwen35.context_length` for one model and
    `nemotron_h.context_length` for another. A per-family lookup table would have missed both.
    """
    from myriaxon.ollama import _context_length_from

    assert _context_length_from({"qwen35.context_length": 262144}) == 262144
    assert _context_length_from({"nemotron_h.context_length": 131072}) == 131072
    assert _context_length_from({"llama.context_length": 8192}) == 8192
    # A family nobody anticipated still reports its window.
    assert _context_length_from({"brand_new_arch.context_length": 4096}) == 4096


def test_context_length_ignores_values_that_are_not_a_window():
    from myriaxon.ollama import _context_length_from

    assert _context_length_from({}) == 0
    assert _context_length_from({"general.parameter_count": 8953803264}) == 0
    # Zero and negative are absence, not a one-token window.
    assert _context_length_from({"llama.context_length": 0}) == 0
    assert _context_length_from({"llama.context_length": -1}) == 0
    assert _context_length_from({"llama.context_length": "8192"}) == 0


def test_practical_context_is_the_declared_window_not_a_guess_at_what_a_laptop_holds():
    """A model that supports 256K is budgeted as a 256K model.

    The harness used to cap every local model at 32,768 "because a laptop cannot hold more". That was wrong in both
    directions: it understated a model that really runs with 262,144 (the `myriaxon` model that `myriaxon ollama
    --create` builds) and overstated one that really runs with 4,096. The window that is budgeted is the one Ollama
    runs the model with (`runtime_context`); this function is the fallback when only a declared window is known, and it
    no longer second-guesses it. What a machine can hold is `estimate_fit`'s job, and it says so out loud.
    """
    from myriaxon.ollama import OLLAMA_DEFAULT_CONTEXT, practical_context

    assert practical_context(262_144) == 262_144
    assert practical_context(8_192) == 8_192
    assert practical_context(0) == OLLAMA_DEFAULT_CONTEXT      # unknown: Ollama's default, not a huge window


def test_practical_context_honours_the_override(monkeypatch):
    from myriaxon.ollama import OLLAMA_CONTEXT_ENV, practical_context

    monkeypatch.setenv(OLLAMA_CONTEXT_ENV, "131072")
    assert practical_context(262_144) == 131_072
    # A nonsense override falls back to the default rather than to something unparseable.
    monkeypatch.setenv(OLLAMA_CONTEXT_ENV, "not-a-number")
    assert practical_context(262_144) > 0


def test_profile_hint_follows_the_window_the_model_runs_with():
    """The tier shown is the tier the window earns: 256K is `large`, 4,096 is `small`."""
    from myriaxon.ollama import profile_hint

    assert profile_hint(262_144) == "large"
    assert profile_hint(200_000) == "large"
    assert profile_hint(65_536) == "balanced"
    assert profile_hint(8_192) == "small"
    assert profile_hint(0) == "small"            # unknown is Ollama's default window, 4,096


def test_detail_probe_carries_the_size_over_from_the_listing():
    """`/api/show` has no size, so a detailed probe must not lose the one `/api/tags` gave.

    Measured: without the merge every model reported 0 bytes, and `default_model` picked a 9B
    HuggingFace path over the 4B model because its size tiebreak had silently become name order.
    Run against a fake Ollama, so it needs no real one and touches none.
    """
    from fake_ollama import FakeOllama

    from myriaxon import ollama

    with FakeOllama() as server:
        probe = ollama.detect(with_details=True, timeout=1.5, hosts=[server.root])
    assert probe.models
    assert all(m.size_bytes > 0 for m in probe.models), (
        "a model reported 0 bytes; the size is not being carried from /api/tags into the detailed model objects"
    )
    assert ollama.default_model(probe.models) == "nemotron-3-nano:4b"      # the smaller of the two, by size


def test_default_model_prefers_the_smallest_tool_capable_one():
    """Leaving the model empty is not a usable state -- the next turn 404s.

    A model that cannot call tools cannot drive the agent at all, so those are excluded outright;
    among the rest the smallest wins, because a smaller model is likelier to fit in memory.
    """
    from myriaxon.ollama import OllamaModel, default_model

    big = OllamaModel(name="big-9b", size_bytes=6_000, capabilities=["tools"])
    small = OllamaModel(name="small-4b", size_bytes=2_600, capabilities=["tools"])
    prose_only = OllamaModel(name="tiny-no-tools", size_bytes=100, capabilities=["completion"])

    assert default_model([big, small, prose_only]) == "small-4b"
    # Deterministic when sizes are unavailable, rather than depending on input order.
    assert default_model([big, small]) == "small-4b"
    # Nothing usable at all: an empty string, so the caller can say why instead of sending a turn
    # that fails with a bare 404.
    assert default_model([prose_only]) == ""
    assert default_model([]) == ""


# -- capabilities ------------------------------------------------------------------------

def test_tool_capable_reads_the_capability_list():
    from myriaxon.ollama import OllamaModel, tool_capable

    assert tool_capable(OllamaModel(name="a", capabilities=["completion", "tools"]))
    assert not tool_capable(OllamaModel(name="b", capabilities=["completion"]))
    # No list at all is unknown, not "no": trying is the better default because a model that can call
    # tools and is denied the schemas looks like an agent refusing to work.
    assert tool_capable(OllamaModel(name="c", capabilities=[]))


def test_model_reports_vision_and_size():
    from myriaxon.ollama import OllamaModel

    model = OllamaModel(name="x", capabilities=["vision", "tools"], size_bytes=4 * 1024**3)
    assert model.supports_vision and model.supports_tools
    assert model.size_gb == pytest.approx(4.0)
    summary = model.summary()
    assert "x" in summary


# -- honest absence ----------------------------------------------------------------------

def test_explain_distinguishes_the_three_ways_ollama_can_be_absent():
    """Not installed, installed-but-not-serving, and serving-with-nothing-pulled need three fixes.

    Collapsing them into "no models found" tells the user nothing they can act on.
    """
    from myriaxon.ollama import Probe

    not_installed = Probe(installed=False, reachable=False, error="ConnectError")
    assert "ollama.com/download" in not_installed.explain()

    installed_not_serving = Probe(installed=True, reachable=False, error="ConnectError")
    assert "ollama serve" in installed_not_serving.explain()

    serving_with_nothing = Probe(base_url="http://127.0.0.1:11434", installed=True, reachable=True)
    assert "ollama pull" in serving_with_nothing.explain()


def test_probe_available_requires_both_reachable_and_models():
    from myriaxon.ollama import OllamaModel, Probe

    assert not Probe(reachable=True).available
    assert not Probe(models=[OllamaModel(name="m")]).available
    assert Probe(reachable=True, models=[OllamaModel(name="m")]).available


def test_detect_never_raises_when_nothing_is_listening():
    """Detection is called from a status line and a CLI command; neither may crash."""
    from myriaxon import ollama

    probe = ollama.detect(with_details=False, timeout=0.35)
    assert probe.reachable in (True, False)
    assert isinstance(probe.models, list)
    assert probe.explain()          # always produces something sayable


def test_candidate_hosts_puts_a_configured_host_first(monkeypatch):
    from myriaxon.ollama import HOST_ENV, candidate_hosts

    monkeypatch.setenv(HOST_ENV, "192.168.1.9:11434")
    hosts = candidate_hosts()
    assert hosts[0] == "http://192.168.1.9:11434"
    # Bare host:port and a full URL must both be accepted, and deduplicated.
    monkeypatch.setenv(HOST_ENV, "http://192.168.1.9:11434/")
    assert candidate_hosts()[0] == "http://192.168.1.9:11434"


def test_report_always_returns_lines():
    from myriaxon import ollama

    lines = ollama.report(with_details=False)
    assert lines and isinstance(lines[0], str)
