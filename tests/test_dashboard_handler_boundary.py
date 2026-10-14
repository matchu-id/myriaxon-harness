"""Two bugs the user found by clicking, made impossible to reintroduce silently.

Both were the same mistake: code inside `Handler` reaching for application state on `self`. The
handler is constructed per request and owns nothing; `DashboardApp` owns the config, the sessions and
the settings. The confusion is easy because both objects are called `self` at their own definition
site, so `self.config` looks right in both files and is only correct in one of them.

Measured cost of not having this test: the vendor picker failed TWICE in a row for the same reason --
first `self.config`, which the user reported as "'BoundHandler' object has no attribute 'config'", and
then `self.config_payload()` on the next attempt. Each round trip was a manual restart of the
dashboard. A check that compares the two classes catches the whole family at once instead of one
button at a time.
"""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon.dashboard import server  # noqa: E402


def _handler_code_lines() -> list[tuple[int, str]]:
    """The handler's source with comments stripped.

    Comments are excluded because a comment can contain the very text this test forbids -- the first
    version of this check reported the comment explaining the fix as a fresh instance of the bug.
    """
    out: list[tuple[int, str]] = []
    for number, line in enumerate(inspect.getsource(server.Handler).splitlines(), start=1):
        code = line.split("#", 1)[0]
        if code.strip():
            out.append((number, code))
    return out


def test_the_handler_never_reads_application_state_on_self():
    """`self.<app member>` inside Handler is the bug. Every such access is one `.app` away."""
    app_members = {name for name in dir(server.DashboardApp) if not name.startswith("__")}
    handler_members = {name for name in dir(server.Handler) if not name.startswith("__")}
    base_members = {name for name in dir(server.BaseHTTPRequestHandler) if not name.startswith("__")}

    # Instance attributes are not visible on the class, so they are harvested from where the app
    # assigns them -- `self.config` is an attribute, and it was the FIRST of the two failures.
    assigned = set(
        re.findall(r"self\.([a-z_][a-z0-9_]*)\s*=", inspect.getsource(server.DashboardApp.__init__))
    )
    app_only = (app_members - handler_members - base_members) | (assigned - handler_members)

    offenders = [
        (number, name, line.strip())
        for number, line in _handler_code_lines()
        for name in app_only
        if re.search(rf"self\.{re.escape(name)}\b", line)
    ]
    assert offenders == [], (
        "Handler code reaching for application state on `self`; these belong to DashboardApp, one "
        f"`.app` away: {offenders}"
    )


def test_every_self_call_in_the_handler_resolves():
    """A `self.foo()` that no class defines is an AttributeError the moment that path is taken."""
    handler_members = {name for name in dir(server.Handler) if not name.startswith("__")}
    missing = [
        (number, name, line.strip())
        for number, line in _handler_code_lines()
        for name in re.findall(r"self\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", line)
        if name not in handler_members
    ]
    assert missing == [], f"Handler calls methods it does not have: {missing}"
