"""Run Myriaxon on Terminal-Bench through Harbor, keeping every key off the command line.

Both secrets this needs live in files rather than arguments:

  * the model key, from the agent's own config (`myriaxon/config.json`) -- the key Myriaxon itself
    uses, so the container and the host are guaranteed to be talking to the same provider;
  * the Daytona key, from `.env.daytona`.

Passing either as a flag would put a live credential in the process table and in the shell history of
whatever recorded the invocation. This reads them in-process and hands them to Harbor through the
environment, which is also how `--ae` reaches the container.

Usage:
    .venv-harbor\\Scripts\\python.exe tools/tb_run.py --tasks mailman regex-log
    .venv-harbor\\Scripts\\python.exe tools/tb_run.py --all --n-concurrent 3
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", nargs="*", default=[],
                        help="task names to run; omit with --all for the whole suite")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--n-concurrent", "-n", type=int, default=1)
    parser.add_argument("--env", "-e", default="daytona", choices=["daytona", "docker"])
    parser.add_argument("--job-name", default="")
    parser.add_argument("--smoke", action="store_true",
                        help="one trivial task, to prove the pipeline before spending on a real one")
    parser.add_argument("--install-only", action="store_true",
                        help="run only the adapter's install step, then stop. Validates the wheel "
                             "build, upload and in-container install without paying for a model turn")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO))

    # --- model credentials ---------------------------------------------------------------
    from myriaxon.config import load_config

    config = load_config()
    if not config.api_key:
        print("no model API key in the Myriaxon config; run `myriaxon auth` first", file=sys.stderr)
        return 2
    if not config.base_url:
        print("no base_url in the Myriaxon config; the container needs an endpoint", file=sys.stderr)
        return 2

    # --- daytona credentials -------------------------------------------------------------
    daytona = load_env_file(REPO / ".env.daytona")
    if args.env == "daytona" and not daytona.get("DAYTONA_API_KEY"):
        print("no DAYTONA_API_KEY in .env.daytona; store it with tools/store_key.py", file=sys.stderr)
        return 2

    # Harbor reads agent env from the process environment for `--ae`-style values, and the adapter
    # reads the model key from here too.
    os.environ["MYRIAXON_API_KEY"] = config.api_key
    os.environ["OPENAI_API_KEY"] = config.api_key
    if daytona:
        os.environ.update(daytona)

    # --- harbor's own path setup ----------------------------------------------------------
    # The same redirections tools/harbor_main.py applies, by import rather than by copy.
    import importlib.util

    spec = importlib.util.spec_from_file_location("harbor_main", REPO / "tools" / "harbor_main.py")
    harbor_main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harbor_main)
    harbor_main._patch_resetperms()
    harbor_main._patch_tempdir_cleanup()
    harbor_main._redirect_home(REPO)
    tmp = REPO / ".uv" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    for var in ("TEMP", "TMP", "TMPDIR"):
        os.environ[var] = str(tmp)

    tasks = args.tasks
    if args.smoke:
        # A task with a short agent budget: the pipeline can be proven for cents instead of dollars.
        tasks = ["regex-log"]
    if not tasks and not args.all:
        print("nothing selected: pass --tasks <name>... or --all", file=sys.stderr)
        return 2

    provider = config.provider if config.provider in ("openai_compat",) else "openai_compat"

    argv = [
        "run",
        "-d", "terminal-bench@2.0",
        "-a", "tools.harbor_myriaxon:MyriaxonAgent",
        "-m", config.model,
        "--ak", f"base_url={config.base_url}",
        "--ak", f"provider={provider}",
        "-e", args.env,
        "-n", str(args.n_concurrent),
        "-y",
    ]
    if args.env == "daytona":
        argv += ["--env-file", str(REPO / ".env.daytona")]
    if args.job_name:
        argv += ["--job-name", args.job_name]
    if args.install_only:
        argv += ["--install-only"]
    if args.all:
        pass
    else:
        for name in tasks:
            argv += ["-i", name]

    print(f"  model   : {config.model} via {config.base_url}")
    print(f"  backend : {args.env}")
    print(f"  tasks   : {', '.join(tasks) if tasks else 'all of terminal-bench@2.0'}")
    print(f"  agent   : MyriaxonAgent (tools/harbor_myriaxon.py)")
    print()

    sys.argv = ["harbor"] + argv
    from harbor.cli.main import app  # type: ignore

    app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
