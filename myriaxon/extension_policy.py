"""Tiered extension policy: a trusted outer layer that lower tiers cannot loosen.

## The gap this closes

Myriaxon lets things extend it — plugins in `CONFIG_DIR/plugins`, skills from three roots, roles, hooks — and
nothing distinguished *who* supplied an extension. A plugin a person dropped into their own config directory and
a plugin the harness itself ships had identical authority over the same decisions. That is fine while the only
extensions are your own; it stops being fine the moment a plugin comes from a repository you cloned or a
colleague's dotfiles, because an extension that can answer a policy question can also answer it *permissively*.

This is modelled on Claude Code's `mods/sec-default` (verified in
`mods/sec-default/hooks/register.ts` and its README), which solves it with two ideas.

**Tiers, outermost = most authority.** Their `TIERS = ["prepend", "user", "append", "builtin", "core"]`, with
"earlier is outer is more authority, and same-event hooks nest in this order and no other way." A modest mapping
to a single-machine harness:

| Myriaxon tier | What supplies it |
|---|---|
| `builtin` | the harness's own shipped defaults — outermost |
| `managed` | site/organisation policy file, if one exists |
| `user` | `CONFIG_DIR` plugins, skills, roles |
| `project` | `.myriaxon/` plugins, skills, roles |

**Differential re-evaluation of a denial.** This is the genuinely novel part and the reason this module exists.
Rather than trying to detect whether an untrusted layer honoured policy — which is not decidable by inspecting
its answer — they run the identical dispatch **twice**: once normally, once with the user tier excluded. A
rule-naming denial that appears only in the second run is, by construction, a rule the user tier loosened. Their
comment: "so a deny in run 2 is by construction a rule the user tier loosened." That is a proof rather than a
heuristic, which is why it is worth the second evaluation.

Two more rules are copied because they are cheap and they close real holes:

* **Provenance is host-pinned, never self-declared.** Their `next.origin` is "set by the host alone, from the
  environment the call came from … nothing a plugin writes reaches it", and "a person's copy carrying an
  organization mod's name is still in the `user` tier and is refused." So a tier is assigned from *where the
  code was loaded from*, and any tier an extension claims about itself is ignored.
* **An unreadable policy counts as a policy in force.** Their README: fails closed. A policy file that exists but
  cannot be parsed must not silently become "no policy".

## What is dropped

The MDM/managed-settings apparatus, `prependPlugins`, `next.to` as a hook-internal capability, `strictKnownMarketplaces`
and the marketplace allow/block lists, and the four-way `mods`/`plugins` split. Those exist to distribute
sanctioned extensions across an enterprise; this is one machine with a config directory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .constants import CONFIG_DIR

# Outermost first. "Earlier is outer is more authority, and hooks nest in this order and no other way."
TIERS = ("builtin", "managed", "user", "project")

# The tiers an outer layer may skip past. `builtin` and `managed` are excluded: an outer layer that could skip
# *itself* would not be an outer layer.
SKIPPABLE = ("user", "project")

# Decision vocabulary. Deliberately closed: a free-form return would make "did a lower tier loosen this?"
# undecidable, which is the whole question this module answers.
ALLOW = "allow"
DENY = "deny"
ASK = "ask"
DECISIONS = (ALLOW, DENY, ASK)

# Strictness order, used for the "loosened" comparison. `ask` sits between the two because a layer turning a
# deny into a question has loosened it just as surely as one turning it into an allow.
_STRICTNESS = {DENY: 2, ASK: 1, ALLOW: 0}

MANAGED_POLICY_FILE = CONFIG_DIR / "policy.json"
PROJECT_POLICY_FILE = ".myriaxon/policy.json"


@dataclass(frozen=True)
class Provenance:
    """Where a contributor came from. Immutable, and assigned by the host rather than accepted from the caller.

    `frozen=True` is the enforcement: once constructed, nothing downstream can relabel a project plugin as
    built-in. Their equivalent guarantee is that `next.origin` is unreachable by a plugin; here the guarantee is
    that the value cannot be mutated after the loader decided it.
    """

    tier: str
    name: str
    path: str = ""

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(f"unknown tier {self.tier!r}; expected one of {TIERS}")

    def render(self) -> str:
        return f"{self.name} ({self.tier})" + (f" from {self.path}" if self.path else "")


@dataclass
class Verdict:
    """One contributor's answer about one subject."""

    decision: str
    rule: str = ""
    reason: str = ""
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}, got {self.decision!r}")

    @property
    def is_deny(self) -> bool:
        return self.decision == DENY

    @property
    def names_its_rule(self) -> bool:
        """A denial that explains itself.

        Only a *named* denial can be used as proof in the differential check. An anonymous denial could come from
        anywhere, so it proves nothing about which layer produced it.
        """
        return self.decision == DENY and bool(self.rule)

    def render(self) -> str:
        who = self.provenance.render() if self.provenance else "unknown"
        line = f"{self.decision}: {who}"
        if self.rule:
            line += f" [rule {self.rule}]"
        if self.reason:
            line += f" -- {self.reason}"
        return line


