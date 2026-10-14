"""Check the shape session_store.serialize_messages produces."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from myriaxon.session_store import serialize_messages

msgs = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi there"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"a.txt"}'}}
        ],
    },
    {"role": "tool", "tool_call_id": "c1", "content": "wrote it"},
]
print(json.dumps(serialize_messages(msgs), indent=1)[:1200])
