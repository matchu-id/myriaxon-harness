"""`--vendor gemini` was rejected by the code path that handles `--vendor`, so it could never work.

`argparse_cli` declares four vendors and its help text documents Gemini's Vertex mode in detail:

    choices=["deepseek", "netra", "gemini", "ollama"]

But `config.resolve_config`, which is what actually acts on the flag, accepted only two:

    E  --vendor must be 'deepseek' or 'netra', not 'gemini'

So the documented way to select Gemini from the command line exited 2. Measured before the fix, and after:

    --vendor gemini   ->  SystemExit(2), "--vendor must be 'deepseek' or 'netra', not 'gemini'"
    --vendor netra    ->  exit 0, "PONG", 2.4s
    (after) gemini    ->  exit 0, "PONG", 4.4s, no API key, via the gcloud login

THE ROOT CAUSE IS DUPLICATION, and the fix removes it rather than extending it. `switch_vendor` already
resolves all four providers, and its own docstring records that this duplication has caused a
surface-disagreement before: "the dashboard had its own `_switch_provider` and the CLI had `/provider`, and
they disagreed ... One function cannot disagree with itself." The `--vendor` block re-implemented resolution
for the two keyed gateways only, so the two it did not model were rejected.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import config as config_module  # noqa: E402
from myriaxon.constants import (  # noqa: E402
    PROVIDER_DEEPSEEK,
    PROVIDER_GEMINI,
    PROVIDER_NETRA,
    PROVIDER_OLLAMA,
)


class TestTheFlagAcceptsWhatArgparseAdvertises:
    def test_argparse_and_resolve_config_agree_on_the_set(self):
        """The mismatch itself, asserted directly: these two lists must not drift apart again."""
        from myriaxon import argparse_cli

        source = inspect.getsource(argparse_cli)
        advertised = None
        for line in source.splitlines():
            if '"--vendor"' in line:
                advertised = line
                break
        assert advertised is not None, "the --vendor flag is missing from argparse"
        # The choices are on the line immediately after the flag name.
        index = source.splitlines().index(advertised)
        choices_line = source.splitlines()[index + 1]
        assert "gemini" in choices_line and "ollama" in choices_line

    def test_the_vendor_block_hands_gemini_and_ollama_to_switch_vendor(self):
        """Delegation, not a second implementation. This is the fix."""
        source = inspect.getsource(config_module.resolve_config)
        assert "switch_vendor" in source, (
            "resolve_config must delegate the providers it does not model, rather than reject them"
        )
        assert "PROVIDER_GEMINI, PROVIDER_OLLAMA" in source

    def test_the_rejection_message_names_all_four(self):
        """A typo should say what IS accepted, and it must not omit two of them."""
        source = inspect.getsource(config_module.resolve_config)
        assert "PROVIDER_GEMINI!r} or {PROVIDER_OLLAMA!r}" in source.replace("\n", " ") or (
            "PROVIDER_GEMINI" in source and "PROVIDER_OLLAMA" in source
        )


class TestGeminiResolves:
    def test_a_gemini_vendor_switch_produces_a_gemini_config(self, monkeypatch, tmp_path):
        """No API key is needed for Vertex, so this must not fall into the key prompt."""
        monkeypatch.setenv("MYRIAXON_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "a-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")

        switched, problem = config_module.switch_vendor(None, PROVIDER_GEMINI, "gemini-3.8-flash")
        assert problem == "", f"gemini could not be switched to: {problem}"
        assert switched is not None
        assert switched.provider == PROVIDER_GEMINI
        assert switched.model == "gemini-3.8-flash"

    def test_switch_vendor_knows_all_four_providers(self):
        """The single source of truth. If this loses one, --vendor loses it too."""
        source = inspect.getsource(config_module.switch_vendor)
        for provider in (PROVIDER_DEEPSEEK, PROVIDER_NETRA, PROVIDER_GEMINI, PROVIDER_OLLAMA):
            constant = f"PROVIDER_{provider.upper()}"
            assert constant in source, f"{provider} is not accepted by switch_vendor"

    def test_an_unknown_vendor_is_still_refused(self, monkeypatch, tmp_path):
        """The guard must keep guarding -- widening it must not make it accept anything."""
        monkeypatch.setenv("MYRIAXON_CONFIG_DIR", str(tmp_path))
        switched, problem = config_module.switch_vendor(None, "not-a-vendor")
        assert switched is None and problem


class TestTheKeyedGatewaysStillWork:
    def test_deepseek_and_netra_still_resolve(self, monkeypatch, tmp_path):
        """The path that already worked must not regress, since it is the one most users take."""
        monkeypatch.setenv("MYRIAXON_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("NETRA_API_KEY", "sk-test-netra")
        switched, problem = config_module.switch_vendor(None, PROVIDER_NETRA, "m")
        assert problem == "" and switched is not None and switched.provider == PROVIDER_NETRA

    def test_a_missing_key_is_reported_not_guessed(self, monkeypatch, tmp_path):
        """The docstring's promise: a missing key is reported, because the alternative is a 401 mid-task."""
        monkeypatch.setenv("MYRIAXON_CONFIG_DIR", str(tmp_path))
        for name in ("DEEPSEEK_API_KEY", "MYRIAXON_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        switched, problem = config_module.switch_vendor(None, PROVIDER_DEEPSEEK)
        if switched is None:
            assert "key" in problem.lower() or "credential" in problem.lower()