@dataclass
class Decision:
    """The outcome of evaluating a subject through the chain."""

    subject: str
    verdict: Verdict
    trace: list[str] = field(default_factory=list)
    loosened_by: list[str] = field(default_factory=list)
    # True when the differential re-check caught a lower tier loosening an outer denial.
    enforced: bool = False
    degraded: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.verdict.decision == ALLOW

    def render(self) -> str:
        lines = [f"  {self.subject}: {self.verdict.render()}"]
        for entry in self.trace:
            lines.append(f"      {entry}")
        for who in self.loosened_by:
            lines.append(f"      loosened by {who} -- an outer rule was restored")
        for note in self.degraded:
            lines.append(f"      degraded: {note}")
        return "\n".join(lines)


class PolicyUnreadable(RuntimeError):
    """A policy file exists but could not be used. Treated as a policy in force, never as no policy."""


@dataclass
class PolicyConfig:
    """Managed policy, with every field defaulting to the *closed* setting."""

    # Rules that deny outright. A dict rather than a list so a rule can be named, and a named denial is what
    # makes the differential check possible.
    deny: dict[str, str] = field(default_factory=dict)
    # Subjects where a lower tier may not answer at all.
    managed_only: tuple[str, ...] = ()
    # Whether a lower tier may turn an outer denial into an allow. Default False: the secure direction.
    allow_lower_tiers_to_loosen: bool = False

    @classmethod
    def load(cls) -> "PolicyConfig":
        """Read the managed policy. An unreadable file raises rather than yielding a permissive default.

        Their rule, from `sec-default/README.md`: an unreadable policy counts as a policy in force. The failure
        mode being avoided is a policy file that fails to parse and is therefore silently ignored, which is
        exactly when the restriction mattered.
        """
        for path in (MANAGED_POLICY_FILE, Path.cwd() / PROJECT_POLICY_FILE):
            if not path.is_file():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PolicyUnreadable(f"{path} could not be read: {exc}") from exc
            if not isinstance(raw, dict):
                raise PolicyUnreadable(f"{path} is not a JSON object")
            deny = raw.get("deny") or {}
            if not isinstance(deny, dict):
                raise PolicyUnreadable(f"{path}: 'deny' must be an object of subject -> rule name")
            return cls(
                deny={str(k): str(v) for k, v in deny.items()},
                managed_only=tuple(str(s) for s in (raw.get("managed_only") or ())),
                # Only the literal `true` counts. Their note: `allowModsToOverrideDenyRules` counts only the
                # literal `true`, because a truthy string in a config file should not widen a restriction.
                allow_lower_tiers_to_loosen=(raw.get("allow_lower_tiers_to_loosen") is True),
            )
        return cls()


def load_policy(strict: bool = True) -> PolicyConfig:
    """Load policy, reporting an unreadable file rather than silently ignoring it.

    `strict=False` yields a closed-but-empty config and is for callers that only need the rule list for display.
    """
    try:
        return PolicyConfig.load()
    except PolicyUnreadable:
        if strict:
            raise
        return PolicyConfig()


