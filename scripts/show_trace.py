"""Ad-hoc trace reader used while developing IQB-14 (kept because reading a trajectory by hand is
the first thing anyone does when a task fails).

    python scripts/show_trace.py bench-out/run-gptoss/traces/<name>.jsonl [--full]
"""
import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    full = "--full" in sys.argv
    if not path.exists():
        print(f"no such trace: {path}")
        return 2
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        status = "ERR " if event["is_error"] else "ok  "
        print(
            f"#{event['index']:<3} {status}{event['tool']:<18} "
            f"{event['duration_ms']:>8.0f}ms  tok {event['input_tokens_delta']}/{event['output_tokens_delta']}"
        )
        args = json.dumps(event["args"], default=str)
        result = str(event["result"]).replace("\n", " ")
        limit = 100000 if full else 200
        print(f"      args: {args[:limit]}")
        print(f"      result: {result[:limit]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
