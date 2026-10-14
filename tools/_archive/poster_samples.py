"""Design a sample poster set, so the visual output can be inspected."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from myriaxon.poster import PosterSpec, design_poster  # noqa: E402

OUT = REPO / "bench-out" / "posters"


def main() -> int:
    specs = [
        PosterSpec(
            title="Ship the harness, not the prompt",
            eyebrow="Myriaxon 1.2",
            subtitle="One agent loop, six providers, and a mission control that tells the truth about what it can reach.",
            bullets=[
                "Persistent agents that keep their objectives across sessions",
                "Every figure on the dashboard read from the machine",
                "Runs in your terminal and in the browser, on the same agent",
            ],
            footer="myriaxon — the agentic harness",
            signature="@arian",
            date="OCTOBER 2026",
            motif="axon",
            size="portrait",
        ),
        PosterSpec(
            title="Design is a decision, not a decoration",
            eyebrow="Studio note 04",
            body="A poster works when one idea survives being read from across a room. Everything else is furniture.",
            footer="Typeset in Bahnschrift and Segoe UI",
            mark_text="MYRIAXON STUDIO",
            theme="light",
            motif="grid",
            size="square",
        ),
        PosterSpec(
            title="Autonomy with receipts",
            eyebrow="Dots",
            subtitle="An agent that records what it did, why it failed, and what it needs from you.",
            bullets=["Retries transient failures", "Blocks goals that need a human", "Never invents a result"],
            theme="signal",
            motif="rings",
            size="story",
            date="2026",
        ),
    ]

    for index, spec in enumerate(specs, start=1):
        result = design_poster(spec, OUT, stem=f"sample-{index}-{spec.theme}")
        print(result.summary())
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
