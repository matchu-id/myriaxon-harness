"""Entry point with a fast path for invocations that need no agent.

Measured motivation: `myriaxon.cli` imports ~150 modules and costs several seconds to import on a
cold machine. `--help` and `--version` need none of that, and `--help` is the single most common
cold invocation for a new user -- making them wait for the whole agent stack to print usage text is
the worst possible first impression of the tool.

So: parse arguments first (cheap, constants only), answer the invocations that are pure text, and
only then import the heavy CLI. Every other path is unchanged, including the exit codes.

**This module is the console-script entry point** (`myriaxon = "myriaxon.__main__:main"` in
`pyproject.toml`), not `myriaxon.cli`. Measured against the built wheel in a clean virtualenv:
pointing the script at `cli:main` made `myriaxon --version` die with
`ModuleNotFoundError: No module named 'rich'`, because it bypassed this fast path and paid the full
agent import to print a version string. The first command a new user runs should not be the one most
likely to fail.
"""
import sys

from .argparse_cli import parse_args

_TEXT_ONLY_FLAGS = ("--version", "-h", "--help")

# What a missing hard dependency means, in one message.
#
# Without this, a partially-installed package produces a bare `ModuleNotFoundError` traceback naming an internal
# module the user has never heard of -- which reads as "this software is broken" rather than "finish the install".
# The three cases are distinguished because the remedy differs, and the optional extras are named because
# `pip install myriaxon` legitimately does not include them.
_INSTALL_HINT = """\
Myriaxon is installed but a required dependency is missing: {missing}

Install the dependencies and try again:

    pip install --upgrade myriaxon

For every provider, MCP servers, poster PNG output and the OS keychain:

    pip install --upgrade "myriaxon[all]"

Already inside a virtualenv? Check it is the one you installed into: {executable}
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Fast path: only when the invocation cannot do any real work. Checked before parsing so an
    # accidental heavy import cannot creep back in, and restricted to these exact flags so no real
    # command is ever answered by the cheap path.
    if argv and all(arg in _TEXT_ONLY_FLAGS for arg in argv):
        parse_args(argv)  # argparse prints help/version and exits
        return 0

    try:
        from .cli import main as cli_main
    except ModuleNotFoundError as exc:
        # `exc.name` is the module that could not be found, which is the useful part: a bare traceback names an
        # internal import several frames down.
        missing = exc.name or "an unknown module"
        sys.stderr.write(_INSTALL_HINT.format(missing=missing, executable=sys.executable))
        return 2

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
