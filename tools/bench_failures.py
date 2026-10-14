"""Read the benchmark's own failure evidence, so a fix targets the real cause.

The scorecard says which parameter failed; the instance record says why. This prints the failing
assertion's evidence, the task prompt, and the relevant slice of the trajectory.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def load(out_dir: Path) -> list[dict]:
    """One record per instance. They live under `instances/`, not at the artifact root -- the root
    holds the scorecard and a JSONL rollup, so globbing it found nothing."""
    records = []
    for path in sorted((out_dir / "instances").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and "assertions" in data:
            records.append(data)
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="bench-out/baseline")
    parser.add_argument("--task", default="", help="only this task id prefix")
    parser.add_argument("--trace", action="store_true", help="show the trajectory")
    parser.add_argument("--prompt", action="store_true", help="show the task prompt")
    args = parser.parse_args()

    out_dir = REPO / args.out
    records = load(out_dir)
    if not records:
        print(f"no instance records under {out_dir}")
        return 1

    for record in records:
        task = record.get("task_id") or record.get("task") or "?"
        if args.task and not task.startswith(args.task):
            continue
        # The field is `passed`, not `ok`. Defaulting the wrong name to True meant failing
        # non-critical assertions were silently skipped -- a checker that hides the thing it exists to
        # show.
        failed = [a for a in (record.get("assertions") or []) if not a.get("passed", True)]
        state = record.get("state") or {}
        status = state.get("status", "?")
        if not failed and record.get("passed"):
            continue

        print("=" * 100)
        print(f"{task}  variant={record.get('variant','?')}  status={status}  "
              f"passed={record.get('passed')}  wall={state.get('wall_clock_s')}s  "
              f"steps={state.get('steps')}")
        print("=" * 100)

        prompt = (record.get("spec") or {}).get("prompt") or ""
        if prompt and args.prompt:
            print("\n--- task prompt ---")
            print(prompt[:1200])

        if state.get("detail"):
            print(f"\nDETAIL: {state['detail']}")
        if record.get("error"):
            print(f"\nERROR: {record['error']}")

        for assertion in failed:
            print(f"\n--- FAILED ASSERTION: {assertion.get('name','?')} "
                  f"[{assertion.get('parameter','?')}] ---")
            detail = assertion.get("evidence")
            if detail is None:
                detail = {k: v for k, v in assertion.items()
                          if k not in ("name", "ok", "parameter")}
            if isinstance(detail, (dict, list)):
                print(json.dumps(detail, indent=2, default=str)[:2500])
            else:
                print(str(detail)[:2500])

        if args.trace:
            steps = (record.get("state") or {}).get("trace") or []
            print(f"\n--- trajectory ({len(steps)} step(s)) ---")
            for index, step in enumerate(steps, 1):
                tool = step.get("tool") or step.get("name") or "?"
                args_in = step.get("args") or step.get("input") or {}
                result = str(step.get("result") or "")[:200].replace("\n", " ")
                print(f"  {index:>2}. {tool}  {json.dumps(args_in, default=str)[:220]}")
                if result:
                    print(f"      -> {result}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
