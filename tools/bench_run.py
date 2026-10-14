"""Run IQB-14 against the configured provider, without putting the key on a command line.

The bench CLI takes `--api-key`, which would put a live secret into the process table and into any
shell history that recorded the invocation. This loads the key from the same config the agent uses and
drives the runner directly.

Usage:
    python tools/bench_run.py --label flash            # every task
    python tools/bench_run.py --only p01 p06 --label smoke
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="live")
    parser.add_argument("--only", nargs="*", default=[], help="task id prefixes to run")
    parser.add_argument("--judge-mode", default="api", choices=["api", "agent"])
    parser.add_argument("--attempts", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--out", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from myriaxon.bench.runner import Runner, RunnerConfig, Target
    from myriaxon.bench.tasks import load_suite
    from myriaxon.config import load_config

    config = load_config()
    key = config.api_key or ""
    if not key:
        print("no API key in the agent config; run `myriaxon auth` first")
        return 2

    suite = load_suite(REPO / "myriaxon" / "bench" / "tasks")
    if args.only:
        suite = [t for t in suite if any(t.id.startswith(p) for p in args.only)]
    if not suite:
        print("no tasks selected")
        return 1

    out_dir = Path(args.out) if args.out else REPO / "bench-out" / args.label
    target = Target(
        name=args.label,
        provider="openai_compat",
        model=config.model,
        base_url=config.base_url,
        api_key=key,
        temperature=args.temperature,
    )

    # The judge runs on the same provider unless told otherwise. `api` mode is one completion per
    # graded parameter; `agent` mode would run a full agent turn for each, costing far more for a
    # rubric that only needs a verdict.
    judge_client = None
    judge_model = config.model
    if args.judge_mode == "api":
        from myriaxon.bench.judge import JudgeSpec, build_judge

        spec = JudgeSpec(
            provider="openai_compat", model=config.model, base_url=config.base_url,
            api_key=key, temperature=0.0, mode="api",
        )
        judge_client = build_judge(spec)
        if judge_client is None:
            print("could not build a judge client; graded parameters will report as unjudged")
    else:
        # Agent mode grades by running the harness itself, which the runner drives through the
        # `agent` judge -- it needs no client object, only the model name.
        from myriaxon.bench.judge import JudgeSpec, build_judge

        judge_client = build_judge(JudgeSpec(
            provider="openai_compat", model=config.model, base_url=config.base_url,
            api_key=key, temperature=0.0, mode="agent",
        ))

    runner_config = RunnerConfig(
        suite=suite,
        target=target,
        out_dir=out_dir,
        attempts=args.attempts,
        judge_client=judge_client,
        judge_model=judge_model,
        dry_run=args.dry_run,
        progress=lambda message: print(f"  {message}", flush=True),
    )

    print(f"IQB-14  target={config.model}  provider={config.provider}->openai_compat")
    print(f"        {len(suite)} task(s), judge={args.judge_mode} on {config.model}")
    print(f"        artifacts -> {out_dir}")
    print()

    started = time.time()
    summary = Runner(runner_config).run()
    elapsed = time.time() - started

    print()
    print(f"finished in {elapsed / 60:.1f} min")
    for key_name in ("instances", "passed", "failed", "errors", "artifacts"):
        value = summary.get(key_name) if isinstance(summary, dict) else None
        if value is not None:
            print(f"  {key_name}: {value}")
    if isinstance(summary, dict) and summary.get("axs") is not None:
        print(f"  AXS: {summary['axs']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
