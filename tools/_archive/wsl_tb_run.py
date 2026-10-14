"""Run the Terminal-Bench suite inside WSL, detached, with a log to poll.

Why this exists instead of reusing tools/tb_run.py directly
----------------------------------------------------------
`tb_run.py` is correct but it was written for the Windows side, and on Windows Harbor cannot reach a
Docker daemon (measured: loopback forwarding is intermittent, the WSL interface times out behind the
Hyper-V firewall). Inside WSL none of that is true -- the daemon is on a local Unix socket.

The other reason is operational. `wsl.exe` cannot be waited on from the agent's session: a call with a
30-second cap on `wsl.exe -d Ubuntu -- echo hello` timed out, while the same call from an ordinary
shell returns immediately. So the benchmark cannot be run in the foreground here. This script is
therefore launched detached inside WSL, writes everything it does to a log file, and is polled. A
detached run also survives the agent session ending, which a foreground one would not.

Usage (from the Windows side):

    wsl.exe -d Ubuntu -- bash <repo>/tools/wsl_tb_run.sh --tasks mailman regex-log
    wsl.exe -d Ubuntu -- bash <repo>/tools/wsl_tb_run.sh --all --n-concurrent 3
    wsl.exe -d Ubuntu -- bash <repo>/tools/wsl_tb_run.sh --status
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", nargs="*", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--n-concurrent", "-n", type=int, default=1)
    parser.add_argument("--install-only", action="store_true")
    parser.add_argument("--job-name", default="")
    args = parser.parse_args()

    if not args.tasks and not args.all:
        print("nothing selected: pass --tasks <name>... or --all", file=sys.stderr)
        return 2

    # Harbor resolves its cache and credentials from ~, and this runs as root inside WSL, where $HOME
    # is /root -- outside the repo and lost when the distro restarts. Pinning it under the repo keeps
    # runs reproducible and inspectable from Windows.
    home = REPO / ".uv" / "home-linux"
    (home / ".cache" / "harbor").mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(home)

    tmp = REPO / ".uv" / "tmp-linux"
    tmp.mkdir(parents=True, exist_ok=True)
    for var in ("TEMP", "TMP", "TMPDIR"):
        os.environ[var] = str(tmp)

    # The model key comes from Myriaxon's own config, so the container and the host agree on the
    # provider. Read here rather than passed as a flag: a real credential does not belong in a command
    # line, where it lands in the process table and in any transcript of the invocation.
    # Myriaxon's config lives under `MYRIAXON_CONFIG_DIR`, which is set in the agent's Windows
    # environment but does not cross into WSL. Without this, `load_config()` looks somewhere else,
    # finds nothing, and returns None -- which surfaced as
    # `AttributeError: 'NoneType' object has no attribute 'api_key'` rather than as a missing config.
    os.environ.setdefault("MYRIAXON_CONFIG_DIR", str(REPO / ".myriaxon"))

    from myriaxon.config import load_config

    config = load_config()
    if config is None:
        print(
            f"no config found at {REPO / '.myriaxon' / 'config.json'}; "
            f"set MYRIAXON_CONFIG_DIR or run `myriaxon auth`",
            file=sys.stderr,
        )
        return 2
    if not config.api_key:
        print("no model API key in the Myriaxon config", file=sys.stderr)
        return 2
    os.environ["MYRIAXON_API_KEY"] = config.api_key
    os.environ["OPENAI_API_KEY"] = config.api_key

    # Inside a container, the host's loopback is not the container's. Docker Desktop publishes the
    # host under this name, and the task containers reach Ollama through it.
    os.environ.setdefault("OLLAMA_HOST", "http://host.docker.internal:11434")

    argv = [
        str(REPO / ".venv-harbor-linux" / "bin" / "harbor"),
        "run",
        "-d", "terminal-bench@2.0",
        "-a", "tools.harbor_myriaxon:MyriaxonAgent",
        "-m", config.model,
        "--ak", f"base_url={config.base_url}",
        "--ak", f"provider=openai_compat",
        "-e", "docker",
        "-n", str(args.n_concurrent),
        "-y",
    ]
    if args.job_name:
        argv += ["--job-name", args.job_name]
    if args.install_only:
        argv += ["--install-only"]
    if not args.all:
        for name in args.tasks:
            argv += ["-i", name]

    print(f"  model   : {config.model} via {config.base_url}")
    print(f"  backend : docker (native, inside WSL)")
    print(f"  tasks   : {', '.join(args.tasks) if args.tasks else 'all of terminal-bench@2.0'}")
    print()
    sys.stdout.flush()

    os.execv(argv[0], argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
