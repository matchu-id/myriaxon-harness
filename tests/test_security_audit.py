"""Security regressions: two holes found by an audit, and the checks that keep them shut.

Both were found by asking what a MODEL can reach, not by grepping for suspicious names. That distinction
matters: `web_tools.py` has no `eval`, no `shell=True` and no `subprocess` -- it looked harmless by every
keyword measure, and it was the widest hole in the package.

  1. `web_fetch` fetched ANY host. Verified against a loopback listener it returned the service's body.
  2. The webhook compared its shared secret with `!=`, which leaks the secret's prefix through response
     timing.
"""
from __future__ import annotations

import http.server
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import web_tools  # noqa: E402


class TestWebFetchRefusesLocalAndPrivateHosts:
    """The hole: `web_fetch` validated the SCHEME only, so it reached anything routable."""

    @pytest.mark.parametrize("url,why", [
        ("http://127.0.0.1/", "loopback"),
        ("http://127.0.0.1:8080/admin", "loopback with a port"),
        ("http://localhost/", "the name for loopback"),
        ("http://[::1]/", "IPv6 loopback"),
        ("http://169.254.169.254/latest/meta-data/", "the cloud metadata range"),
        ("http://10.0.0.5/", "RFC1918"),
        ("http://192.168.1.1/", "RFC1918"),
        ("http://172.16.0.1/", "RFC1918"),
        ("http://metadata.google.internal/x", "a metadata hostname"),
        ("http://anything.internal/x", "an internal hostname"),
        ("file:///etc/passwd", "a scheme that is not http"),
    ])
    def test_it_is_refused(self, url, why):
        reason = web_tools._refuse_reason(url)
        assert reason, f"{url} ({why}) was not refused"

    def test_the_refusal_explains_itself_and_offers_the_way_out(self):
        """A refusal a model cannot understand is one it retries."""
        text, is_error = web_tools.web_fetch("http://127.0.0.1/")
        assert is_error is True
        assert "deliberate restriction" in text
        assert "MYRIAXON_ALLOW_PRIVATE_FETCH=1" in text

    @pytest.mark.parametrize("url", [
        "https://example.com",
        "http://example.com/page",
        "https://api.github.com/repos",
    ])
    def test_public_hosts_are_still_allowed(self, url):
        """The fix must not break the tool's actual job."""
        assert web_tools._refuse_reason(url) == ""

    def test_a_loopback_listener_really_is_unreachable(self):
        """End to end, against a real server -- the form the hole was found in.

        A `_refuse_reason` unit test would pass even if `web_fetch` never called it, so this asserts the
        behaviour rather than the helper.
        """

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = b"INTERNAL-ONLY"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # noqa: ANN002
                pass

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        server = http.server.HTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        time.sleep(0.2)
        try:
            text, is_error = web_tools.web_fetch(f"http://127.0.0.1:{port}/")
        finally:
            server.shutdown()

        assert is_error is True, "web_fetch reached a loopback-only service"
        assert "INTERNAL-ONLY" not in text

    def test_the_opt_in_re_enables_local_fetching(self, monkeypatch):
        """An escape hatch, so the restriction is a policy and not a wall."""
        monkeypatch.setattr(web_tools, "_ALLOW_PRIVATE", {"value": True})
        assert web_tools._refuse_reason("http://127.0.0.1:9/") == ""


class TestTheWebhookSecretIsComparedConstantTime:
    """The second hole: `supplied != config.secret` leaks the secret's prefix by timing."""

    def _matches(self, supplied, expected):
        from myriaxon.dashboard.server import _secret_matches

        return _secret_matches(supplied, expected)

    def test_a_correct_secret_is_accepted(self):
        assert self._matches("s3cret", "s3cret") is True

    def test_a_wrong_secret_is_rejected(self):
        assert self._matches("s3cret", "s3cres") is False

    def test_a_prefix_is_rejected(self):
        """The specific shape a timing attack recovers one character at a time."""
        assert self._matches("s3c", "s3cret") is False

    def test_non_ascii_does_not_raise(self):
        """`compare_digest` refuses non-ASCII str, which would turn a bad secret into a 500."""
        assert self._matches("ünïcode", "ünïcode") is True
        assert self._matches("ünïcode", "other") is False

    def test_missing_values_are_handled(self):
        assert self._matches("", "") is True
        assert self._matches("", "x") is False
        assert self._matches("x", "") is False
        assert self._matches(None, "x") is False
        assert self._matches("x", None) is False

    def test_it_uses_compare_digest_not_equality(self):
        """Asserted on the EXECUTABLE CODE, not the source text.

        The first version inspected the raw source for `!=` and failed on a correct implementation, because the
        docstring explaining the timing bug quotes the old comparison. A string search cannot tell code from the
        prose describing it -- the same mistake made twice now in this repository, so it is worth naming.
        """
        import ast
        import inspect

        from myriaxon.dashboard import server

        function = ast.parse(inspect.getsource(server._secret_matches)).body[0]
        # Drop the docstring, then look only at what actually runs.
        body = [node for node in function.body
                if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
        code = ast.unparse(ast.Module(body=body, type_ignores=[]))

        assert "compare_digest" in code, "the comparison is not constant-time"
        assert "!=" not in code, f"an equality comparison survived in the code path: {code[:200]}"

    def test_the_handler_calls_the_safe_helper(self):
        """A safe helper nothing calls is not a fix."""
        import ast
        import inspect

        from myriaxon.dashboard import server

        # Walk the whole module's CODE for the call and for the old comparison, ignoring all prose.
        tree = ast.parse(inspect.getsource(server))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.body:
                if isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                    node.body = node.body[1:]
        code = ast.unparse(tree)

        assert "_secret_matches(" in code, "the handler does not call the constant-time helper"
        assert "supplied != config.secret" not in code, "the original comparison is still in the code path"
