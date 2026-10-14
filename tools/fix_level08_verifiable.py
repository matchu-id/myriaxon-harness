#!/usr/bin/env python3
"""Replace level 8's debounce and keepLatest questions with designs the harness can actually test.

The problem with both originals: the verifier passes each test's `args` through JSON and compares the
function's return value to `expected`. A debounce needs a function and a timer to be injected, and neither
survives JSON -- so the questions as written could not be verified at all, and unverified expectations are
exactly what this pipeline exists to avoid.

The replacements keep the same subject and become testable by making the interesting state observable:

  * `debounceState(calls, waitMs)` folds a sequence of call timestamps into what the debounced function
    would do -- how many times it fired, when, and which arguments it carried. This is the debounce rule
    expressed as a pure function, which is also the clearest way to see what debouncing means: a call runs
    only if no later call arrives inside the window.
  * `latestOnly(values)` applies the discard-stale-results rule to a list of arriving responses, returning
    which of them were current when they arrived. That is the same rule a request guard implements, without
    needing the requests.
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework\fullstack-developer\levels\08.json")

DEBOUNCE = {
    "prompt": "Implement `debounceState(calls, waitMs)`: given a sequence of call timestamps, report what a "
              "debounced function would have done. The editor below shows the signature and the tests.",
    "explanation": "This is the debouncing rule written as a pure function: a call is the one that fires "
                   "only if no later call arrives within the wait window, which is why the final call in a "
                   "burst is the one that survives. Expressing it this way makes the rule checkable, and "
                   "the count of firings is what shows a five-letter word becoming one request.",
    "starter": "function debounceState(calls, waitMs) {\n"
               "  // calls: array of { at, arg }, in non-decreasing order of `at`.\n"
               "  // A call fires when no later call lands within waitMs of it.\n"
               "  // Return { fired, firedArgs }.\n"
               "  return { fired: 0, firedArgs: [] };\n"
               "}\n",
    "instructions": "Given `calls`, an array of `{ at, arg }` objects in non-decreasing time order, and a "
                    "`waitMs` window, work out which calls would actually run.\n\nA call runs when **no "
                    "later call starts within `waitMs` of it** \u2014 that is, when the next call's `at` is "
                    "at least `waitMs` greater, or there is no next call.\n\nReturn an object with:\n\n- "
                    "`fired` \u2014 how many calls ran\n- `firedArgs` \u2014 the `arg` of each call that "
                    "ran, in order\n\nPress **Run tests** to compile and run this TypeScript in your "
                    "browser using the real TypeScript compiler. Nothing runs on the server.",
    "tests": [
        {"id": "t1",
         "name": "a lone call fires, since nothing follows it",
         "args": [[{"at": 0, "arg": "a"}], 300],
         "expected": {"fired": 1, "firedArgs": ["a"]}},
        {"id": "t2", "hidden": True,
         "name": "hidden: three calls inside the window collapse to one, carrying the last arguments",
         "args": [[{"at": 0, "arg": "a"}, {"at": 100, "arg": "b"}, {"at": 200, "arg": "c"}], 300],
         "expected": {"fired": 1, "firedArgs": ["c"]}},
        {"id": "t3", "hidden": True,
         "name": "hidden: a burst separated by more than the window fires twice",
         "args": [[{"at": 0, "arg": "a"}, {"at": 400, "arg": "b"}], 300],
         "expected": {"fired": 2, "firedArgs": ["a", "b"]}},
        {"id": "t4", "hidden": True,
         "name": "hidden: a call exactly on the window boundary counts as separated, not as a continuation",
         "args": [[{"at": 0, "arg": "a"}, {"at": 300, "arg": "b"}], 300],
         "expected": {"fired": 2, "firedArgs": ["a", "b"]}},
    ],
}

KEEP_LATEST = {
    "prompt": "Implement `currentResponses(arrivals)`: given responses returning in the order they arrive, "
              "report which of them were still current when they arrived. The editor below shows the "
              "signature and the tests.",
    "explanation": "A response is current when no later request has been issued since it was sent, which is "
                   "the rule that stops a slow earlier answer overwriting a newer one. Reporting it as a "
                   "list of values and nulls makes the discarding visible rather than something that "
                   "happens silently inside a handler.",
    "starter": "function currentResponses(arrivals) {\n"
               "  // arrivals: array of { sentSeq, value }, in the order the responses arrive.\n"
               "  // The highest sentSeq seen so far is the latest request issued.\n"
               "  // Return the value when its request is still the latest, else null.\n"
               "  return [];\n"
               "}\n",
    "instructions": "Given `arrivals`, an array of `{ sentSeq, value }` in the order the responses come "
                    "back, decide which are still current.\n\nA response is current when its `sentSeq` is "
                    "the **highest `sentSeq` among the arrivals processed so far** \u2014 that is, no "
                    "later request has been issued by the time it arrives.\n\nReturn an array the same "
                    "length as `arrivals`, holding each `value` when it was current and `null` when it was "
                    "stale.\n\nPress **Run tests** to compile and run this TypeScript in your browser "
                    "using the real TypeScript compiler. Nothing runs on the server.",
    "tests": [
        {"id": "t1",
         "name": "a single response is current",
         "args": [[{"sentSeq": 1, "value": "first"}]],
         "expected": ["first"]},
        {"id": "t2", "hidden": True,
         "name": "hidden: a slow response for an older request arrives after a newer one and is discarded",
         "args": [[{"sentSeq": 2, "value": "newer"}, {"sentSeq": 1, "value": "older"}]],
         "expected": ["newer", None]},
        {"id": "t3", "hidden": True,
         "name": "hidden: responses arriving in request order are all current",
         "args": [[{"sentSeq": 1, "value": "a"}, {"sentSeq": 2, "value": "b"}]],
         "expected": ["a", "b"]},
    ],
}


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    replaced = 0

    for question in data["questions"]:
        spec = question.get("code")
        if not spec or spec.get("language") != "typescript":
            continue
        prompt = question.get("prompt", "")

        if "debounce(fn, waitMs)" in prompt:
            replacement = DEBOUNCE
        elif "keepLatest(handlers)" in prompt:
            replacement = KEEP_LATEST
        else:
            continue

        question["prompt"] = replacement["prompt"]
        question["explanation"] = replacement["explanation"]
        spec["entryPoint"] = "debounceState" if replacement is DEBOUNCE else "currentResponses"
        spec["starterCode"] = replacement["starter"]
        spec["instructionsMarkdown"] = replacement["instructions"]
        spec["tests"] = replacement["tests"]
        replaced += 1

    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    print(f"  replaced {replaced} question(s) with verifiable designs")
    return 0 if replaced == 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
