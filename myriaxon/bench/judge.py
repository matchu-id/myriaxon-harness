"""Model judges for IQB-14.

A judge is the only place in this standard where scoring stops being deterministic, so it is
quarantined by construction:

* the judge's provider, model, temperature and endpoint are recorded in the run manifest, and every
  result it produces is tagged `judged: true` so the report can state what share of scoring it did;
* a judge that cannot be constructed fails loudly at startup rather than at scoring time;
* `llm-rubric` with no judge configured is a **harness error, never a pass** (see assertions.py).

Two judges are provided:

* `OpenAICompatJudge` -- a bare chat-completions call. Deterministic (`temperature=0`) and cheap,
  which is what a benchmark wants for bulk point-recall grading.
* `MyriaxonJudge` -- shells out to `python -m myriaxon -p <prompt> --output-format json`, i.e. the
  judge *is* Myriaxon driven by a chosen provider/model. Use it when the question is "how well does
  our own agent product do at this grading task"; use the former when the question is "what did the
  model under test actually output". Recording which one scored a run is what keeps those two
  questions from being confused after the fact.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class JudgeSpec:
    provider: str
    model: str
    base_url: str = ""
    api_key_env: str = ""
    api_key: str = ""
    temperature: float = 0.0
    mode: str = "agent"  # "agent" = myriaxon CLI, "api" = direct chat completion
    timeout_s: float = 180.0

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_present": bool(self.api_key or self.api_key_env),
            "temperature": self.temperature,
            "mode": self.mode,
            "implementation": "myriaxon" if self.mode == "agent" else "openai-compatible-chat",
        }


class OpenAICompatJudge:
    """Direct chat-completions judge. No tools, no agent loop -- just the grading call."""

    def __init__(self, spec: JudgeSpec, api_key: str) -> None:
        self.spec = spec
        self.api_key = api_key
        self.calls = 0

    def __call__(self, prompt: str, model: str | None = None) -> str:
        import openai

        client = openai.OpenAI(
            api_key=self.api_key,
            base_url=self.spec.base_url or None,
            max_retries=2,
            timeout=self.spec.timeout_s,
        )
        self.calls += 1
        reply = client.chat.completions.create(
            model=model or self.spec.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=self.spec.temperature,
        )
        return reply.choices[0].message.content or ""


class MyriaxonJudge:
    """Judge implemented by running Myriaxon itself, non-interactively, against a chosen model.

    Runs in a throwaway empty directory: the judge must not be able to read the workspace it is
    grading, or it could score an artifact by inspecting the environment the agent left behind
    rather than by reading the text it was given.
    """

    def __init__(self, spec: JudgeSpec, api_key: str, python_executable: str = "") -> None:
        self.spec = spec
        self.api_key = api_key
        self.python = python_executable or "python"
        self.calls = 0

    def __call__(self, prompt: str, model: str | None = None) -> str:
        import os
        import sys
        import tempfile
        from pathlib import Path

        env = os.environ.copy()
        env["MYRIAXON_PROVIDER"] = self.spec.provider
        env["MYRIAXON_MODEL"] = model or self.spec.model
        env["MYRIAXON_API_KEY"] = self.api_key
        if self.spec.base_url:
            env["MYRIAXON_BASE_URL"] = self.spec.base_url

        # NOT tempfile.TemporaryDirectory: Python creates those mode-0700, which restricted
        # environments (including the sandbox this was developed in) refuse to write into, so the
        # judge died with a PermissionError before it ever reached a model. A plain mkdir gives
        # normal permissions; the directory is intentionally left behind rather than aggressively
        # cleaned, because a failed cleanup must never mask a judge result.
        scratch = Path(tempfile.gettempdir()) / "iqb_judge_workspace"
        scratch.mkdir(parents=True, exist_ok=True)

        self.calls += 1
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "myriaxon", "-p", prompt, "--output-format", "json", "-y", "--no-resume"],
                cwd=str(scratch),
                env=env,
                capture_output=True,
                text=True,
                timeout=self.spec.timeout_s,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"myriaxon judge timed out after {self.spec.timeout_s}s") from exc

        stdout = (proc.stdout or "").strip()
        if not stdout:
            raise RuntimeError(f"myriaxon judge produced no output (rc={proc.returncode}): {proc.stderr[:300]!r}")
        # The JSON object is the last line; anything before it belongs to the CLI's own banner.
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("error"):
                    raise RuntimeError(f"myriaxon judge reported an error: {data['error']}")
                return data.get("text") or ""
        raise RuntimeError(f"myriaxon judge produced non-JSON output: {stdout[:300]!r}")


def build_judge(spec: JudgeSpec | None, *, python_executable: str = ""):
    """Construct the configured judge, or return None when judging is disabled.

    Raises rather than returning a broken judge: a run configured to judge with a model that cannot
    be reached must fail at startup, not silently downgrade every `llm-rubric` assertion to a
    harness error halfway through a suite.
    """
    if spec is None or not spec.model:
        return None
    import os

    api_key = spec.api_key or os.environ.get(spec.api_key_env, "")
    if not api_key:
        raise ValueError(
            f"judge model {spec.provider}:{spec.model} requested but no API key found "
            f"(checked --judge-api-key and {spec.api_key_env or 'no env var'})"
        )
    if spec.mode == "agent":
        return MyriaxonJudge(spec, api_key, python_executable)
    return OpenAICompatJudge(spec, api_key)


def default_endpoint(provider: str) -> str:
    """Endpoint for the providers this repo already knows, so `--judge-provider deepseek` alone is
    enough and nobody has to remember the base URL to reproduce a run."""
    from ..constants import (
        DEFAULT_DEEPSEEK_BASE_URL,
        DEFAULT_GEMINI_BASE_URL,
        DEFAULT_OPENROUTER_BASE_URL,
    )

    return {
        "deepseek": DEFAULT_DEEPSEEK_BASE_URL,
        "openrouter": DEFAULT_OPENROUTER_BASE_URL,
        "gemini": DEFAULT_GEMINI_BASE_URL,
    }.get(provider, "")


def default_key_env(provider: str) -> str:
    from ..constants import (
        DEEPSEEK_API_KEY_ENV_FALLBACK,
        GEMINI_API_KEY_ENV_FALLBACK,
        OPENROUTER_API_KEY_ENV_FALLBACK,
    )

    return {
        "deepseek": DEEPSEEK_API_KEY_ENV_FALLBACK,
        "openrouter": OPENROUTER_API_KEY_ENV_FALLBACK,
        "gemini": GEMINI_API_KEY_ENV_FALLBACK,
    }.get(provider, "MYRIAXON_API_KEY")
