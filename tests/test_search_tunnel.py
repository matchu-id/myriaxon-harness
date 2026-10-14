"""Search backends and the tunnel helper.

Both modules exist to make an honest limitation actionable, so what matters is that the *limitation*
is reported accurately and the fix is reachable: a search that says which backends it tried, and a
tunnel that refuses to open a public door without a secret.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon import constants, tunnel, web_tools  # noqa: E402


class TestSearchProviders(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mx-search-")
        self._saved_dir = constants.CONFIG_DIR
        constants.CONFIG_DIR = Path(self._tmp.name)
        self._saved_env = {
            p["env"]: os.environ.pop(p["env"], None) for p in web_tools.SEARCH_PROVIDERS
        }

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        constants.CONFIG_DIR = self._saved_dir
        self._tmp.cleanup()

    def test_no_key_means_no_provider(self) -> None:
        self.assertIsNone(web_tools.configured_search_provider())

    def test_the_key_free_backends_are_always_in_the_list(self) -> None:
        names = web_tools.search_backends()
        for expected in ("wikipedia", "duckduckgo", "marginalia"):
            self.assertIn(expected, names)

    def test_an_environment_key_is_found(self) -> None:
        os.environ["BRAVE_API_KEY"] = "test-key-123"
        found = web_tools.configured_search_provider()
        self.assertIsNotNone(found)
        provider, key = found
        self.assertEqual(provider["name"], "brave")
        self.assertEqual(key, "test-key-123")
        self.assertEqual(web_tools.search_backends()[0], "brave",
                         "a configured index must be tried before the scrapers")

    def test_a_stored_key_is_found_and_the_environment_wins(self) -> None:
        path = Path(self._tmp.name) / web_tools.SEARCH_CONFIG_FILE
        path.write_text(json.dumps({"tavily": "stored-key"}), encoding="utf-8")
        provider, key = web_tools.configured_search_provider()
        self.assertEqual((provider["name"], key), ("tavily", "stored-key"))

        os.environ["TAVILY_API_KEY"] = "env-key"
        provider, key = web_tools.configured_search_provider()
        self.assertEqual(key, "env-key", "an explicit environment variable should win")

    def test_a_corrupt_config_file_does_not_break_search(self) -> None:
        (Path(self._tmp.name) / web_tools.SEARCH_CONFIG_FILE).write_text(
            "{not json", encoding="utf-8")
        self.assertIsNone(web_tools.configured_search_provider())

    def test_a_search_without_a_key_says_so_in_the_result(self) -> None:
        """The note is the whole point: a caller who does not know its search is thin will
        over-trust an answer about anything current.

        This test REACHES THE NETWORK, so it has two honest outcomes and one dishonest one. The note is
        appended only to a SUCCESSFUL result, so it cannot be asserted when the backends are unreachable
        -- and asserting `is_error is False` in a sandbox tests the sandbox. So: if nothing was searched
        because the network is unavailable, that is skipped with the reason, and if a search DID run then
        the note must be there. The dishonest version, which this replaces, reported an environment
        limitation as a product failure.
        """
        text, is_error = web_tools.web_search("python programming language", limit=2)
        if is_error:
            self.skipTest(f"no search backend could run here, so the note cannot be asserted: {text[:120]}")
        self.assertIn("no search API key is configured", text)
        self.assertIn("BRAVE_API_KEY", text)

    def test_a_search_that_cannot_run_reports_the_reason(self) -> None:
        """The offline half, which IS deterministic: a failed search must say it failed rather than
        return an empty result set, and must name the missing package when that is the cause."""
        text, is_error = web_tools.web_search("python programming language", limit=2)
        if not is_error:
            self.skipTest("a backend answered, so the failure path is not under test here")
        self.assertIn("not a result of zero", text,
                      "a search failure must not read as 'the subject does not exist'")


class TestTunnelUrls(unittest.TestCase):
    def test_each_tool_url_shape_is_recognised(self) -> None:
        cases = {
            "https://tidy-pond-1234.trycloudflare.com": "cloudflared",
            "Forwarding https://abc-1-2-3.ngrok-free.app -> http://localhost:8765": "ngrok",
            "https://abc123.ngrok.io": "ngrok legacy",
            "https://quiet-fox.loca.lt": "localtunnel",
        }
        for line, label in cases.items():
            with self.subTest(tool=label):
                found = tunnel.find_url(line)
                self.assertTrue(found.startswith("https://"), line)

    def test_the_url_is_extracted_from_surrounding_noise(self) -> None:
        line = "2026-10-01 INF |  https://tidy-pond-1234.trycloudflare.com  |  press Ctrl+C"
        self.assertEqual(tunnel.find_url(line), "https://tidy-pond-1234.trycloudflare.com")

    def test_noise_without_a_url_returns_nothing(self) -> None:
        """A false positive here sends the user to configure a webhook that will never fire."""
        for line in ("starting tunnel...", "error: could not connect", "", "http://127.0.0.1:8765"):
            with self.subTest(line=line):
                self.assertEqual(tunnel.find_url(line), "")

    def test_the_webhook_url_appends_the_path_once(self) -> None:
        self.assertEqual(tunnel.webhook_url("https://x.trycloudflare.com"),
                         "https://x.trycloudflare.com/api/inbound")
        self.assertEqual(tunnel.webhook_url("https://x.trycloudflare.com/"),
                         "https://x.trycloudflare.com/api/inbound")
        self.assertEqual(tunnel.webhook_url(""), "")

    def test_the_install_hint_names_every_tunnel(self) -> None:
        lines = tunnel.install_hint(8765)
        text = "\n".join(lines)
        for spec in tunnel.TUNNELS:
            with self.subTest(tool=spec["name"]):
                self.assertIn(str(spec["name"]), text)
        self.assertIn("8765", text, "the hint must name the actual port")

    def test_the_commands_printed_are_the_commands_run(self) -> None:
        """The advice and the behaviour come from one table, so they cannot drift apart."""
        for spec in tunnel.TUNNELS:
            with self.subTest(tool=spec["name"]):
                argv = [str(a) for a in spec["argv"](8765)]
                self.assertIn("8765", " ".join(argv))
                self.assertIn(" ".join(argv), "\n".join(tunnel.install_hint(8765)))


class TestTunnelSafety(unittest.TestCase):
    def test_available_only_lists_installed_binaries(self) -> None:
        import shutil

        for spec in tunnel.available():
            with self.subTest(tool=spec["name"]):
                self.assertTrue(shutil.which(str(spec["binary"])))

    def test_starting_without_a_binary_fails_rather_than_hanging(self) -> None:
        """A tunnel that cannot start must report, not wait: this is what the timeout is for."""
        original = tunnel.available
        tunnel.available = lambda: []
        try:
            t = tunnel.Tunnel(8765, timeout=1)
            self.assertFalse(t.start())
            self.assertIn("no tunnel tool", t.error)
        finally:
            tunnel.available = original

    def test_a_tunnel_that_never_prints_a_url_gives_up(self) -> None:
        """ngrok without an authtoken runs and prints no URL; waiting forever is the failure mode."""
        import sys as _sys

        original_available = tunnel.available
        original_timeout = tunnel.Tunnel.__init__

        def fake_init(self, port, on_line=None, timeout=45.0):
            original_timeout(self, port, on_line=on_line, timeout=0.4)

        tunnel.available = lambda: [{
            "name": "sleepy", "binary": _sys.executable,
            "argv": lambda port: [_sys.executable, "-c", "import time; time.sleep(5)"],
            "install": "-", "note": "-",
        }]
        tunnel.Tunnel.__init__ = fake_init
        try:
            t = tunnel.Tunnel(8765)
            started = t.start()
            self.assertFalse(started, "no URL was printed, so start must report failure")
            self.assertTrue(t.error)
            t.stop()
        finally:
            tunnel.available = original_available
            tunnel.Tunnel.__init__ = original_timeout


if __name__ == "__main__":
    unittest.main(verbosity=2)
