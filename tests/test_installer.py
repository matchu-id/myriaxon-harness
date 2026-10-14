"""The installer must resolve the directory Python actually imports from.

This guards a defect that was measured, not imagined: inside a virtualenv, `site.getsitepackages()`
returned the virtualenv *root* (`.venv`) before `site-packages`. The installer wrote its editable
redirect there — somewhere the import system never reads — and then used the same wrong answer to
decide which copy to delete, so it removed the working install and reported success. The result was
an uninstall masquerading as a successful install, which is the worst possible outcome for the one
script whose job is to leave the user with a working tool.

The assertions below are about the properties that make the redirect work, not about the exact
path, so they hold on any platform and in any environment layout.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import install_dev  # noqa: E402


class TestSitePackagesResolution(unittest.TestCase):
    def test_resolves_to_site_packages_not_the_environment_root(self) -> None:
        resolved = install_dev.site_packages_dir()
        self.assertEqual(resolved.name, "site-packages", f"resolved to {resolved}")

    def test_resolved_directory_is_importable_from(self) -> None:
        """The strongest available check: put a uniquely named module there, remove it again, and
        confirm the interpreter really does resolve imports from this directory."""
        import importlib
        import uuid

        target = install_dev.site_packages_dir()
        probe_name = f"_myriaxon_probe_{uuid.uuid4().hex[:8]}"
        probe = target / f"{probe_name}.py"
        probe.write_text("VALUE = 42\n", encoding="utf-8")
        try:
            module = importlib.import_module(probe_name)
            self.assertEqual(module.VALUE, 42)
        finally:
            probe.unlink(missing_ok=True)
            sys.modules.pop(probe_name, None)

    def test_rejects_the_bare_environment_root(self) -> None:
        """`sys.prefix` is the wrong answer this guards against -- assert they differ."""
        resolved = install_dev.site_packages_dir()
        self.assertNotEqual(resolved.resolve(), Path(sys.prefix).resolve())

    def test_is_deterministic(self) -> None:
        self.assertEqual(install_dev.site_packages_dir(), install_dev.site_packages_dir())


class TestDependencyParsing(unittest.TestCase):
    """The fallback installs the real dependency set, read from pyproject.toml rather than a copy
    in the script -- so this asserts the parse actually finds them."""

    def test_base_requirements_are_found(self) -> None:
        requirements = install_dev.declared_requirements("")
        self.assertTrue(requirements, "no dependencies parsed from pyproject.toml")
        joined = " ".join(requirements)
        for expected in ("anthropic", "rich", "pygments", "prompt_toolkit"):
            with self.subTest(package=expected):
                self.assertIn(expected, joined)

    def test_httpx_is_an_extra_rather_than_a_base_requirement(self) -> None:
        """`httpx` used to be asserted here, justified by the comment that it was "the default transport for
        every OpenAI-compatible provider". That stopped being true when `direct_http_client.py` moved to
        `http.client`, so asserting it in the BASE set asserts a dependency the core path does not use -- and
        a base requirement nobody needs is what makes a small install fail on a package it never touches.

        The second half is the one that matters: it must still be reachable through the extras, because the
        web tools, the Ollama provider and the MCP registry search DO use it, and their error messages tell
        the user to install `myriaxon[web]`. An extra that does not exist would make that advice a dead end.
        """
        base = " ".join(install_dev.declared_requirements(""))
        self.assertNotIn("httpx", base, "httpx is not needed to run the harness any more")

        for extra in ("web", "mcp", "all"):
            with self.subTest(extra=extra):
                self.assertIn("httpx", " ".join(install_dev.declared_requirements(extra)))

    def test_extras_are_added(self) -> None:
        base = install_dev.declared_requirements("")
        all_extra = install_dev.declared_requirements("all")
        self.assertGreater(len(all_extra), len(base))
        joined = " ".join(all_extra)
        for expected in ("openai", "google-genai", "mcp", "keyring"):
            with self.subTest(package=expected):
                self.assertIn(expected, joined)

    def test_unknown_extra_is_harmless(self) -> None:
        self.assertEqual(
            install_dev.declared_requirements("does-not-exist"),
            install_dev.declared_requirements(""),
        )


class TestRedirectWriter(unittest.TestCase):
    def test_redirect_names_this_checkout_in_utf8(self) -> None:
        """The path is written as UTF-8 regardless of the console's locale encoding -- that is the
        entire reason the manual redirect exists."""
        written = install_dev.write_manual_editable()
        try:
            raw = written.read_bytes()
            self.assertEqual(written.name, "myriaxon.pth")
            self.assertEqual(written.parent.name, "site-packages")
            decoded = raw.decode("utf-8").strip()
            self.assertEqual(decoded, str(install_dev.REPO))
            # A non-ASCII checkout is the case this exists for; assert the bytes survive it.
            self.assertEqual(decoded.encode("utf-8"), raw.strip())
        finally:
            written.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
