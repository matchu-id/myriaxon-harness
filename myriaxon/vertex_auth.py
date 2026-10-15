"""Vertex AI authentication from the operator's own gcloud login -- no API key, nothing on disk.

Reproduced from `Project-Lighthouse/Lighthouse-Platform/lighthouse/vertex.py`, which is known to work on
this machine. Every earlier attempt to reach Gemini failed, and each failure looked like a permissions
problem while actually being one of three details:

    wrote                     correct                        symptom of getting it wrong
    ------------------------  -----------------------------  ------------------------------------
    us-central1 / a region    location = "global"            the API is enabled per endpoint
    global-aiplatform...      aiplatform.googleapis.com      "global" is not a hostname prefix
    gemini-2.5-flash          gemini-3.8-flash               the model must exist in that project

The hostname rule is the subtle one and it is a single line in Lighthouse:

    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"

Why a token and not a key: the owner's requirement is to use the Google account they are already signed
into, and a key would be a second, weaker credential to look after. The cost is that a gcloud token
expires, which is why the refresh below is the substance of this module rather than a detail.

What this is NOT: a service account. There is no key file, nothing written to disk, and no long-lived
secret. When the gcloud login expires the calls fail with a message that says to sign in again, which is
the honest failure for a credential the operator controls.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

# Matches Lighthouse's own cache window. A gcloud access token lives about an hour; refreshing at 45
# minutes leaves a quarter of the life as margin, so a slow request cannot start on a token that expires
# while it is in flight.
TOKEN_TTL_SECONDS = 45 * 60

# The project is whatever the CALLER's environment says. There is deliberately no baked-in default.
#
# A hardcoded fallback project id was here, and it was wrong twice over. It was somebody's real corporate
# project id sitting in a public repository, and it was a value that only ever worked for that one
# organisation -- so every other user's first Vertex request would have gone to a project they cannot read,
# and failed with a 403 that named a stranger's project rather than explaining that nothing was configured.
#
# Empty means "not configured", and `describe()` says so. See `_require_project` for where that is turned into
# an actionable message instead of a request that cannot succeed.
DEFAULT_PROJECT = os.environ.get("MYRIAXON_VERTEX_PROJECT", "").strip()
DEFAULT_LOCATION = os.environ.get("MYRIAXON_VERTEX_LOCATION", "global")
DEFAULT_MODEL = os.environ.get("MYRIAXON_VERTEX_MODEL", "gemini-3.8-flash")


def _require_project(project: str = "") -> str:
    """The project to bill against, or a message naming what to set.

    Raises `VertexAuthError` rather than returning a guess. An empty project cannot produce a working request,
    so the honest outcome is an explanation -- a request to `projects//locations/...` would fail with a
    confusing 404 from Google instead of telling the user which variable is missing.
    """
    resolved = (project or DEFAULT_PROJECT or os.environ.get("GOOGLE_CLOUD_PROJECT", "")).strip()
    if not resolved:
        raise VertexAuthError(
            "No Google Cloud project is configured for Vertex AI. Set GOOGLE_CLOUD_PROJECT (what the "
            "google-genai SDK reads) or MYRIAXON_VERTEX_PROJECT, or run:\n"
            "    gcloud config set project YOUR_PROJECT_ID"
        )
    return resolved

_token: dict[str, object] = {"value": None, "expires": 0.0}
_lock = threading.Lock()


class VertexAuthError(RuntimeError):
    """No usable credential. The message always names the fix, because the cause is always the same."""


def _gcloud() -> str:
    found = shutil.which("gcloud")
    if found:
        return found
    # The Cloud SDK installer does not always reach PATH, and on Windows the entry point is a .cmd that
    # `shutil.which` finds only when the extension is in PATHEXT. Checked explicitly so "gcloud is not
    # installed" is only reported when it genuinely is not.
    for candidate in (
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"),
        "/usr/bin/gcloud", "/usr/local/bin/gcloud",
        os.path.expanduser("~/google-cloud-sdk/bin/gcloud"),
    ):
        if candidate and os.path.exists(candidate):
            return candidate
    raise VertexAuthError(
        "gcloud was not found. Install the Google Cloud SDK, then run: gcloud auth login"
    )


def access_token(force: bool = False) -> str:
    """A bearer token for Vertex AI, cached for `TOKEN_TTL_SECONDS`.

    `force=True` discards the cache and asks again, which is what a 401 handler needs: a token can be
    invalidated before its nominal expiry (revoked, or the machine woke from sleep), and retrying once
    with a fresh one is the difference between a transient failure and a broken session.
    """
    with _lock:
        cached = _token["value"]
        if cached and not force and time.time() < float(_token["expires"]):  # type: ignore[arg-type]
            return str(cached)

        binary = _gcloud()
        try:
            done = subprocess.run(
                [binary, "auth", "print-access-token"],
                capture_output=True, text=True, timeout=120, encoding="utf8", errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise VertexAuthError(
                "gcloud took longer than 120s to produce a token. A cold gcloud start can be slow; "
                "try again, and if it persists run `gcloud auth login`."
            ) from exc
        except OSError as exc:
            raise VertexAuthError(f"could not run gcloud: {exc}") from exc

        token = (done.stdout or "").strip()
        if done.returncode != 0 or not token:
            detail = (done.stderr or "").strip()[:200]
            raise VertexAuthError(
                "gcloud is not signed in, so Vertex AI cannot be called. Run:  gcloud auth login"
                + (f"\n  gcloud said: {detail}" if detail else "")
            )

        _token["value"] = token
        _token["expires"] = time.time() + TOKEN_TTL_SECONDS
        return token


def invalidate() -> None:
    """Forget the cached token, so the next call fetches a new one."""
    with _lock:
        _token["value"] = None
        _token["expires"] = 0.0


def project() -> str:
    """The project to use, in the order a reader expects.

    `GOOGLE_CLOUD_PROJECT` IS CONSULTED, and that is a fix rather than a convenience. Removing the hardcoded
    default left this reading only MYRIAXON_VERTEX_PROJECT, so a machine configured the DOCUMENTED way -- the
    variable the google-genai SDK itself reads, and the one `docs/VERTEX-SETUP.md` tells you to set -- resolved
    to an empty project and produced:

        https://aiplatform.googleapis.com/v1/projects//locations/global/...
        400 RESOURCE_PROJECT_INVALID

    Measured: with only `GOOGLE_CLOUD_PROJECT` set, `generate_url()` returned a URL with an empty project
    segment. The hardcoded default had been hiding that by supplying a project nobody had configured.

    Order: the Myriaxon-specific override first, then the standard Google variable, then `gcloud`'s own
    configuration. Empty means genuinely unconfigured, and `_require_project` turns that into a message naming
    what to set instead of a URL that cannot work.
    """
    explicit = (os.environ.get("MYRIAXON_VERTEX_PROJECT") or "").strip()
    if explicit:
        return explicit
    standard = (os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    if standard:
        return standard
    return (DEFAULT_PROJECT or "").strip()


def location() -> str:
    return (os.environ.get("MYRIAXON_VERTEX_LOCATION") or DEFAULT_LOCATION).strip()


def model() -> str:
    return (os.environ.get("MYRIAXON_VERTEX_MODEL") or DEFAULT_MODEL).strip()


def host(loc: str = "") -> str:
    """The hostname rule, copied from the one line of Lighthouse that makes `global` work.

    `global` is not a region prefix -- `global-aiplatform.googleapis.com` does not exist and answers 404.
    The global endpoint is the bare host, and every other location is prefixed.
    """
    loc = loc or location()
    return "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"


def base_url(loc: str = "") -> str:
    loc = loc or location()
    return f"https://{host(loc)}/v1"


def generate_url(model_name: str = "", *, proj: str = "", loc: str = "") -> str:
    """The `generateContent` endpoint for one model."""
    proj = proj or project()
    loc = loc or location()
    model_name = model_name or model()
    return (f"https://{host(loc)}/v1/projects/{proj}/locations/{loc}"
            f"/publishers/google/models/{model_name}:generateContent")


def describe() -> str:
    """One line for a status panel or an error message."""
    return (f"vertex: {project()} / {location()} / {model()} "
            f"(auth: your gcloud login, no API key)")


class GcloudCredentials:
    """Shaped like `google.auth.credentials.Credentials`, backed by the gcloud login.

    This is the one piece of glue that lets the EXISTING Vertex support work without Application Default
    Credentials.

    Myriaxon already knows how to talk to Vertex: `config.vertex_project` switches the Gemini client to
    `genai.Client(vertexai=True, project=..., location=...)`, and `gemini_agent` already translates
    OpenAI-shaped messages and tools into Gemini's. What it did not have was a credential -- the client
    looks for ADC, `gcloud auth login` does not create ADC, and the documented way to create ADC is
    `gcloud auth application-default login`, an interactive browser flow.

    A shim is enough because everything in the chain only ever reads `.token`:

        genai.Client(credentials=<this>, vertexai=True, project=..., location=...)

    Deliberately NOT a subclass of `google.auth.credentials.Credentials`. Duck typing is what the client
    actually relies on, and inheriting would make this module unrunnable without google-auth installed --
    turning an optional convenience into a hard dependency and breaking the "pure Python, no vendor lock
    on the credential" property the owner asked for.

    `expired` is always False because the refresh is handled inside `access_token`, which caches for 45
    minutes and re-runs gcloud when the window closes. Reporting expiry to the client as well would give
    two refresh paths that could disagree.
    """

    def __init__(self) -> None:
        self._token = ""

    @property
    def token(self) -> str:
        if not self._token:
            self._token = access_token()
        return self._token

    @token.setter
    def token(self, value: str) -> None:
        # The client sets this on refresh; accepting and forwarding it keeps the cache in step rather
        # than letting the client hold a token this module knows nothing about.
        self._token = value or ""

    @property
    def expired(self) -> bool:
        return False

    @property
    def valid(self) -> bool:
        return True

    @property
    def quota_project_id(self) -> str:
        return project()

    def refresh(self, request=None) -> None:  # noqa: ARG002 -- signature matches google.auth
        """Ask gcloud again. Called by the client when a request comes back 401."""
        self._token = access_token(force=True)

    def before_request(self, request=None, method=None, url=None, headers=None) -> None:  # noqa: ARG002
        """The hook google-auth calls before every request, which is where the token enters."""
        if headers is not None:
            headers["Authorization"] = f"Bearer {self.token}"

    def apply(self, headers: dict) -> None:
        """google-auth's own name for attaching the token to a request."""
        headers["Authorization"] = f"Bearer {self.token}"

    def __repr__(self) -> str:  # never the token itself
        return f"<GcloudCredentials project={project()!r} location={location()!r}>"
