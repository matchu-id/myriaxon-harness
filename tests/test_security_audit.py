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


class TestOneExecutionGate:
    """Every path that runs a shell asks the SAME question.

    Two failures in this repository had one shape: a control that held on the path it was written for and not on
    another path doing the same thing. `run_bash` refused a confined shell without a sandbox; a Locus condition,
    which also runs a shell, consulted none of that. The confinement gate guarded file tools; `member_workspace`
    returned a path inside the directory it trusted.

    Both were a decision duplicated rather than shared. `shell_may_run` is now the only answer to "may a shell
    run here", and these tests hold that in place.
    """

    def _gate(self, **kwargs):
        from myriaxon.tools import shell_may_run

        return shell_may_run(**kwargs)

    def test_a_session_may_run_a_shell(self):
        """Unconfined: a person is present, and `confirm_action` owns the prompt."""
        allowed, reason = self._gate(confined=None)
        assert allowed is True and reason == ""

    def test_a_confined_context_may_not_without_a_sandbox(self):
        allowed, reason = self._gate(confined="/some/workspace", sandbox_requested=False)
        assert allowed is False
        assert "cannot be confined" in reason

    def test_a_confined_context_may_not_even_when_a_sandbox_was_requested_but_is_absent(self):
        """`sandbox_requested` is a request, not a guarantee -- availability decides."""
        allowed, _ = self._gate(confined="/some/workspace", sandbox_requested=True)
        # This machine has no Docker or Windows Sandbox, so the request cannot be honoured.
        from myriaxon import tools

        if not (tools.docker_available() or tools.windows_sandbox_available()):
            assert allowed is False
        else:
            assert allowed is True

    def test_run_bash_uses_the_shared_gate(self):
        """Asserted on code, so a re-derived copy cannot creep back in."""
        import ast
        import inspect

        from myriaxon import tools

        function = ast.parse(inspect.getsource(tools.confirm_action)).body[0]
        body = [n for n in function.body
                if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        code = ast.unparse(ast.Module(body=body, type_ignores=[]))
        assert "shell_may_run" in code, "confirm_action re-derives the shell decision"

    def test_conditions_use_the_shared_gate(self):
        """The path that bypassed every control, now asking the same question."""
        import ast
        import inspect

        from myriaxon import conditions

        source = inspect.getsource(conditions)
        assert "shell_may_run" in source, "conditions run a shell without asking the shared gate"
        # And the superseded mechanism is gone, so there is one policy rather than two.
        assert "MYRIAXON_CONDITIONS_ALLOW_SHELL" not in ast.unparse(ast.parse(source)), (
            "a second, independent shell policy is back"
        )

    def test_a_command_condition_is_refused_and_says_why(self, tmp_path):
        """End to end: the exploit from the audit must not write the file."""
        from myriaxon import conditions

        marker = tmp_path / "pwned.txt"
        condition = conditions.Condition(
            kind="command", command=f"echo pwned > {marker}", description="audit probe")
        result = condition.evaluate()

        assert marker.exists() is False, "a condition executed a shell command"
        assert result.error is True, "the refusal must be reported, not silent"
        assert "cannot be confined" in result.detail

    def test_the_refusal_is_reported_rather_than_swallowed(self, tmp_path):
        """A monitor that quietly stopped working is worse than one asking for permission.

        `holds=True` with `error=True` is deliberate: the condition does not fire, and the failure is visible
        rather than reading as "the thing you were watching is fine".
        """
        from myriaxon import conditions

        condition = conditions.Condition(
            kind="command", command="echo hi", description="probe")
        result = condition.evaluate()
        assert result.holds is True and result.error is True


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
