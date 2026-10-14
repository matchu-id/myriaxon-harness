"""Consensus: several independent attempts, and disagreement escalates rather than being averaged.

Level 4 requirement 4.10. It is the only mechanism in this harness that can produce a better answer than
the best single agent, and the only one whose whole value depends on the attempts being INDEPENDENT.

WHAT INDEPENDENCE MEANS HERE, AND WHY IT IS ENFORCED

If attempt B can read attempt A's answer, B is not a second opinion -- it is a revision, and the agreement
between them measures nothing. So the runner hands each attempt the same question and NOTHING ELSE: no
earlier attempt's text, no hint that other attempts exist. That is a property of this module's interface
rather than advice to the caller, because a prompt-shaped convention is a convention that gets broken the
first time somebody passes context "just to be helpful".

DISAGREEMENT ESCALATES. IT IS NEVER AUTO-PICKED

The tempting design is to take the majority, or the longest answer, or the most confident one. All three
are wrong for the same reason: they convert an unresolved question into a decision nobody made. If three
attempts produce two answers, the honest output is "two of three agree, here is the dissenting one" and a
person decides. A majority that quietly wins teaches nobody that the question was contested.

WHAT THIS IS NOT

Not a vote on facts that have one right answer. If the attempts can be CHECKED -- run the test, call the
endpoint, compare against the file -- then verification beats consensus and is cheaper, because one
verified attempt is worth any number of agreeing guesses. Consensus is for questions that cannot be
settled from inside the sandbox: a design choice, an estimate, a judgement about a vendor document. The
caller decides which situation it is in, and this module's docstring is where that expectation lives.

Similarity comes from `agent_discipline`, which already measures whether two turns say the same thing --
measured there against a real restatement. Reusing it means consensus inherits that measurement rather than
inventing a second notion of "the same answer" that could disagree with the first.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .agent_discipline import similarity

# How many independent attempts by default. Three is the smallest number that can produce a majority AND a
# minority, so it is the smallest number whose result is more informative than a single attempt.
DEFAULT_ATTEMPTS = 3

# Agreement is judged more loosely than repetition, and the difference is measured rather than chosen.
#
# The repetition detector uses 5-word runs and a 0.80 threshold, which catches a RESTATEMENT -- an agent
# saying the same thing again in almost the same words. That is the right grain there, because a false
# positive ends a working run.
#
# Consensus is a different question: two independent attempts asked the same thing will PARAPHRASE, not
# repeat. Measured on a realistic pair that clearly agreed:
#
#     grain   paraphrase   different   separation
#       5        52%           0%        sees agreement as disagreement
#       3        67%           8%
#       2        75%          15%        5x -- the safest margin
#
# So consensus clusters on bigrams. Using the detector's settings here put two agreeing answers in separate
# clusters and reported a split that did not exist, which is a false alarm in the direction that wastes a
# person's attention.
AGREEMENT_GRAIN = 2
AGREEMENT_THRESHOLD = 0.60

# Above this many attempts the cost is real and the information gain is small; a caller that wants more can
# say so explicitly.
MAX_ATTEMPTS = 9


@dataclass
class Attempt:
    """One independent answer."""

    index: int = 0
    author: str = ""
    text: str = ""
    error: str = ""
    seconds: float = 0.0

    @property
    def failed(self) -> bool:
        return bool(self.error) or not (self.text or "").strip()


@dataclass
class Agreement:
    """One cluster of attempts that said substantially the same thing."""

    text: str = ""
    authors: list[str] = field(default_factory=list)
    indices: list[int] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.indices)


@dataclass
class ConsensusResult:
    """What the attempts amounted to."""

    question: str = ""
    attempts: list[Attempt] = field(default_factory=list)
    clusters: list[Agreement] = field(default_factory=list)
    # "agreed" when every successful attempt landed in one cluster; "split" when they did not;
    # "insufficient" when fewer than two attempts succeeded, which is not a consensus at all.
    outcome: str = "insufficient"
    # The escalation text, when there is one. Empty on agreement, because a settled question does not need
    # a person's attention.
    escalation: str = ""

    @property
    def succeeded(self) -> list[Attempt]:
        return [a for a in self.attempts if not a.failed]

    @property
    def answer(self) -> str:
        """The agreed answer, or "" when the attempts did not agree."""
        return self.clusters[0].text if self.outcome == "agreed" and self.clusters else ""

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "outcome": self.outcome,
            "attempts": len(self.attempts),
            "succeeded": len(self.succeeded),
            "clusters": [{"size": c.size, "authors": c.authors, "chars": len(c.text)}
                         for c in self.clusters],
            "escalation": self.escalation,
        }


def run(question: str, ask: Callable[[str, int], str], *, attempts: int = DEFAULT_ATTEMPTS,
        authors: list[str] | None = None) -> ConsensusResult:
    """Ask the same question `attempts` times INDEPENDENTLY and report what came back.

    `ask(question, index)` is the caller's way of producing one answer. It receives the question and the
    attempt number and NOTHING ELSE -- no previous answers, no shared scratch state. That signature is the
    enforcement of independence: a caller physically cannot pass attempt 0's answer into attempt 1 through
    this interface, which is the property the whole mechanism rests on.

    The index exists so an author name can vary per attempt; it must not be used to vary the QUESTION, and a
    caller that does so is measuring the question rather than the answer.
    """
    attempts = max(2, min(int(attempts or DEFAULT_ATTEMPTS), MAX_ATTEMPTS))
    names = list(authors or []) or [f"attempt-{i + 1}" for i in range(attempts)]

    result = ConsensusResult(question=question)
    for index in range(attempts):
        author = names[index] if index < len(names) else f"attempt-{index + 1}"
        started = time.time()
        try:
            text = ask(question, index)
            result.attempts.append(Attempt(index=index, author=author, text=str(text or ""),
                                           seconds=time.time() - started))
        except Exception as exc:  # noqa: BLE001 - one failed attempt must not lose the others
            result.attempts.append(Attempt(index=index, author=author,
                                           error=f"{type(exc).__name__}: {exc}",
                                           seconds=time.time() - started))

    result.clusters = _cluster(result.succeeded)
    _judge(result)
    return result


def _cluster(attempts: list[Attempt]) -> list[Agreement]:
    """Group attempts that said substantially the same thing, biggest group first.

    Single-pass clustering against each cluster's FIRST member rather than pairwise comparison of everything.
    With at most nine attempts the difference in quality is nil, and single-pass keeps the result
    explainable: an answer joins the first cluster it resembles, so a reader can always see which attempt
    each member was compared against.
    """
    clusters: list[Agreement] = []
    for attempt in attempts:
        for cluster in clusters:
            # Bigrams and a looser bar: independent attempts paraphrase, they do not repeat. See
            # AGREEMENT_GRAIN for the measurement that set both.
            if similarity(cluster.text, attempt.text, grain=AGREEMENT_GRAIN) >= AGREEMENT_THRESHOLD:
                cluster.authors.append(attempt.author)
                cluster.indices.append(attempt.index)
                break
        else:
            clusters.append(Agreement(text=attempt.text, authors=[attempt.author],
                                      indices=[attempt.index]))
    clusters.sort(key=lambda c: c.size, reverse=True)
    return clusters


def _judge(result: ConsensusResult) -> None:
    """Set the outcome and, when the attempts disagree, the escalation."""
    succeeded = result.succeeded
    if len(succeeded) < 2:
        result.outcome = "insufficient"
        failed = [a for a in result.attempts if a.failed]
        detail = "; ".join(f"{a.author}: {a.error}" for a in failed[:3]) or "no usable answers"
        result.escalation = (
            "Consensus could not be formed: fewer than two attempts produced an answer. "
            f"{detail}. One answer is not agreement, so this needs a person rather than being reported as "
            "settled."
        )
        return

    top = result.clusters[0]
    if top.size == len(succeeded):
        result.outcome = "agreed"
        return

    result.outcome = "split"
    agreed = ", ".join(top.authors)
    dissent = [c for c in result.clusters[1:]]
    lines = [
        f"The attempts did not agree: {top.size} of {len(succeeded)} said one thing ({agreed}), and "
        f"{len(dissent)} answer(s) differed.",
        "",
        "Agreed by the majority (NOT adopted -- a majority is not a decision):",
        f"  {top.text[:800]}",
    ]
    for index, cluster in enumerate(dissent, start=1):
        lines.append("")
        lines.append(f"Dissenting answer {index}, from {', '.join(cluster.authors)}:")
        lines.append(f"  {cluster.text[:800]}")
    lines.append("")
    lines.append(
        "A person needs to decide which is right. This was deliberately not resolved automatically: a "
        "majority that quietly wins teaches nobody that the question was contested."
    )
    result.escalation = "\n".join(lines)


def format_result(result: ConsensusResult) -> str:
    """A short report for a log or a CLI."""
    lines = [
        f"Consensus on {len(result.attempts)} independent attempt(s): {result.outcome}",
    ]
    for cluster in result.clusters:
        lines.append(f"  {cluster.size} attempt(s) agreed ({', '.join(cluster.authors)})")
    for attempt in result.attempts:
        if attempt.failed:
            lines.append(f"  {attempt.author} failed: {attempt.error}")
    return "\n".join(lines)
