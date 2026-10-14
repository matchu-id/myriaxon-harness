"""Add a streaming hook next to each live.update(render_markdown(...)) call site.

The agents already accumulate the answer text and hand it to `live.update` on every delta, so that
call site is where streaming is available without touching the agent loop. The hook function is a
no-op for a normal terminal Console, so the CLI is unaffected; the dashboard's EventConsole turns it
into an `answer_delta` event.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

EDITS = [
    ("myriaxon/openai_agent.py", "live.update(render_markdown(text_buffer))", "text_buffer"),
    ("myriaxon/gemini_agent.py", 'live.update(render_markdown("".join(text_parts)))', '"".join(text_parts)'),
    ("myriaxon/gemini_agent.py", "live.update(render_markdown(text_buffer))", "text_buffer"),
]

HOOK = '''

def _stream_answer_to_console(console, text: str) -> None:
    """Report the accumulating answer to a console that streams, if it has that capability.

    Deliberately duck-typed rather than an isinstance check against the dashboard's console class:
    the CLI must not import the dashboard package at all, and any console without `stream_answer`
    (every terminal Console, including in tests) simply does nothing.
    """
    publisher = getattr(console, "stream_answer", None)
    if callable(publisher):
        publisher(text)
'''

for relative, needle, argument in EDITS:
    path = REPO / relative
    source = path.read_text(encoding="utf-8")
    if needle not in source:
        print(f"MISS  {relative}: {needle}")
        continue
    # Preserve the indentation of the line being followed so the call lands inside the same block.
    line_start = source.rfind("\n", 0, source.index(needle)) + 1
    indent = ""
    for char in source[line_start:]:
        if char == " ":
            indent += char
        else:
            break
    replacement = f"{needle}\n{indent}_stream_answer_to_console(self.console, {argument})"
    path.write_text(source.replace(needle, replacement, 1), encoding="utf-8")
    print(f"PATCHED {relative} -> {argument}")

# Insert the helper once per file, after the imports block (before the first top-level class/def).
for relative in ("myriaxon/openai_agent.py", "myriaxon/gemini_agent.py"):
    path = REPO / relative
    source = path.read_text(encoding="utf-8")
    if "_stream_answer_to_console" not in source or "def _stream_answer_to_console" in source:
        continue
    marker = "\n\n\nclass "
    index = source.find(marker)
    if index == -1:
        marker = "\n\n\ndef "
        index = source.find(marker)
    if index == -1:
        print(f"NO MARKER in {relative}")
        continue
    source = source[:index] + HOOK + source[index:]
    path.write_text(source, encoding="utf-8")
    print(f"HELPER inserted into {relative}")
