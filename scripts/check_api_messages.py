"""Inspect the dashboard's /api/messages payload shape."""
import json
import sys
import urllib.request

SESSION = sys.argv[1] if len(sys.argv) > 1 else ""
url = f"http://127.0.0.1:8765/api/messages?session={SESSION}"
data = json.loads(urllib.request.urlopen(url, timeout=20).read().decode())

print("top-level keys:", list(data.keys()))
for index, message in enumerate(data["messages"], 1):
    role = message.get("role")
    content = message.get("content")
    print(f"  {index}: keys={list(message.keys())} role={role!r} content={type(content).__name__}")
    if isinstance(content, str):
        print(f"       text={content[:70]!r}")
    elif isinstance(content, list):
        for block in content[:3]:
            print(f"       block={str(block)[:70]}")
print()
print("trajectory:")
for event in data["trajectory"]:
    print(f"  {event['tool']} error={event['is_error']} args={str(event['args'])[:60]}")
