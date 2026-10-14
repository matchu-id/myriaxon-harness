#!/usr/bin/env python3
"""Verify the session writer claim: a stale writer cannot destroy a newer transcript.

The defect this closes was measured, not assumed: `save_session` was a pure last-write-wins overwrite. Two
Myriaxon processes sharing a project directory -- two terminals, a dashboard turn and a terminal turn, a Locus
waking while the user types -- meant the second writer silently replaced the first one's transcript, and the
first never learned it. The losing side is unbounded: however many turns the other process had accumulated.

OpenClaw fences this with `activeWriterRunId` / `expectedWriterRunId`, verified in
`src/config/sessions/session-accessor.sqlite-scope.ts`. This is the same shape reduced to what a JSON session
file supports.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from myriaxon import session_store as S  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))


def save(root: Path, writer: str, text: str, strict: bool = True):
    return S.save_session(
        "anthropic", "m", "auto", None, False,
        [{"role": "user", "content": text}], root, writer_id=writer, strict=strict,
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp())

    print("=== 1. writer ids are unique even within one process ===")
    # pid alone would be reused across restarts, and two recycled pids comparing equal would defeat the fence.
    ids = {S.new_writer_id() for _ in range(50)}
    check("fifty ids are all distinct", len(ids) == 50, f"{len(ids)} distinct")
    check("an id carries the pid", all(i.startswith(str(__import__("os").getpid())) for i in ids))

    print()
    print("=== 2. THE FIX: a stale writer's save is refused, not silently applied ===")
    a, took_a = S.claim_session(tmp)
    check("the first claim is not a takeover", not took_a)
    check("A can write", save(tmp, a, "A turn 1").written)

    b, took_b = S.claim_session(tmp)
    check("a second claim reports the takeover", took_b)
    check("B can write", save(tmp, b, "B turn 1").written)

    stale = save(tmp, a, "A turn 2 -- STALE")
    check("A's stale write is REFUSED", not stale.written)
    check("it is marked as fenced rather than failed", stale.fenced)
    check("the reason names both writers", "holder" in stale.reason and "this writer" in stale.reason,
          stale.reason[:90])

    final = S.load_session(tmp)
    contents = [m["content"] for m in final["messages"]]
    check("B's transcript SURVIVED", contents == ["B turn 1"], str(contents))
    check("the claim on disk is B's", final[S.SESSION_CLAIM_FIELD] == b)

    print()
    print("=== 3. the current holder can keep writing ===")
    check("B's second write succeeds", save(tmp, b, "B turn 2").written)
    contents = [m["content"] for m in S.load_session(tmp)["messages"]]
    check("and it took effect", contents == ["B turn 2"], str(contents))

    print()
    print("=== 4. a caller that passes no writer id is unaffected ===")
    # The fence must not break every existing caller. `writer_id=""` means unconditional, which is the old
    # behaviour -- silently fencing a caller that never opted in would break sessions rather than protect them.
    tmp2 = Path(tempfile.mkdtemp())
    out = S.save_session("anthropic", "m", "auto", None, False,
                         [{"role": "user", "content": "no claim"}], tmp2)
    check("an unclaimed save still writes", out.written)
    check("and records no claim field",
          S.SESSION_CLAIM_FIELD not in (S.load_session(tmp2) or {}),
          str(list((S.load_session(tmp2) or {}).keys())))

    print()
    print("=== 5. strict=False is a SOFT fence: it reports the conflict and still writes ===")
    # `strict` controls whether a detected conflict refuses the write, not whether the conflict is detected.
    # The first implementation gated the whole check on `strict`, so a soft caller was not fenced at all --
    # `strict=False` silently meant "no fence". This test originally asserted the opposite of the intended
    # semantics and was corrected rather than the code: a soft write that proceeds must still say it was
    # displaced, because a caller that never learns it lost the session has the same bug as one overwritten.
    tmp3 = Path(tempfile.mkdtemp())
    first, _ = S.claim_session(tmp3)
    save(tmp3, first, "first")
    other, _ = S.claim_session(tmp3)
    save(tmp3, other, "second")
    soft = save(tmp3, first, "stale again", strict=False)
    check("the displaced writer's soft write proceeds", soft.written)
    check("but it is still REPORTED as fenced", soft.fenced)
    check("and the reason says it was written anyway", "written anyway" in soft.reason, soft.reason[-60:])
    check("the soft write carried its own claim",
          S.load_session(tmp3)[S.SESSION_CLAIM_FIELD] == first)

    tmp3b = Path(tempfile.mkdtemp())
    a3, _ = S.claim_session(tmp3b)
    save(tmp3b, a3, "first")
    b3, _ = S.claim_session(tmp3b)
    save(tmp3b, b3, "second")
    hard = save(tmp3b, a3, "must not land", strict=True)
    check("the same write in strict mode is refused", not hard.written and hard.fenced)
    check("and the newer transcript survives",
          [m["content"] for m in S.load_session(tmp3b)["messages"]] == ["second"])

    print()
    print("=== 6. a plain load/save round trip is unchanged ===")
    tmp4 = Path(tempfile.mkdtemp())
    w, _ = S.claim_session(tmp4)
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    S.save_session("openai", "gpt", "plan", "high", True, msgs, tmp4, writer_id=w)
    loaded = S.load_session(tmp4)
    check("provider round-trips", loaded["provider"] == "openai")
    check("model round-trips", loaded["model"] == "gpt")
    check("mode round-trips", loaded["mode"] == "plan")
    check("effort round-trips", loaded["effort"] == "high")
    check("thinking round-trips", loaded["thinking"] is True)
    check("messages round-trip", [m["content"] for m in loaded["messages"]] == ["hi", "hello"])

    print()
    print("=== 7. a missing or corrupt file degrades to no claim ===")
    tmp5 = Path(tempfile.mkdtemp())
    fresh, taken = S.claim_session(tmp5)
    check("a missing file yields a claim without a takeover", bool(fresh) and not taken)
    (tmp5 / ".myriaxon").mkdir(parents=True, exist_ok=True)
    (tmp5 / ".myriaxon" / "session.json").write_text("{ not json", encoding="utf-8")
    check("a corrupt file loads as None", S.load_session(tmp5) is None)
    again, taken2 = S.claim_session(tmp5)
    check("and claiming still works", bool(again) and not taken2)

    print()
    print(f"=== {len(PASS)} passed, {len(FAIL)} failed ===")
    for label in FAIL:
        print(f"  FAILED: {label}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
