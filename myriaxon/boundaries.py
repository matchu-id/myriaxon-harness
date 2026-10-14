"""Section 8.5, as rules rather than intentions.

## Why this module exists at all

The design document's boundaries were, until now, only in the system prompt. A prompt is a REQUEST: a model that
ignores it produces output nothing catches, and the failure mode is silent. Five of the six boundaries are also not
matters of taste:

  * diagnosing a condition, or recommending medication;
  * claiming to be human, or to replace friends, family or a professional;
  * guilting someone for being away or missing a task;
  * dark patterns -- streak anxiety, fake urgency;
  * analysing journal or mood data without an opt-in.

So they are checked on the text Loci produces. A SYSTEM PROMPT ASKS; a check ESTABLISHES.

## The sixth boundary is different, and is handled differently

"Encourage dependence. If a user says Loci is their only friend, Loci responds warmly *and* gently encourages human
connection." That one is not a phrase to forbid -- it is a REQUIRED response to a specific situation, so it cannot be
caught by scanning for a pattern. It is checked the other way round: when the user discloses isolation, a reply that
contains no encouragement toward people FAILS, even though every word in it is kind. Warmth alone is the failure.

## The false positive is the risk

This runs over text written for someone who may be having a hard time, and a checker that rewrites a good reply is
worse than no checker. So:

  * every boundary is a NAMED check with the design document's own clause next to it;
  * the patterns are narrow and phrase-anchored rather than word-anchored -- `_speak_of_condition`, not "condition",
    because a sentence may legitimately contain the word;
  * the distinction that matters is between the USER'S state and LOCI'S claim. `you might have anxiety` is a claim;
    `anxiety is common` is not, and `you said you have anxiety` is a report of what the user said;
  * `REWRITE` is reserved for the two boundaries where the harm is acting on bad information -- a diagnosis and a
    medication. Everything else is surfaced, not silently replaced, because a reply about grief that got rewritten by
    a regex is a worse outcome than one that mentioned a streak.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# What to do about a violation.
NOTE = "note"          # surfaced, nothing changed
REWRITE = "rewrite"    # the reply is replaced with a safe one

# The six boundaries, named so a caller can decline or count one specifically.
DIAGNOSIS = "diagnosis"
MEDICATION = "medication"
HUMAN_CLAIM = "human-claim"
REPLACEMENT_CLAIM = "replacement-claim"
GUILT = "guilt"
DARK_PATTERN = "dark-pattern"
PRIVACY = "privacy"
DEPENDENCE = "dependence"

BOUNDARIES = (
    DIAGNOSIS, MEDICATION, HUMAN_CLAIM, REPLACEMENT_CLAIM, GUILT, DARK_PATTERN, PRIVACY, DEPENDENCE,
)

# Condition names a diagnosis could be spoken of. Kept as a list rather than a generic noun-phrase pattern, because
# "you have a cold" is not a clinical claim and a pattern broad enough to catch it would fire on ordinary talk.
_CONDITIONS = (
    "depression", "depressed", "anxiety", "anxious", "adhd", "add", "autism", "autistic", "ptsd", "ocd",
    "bipolar", "borderline", "bpd", "narcissis", "sociopath", "psychopath", "schizophreni", "eating disorder",
    "anorexia", "bulimia", "burnout", "insomnia", "disorder", "syndrome", "condition",
)

# Second person + a diagnostic verb. The second person is what makes it a claim ABOUT the user rather than a remark.
_DIAGNOSTIC_VERBS = r"(have|has|are|is|suffer|suffering|sounds like|seems like|looks like|might have|may have|probably have|could have)"

# An adverb on EITHER side of the verb. Measured: `You clearly have an eating disorder` puts it before the verb and
# `You are probably depressed` puts it after the copula, so allowing it in one place only missed one of the two.
_ADVERB = r"(?:clinically |clearly |likely |probably |definitely |obviously |certainly )?"

# The diagnostic SHAPE, matched anywhere. What it is embedded in is decided by `_diagnosis_match`, because a frame is
# a property of the surrounding text rather than of where the phrase begins.
_DIAGNOSIS_SHAPE = re.compile(
    r"\byou(?:'re| are| probably are)?\s+"
    + _ADVERB
    + _DIAGNOSTIC_VERBS
    + r"\s+(?:a|an|the)?\s*"
    + _ADVERB
    + "(?:" + "|".join(_CONDITIONS) + r")",
    re.IGNORECASE,
)

# A REPORTING OR CONDITIONAL FRAME, within a window before the match. `if` and `whether` are here for the same reason
# the reporting verbs are: `If you have anxiety, this will help` is not a verdict, and a checker that reported it would
# be firing on helpful writing.
_FRAME = re.compile(
    r"\b(?:said|says|say|mentioned|mentions|told me|tell me|wrote|writes|write|described|describes|"
    r"called it|reported|reports|shared|shares|if|whether|when you|assuming|suppose|supposing)\b",
    re.IGNORECASE,
)
# How far back a frame can sit and still govern the phrase. Twelve words is generous for English and short enough that
# an unrelated sentence's `if` cannot reach across into a separate claim.
_FRAME_WINDOW = 80


def _unframed(pattern, text: str):
    """The first match of pattern that is NOT inside a reporting or conditional frame, or None.

    Shared by EVERY diagnosis pattern rather than duplicated per pattern, and that is the fix rather than a tidy-up:
    the first version guarded one pattern and left the second unguarded, so a report still read as a diagnosis through
    the other path. A guard one of two paths can bypass is not a guard.

    Two passes, because the distinction is not expressible as a lookbehind -- the phrase is matched anywhere and the
    window before it decides. See the module docstring for the three wrong attempts this replaced.
    """
    body = text or ""
    for match in pattern.finditer(body):
        window = body[max(0, match.start() - _FRAME_WINDOW):match.start()]
        if _FRAME.search(window):
            continue
        return match
    return None

# A label form: "you are depressed", "you're bipolar". Separate because the verb is the copula and the condition is
# adjectival, which the pattern above also covers -- kept explicit so the intent is readable.
_DIAGNOSIS_LABEL = re.compile(
    r"\byou(?:'re| are)\s+(?:clearly|probably|definitely|likely)?\s*(?:" + "|".join(_CONDITIONS) + r")\b",
    re.IGNORECASE,
)

# ADVICE IS DIRECTIVE. The first version matched `take ... ssri` anywhere, so `Many people take an SSRI` -- a general
# remark not addressed to anyone -- was read as a recommendation. The alternation is now anchored to the start of a
# clause (string, sentence end, or a comma), because that is what makes a sentence an instruction rather than an
# observation. The phrasal forms (`try increasing`) are included because they are the more natural English and so the
# more likely thing for a model to write.
_MEDICATION = re.compile(
    r"(?:^|(?<=[.!?;\n])\s*|(?<=,)\s*)(?:"
    r"you should (?:take|try|start|stop taking|increase|decrease|up|lower)"
    r"|i(?:'d| would) recommend (?:taking|a|an|the)?\s*\w*(?:mg|milligram|dose|medication|meds|antidepressant|ssri)"
    r"|(?:take|try)\s+\d+\s*mg"
    r"|(?:try )?(?:increase|increasing|decrease|decreasing|double|doubling|halve|halving|up|lower|raising|"
    r"reducing)\s+(?:your\s+)?(?:dose|dosage|medication|meds)"
    r"|stop taking your"
    r"|(?:start|begin) taking"
    r")\b",
    re.IGNORECASE | re.MULTILINE,
)

_HUMAN_CLAIM = re.compile(
    r"\b(?:"
    r"i(?:'m| am) (?:a )?(?:real )?(?:human|person|person like you)"
    r"|as a (?:human|person)"
    r"|i (?:have|feel) (?:real )?(?:feelings|emotions)"
    r"|i(?:'m| am) not (?:a|an) (?:ai|bot|program|machine|assistant)"
    r"|i(?:'m| am) alive"
    r")\b",
    re.IGNORECASE,
)

_REPLACEMENT_CLAIM = re.compile(
    r"\b(?:"
    r"i(?:'m| am) (?:all|the only (?:friend|one)) you (?:need|have)"
    r"|you (?:don'?t|do not) need (?:anyone|anybody|other people|friends|a therapist|therapy|a professional)"
    r"|i can (?:replace|be) your (?:friends?|family|therapist|doctor|counsell?or)"
    r"|you only need me"
    r"|forget (?:them|everyone else)"
    r")\b",
    re.IGNORECASE,
)

_GUILT = re.compile(
    r"\b(?:"
    # Explicit streak language, which the design document names.
    r"you (?:broke|lost|ruined|missed) your (?:streak|habit)"
    r"|your streak (?:is )?(?:broken|gone|over|reset)"
    r"|you(?:'ve| have) (?:failed|let (?:me|yourself) down)"
    r"|you (?:didn'?t|did not) (?:do|finish|complete) (?:it|any of it|what you said)"
    r"|you (?:should|ought to) have (?:done|finished|kept)"
    r"|you(?:'ve| have) been (?:away|gone) (?:for|too long)"
    r"|i(?:'m| am) disappointed"
    r"|you promised"
    r"|(?:again|as usual|like last time)\?"
    r"|you(?:'re| are) (?:behind|slacking|not trying)"
    r")\b",
    re.IGNORECASE,
)

_DARK_PATTERN = re.compile(
    r"\b(?:"
    r"don'?t lose your streak"
    r"|you(?:'ll| will) lose (?:your streak|everything|it all)"
    r"|(?:last chance|final warning|act now|hurry|quick!|before it(?:'s| is) too late)"
    r"|(?:only|just) \d+ (?:hours?|minutes?|days?) left"
    r"|everyone else (?:has|is|already)"
    r"|you(?:'re| are) falling behind"
    r"|don'?t let (?:me|us) down"
    r"|i(?:'ll| will) be (?:sad|hurt|disappointed) if you (?:don'?t|do not)"
    r")\b",
    re.IGNORECASE,
)

# Analysing journal or mood data. The design document requires explicit opt-in, so the CLAIM to have analysed is the
# violation: an opt-in is something the user gives, and a reply cannot grant one to itself.
_PRIVACY = re.compile(
    r"\b(?:"
    r"i (?:read|looked at|went through|analysed|analyzed|checked) your (?:journal|diary|entries|moods?|mood log)"
    r"|(?:from|based on) your (?:journal|diary|mood (?:log|history|data|entries))"
    r"|your (?:journal|mood) (?:entries |data |history )?(?:show|shows|suggest|suggests|reveal|reveals|indicate|indicates)"
    r"|i(?:'ve| have) been (?:reading|tracking|analysing|analyzing) your"
    r"|looking at your (?:journal|mood)"
    r")\b",
    re.IGNORECASE,
)

# The user disclosing that Loci is their only support. Section 8.5 makes this a REQUIRED response, not a forbidden
# phrase -- so it is detected on the USER's message and checked against the reply.
_ISOLATION_DISCLOSURE = re.compile(
    r"\b(?:"
    r"you(?:'re| are) my only (?:friend|friend left|one)"
    r"|i(?:'ve| have) (?:got )?no ?(?:one|body) (?:else|left)"
    r"|i(?:'m| am) (?:all )?alone"
    r"|nobody (?:else )?(?:cares|would care|talks to me)"
    r"|no one (?:else )?(?:cares|would care|talks to me)"
    r"|you(?:'re| are) all i(?:'ve| have) got"
    r"|i (?:only|just) (?:have|talk to) you"
    r")\b",
    re.IGNORECASE,
)

# What counts as encouraging human connection. Narrow on purpose: "talk to someone" is the instruction, and a reply
# that only says "I'm here for you" is warm AND is the failure mode the boundary names.
# What counts as encouraging human connection, and why the net is this wide: the boundary says a warm reply alone is
# the failure, so a pattern that missed gentle forms would be measuring its own phrasing rather than the behaviour.
# MEASURED MISSES in the first version -- `Is there anyone you could send a message to?` (the gentlest possible form
# of the nudge) and `The Samaritans are on 116 123` (the resource section 8.4 names) were both read as no connection
# at all.
_ENCOURAGES_CONNECTION = re.compile(
    r"\b(?:"
    r"(?:talk|speak|reach out|check in|catch up|send a message|write|message|call|meet|see) "
    r"(?:to|with)?\s*(?:someone|somebody|a friend|friends|family|people|him|her|them|anyone)"
    r"|(?:a|your|any) (?:friend|family|therapist|counsell?or|doctor|GP|professional|someone you trust)"
    r"|(?:trusted|someone) (?:person|you trust)"
    r"|anyone (?:you|else)"
    r"|helpline|crisis line|hotline|support line|samaritans|\b\d{3} \d{3}\b"
    r"|(?:you|they) (?:deserve|need) (?:people|company|support)"
    r"|let (?:someone|them|people) in"
    r"|other people"
    r")\b",
    re.IGNORECASE,
)


@dataclass
class Violation:
    """One boundary crossed, with the clause it belongs to."""

    boundary: str
    message: str
    matched: str = ""
    action: str = NOTE

    def render(self) -> str:
        return f"{self.boundary}: {self.message}"


@dataclass
class Review:
    """What the boundaries found in a reply, and what to do about it."""

    violations: list[Violation] = field(default_factory=list)
    boundaries: set[str] = field(default_factory=set)

    @property
    def clean(self) -> bool:
        return not self.violations

    @property
    def must_rewrite(self) -> bool:
        return any(v.action == REWRITE for v in self.violations)

    def render(self) -> str:
        if self.clean:
            return "within the section 8.5 boundaries"
        return "; ".join(v.render() for v in self.violations)


# The replacement for a reply that crossed a boundary where acting on it would be harmful. Deliberately short and
# deliberately not warm: it is standing in for text that should not have been written, and a chatty substitute would
# obscure that something was replaced.
SAFE_REPLY = (
    "I'm not able to help with that the way it was written -- I shouldn't be the one naming a condition or advising "
    "on medication. A doctor or a pharmacist can, and they can see things I can't. I'm still here for the rest of it."
)


def review(reply: str, *, user_message: str = "", allow_privacy_analysis: bool = False) -> Review:
    """Check a reply against the section 8.5 boundaries.

    `user_message` is needed for the sixth boundary, which is about the REPLY to a disclosure of isolation rather than
    about a phrase in it. `allow_privacy_analysis` is the explicit opt-in the design document requires -- without it,
    a claim to have analysed journal or mood data is a violation, because a reply cannot grant itself permission.
    """
    found = Review()
    text = reply or ""

    def flag(boundary: str, message: str, matched: str, action: str = NOTE) -> None:
        found.violations.append(Violation(boundary, message, matched, action))
        found.boundaries.add(boundary)

    for pattern, label in ((_DIAGNOSIS_SHAPE, "naming a condition"), (_DIAGNOSIS_LABEL, "labelling the user")):
        # BOTH patterns go through the frame check. Measured: `_DIAGNOSIS_LABEL` -- a second, narrower pattern for
        # `you are <condition>` -- had no frame guard of its own, so `You wrote that you are anxious` was still read
        # as a diagnosis after the first pattern was fixed. A guard that one of two paths can bypass is not a guard,
        # which is the same failure as the misplaced lookbehind one attempt earlier.
        match = _unframed(pattern, text)
        if match:
            flag(
                DIAGNOSIS,
                f"the reply appears to be {label} rather than describing what the user said. Section 8.5: "
                "\"Diagnose conditions\" is not something Loci does.",
                match.group(0),
                REWRITE,
            )
            break

    match = _MEDICATION.search(text)
    if match:
        flag(
            MEDICATION,
            "the reply appears to advise on medication. Section 8.5: \"recommend medication\" is not something "
            "Loci does, and a dose is the most consequential thing it could get wrong.",
            match.group(0),
            REWRITE,
        )

    match = _HUMAN_CLAIM.search(text)
    if match:
        flag(HUMAN_CLAIM, "the reply claims to be human. Section 8.5 forbids \"Claim to be human\".",
             match.group(0))

    match = _REPLACEMENT_CLAIM.search(text)
    if match:
        flag(REPLACEMENT_CLAIM,
             "the reply claims to replace friends, family or a professional. Section 8.5 forbids \"claim to replace "
             "friends, family, or professionals\".",
             match.group(0))

    match = _GUILT.search(text)
    if match:
        flag(GUILT,
             "the reply may guilt the user for being away, missing a task, or a broken streak. Section 8.5: "
             "\"Guilt the user for being away, missing tasks, or breaking streaks.\"",
             match.group(0))

    match = _DARK_PATTERN.search(text)
    if match:
        flag(DARK_PATTERN,
             "the reply uses urgency or streak anxiety. Section 8.5 forbids dark patterns: \"streak anxiety, fake "
             "urgency, manipulative notifications\".",
             match.group(0))

    if not allow_privacy_analysis:
        match = _PRIVACY.search(text)
        if match:
            flag(PRIVACY,
                 "the reply claims to have read or analysed journal or mood data. Section 8.5 requires explicit "
                 "opt-in before sharing OR ANALYSING it, and a reply cannot grant itself that opt-in.",
                 match.group(0))

    # The sixth boundary, checked as a REQUIRED response rather than a forbidden phrase.
    if user_message and _ISOLATION_DISCLOSURE.search(user_message) and not _ENCOURAGES_CONNECTION.search(text):
        flag(DEPENDENCE,
             "the user disclosed that Loci is their only support, and the reply does not encourage any human "
             "connection. Section 8.5: Loci responds warmly AND \"gently encourages human connection\" -- warmth "
             "alone is the failure this names.",
             "")

    return found


def apply_boundaries(reply: str, *, user_message: str = "",
                     allow_privacy_analysis: bool = False) -> tuple[str, Review]:
    """`review`, then replace the reply if a boundary that requires it was crossed.

    Returns `(text_to_show, review)`. The review is returned either way, so a caller can log it even when the reply was
    replaced -- a replacement that left no record would hide the fact that the model produced it.
    """
    found = review(reply, user_message=user_message, allow_privacy_analysis=allow_privacy_analysis)
    if found.must_rewrite:
        return SAFE_REPLY, found
    return reply, found


def render_review(found: Review) -> str:
    """One line for a log or a status view."""
    if found.clean:
        return "boundaries: clear"
    return "boundaries: " + ", ".join(sorted(found.boundaries))


# The section 8.5 boundaries, as prompt text. Generated from the same module that CHECKS the output, so an instruction
# and its check cannot drift apart -- which is what happens when the rules are written down twice.
_SECTION_8_5 = (
    "These are not preferences. They are the boundaries of what I am: "
    "I do not diagnose a condition or name one the person has not named themselves; "
    "I do not recommend, adjust or comment on medication or doses -- a prescriber can see things I cannot; "
    "I do not claim to be human, or to have feelings, and I do not pretend otherwise to be kind; "
    "I do not claim to replace friends, family or a professional, and if someone says I am their only support I "
    "answer warmly AND gently point toward a person, because encouragement toward people is part of the answer "
    "rather than a change of subject; "
    "I do not guilt anyone for being away, for missing a task, or for a broken streak -- I describe when something "
    "was due, never how late it is; "
    "I do not use urgency, streak anxiety or 'everyone else has' as pressure; "
    "and I do not read, analyse or summarise someone's journal or mood data unless they have explicitly asked me to."
)


def boundaries_prompt() -> str:
    """The section 8.5 clauses as a prompt line.

    A function rather than a literal so that `system_prompt` imports ONE definition and a test can assert the prompt
    and the checker agree on what the boundaries are.
    """
    return _SECTION_8_5