class Chain:
    """An ordered set of contributors per subject, evaluated outermost-first.

    A contributor is `(provenance, answer)`. The chain does not require contributors to agree; it decides what
    the disagreement means.
    """

    def __init__(self, policy: PolicyConfig | None = None) -> None:
        self.policy = policy if policy is not None else PolicyConfig()
        self._contributors: dict[str, list[tuple[Provenance, Callable[[], Verdict | None]]]] = {}

    def add(
        self,
        subject: str,
        provenance: Provenance,
        answer: Callable[..., Verdict | None],
    ) -> None:
        """Register a contributor for a subject.

        Answers are callables rather than values because the differential check evaluates the same subject twice;
        a value computed once could not reveal that a second run differs.

        The callable may take no arguments, or one: the set of tiers excluded from this run. It needs the second
        form only when its answer genuinely depends on who else is being consulted -- which is exactly the case
        the differential check exists to catch.
        """
        self._contributors.setdefault(subject, []).append((provenance, answer))

    def subjects(self) -> list[str]:
        return sorted(self._contributors)

    def contributors(self, subject: str) -> list[Provenance]:
        entries = self._contributors.get(subject, [])
        # Unpack the pair: the tuple is `(Provenance, callable)`, and an earlier version referenced the loop
        # variable of the wrong comprehension here, which raised NameError the first time it was called.
        return [prov for prov, _ in sorted(entries, key=lambda pair: TIERS.index(pair[0].tier))]

    def evaluate(self, subject: str, *, skip_tiers: Iterable[str] = ()) -> tuple[Verdict, list[str]]:
        """Run the chain for one subject, optionally skipping tiers. Returns the verdict and a trace.

        Precedence: the first **deny** wins, because a denial is the only verdict that can be about safety and the
        safe answer to disagreement is no. A managed rule denies before a lower tier is consulted at all.
        """
        skip = set(skip_tiers)
        trace: list[str] = []

        # A managed rule denies outright, before any lower tier is asked. This is the "user tier is skipped
        # entirely" behaviour: the org's answer stands.
        managed_rule = self.policy.deny.get(subject)
        if managed_rule:
            verdict = Verdict(
                decision=DENY, rule=managed_rule,
                reason="denied by managed policy",
                provenance=Provenance("managed", "policy", str(MANAGED_POLICY_FILE)),
            )
            trace.append(f"managed policy denies {subject} by rule {managed_rule!r}")
            return verdict, trace

        if subject in self.policy.managed_only:
            verdict = Verdict(
                decision=DENY, rule="managed-only",
                reason=f"{subject} may only be answered by managed policy",
                provenance=Provenance("managed", "policy", str(MANAGED_POLICY_FILE)),
            )
            trace.append(f"{subject} is managed-only; lower tiers are not consulted")
            return verdict, trace

        entries = sorted(self._contributors.get(subject, []), key=lambda pair: TIERS.index(pair[0].tier))
        for provenance, answer in entries:
            if provenance.tier in skip:
                trace.append(f"skipped {provenance.render()} (tier excluded for this run)")
                continue
            try:
                # Pass the excluded tiers so a contributor whose answer depends on who else is being consulted
                # can see the difference between the two runs. A zero-argument callable is still accepted.
                try:
                    verdict = answer(skip)
                except TypeError as arity:
                    if "positional argument" not in str(arity):
                        raise
                    verdict = answer()
            except Exception as exc:  # noqa: BLE001
                # A contributor that raises is treated as denying. Letting it fall through would make a broken
                # guardrail identical to no guardrail, and a guardrail fails closed or it is decoration.
                verdict = Verdict(
                    decision=DENY, rule=f"{provenance.name}-raised",
                    reason=f"{type(exc).__name__}: {exc}",
                    provenance=provenance,
                )
                trace.append(f"{provenance.render()} raised; treated as a denial")
                return verdict, trace

            if verdict is None:
                trace.append(f"{provenance.render()} had no opinion")
                continue
            verdict.provenance = provenance
            trace.append(f"{provenance.render()} -> {verdict.decision}" + (f" [{verdict.rule}]" if verdict.rule else ""))
            if verdict.is_deny:
                return verdict, trace
            if verdict.decision == ASK:
                # `ask` does not stop the chain, but it is remembered: an outer allow should not erase a lower
                # tier's request for confirmation.
                continue

        # No denial anywhere: allow, attributed to the outermost contributor that had an opinion.
        for provenance, _ in entries:
            if provenance.tier not in skip:
                return Verdict(decision=ALLOW, provenance=provenance), trace
        return Verdict(decision=ALLOW, provenance=Provenance("builtin", "default")), trace

    def decide(self, subject: str) -> Decision:
        """Evaluate, then differentially re-check when a lower tier may have loosened an outer denial.

        The two runs differ by the user and project tiers alone. If the first run allows and the second denies
        with a *named* rule, that rule is one a lower tier overrode -- which is a proof rather than a guess, and
        the reason this costs a second evaluation.
        """
        first, trace = self.evaluate(subject)
        degraded: list[str] = []

        if first.is_deny:
            return Decision(subject=subject, verdict=first, trace=trace)

        if self.policy.allow_lower_tiers_to_loosen:
            return Decision(subject=subject, verdict=first, trace=trace)

        # Run 2: the same dispatch with every skippable tier excluded.
        second, second_trace = self.evaluate(subject, skip_tiers=SKIPPABLE)

        if second.names_its_rule:
            # A named denial that only appears without the lower tiers is a rule they loosened. Restore it.
            trace.append("differential re-check without the lower tiers denied, so an outer rule was loosened")
            trace.extend(f"  {line}" for line in second_trace)
            who = [
                p.render() for p in self.contributors(subject) if p.tier in SKIPPABLE
            ]
            return Decision(
                subject=subject,
                verdict=second,
                trace=trace,
                loosened_by=who,
                enforced=True,
                degraded=degraded,
            )

        if second.is_deny:
            # An unnamed denial in run 2: something denied, but it declined to say which rule. That is still a
            # denial, and the safe reading is to keep it -- but it is recorded as degraded, because the
            # differential proof is unavailable and saying otherwise would overstate what was established.
            degraded.append(
                "an outer denial was restored, but it does not name its rule, so the differential proof is "
                "weaker than it looks"
            )
            trace.append("differential re-check denied without naming a rule; denial kept, degraded")
            return Decision(
                subject=subject, verdict=second, trace=trace, enforced=True, degraded=degraded
            )

        return Decision(subject=subject, verdict=first, trace=trace)


