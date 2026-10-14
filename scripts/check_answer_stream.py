"""Does the answer stream incrementally, and is it markdown?

    python scripts/check_answer_stream.py [port]

The screenshot that prompted this work showed an answer whose `**bold**`, `### headings` and `---`
marks were all visible as literal text. Two separate defects had to be fixed: the browser never
received incremental answer text at all (only a final notice), and the renderer was too naive for
headings, lists and tables. This asserts on the event stream, which is what the browser consumes.
"""
import json
import sys
import threading
import time
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
BASE = f"http://127.0.0.1:{PORT}"

passed, failed = [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' -- ' + str(detail)) if detail and not condition else ''}")


def get(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=60).read().decode())


def post(path, body):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    return json.loads(urllib.request.urlopen(request, timeout=60).read().decode())


PROMPT = (
    "Answer in markdown only. Use a heading, a bullet list, a table with two columns and two rows, "
    "and bold at least one word. Keep it under 80 words."
)


def main() -> int:
    session_id = post("/api/sessions", {"mode": "auto_edit"})["session"]["id"]

    events = []
    done = threading.Event()

    def listen():
        try:
            with urllib.request.urlopen(f"{BASE}/api/events?session={session_id}", timeout=300) as response:
                for raw in response:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line.startswith("data:"):
                        try:
                            event = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        events.append(event)
                        if event.get("type") == "turn_end":
                            done.set()
                            return
        except Exception:  # noqa: BLE001
            done.set()

    listener = threading.Thread(target=listen, daemon=True)
    listener.start()
    time.sleep(0.5)
    post("/api/chat", {"session": session_id, "message": PROMPT})

    deadline = time.time() + 300
    while time.time() < deadline and not done.is_set():
        time.sleep(1)
    listener.join(timeout=5)

    deltas = [e for e in events if e.get("type") == "answer_delta"]
    kinds = {}
    for event in events:
        kinds[event.get("type")] = kinds.get(event.get("type"), 0) + 1
    print(f"    events: {kinds}\n")

    check("answer_delta events arrived", len(deltas) > 0, len(deltas))
    check("more than one delta (i.e. it really streamed)", len(deltas) > 1, len(deltas))

    if deltas:
        final = deltas[-1].get("text") or ""
        # The payload is cumulative, so the last one must be a superset of the first.
        first = deltas[0].get("text") or ""
        check("delta payload is cumulative", final.startswith(first), f"first={first[:40]!r}")
        check("final delta is non-trivial", len(final) > 40, len(final))
        check("contains a heading", "#" in final, final[:80])
        check("contains a table row", "|" in final, final[:80])
        check("contains bold", "**" in final, final[:80])

    check("turn completed", "turn_end" in kinds, kinds)
    check("no error event", "error" not in kinds,
          next((e.get("message") for e in events if e.get("type") == "error"), ""))

    # The answer must be presented exactly once. Measured defect: the streamed deltas rendered it as
    # markdown AND the agent's final print of the same text rendered it again as a raw block.
    full_answers = [e for e in events if e.get("type") == "answer"]
    check("only one answer channel used", not (deltas and full_answers), kinds)
    check("some answer channel fired", bool(deltas or full_answers), kinds)
    if deltas:
        final_text = (deltas[-1].get("text") or "").strip()
        duplicates = [
            e for e in events
            if e.get("type") == "notice"
            and len((e.get("text") or "").strip()) > 40
            and (e.get("text") or "").strip()[:60] == final_text[:60]
        ]
        check("the answer is not also sent as a notice", not duplicates, len(duplicates))

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        print("failed:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
