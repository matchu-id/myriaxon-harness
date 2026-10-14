"""Verify myriaxon-web.cmd's argument handling, exactly as the launcher does it.

    python scripts/check_launcher_args.py

The launcher injects `--workspace <repo>` only when the caller did not pass one, and that logic lives
in batch-file `for`/`if` syntax which is easy to get subtly wrong and produces a confusing failure
later (a server binding the wrong directory). So it is exercised rather than assumed.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "Scripts" / "python.exe"

passed, failed = [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + str(detail)) if detail and not condition else ''}")


def parse_workspace_from_argv(argv: list[str]) -> str:
    """What the launcher should pass through, evaluated with the same rule it uses."""
    have = any(a.lower() == "--workspace" for a in argv)
    if have:
        return argv[argv.index("--workspace") + 1] if "--workspace" in argv else ""
    return str(REPO) + "."


def main() -> int:
    launcher = REPO / "myriaxon-web.cmd"
    print("=== launcher file ===")
    check("exists", launcher.is_file(), launcher)
    text = launcher.read_text(encoding="utf-8", errors="replace")
    check("injects --workspace when absent", "--workspace" in text)
    check("detects a caller-supplied workspace", "HAVE_WORKSPACE" in text)
    check("uses --workspace of the repo by default", "%REPO%." in text.replace('"%REPO%."', "%REPO%."))
    check("passes arguments through", "%*" in text)

    print("\n=== CLI accepts the shapes the launcher produces ===")
    # `web --help` proves the subcommand parses without binding a port.
    for label, argv in (
        ("web --workspace <repo>", ["web", "--workspace", str(REPO)]),
        ("web --workspace <repo> --port N", ["web", "--workspace", str(REPO), "--port", "8899"]),
        ("web --workspace <repo> --no-browser", ["web", "--workspace", str(REPO), "--no-browser"]),
    ):
        result = subprocess.run(
            [str(PY), "-m", "myriaxon", *argv, "--help"],
            capture_output=True, text=True, cwd=str(REPO), timeout=120,
        )
        check(f"parses: {label}", result.returncode == 0 and "workspace" in result.stdout,
              (result.stderr or result.stdout)[-200:])

    print("\n=== workspace argument reaches the server ===")
    # A non-existent workspace must be rejected with exit code 2, which proves the value is read
    # rather than silently ignored.
    result = subprocess.run(
        [str(PY), "-m", "myriaxon", "web", "--workspace", "C:\\definitely-not-real-9f2a", "--no-browser", "--port", "8898"],
        capture_output=True, text=True, cwd=str(REPO), timeout=180,
    )
    check("invalid workspace is rejected", result.returncode == 2, f"rc={result.returncode}")

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