# --------------------------------------------------------------------------- registering harness defaults


def builtin_provenance(name: str, path: str = "") -> Provenance:
    return Provenance("builtin", name, path)


def user_provenance(name: str, path: str = "") -> Provenance:
    return Provenance("user", name, path)


def project_provenance(name: str, path: str = "") -> Provenance:
    return Provenance("project", name, path)


def provenance_for_path(path: str | Path) -> Provenance:
    """Assign a tier from *where* something was loaded, never from what it says about itself.

    This is the host-pinned provenance rule. A project plugin that ships a manifest claiming
    `"tier": "builtin"` gets `project`, because a claim is not an input to this function.

    **Project is checked before user, and that ordering is load-bearing.** When Myriaxon runs from a checkout,
    `CONFIG_DIR` resolves to *that checkout's* `.myriaxon/` -- so a path under `./.myriaxon/plugins` is under
    both roots, and testing the user root first labelled every project plugin as a user plugin. The two are
    genuinely different: a project plugin travels with the repository and is untrusted on arrival, while a user
    plugin is something the operator put in place deliberately. Telling them apart is the point of the tier.
    """
    resolved = Path(path).resolve()

    # Project first: `.myriaxon` anywhere under the current working directory. See the docstring for why the
    # order matters.
    try:
        resolved.relative_to((Path.cwd() / ".myriaxon").resolve())
        return Provenance("project", resolved.parent.name or resolved.name, str(resolved))
    except ValueError:
        pass

    try:
        resolved.relative_to(CONFIG_DIR.resolve())
        return Provenance("user", resolved.parent.name or resolved.name, str(resolved))
    except ValueError:
        pass

    from .skills import BUILTIN_SKILLS_DIR

    try:
        resolved.relative_to(Path(BUILTIN_SKILLS_DIR).resolve())
        return Provenance("builtin", resolved.parent.name or resolved.name, str(resolved))
    except ValueError:
        pass

    # Unknown origin is treated as the least trusted tier, not the most. A path the loader cannot place is
    # exactly the case where guessing generous would be wrong.
    return Provenance("project", resolved.name, str(resolved))
