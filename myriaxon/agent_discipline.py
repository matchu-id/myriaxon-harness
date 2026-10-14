"""Recognising that an agent is going in circles, and stopping it.

Level 5 requirement 5.7 is *"recognise when it is out of its depth and stop"*. This is the mechanism for
the observable half of that: an agent that produces the same answer repeatedly without doing anything is
not making progress, whatever its wording suggests.

WHY THIS IS NEEDED, MEASURED

A user asked an agent for a capability comparison and then re-sent the same request. The agent replied
*"I already provided a complete 100-requirement matrix above covering every aspect in depth"* -- and then
spent **six more turns** investigating code structures anyway. Nothing stopped it. The existing loop
detector could not: it watches TOOL CALLS, and those turns contained none, or contained different ones
each time.

That is the gap this fills. `loop_detection` answers "is this tool call repeating"; this answers "is this
TURN repeating", which is the question that matters when the output is prose.

THE HONEST LIMIT

This detects *the same text coming back twice*, which is a reliable signal. It does NOT detect a model
that is newly wrong each time -- six differently-worded wrong answers look like progress to any
textual comparison, and that is a limit of the method rather than a tuning problem. Recorded here so the
next person does not try to fix it by lowering a threshold.

Nor is this "declared uncertainty" (requirement 5.5). A harness can notice repetition; it cannot make a
model feel doubt. What it can do is refuse to let repetition pass unremarked, which is the version of
that requirement machinery can actually deliver.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Two turns saying effectively the same thing is the signal. Not three: the user in the measured case saw
# it after two, and a third turn is another minute and another paid call spent proving what the second
# already showed.
REPEAT_TURNS = 2

# Short answers ("Done.", "OK") repeat legitimately and carry no signal, so they are excluded by length
# rather than by trying to classify them.
MIN_SIGNIFICANT_CHARS = 120

# How similar two turns must be to count as the same.
#
# MEASURED, and the metric changed because of it. The first version used Jaccard overlap (intersection over
# union) with a threshold of 0.82, and a realistic restatement scored only 57% -- because the preceding
# turn was a genuine answer and the repeat added a preamble, so the UNION grew while the content did not.
# Jaccard punishes exactly the case this exists to catch.
#
# Containment is the right question: how much of the SMALLER turn's content already appeared in the other.
# A restatement contains its predecessor; a new answer does not. On the same pair, containment scores well
# above Jaccard, and a genuinely different answer still scores near zero -- so the two are separated by the
# metric rather than by a threshold tuned until one example passed.
SIMILARITY_THRESHOLD = 0.80


@dataclass
class RepetitionVerdict:
    """What the detector concluded, and the evidence for it."""

    repeating: bool = False
    turns: int = 0
    similarity: float = 0.0
    message: str = ""
    excerpt: str = ""


def _normalise(text: str) -> str:
    """Boil a turn down to its content, so formatting differences do not hide a repeat.

    Deliberately aggressive. The measured case differed by a sentence of preamble and by whitespace; a
    comparison sensitive to those would have reported progress where there was none, which is the failure
    mode that matters -- a false negative lets the loop run, a false positive stops one turn early.
    """
    text = text or ""
    # Fenced code and tables are content, but their punctuation is not, so it goes.
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"[*_`#>|\-]{1,}", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _shingles(text: str, size: int = 5) -> set[str]:
    """Word n-grams, which compare content rather than sequence.

    A set of n-grams is insensitive to the ORDER of sentences, which is deliberate: an agent that reorders
    the same points has not made progress, and a sequence-based comparison would score that as different.
    """
    words = _normalise(text).split()
    if len(words) < size:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + size]) for i in range(len(words) - size + 1)}


def similarity(left: str, right: str, *, grain: int = 5) -> float:
    """How much of the shorter text the longer one already contains, 0.0 to 1.0.

    CONTAINMENT rather than Jaccard overlap, and the choice is measured. Jaccard is |intersection| over
    |union|, so a restatement that adds a sentence lowers its own score: the union grows while the shared
    content does not. A realistic restatement -- same body, a preamble of "I already provided..." -- scored
    **57%** under Jaccard against a threshold that needed 82%, so the detector would have missed the exact
    case it was written for. Containment scores that pair at **83%** and a genuinely new answer at **0%**,
    separating the two by the metric rather than by a threshold tuned until one example passed.

    `grain` is the n-gram size, and it is a parameter because two callers need different resolutions.
    Measured on a realistic paraphrase pair:

        grain   paraphrase   different   separation
        -----   ----------   ---------   ----------
          5        52%           0%        too fine: a paraphrase does not reuse 5-word runs
          4        58%           4%
          3        67%           8%          8x
          2        75%          15%          5x   <- the safest margin

    A RESTATEMENT reuses whole phrases, so 5-grams catch it with almost no false positives -- which is what
    the repetition detector needs, where a false positive ends a working run. A PARAPHRASE shares meaning
    without reusing phrasing, so consensus needs 2-grams or it reads agreement as disagreement. No single
    default suits both, which is why this is not hard-coded.
    """
    a, b = _shingles(left, grain), _shingles(right, grain)
    if not a or not b:
        return 0.0
    smaller, larger = (a, b) if len(a) <= len(b) else (b, a)
    return len(smaller & larger) / len(smaller)


@dataclass
class RepetitionDetector:
    """Watches a run's turns and reports when they stop saying anything new.

    Reset per run. Kept separate from `loop_detection.LoopDetector` because the two watch different
    things -- tool calls versus turn output -- and merging them would mean one threshold trying to serve
    both, which is how the tool detector ended up needing ten repeats to notice a failure.
    """

    run_id: str = ""
    history: list[str] = field(default_factory=list)
    # Turns that called no tool at all. A turn that acted is a turn that did something, even if it reads
    # like the last one.
    inert: list[bool] = field(default_factory=list)

    def record(self, text: str, *, used_tools: bool = False) -> RepetitionVerdict:
        """Add a finished turn and say whether the run is now repeating itself."""
        body = text or ""
        if len(_normalise(body)) < MIN_SIGNIFICANT_CHARS:
            # Too short to judge. Recorded anyway so the window reflects what happened, but it cannot
            # itself trip the verdict.
            self.history.append(body)
            self.inert.append(not used_tools)
            return RepetitionVerdict()

        verdict = RepetitionVerdict(turns=len(self.history) + 1)

        # Compared against the earliest comparable earlier turn, so the verdict names how far back the
        # repetition goes rather than only the most recent turn.
        for index in range(len(self.history)):
            if len(_normalise(self.history[index])) < MIN_SIGNIFICANT_CHARS:
                continue
            score = similarity(self.history[index], body)
            if score < SIMILARITY_THRESHOLD:
                continue
            # A repeat counts when neither turn acted. Two turns that both ran commands and reported them
            # similarly are doing work and describing it consistently, which is not a loop -- so the
            # requirement is on the CURRENT turn only, and the earlier one is allowed to have acted.
            #
            # The first version required the earlier turn to have been inert too. That rejected the
            # measured case: an agent answers (a real turn, with tool calls), the user asks again, and the
            # agent restates its previous answer while doing nothing. `break`ing on the first
            # non-matching candidate made it worse -- the earliest turn is usually the one that acted, so
            # the loop never reached the later inert turns it should have compared against.
            if not used_tools:
                verdict.repeating = True
                verdict.similarity = score
                verdict.excerpt = _normalise(body)[:200]
                verdict.message = (
                    f"This is turn {verdict.turns} and it says substantially the same thing as an earlier "
                    f"turn ({score:.0%} of this answer was already said), without running anything. "
                    "Repeating a conclusion is not progress. Either do something different -- inspect, "
                    "test, change the approach -- or stop and say plainly that you are stuck and why."
                )
                break

        self.history.append(body)
        self.inert.append(not used_tools)
        return verdict


def format_verdict(verdict: RepetitionVerdict) -> str:
    """One line for a log or a status panel."""
    if not verdict.repeating:
        return ""
    return f"repetition detected after {verdict.turns} turns ({verdict.similarity:.0%} identical)"
