"""Report the tail of a Claude Code session: the last tool calls and any errors.

The session on the VPS stopped mid-task, and the last assistant message alone does not say whether it
finished, asked a question, hit an error, or ran out of budget. Those need different responses, so this
prints the last few tool invocations with their outcome and flags anything that looks like a failure.

Streams the file, because it is 67 MB.
"""

from __future__ import annotations

import json
import sys

TAIL = 25
ERROR_MARKERS = (
    "error", "failed", "refused", "denied", "timeout", "timed out",
    "rate limit", "usage limit", "context low", "no such file", "permission",
    "not found", "exceeded", "aborted",
)


def clip(text: str, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("usage: tail_claude_session.py <session.jsonl>")
        return 2

    events: list[str] = []
    errors: list[str] = []

    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue

            kind = record.get("type", "")
            stamp = str(record.get("timestamp", ""))[11:19]
            message = record.get("message") or {}
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue

            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")

                if ptype == "tool_use":
                    args = part.get("input") or {}
                    detail = (
                        args.get("command")
                        or args.get("file_path")
                        or args.get("path")
                        or args.get("pattern")
                        or args.get("query")
                        or ""
                    )
                    events.append(f"[{stamp}] {part.get('name')}: {clip(detail, 150)}")

                elif ptype == "tool_result":
                    body = part.get("content")
                    if isinstance(body, list):
                        body = " ".join(
                            b.get("text", "") for b in body if isinstance(b, dict)
                        )
                    body = str(body or "")
                    lowered = body.lower()
                    if any(marker in lowered for marker in ERROR_MARKERS):
                        errors.append(f"[{stamp}] {clip(body, 260)}")

                elif ptype == "text" and part.get("text", "").strip():
                    events.append(f"[{stamp}] (reply) {clip(part['text'], 200)}")

    print(f"  --- last {TAIL} events ---")
    for item in events[-TAIL:]:
        print(f"  {item}")
    print()

    if errors:
        print("  --- last 6 tool results that mention a problem ---")
        for item in errors[-6:]:
            print(f"  {item}")
    else:
        print("  no error-looking tool results found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
