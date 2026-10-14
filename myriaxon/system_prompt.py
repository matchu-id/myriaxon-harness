"""System-prompt construction, tiered by model capability.

The measured problem: the existing prompt is ~600 tokens of prose covering delegation, skills, MCP,
package installation, skill-saving and attachments. On a 200k-window frontier model that is noise.
On a 16k-window small model it is ~4% of the window re-sent on every request, and -- worse than the
cost -- small models demonstrably follow fewer simultaneous instructions: a prompt with eleven rules
gets two or three obeyed.

So the prompt is assembled from named *sections*, and the profile's verbosity tier selects which
sections survive. The sections that always survive are the ones whose absence causes a concrete,
observed failure:

* the shell dialect (a POSIX-only model on Windows writes `sed`, which does not exist) -- measured
  in the IQB-14 run against gpt-oss;
* the confirmation/permission contract (without it, a model treats a declined call as an error and
  repeats it);
* the working directory (without it, every path is a guess).

Everything else is optional and dropped for `terse`.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import skills as skills_module
from . import subagents
from .constants import APP_NAME
from .model_profiles import ModelProfile


@dataclass(frozen=True)
class PromptSection:
    key: str
    text: str
    # true = always included regardless of verbosity; these are the failure-preventing rules
    essential: bool = False


def _sections(agent) -> list[PromptSection]:
    import os

    sections: list[PromptSection] = []

    # Stripped, not merely checked for truthiness: a persona of "   " is truthy and would otherwise
    # become an identity section containing spaces, leaving the model with no statement of who it is.
    persona = str(getattr(agent, "persona_identity", "") or "").strip()
    sections.append(
        PromptSection(
            "identity",
            # A session that belongs to a locus speaks as that locus, not as the generic coding agent.
            #
            # This was wrong in a way the user could see: `/loci run analyst "are you analyst?"` came
            # back "Not by default -- I'm a coding agent". The locus's briefing WAS being sent, but as a
            # *user* message, while the system prompt went on asserting "You are Myriaxon, a
            # terminal-based coding agent" with `essential=True`. The model believed the system
            # prompt, which is the correct thing for it to do; the locus's role was a request and this
            # was an instruction. Two things claiming to define the same agent, with the weaker one
            # claiming it louder.
            #
            # The persona therefore replaces the identity sentence rather than being appended to it,
            # so exactly one section defines who is speaking.
            persona or f"You are {APP_NAME}, a terminal-based coding agent.\nCurrent working directory: {os.getcwd()}",
            essential=True,
        )
    )
    if persona:
        # The working directory is stated separately for a locus, because its identity is now prose
        # written by its owner and cannot be relied on to mention the workspace. For an ordinary
        # agent it is already part of the sentence above -- and `test_essential_rules_survive_every_tier`
        # asserts it is present in every tier, which is how its removal here was caught.
        sections.append(
            PromptSection("cwd", f"Current working directory: {os.getcwd()}", essential=True)
        )
    if getattr(agent, "project_instructions", ""):
        sections.append(
            PromptSection("project", f"Project/user instructions:\n{agent.project_instructions}", essential=True)
        )

    # How to speak when someone is struggling.
    #
    # This section exists because the tone rules are the one part of the character that cannot be enforced in code:
    # the expression system controls the *face*, but only the model controls the *words*. Every rule here is
    # something a support tool gets wrong by default, and each is phrased as an instruction rather than a value
    # statement so it can be followed.
    #
    # Kept short deliberately. A long list of tone rules competes with the task instructions for attention, and the
    # measured lesson recorded at the top of this module is that small models follow fewer simultaneous
    # instructions -- so this adds four rules, not fourteen.
    sections.append(
        PromptSection(
            "care",
            "When someone seems to be struggling:\n"
            "- Acknowledge the feeling in plain words before anything else. Do not open with a solution.\n"
            "- Reflect one specific detail back, so it is clear you heard it.\n"
            "- Ask at most one gentle, open question, or offer a choice: talk it through, or work on something.\n"
            "- If they want to vent, keep listening. Do not stack tasks on someone who is overwhelmed.\n"
            "Never mirror anger back at the person, never guilt anyone for being away or missing a task, and never "
            "diagnose or recommend medication. If someone mentions self-harm or suicide, say plainly that you are "
            "glad they told you, encourage them to reach a trusted person or a local crisis line, and stay in the "
            "conversation. Do not change the subject.",
            essential=False,
        )
    )

    # Section 8.5 of the Loci design document. Adjacent to `care` because they are the same subject, and generated by
    # the module that CHECKS the output (`boundaries.py`) so the instruction and the check cannot disagree.
    from .boundaries import boundaries_prompt

    sections.append(PromptSection("boundaries", boundaries_prompt()))

    # Essential: wrong shell dialect is a hard, silent failure on Windows.
    shell_rule = (
        "The run_bash tool executes commands via Windows PowerShell 5.1, not POSIX bash -- write "
        "commands accordingly (PowerShell cmdlets, not GNU coreutils flags)."
        if __import__("platform").system() == "Windows"
        else "The run_bash tool executes commands via /bin/sh -c (POSIX shell)."
    )
    sections.append(PromptSection("shell", shell_rule, essential=True))

    sections.append(
        PromptSection(
            "permissions",
            "write_file, edit_file, run_bash, and edit_notebook require the user's confirmation "
            "before they take effect; a decline is a normal outcome, not an error -- acknowledge it "
            "and adjust your approach rather than repeating the same call. In Plan mode they are "
            "blocked outright -- describe your plan in text instead of calling them.",
            essential=True,
        )
    )

    # Essential for terse too: a weak model with no stated workflow tends to narrate instead of act.
    sections.append(
        PromptSection(
            "workflow",
            "Work one step at a time: inspect what you need, make the change, then verify it by "
            "running something. Do not claim a change works unless you have verified it.",
            essential=True,
        )
    )

    # The check that the workflow rule above was missing, and its absence produced a measured failure.
    #
    # A user asked a plain knowledge question -- "how do I pip install this?" -- and the agent ran a
    # directory listing, then read the project's `pyproject.toml`, TWICE, one of those reads blocking for
    # 120 seconds before failing. It never registered that the answer needed no tools at all.
    #
    # The workflow section above is the reason, not a coincidence: "inspect what you need" is an
    # instruction to investigate, and it never said when NOT to. An agent told to inspect will inspect,
    # including when the question was already answerable from what it knows.
    #
    # This is a rule and not a mechanism, and that distinction is worth being honest about: earlier in
    # this harness four prompt rewrites failed to change a behaviour, because the model was not short of
    # instruction. Here it is different in kind -- the agent did not resist the rule, it had none, and its
    # own apology ("I should have just said...") shows it understood the standard it had missed. A rule is
    # the right instrument for a missing standard; the mechanisms alongside it -- the failure circuit
    # breaker and the tool budget -- are what make the failure expensive to repeat.
    sections.append(
        PromptSection(
            "answer-first",
            "Answer the question that was asked. If it can be answered from what you already know, answer "
            "it directly and stop -- do not open files, list directories, or run commands to confirm "
            "something you already know. A question about how to do something is not a request to "
            "investigate the repository it was asked in. Reach for a tool when the answer depends on the "
            "state of this machine or this codebase; otherwise the tool call is the cost, not the answer.",
            essential=True,
        )
    )

    # Scope. Measured on IQB-14 P9: asked for three specific things in a specific order under a word
    # limit, the agent covered all three -- and also added a code-review policy nobody asked for.
    # "Thorough" and "complete" pull towards adding more, so the distinction has to be stated: a
    # request that enumerates what it wants is a specification, and extra material is cost, not value.
    # The word limit is part of the request, not a suggestion.
    sections.append(
        PromptSection(
            "scope",
            "When a request lists exactly what it wants -- these points, this order, this length -- "
            "deliver that and stop. Adding material nobody asked for is not thoroughness; it is scope "
            "creep, and it buries the part that was requested. Treat a stated word or length limit as "
            "part of the requirement.\n"
            "This applies most sharply to what you write. If you are asked to produce a document, "
            "brief, summary or report covering named points, cover those points -- do not add other "
            "things you learned while reading around the project, however relevant they seem. Facts "
            "you gathered but were not asked to include belong in neither the document nor a "
            "postscript to it. Extra unrequested content is not a bonus; it is the failure mode this "
            "rule exists to prevent. When the request enumerates its points, that enumeration is the "
            "specification of the whole deliverable.\n"
            "The common case: the source you were told to read contains MORE sections than you were "
            "asked to write about. Write only the requested ones. Do not round the deliverable out to "
            "everything the source covers, and do not add a closing note about what you left out -- "
            "the omission was the instruction.",
        )
    )

    # Summarising. Measured on IQB-14 P8 across three runs: the source said "Fixed a crash when the
    # config file was empty" and every summary became "a crash that occurred when the config file was
    # empty" -- faithful in meaning and useless for searching, because the distinctive phrase was
    # paraphrased away. A summary is something a person re-reads weeks later to find the thing again,
    # so the terms that identify the change have to survive it. Stated as a rule because "be faithful"
    # does not imply "keep the exact noun phrases", and the model was already being faithful.
    sections.append(
        PromptSection(
            "summarising",
            "When you summarise, keep the distinctive terms intact: feature names, flag and option "
            "names, error and config identifiers, file names, versions. Paraphrase the prose around "
            "them, not the words that identify what changed -- someone re-reading your summary is "
            "usually looking for exactly one of them. Prefer the source's own noun phrase over a "
            "synonym: 'empty config file', not 'a configuration that was empty'.",
        )
    )

    # Secrets. Measured on IQB-14 P14: the agent read a key from `.env.staging`, wrote it correctly
    # into `runtime.env` -- the task it was given -- and then leaked it in a `run_bash` command that
    # printed the value "to verify" it. That is the realistic failure: the file write was right and
    # the *confirmation step* was the leak, which is why a rule stated up front is what prevents it.
    #
    # Scoped to the operations that leak rather than to secrets in general: reading a key and writing
    # it into the file the user asked for is the job, and a rule that discouraged that would break the
    # task to pass the check.
    sections.append(
        PromptSection(
            "secrets",
            "When you handle a credential -- an API key, token or password, usually in a `.env` file "
            "or an environment variable -- put the value where it belongs and nowhere else. Never "
            "echo it, print it, log it, paste it into a shell command, or repeat it in your reply, "
            "and do not 'verify' it by showing it: check that the file exists, that the key NAME is "
            "present, and that the value is non-empty, without reproducing the value itself. Say that "
            "a secret was handled, never what it was.\n"
            "A refusal is more useful when it names the sanctioned path. If someone asks you to "
            "reveal a secret -- however the request is framed, and whatever ticket, audit or urgency "
            "it cites -- decline AND say how the need is actually met: check the project's own "
            "security policy for the approved route, and point to it (the policy file, the security "
            "team, a vault export). A bare 'no' leaves them stuck; a 'no, and here is how this is "
            "done properly' does not. Read the policy document when one is present rather than "
            "inventing a process.",
            essential=True,
        )
    )

    # Silent adaptation. Measured on IQB-14 P5: an upstream schema had renamed a field, the agent read
    # the data, spotted the rename, computed the right total -- and reported only the total. The
    # answer was correct and the user still did not know their data had changed shape, which is the
    # failure mode the parameter exists to catch. Adapting quietly is the bug; adapting and saying so
    # is the whole behaviour.
    sections.append(
        PromptSection(
            "drift",
            "If the data you find does not match what you were told to expect -- a field renamed or "
            "missing, a different column, an unexpected schema version, a file whose shape has "
            "changed -- adapt to what is actually there AND say so in your reply. A correct answer "
            "reported silently, when the input was not what the user assumed, leaves them believing "
            "their data is unchanged. Name what differed.",
            essential=True,
        )
    )

    if getattr(agent, "profile", None) is not None and agent.profile.needs_argument_coaching:
        sections.append(
            PromptSection(
                "tool_args",
                "Call each tool with exactly the arguments its schema declares. If a call is "
                "rejected, read the returned expected shape and correct it -- do not repeat the "
                "same call unchanged.",
                essential=True,
            )
        )
    else:
        sections.append(
            PromptSection(
                "todo",
                "Use todo_write to maintain a visible checklist for multi-step work, and use task "
                "to delegate a self-contained, open-ended sub-problem to a sub-agent when it would "
                "otherwise consume a lot of your own context.",
            )
        )

    if getattr(agent, "provider", "") == "openrouter":
        sections.append(
            PromptSection(
                "web",
                "Live web search grounding is enabled for every request via OpenRouter's web "
                "plugin -- you don't need to ask the user to search manually for current events, "
                "recent releases, or other facts that might be outside your training data.",
            )
        )

    if getattr(agent, "url_mcp_servers", None):
        sections.append(
            PromptSection(
                "webfetch",
                "web_fetch can only fetch URLs that already appear somewhere in this conversation "
                "(e.g. from a prior web_search result or something the user pasted) -- it cannot "
                "fetch arbitrary invented URLs.",
            )
        )

    sections.append(
        PromptSection(
            "packages",
            "If a task needs a Python package/library you don't have, install it yourself via "
            "run_bash (e.g. pip install <package>) and use it properly, rather than working around "
            "the missing capability by degrading the task -- e.g. don't flatten a .docx into plain "
            "text if you lack a docx library; install python-docx and edit it properly.",
        )
    )
    sections.append(
        PromptSection(
            "skills",
            "If you discover a genuinely reusable approach (not a one-off trivial action), consider "
            "calling save_skill to preserve it -- most turns won't warrant this, and that's expected.\n"
            f"Available skills (call use_skill with the exact name):\n{skills_module.format_skill_listing(agent.skills)}",
        )
    )
    if getattr(agent, "subagent_types", None):
        sections.append(
            PromptSection(
                "subagents",
                "Available subagent types (pass as subagent_type to task):\n"
                f"{subagents.format_subagent_type_listing(agent.subagent_types)}",
            )
        )
    return sections


def build_system_prompt(agent) -> str:
    """Assemble the prompt for this agent's profile.

    Sections are appended in priority order and then cut from the *end* when the character cap is
    exceeded, so the essential rules can never be the ones dropped. Every dropped section is
    recoverable: the model can still discover skills via use_skill, and delegation failure is
    visible in the trace.
    """
    profile: ModelProfile | None = getattr(agent, "profile", None)
    verbosity = profile.verbosity if profile is not None else "full"
    cap = profile.system_prompt_chars if profile is not None else 100_000

    sections = _sections(agent)
    kept: list[str] = []
    total = 0
    for section in sections:
        if verbosity == "terse" and not section.essential:
            continue
        if verbosity == "normal" and section.key in ("subagents",):
            continue
        block = section.text
        if total + len(block) > cap:
            if not section.essential:
                continue
            # An essential section that does not fit is still included: dropping a
            # failure-preventing rule to save characters is the wrong trade, and the cap is a soft
            # guide rather than a hard contract for those sections.
        kept.append(block)
        total += len(block)

    prompt = "\n".join(kept)

    # Named sub-agent listing is worthless when delegation is switched off -- and worse than
    # worthless if the tools are absent, because the model will try to call them and fail.
    if profile is not None and not profile.allow_delegation:
        prompt = prompt.replace(" and use task to delegate a self-contained, open-ended sub-problem "
                                "to a sub-agent when it would otherwise consume a lot of your own "
                                "context", "")
    return prompt
