"""Summarise a Claude Code session log (.jsonl) without loading it into memory.

Written to be piped into the VPS's python3 over SSH, which sidesteps the quoting problems that come
from nesting PowerShell, bash and JSON escaping in one command. The log on that machine is 67 MB and
26k lines, so everything here streams.

Usage:  ssh host python3 - < analyse_claude_session.py
"""

from __future__ import annotations

import collections
import json
import sys

MAX_TEXT = 240
TAIL_MESSAGES = 8


def clip(text: str, limit: int = MAX_TEXT) -> str:
    text = " ".join((text or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def content_text(message: dict) -> tuple[str, list[str]]:
    """Return (human text, tool names) for one message."""
    content = message.get("content")
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []
    texts, tools = [], []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            texts.append(part.get("text", ""))
        elif part.get("type") == "tool_use":
            tools.append(str(part.get("name", "?")))
    return "\n".join(texts), tools


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("usage: analyse_claude_session.py <session.jsonl>")
        return 2

    counts: collections.Counter[str] = collections.Counter()
    tool_counts: collections.Counter[str] = collections.Counter()
    files_touched: collections.Counter[str] = collections.Counter()
    user_messages: list[str] = []
    assistant_messages: list[str] = []
    first_ts = last_ts = ""
    total = 0

    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            total += 1
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                counts["unparseable"] += 1
                continue

            kind = record.get("type", "?")
            counts[kind] += 1
            stamp = record.get("timestamp", "")
            if stamp:
                first_ts = first_ts or stamp
                last_ts = stamp

            message = record.get("message") or {}
            if not isinstance(message, dict):
                continue
            text, tools = content_text(message)
            for name in tools:
                tool_counts[name] += 1

            # Collect the file paths the tools were aimed at, which is the clearest signal of what
            # the session has actually been working on.
            content = message.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool_use":
                        args = part.get("input") or {}
                        for key in ("file_path", "path", "notebook_path"):
                            if isinstance(args.get(key), str):
                                files_touched[args[key]] += 1

            if kind == "user":
                if text and not text.startswith("<") and "tool_result" not in text[:20]:
                    user_messages.append(text)
            elif kind == "assistant" and text.strip():
                assistant_messages.append(text)

    print(f"  lines        : {total:,}")
    print(f"  record types : {dict(counts)}")
    print(f"  time span    : {first_ts}  ->  {last_ts}")
    print()

    print("  --- tools used (top 12) ---")
    for name, n in tool_counts.most_common(12):
        print(f"    {n:>6}  {name}")
    print()

    print("  --- files touched most (top 15) ---")
    for name, n in files_touched.most_common(15):
        print(f"    {n:>6}  {clip(name, 110)}")
    print()

    print(f"  --- last {TAIL_MESSAGES} HUMAN messages ---")
    for text in user_messages[-TAIL_MESSAGES:]:
        print(f"    > {clip(text)}")
    print()

    print(f"  --- last {TAIL_MESSAGES} ASSISTANT summaries ---")
    for text in assistant_messages[-TAIL_MESSAGES:]:
        print(f"    < {clip(text)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
