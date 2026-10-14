"""`python -m myriaxon.bench <command>` -- the IQB-14 entry point.

Commands are split by cost, which is the distinction that matters operationally:

* `run`      -- spends money, calls the model, writes artifacts.
* `report`   -- free. Re-scores completed artifacts; can be re-run after changing a scoring rule.
* `compare`  -- free. Two scorecards side by side.
* `lint`     -- free. Validates task files without calling anything (CI-safe).
* `export`   -- free. Emits a promptfoo config for the adversarial subset.

Because `report` and `compare` never touch the network, a disputed score is settled by re-running
a pure function over stored traces, not by spending tokens and arguing about variance.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .parameters import STANDARD_ID, STANDARD_VERSION
from .report import compare_markdown, write_report
from .runner import EnvAbort, Runner, RunnerConfig, Target, build_agent  # noqa: F401 (build_agent re-exported for tests)
from .scoring import InstanceRecord, score_suite
from .tasks import KINDS, TaskError, load_suite


def _load_records(out_dir: Path, *, include_non_attributable: bool = True) -> list[InstanceRecord]:
    jsonl = out_dir / "instances.jsonl"
    records: list[InstanceRecord] = []
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    records.append(InstanceRecord.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError):
                    continue
    if not records:
        for path in sorted((out_dir / "instances").glob("*.json")):
            try:
                records.append(InstanceRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, KeyError):
                continue
    if not include_non_attributable:
        from .state import NON_ATTRIBUTABLE

        records = [r for r in records if r.state.status not in NON_ATTRIBUTABLE]
    return records


def _manifest_from(out_dir: Path) -> dict:
    path = out_dir / "manifest.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


# ---------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------
def cmd_lint(args) -> int:
    suite = load_suite(Path(args.suite), subset=args.only, kinds=args.kind)
    print(f"{STANDARD_ID}@{STANDARD_VERSION}: {len(suite)} task(s) valid")
    for spec in suite:
        print(
            f"  {spec.id:<32} kind={spec.kind:<16} attempts={len(spec.attempt_plan())} "
            f"assertions={len(spec.assertions)} steps<={spec.budget.max_steps} "
            f"${spec.budget.max_cost_usd}"
        )
        if spec.notes:
            print(f"      -> {spec.notes}")
    kinds_present = {s.kind for s in suite}
    missing = [k for k in KINDS if k not in kinds_present]
    if missing:
        print(f"\nNOTE: no task covers these evidence families: {', '.join(missing)}")
        print("      Parameters fed only by those families will report n/a and be excluded from AXS.")
    return 0


def cmd_run(args) -> int:
    import os

    # Same .env convention the rest of Myriaxon uses (and the existing eval scripts in tests/ relied
    # on): a key already present in the real environment is never overridden. Without this, running
    # the benchmark required hand-exporting keys, which is exactly the kind of friction that leads
    # to runs being reproduced with different credentials than the ones they were recorded with.
    from ..dotenv_support import load_dotenv_into_environ

    load_dotenv_into_environ()

    suite = load_suite(Path(args.suite), subset=args.only, kinds=args.kind)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    key = args.api_key or os.environ.get(args.api_key_env, "")
    target = Target(
        name=args.label or f"{args.provider}:{args.model}",
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        api_key=key,
        api_key_env=args.api_key_env,
        temperature=args.temperature,
        seed=args.seed,
        max_tokens=args.max_tokens,
        effort=args.effort,
        mode=args.mode,
    )

    judge_fn = None
    judge_spec = None
    if args.judge_model:
        from .judge import JudgeSpec, build_judge, default_endpoint, default_key_env

        judge_provider = args.judge_provider
        judge_spec = JudgeSpec(
            provider=judge_provider,
            model=args.judge_model,
            base_url=args.judge_base_url or default_endpoint(judge_provider),
            api_key_env=args.judge_api_key_env or default_key_env(judge_provider),
            api_key=os.environ.get(args.judge_api_key_env or default_key_env(judge_provider), ""),
            temperature=args.judge_temperature,
            mode=args.judge_mode,
            timeout_s=args.judge_timeout,
        )
        try:
            judge_fn = build_judge(judge_spec)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(
            f"judge: {judge_spec.provider}:{judge_spec.model} via {judge_spec.to_dict()['implementation']} "
            f"(temperature={judge_spec.temperature})"
        )

    if not target.resolve_key() and not args.dry_run:
        print(
            f"error: no API key for {args.provider} (checked --api-key and {args.api_key_env})",
            file=sys.stderr,
        )
        return 2

    runner = Runner(
        RunnerConfig(
            suite=suite,
            target=target,
            out_dir=out_dir,
            attempts=args.attempts,
            judge_client=judge_fn,
            judge_model=args.judge_model,
            keep_workspaces=args.keep_workspaces,
            resume=not args.no_resume,
            dry_run=args.dry_run,
            workspace_root=Path(args.workspace_root) if args.workspace_root else (out_dir / "scratch"),
        )
    )
    aborted: str | None = None
    try:
        records = runner.run()
    except EnvAbort as exc:
        # Not a crash: the environment is failing, so stop early and still report on what completed.
        # The alternative (grinding through every remaining task) produces a run full of
        # non-attributable instances and no usable evidence.
        aborted = str(exc)
        records = runner.records
        print(f"\nRUN ABORTED: {exc}", file=sys.stderr)
    manifest = runner.manifest(suite)
    manifest["aborted"] = aborted
    if judge_spec is not None:
        manifest["judge"] = judge_spec.to_dict()
        manifest["judge_calls"] = getattr(judge_fn, "calls", None)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    if args.dry_run:
        print(f"\ndry run: {len(suite)} task(s) planned, no calls made")
        return 0
    if not records:
        print("no instances recorded", file=sys.stderr)
        return 1

    summary = score_suite(records, label=target.name, manifest=manifest)
    json_path, md_path = write_report(summary, out_dir)
    axs = "n/a" if summary.axs is None else f"{summary.axs:.1f}"
    print(f"\nAXS {axs}/100 (coverage {summary.coverage:.0%})")
    print(f"scorecard: {md_path}")
    print(f"json:      {json_path}")
    return 0


def cmd_report(args) -> int:
    out_dir = Path(args.out)
    records = _load_records(out_dir)
    if not records:
        print(f"error: no instance artifacts under {out_dir}", file=sys.stderr)
        return 2
    manifest = _manifest_from(out_dir)
    label = args.label or manifest.get("target", {}).get("name") or out_dir.name
    summary = score_suite(records, label=label, manifest=manifest)
    json_path, md_path = write_report(summary, out_dir, stem=args.stem)
    axs = "n/a" if summary.axs is None else f"{summary.axs:.1f}"
    print(f"AXS {axs}/100 (coverage {summary.coverage:.0%}) over {len(records)} instances")
    print(f"scorecard: {md_path}")
    print(f"json:      {json_path}")
    if args.print:
        print()
        print(md_path.read_text(encoding="utf-8"))
    return 0


def cmd_compare(args) -> int:
    base_dir = Path(args.a)
    other_dir = Path(args.b)
    base_dir = base_dir if base_dir.is_dir() else base_dir.parent
    other_dir = other_dir if other_dir.is_dir() else other_dir.parent

    records_a = _load_records(base_dir)
    records_b = _load_records(other_dir)
    if not records_a or not records_b:
        print("error: both sides need instance artifacts", file=sys.stderr)
        return 2
    manifest_a = _manifest_from(base_dir)
    manifest_b = _manifest_from(other_dir)
    base = score_suite(
        records_a, label=args.label_a or manifest_a.get("target", {}).get("name") or "A", manifest=manifest_a
    )
    other = score_suite(
        records_b, label=args.label_b or manifest_b.get("target", {}).get("name") or "B", manifest=manifest_b
    )
    text = compare_markdown(base, other)
    print(text)
    if args.save:
        Path(args.save).write_text(text, encoding="utf-8")
        print(f"\nsaved: {args.save}")
    return 0


def cmd_export(args) -> int:
    from .exporters import export_promptfoo

    suite = load_suite(Path(args.suite), subset=args.only, kinds=args.kind)
    info = export_promptfoo(suite, Path(args.out), provider_script=args.provider_script)
    print(f"exported {info['exported_tests']} test(s) -> {info['path']}")
    if info["unmapped_assertions"]:
        print(f"NOTE: {len(info['unmapped_assertions'])} assertion(s) have no promptfoo equivalent:")
        for row in info["unmapped_assertions"][:10]:
            print(f"  {row['task']}: {row['type']}")
    return 0


# ---------------------------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m myriaxon.bench",
        description=f"{STANDARD_ID} v{STANDARD_VERSION} -- agentic AI evaluation harness.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_suite_opts(p):
        # Resolved against the installed package, not the current working directory. The shipped
        # IQB-14 suite used to be a bare relative string ("bench/tasks"), so a benchmark run only
        # worked from a source checkout *and* only from the repository root -- an installed copy,
        # or a run from any other directory, silently found an empty suite and reported zero tasks
        # rather than an error.
        p.add_argument(
            "--suite",
            default=str(Path(__file__).resolve().parent / "tasks"),
            help="Directory of task folders (default: the IQB-14 suite shipped with the package)",
        )
        p.add_argument("--only", nargs="*", default=None, help="Restrict to these task ids")
        p.add_argument("--kind", nargs="*", default=None, choices=list(KINDS), help="Restrict to these evidence families")

    p_lint = sub.add_parser("lint", help="Validate task files without making any API call")
    add_suite_opts(p_lint)
    p_lint.set_defaults(func=cmd_lint)

    p_run = sub.add_parser("run", help="Run a suite against one target (costs money)")
    add_suite_opts(p_run)
    p_run.add_argument("--provider", required=True, choices=["deepseek", "openrouter", "openai_compat", "gemini", "anthropic"])
    p_run.add_argument("--model", required=True)
    p_run.add_argument("--base-url", default="")
    p_run.add_argument("--api-key", default="")
    p_run.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    p_run.add_argument("--label", default="")
    p_run.add_argument("--temperature", type=float, default=0.0)
    p_run.add_argument("--seed", type=int, default=None)
    p_run.add_argument("--max-tokens", type=int, default=None)
    p_run.add_argument("--effort", default=None)
    p_run.add_argument("--mode", default="auto_edit")
    p_run.add_argument("--attempts", type=int, default=None, help="Override each task's repeat count")
    p_run.add_argument("--out", default="bench-out", help="Artifact directory")
    p_run.add_argument("--judge-model", default=None, help="Model for llm-rubric assertions (none = keyword rubric)")
    p_run.add_argument(
        "--judge-provider",
        default="deepseek",
        choices=["deepseek", "openrouter", "openai_compat", "gemini"],
        help="Provider serving the judge model (default: deepseek)",
    )
    p_run.add_argument(
        "--judge-mode",
        default="agent",
        choices=["agent", "api"],
        help="agent (default) = run Myriaxon itself as the judge; api = a bare chat-completion call",
    )
    p_run.add_argument(
        "--judge-base-url",
        default="",
        help="Judge endpoint (default: the provider's known endpoint)",
    )
    p_run.add_argument(
        "--judge-api-key-env",
        default="",
        help="Env var holding the judge key (default: the provider's standard env var)",
    )
    p_run.add_argument("--judge-temperature", type=float, default=0.0)
    p_run.add_argument("--judge-timeout", type=float, default=180.0)
    p_run.add_argument("--keep-workspaces", action="store_true", help="Copy each post-run workspace into the artifact dir")
    p_run.add_argument(
        "--workspace-root",
        default=None,
        help="Where per-instance scratch workspaces are created (default: <out>/scratch)",
    )
    p_run.add_argument("--no-resume", action="store_true", help="Ignore already-recorded instances")
    p_run.add_argument("--dry-run", action="store_true", help="Plan the run without calling the model")
    p_run.set_defaults(func=cmd_run)

    p_report = sub.add_parser("report", help="Re-score an artifact directory (free, no API calls)")
    p_report.add_argument("--out", default="bench-out")
    p_report.add_argument("--label", default="")
    p_report.add_argument("--stem", default=None)
    p_report.add_argument("--print", action="store_true", help="Print the markdown scorecard to stdout")
    p_report.set_defaults(func=cmd_report)

    p_cmp = sub.add_parser("compare", help="Compare two artifact directories (free)")
    p_cmp.add_argument("a")
    p_cmp.add_argument("b")
    p_cmp.add_argument("--label-a", default="")
    p_cmp.add_argument("--label-b", default="")
    p_cmp.add_argument("--save", default="")
    p_cmp.set_defaults(func=cmd_compare)

    p_export = sub.add_parser("export", help="Export the adversarial subset as a promptfoo config")
    add_suite_opts(p_export)
    p_export.add_argument("--out", default="bench-out/promptfooconfig.generated.yaml".replace(".yaml", ".json"))
    p_export.add_argument("--provider-script", default="redteam/myriaxon_provider.py")
    p_export.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except TaskError as exc:
        print(f"task error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
