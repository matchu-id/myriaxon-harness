"""Secret redaction: scrub credentials before they reach a subprocess, a log, or the model.

## The gap this closes

Myriaxon had no redaction at all. Two distinct exposures, with different fixes:

**1. Ambient credentials leaking into a spawn.** Every subprocess inherits the environment, so a tool that runs a
command hands it `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, and whatever else the operator exported. DeepSeek's harness
drops credential-shaped variables before any spawn (`subprocess/subprocess-local/src/spawn.ts`, verified) while
honouring explicit caller-supplied values, "so explicit values are honored, ambient creds never leak."

**2. A secret a tool printed.** This one is **unsolved in DeepSeek too** — their own scan records that "nothing
redacts secrets a TOOL PRINTS into its own output", so `cat .env` is in the transcript permanently. There is no
design to copy, only the placement: the writer. This module redacts at the point text is recorded, which is the
earliest point every path passes through.

## What this deliberately does not do

It does not try to detect "secrets" by entropy in arbitrary prose. A high-entropy string is as likely to be a
commit SHA, a UUID, a minified line, or a base64 image as a credential, and a redactor that mangles ordinary
output teaches its reader to disable it. It matches **named shapes** — the prefixes and formats real credentials
use — and a caller can register additional patterns for its own deployment.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable

# The replacement. Deliberately says which shape matched, because "why is this redacted?" is a question a reader
# will ask and answering it in the text saves a trip to the source.
REDACTED = "[redacted {kind}]"

# The marker used inside a subprocess environment.
ENV_REDACTED = "<redacted: inherited credential>"

# Environment variable names that are credentials. Matched case-insensitively against the whole name, and by
# suffix so `AWS_SECRET_ACCESS_KEY` and `MY_SERVICE_API_KEY` are both caught. Kept as a tuple so the shape of the
# rule is inspectable, and so a deployment can add to it rather than reimplement it.
CREDENTIAL_ENV_NAMES = frozenset({
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "COHERE_API_KEY",
    "HUGGINGFACE_API_KEY", "REPLICATE_API_TOKEN", "PERPLEXITY_API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AZURE_OPENAI_API_KEY", "AZURE_CLIENT_SECRET",
    "GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "BITBUCKET_TOKEN",
    "NPM_TOKEN", "PYPI_TOKEN", "TWINE_PASSWORD", "DOCKER_PASSWORD",
    "SLACK_TOKEN", "SLACK_BOT_TOKEN", "DISCORD_TOKEN", "TELEGRAM_BOT_TOKEN",
    "STRIPE_SECRET_KEY", "STRIPE_API_KEY", "SENDGRID_API_KEY", "MAILGUN_API_KEY",
    "DATABASE_URL", "REDIS_URL", "MONGODB_URI", "POSTGRES_PASSWORD",
    "MYSQL_ROOT_PASSWORD", "PGPASSWORD", "NETRA_API_KEY",
})

# Suffixes and prefixes that make a variable credential-shaped whatever its first word is.
CREDENTIAL_ENV_SUFFIXES = ("_API_KEY", "_APIKEY", "_SECRET", "_SECRET_KEY", "_TOKEN",
                           "_ACCESS_KEY", "_PRIVATE_KEY", "_PASSWORD", "_PASSWD", "_CREDENTIALS")
CREDENTIAL_ENV_PREFIXES = ("AWS_", "AZURE_", "GCP_", "GOOGLE_", "OPENAI_", "ANTHROPIC_", "MYRIAXON_")

# Named credential SHAPES. Each is a real published prefix, chosen because the prefix is what makes a match
# trustworthy: `sk-` followed by 20+ characters is a key, whereas a bare 40-character alphanumeric could be
# anything.
_SHAPES: tuple[tuple[str, re.Pattern], ...] = (
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("deepseek-key", re.compile(r"\bsk-[0-9a-f]{32}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("stripe-key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("pypi-token", re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_\-]{20,}")),
    ("private-key-block", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----",
        re.DOTALL)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer-header", re.compile(r"(?i)\b(?:authorization|bearer)\s*[:=]\s*(?:bearer\s+)?[A-Za-z0-9._\-]{16,}")),
    # A connection string with an inline password. The password group is what is replaced, so the shape of the
    # URL survives and the message stays readable.
    ("connection-string", re.compile(
        r"(?i)\b([a-z][a-z0-9+.\-]*://[^:/@\s]+):([^@/\s]{3,})@")),
)

# Assignment shapes: `NAME=<value>` where NAME is credential-shaped and the value is long enough to be one.
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)"
    r"\s*[:=]\s*[\"']?([^\s\"',;]{8,})[\"']?"
)


@dataclass
class Redaction:
    """One redaction that happened, for a count and for a report."""

    kind: str
    count: int = 0


@dataclass
class Report:
    """What a redaction pass changed.

    Returns counts by kind rather than the values. A report that quoted what it removed would reintroduce the
    secret into whatever reads the report — which is how a redactor becomes a leak.
    """

    kinds: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.kinds.values())

    @property
    def changed(self) -> bool:
        return bool(self.kinds)

    def render(self) -> str:
        if not self.kinds:
            return "no secrets found"
        parts = ", ".join(f"{kind} x{count}" for kind, count in sorted(self.kinds.items()))
        return f"{self.total} redaction(s): {parts}"


def redact(text: str, *, extra_patterns: Iterable[tuple[str, str]] = ()) -> tuple[str, Report]:
    """Replace credential-shaped values in `text`. Returns `(clean_text, report)`.

    The connection-string case is handled first and specially, because replacing the whole URL would destroy the
    useful part of the line: a reader needs to know *which* database, not which password.
    """
    if not text:
        return text, Report()

    report = Report()
    result = text

    for kind, pattern in _SHAPES:
        if kind == "connection-string":
            def _userinfo(match: re.Match) -> str:
                report.kinds[kind] = report.kinds.get(kind, 0) + 1
                return f"{match.group(1)}:{REDACTED.format(kind=kind)}@"
            result = pattern.sub(_userinfo, result)
            continue

        def _replace(match: re.Match, _kind: str = kind) -> str:
            report.kinds[_kind] = report.kinds.get(_kind, 0) + 1
            return REDACTED.format(kind=_kind)
        result = pattern.sub(_replace, result)

    # `NAME=value` for a credential-shaped NAME. Runs last so a value already caught by a named shape is not
    # counted twice.
    def _assign(match: re.Match) -> str:
        name, value = match.group(1), match.group(2)
        if value.startswith("[redacted"):
            return match.group(0)
        report.kinds["assigned-" + name.lower()] = report.kinds.get("assigned-" + name.lower(), 0) + 1
        return f"{name}={REDACTED.format(kind='value')}"
    result = _ASSIGNMENT.sub(_assign, result)

    for kind, pattern_text in extra_patterns:
        try:
            pattern = re.compile(pattern_text)
        except re.error:
            # A caller-supplied pattern that does not compile is skipped rather than raising. This runs on the
            # output path, and failing a tool result because an optional pattern was malformed would be worse
            # than the pattern not applying.
            continue

        def _extra(match: re.Match, _kind: str = kind) -> str:
            report.kinds[_kind] = report.kinds.get(_kind, 0) + 1
            return REDACTED.format(kind=_kind)
        result = pattern.sub(_extra, result)

    return result, report


# --------------------------------------------------------------------------- environment scrubbing


def is_credential_name(name: str) -> bool:
    """Whether an environment variable NAME is credential-shaped.

    Name-based, not value-based, and deliberately so. A rule that inspected values would have to guess, and the
    guess is wrong in both directions: a short password looks like a normal word, and a long commit SHA looks
    like a key.
    """
    upper = (name or "").upper()
    if not upper:
        return False
    if upper in CREDENTIAL_ENV_NAMES:
        return True
    if upper.endswith(CREDENTIAL_ENV_SUFFIXES):
        return True
    # A prefix alone is not enough -- `AWS_REGION` and `OPENAI_MODEL` are not secrets and dropping them breaks
    # tools that need them.
    if upper.startswith(CREDENTIAL_ENV_PREFIXES):
        return any(word in upper for word in ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL"))
    return False


def scrub_environment(
    source: dict | None = None,
    *,
    explicit: dict | None = None,
    keep: Iterable[str] = (),
) -> dict:
    """A spawn environment with ambient credentials removed and explicit values honoured.

    Their ordering rule, which is the important part: **explicit values merge AFTER the scrub.** A caller that
    passes `{"MY_API_KEY": value}` means it, so the value is honoured; an inherited `MY_API_KEY` is not the same
    statement and is dropped. Scrubbing after the merge would break the first case, and merging after a scrub that
    had already removed the name from the ambient set is what makes the distinction expressible.

    `keep` names variables to preserve regardless -- for the case where a tool genuinely needs one, made explicit
    at the call site rather than by weakening the rule.
    """
    ambient = dict(os.environ if source is None else source)
    kept = {name.upper() for name in keep}

    scrubbed: dict[str, str] = {}
    for name, value in ambient.items():
        if name.upper() in kept or not is_credential_name(name):
            scrubbed[name] = value

    if explicit:
        # Merged after, and deliberately NOT filtered: an explicit value is a decision by the caller, and
        # dropping it would silently break a tool the caller configured.
        scrubbed.update(explicit)

    return scrubbed


def scrub_environment_report(source: dict | None = None) -> list[str]:
    """Which variable NAMES were dropped. Names only -- never values.

    Exists so a user can see *why* a tool cannot authenticate, without the diagnostic itself carrying the
    credential it is reporting on.
    """
    ambient = dict(os.environ if source is None else source)
    return sorted(name for name in ambient if is_credential_name(name))
