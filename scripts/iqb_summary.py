"""Print a compact IQB-14 result summary from a scorecard JSON (no directory recursion, so it
works even when a run left unwritable scratch directories behind).

    python scripts/iqb_summary.py bench-out/<run>/<label>.scorecard.json
"""
import json
import sys
from pathlib import Path

# This Windows console is cp1252: printing the block characters used in the score bar raises
# UnicodeEncodeError, which would kill the report after it already printed the headline numbers.
# Reconfigure rather than degrade to ASCII so the output matches the markdown scorecard exactly.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BAR = "\u2588"
DOT = "\u00b7"


def bar(score, width=10):
    if score is None:
        return DOT * width
    filled = int(round(max(0.0, min(1.0, score)) * width))
    return BAR * filled + DOT * (width - filled)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    target = data.get("manifest", {}).get("target", {})
    totals = data["totals"]
    print(f"{data['standard']}  --  {data['label']}")
    print(f"model={target.get('model')}  provider={target.get('provider')}  temp={target.get('temperature')}")
    axs = "n/a" if data["axs"] is None else f"{data['axs']}"
    flag = "  ** PROVISIONAL **" if data.get("provisional") else ""
    print(f"AXS {axs}/100   coverage {data['coverage']:.0%}{flag}")
    print(
        f"instances={totals['instances']} attributable={totals['attributable_instances']} "
        f"tasks={totals['unique_tasks']} tool_calls={totals['total_tool_calls']} "
        f"failed_calls={totals['failed_tool_calls']}"
    )
    cost = totals["total_cost_usd"]
    per_solved = totals.get("cost_per_solved_task_usd")
    cost_text = "n/a" if cost is None else f"${cost:.4f}"
    per_solved_text = "n/a" if per_solved is None else f"${per_solved:.4f}"
    print(
        f"tokens={totals['total_input_tokens']:,}/{totals['total_output_tokens']:,}  "
        f"cost={cost_text}  per_solved={per_solved_text}  "
        f"wall={totals['wall_clock_s']:.0f}s  judged={totals['judged_assertions']}/{totals['total_assertions']}"
    )
    print()
    print(f"{'#':>2}  {'parameter':<30} {'score':>6}  {'pass rate [95% CI]':<22} {'graded':>6}  bar")
    for entry in sorted(data["parameters"], key=lambda e: e["index"]):
        score = "   n/a" if entry["score"] is None else f"{entry['score']:.3f}"
        if entry["pass_rate"] is None:
            rate = "n/a"
        elif entry["ci95"]:
            rate = f"{entry['pass_rate']:.2f} [{entry['ci95'][0]:.2f},{entry['ci95'][1]:.2f}]"
        else:
            rate = f"{entry['pass_rate']:.2f}"
        graded = "   n/a" if entry["graded"] is None else f"{entry['graded']:.3f}"
        print(f"{entry['index']:>2}  {entry['name']:<30} {score}  {rate:<22} {graded}  {bar(entry['score'])}")
    print()
    print("per-task:")
    for row in data["task_table"]:
        print(
            f"  {row['task_id']:<48} {str(row['passed'])}/{row['attributable']}  "
            f"steps={row['mean_tool_calls']}  {row['mean_seconds']}s  "
            f"pass@2={row['pass@k_2']} pass^2={row['pass^k_2']}  statuses={row['statuses']}"
        )
    if data.get("warnings"):
        print()
        print("warnings:")
        for warning in data["warnings"]:
            print(f"  - {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
