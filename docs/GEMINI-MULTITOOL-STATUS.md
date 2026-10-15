# Gemini 3.x multi-tool turns — state of the fix

**Measured 15 October 2026.** This records what is fixed, what is not, and the evidence for both, so the next
attempt starts from the measurements rather than repeating them.

## The defect

A multi-step Gemini run fails on the **second** turn with:

```
400 INVALID_ARGUMENT
Function call is missing a thought_signature in functionCall parts.
... function call `default_api:write_file`, position 5
```

The run **does** the work — a Locus objective "create one.txt with ALPHA and two.txt with BETA" produced both
files — and then dies when the conversation is replayed. So an agent appears to stop after its first tool call.

## What is now established by measurement

| Fact | Evidence |
|---|---|
| Vertex **does** return a signature | raw `generateContent`: `thoughtSignature`, 300 chars |
| Replaying it **works** | turn 2 with the signature → 200, model answers |
| Omitting it **fails** | turn 2 without it → the 400 above |
| The SDK exposes it correctly | `part.thought_signature` = 283 chars, non-streaming |
| The **streaming** path exposes it too | `.thought_signature` = 325 chars |
| **Only the FIRST call in a turn carries it** | two-tool request → `part 0: signature=YES 356ch`, `part 1: ABSENT` |
| `gemini-3.8-flash` uses **generateContent**, not Interactions | `_resolve_protocol('gemini-3.8-flash', 'auto')` → `'generate'` |
| Grouping calls is **required** | separate Contents → the signature error; grouped → the *count* error |
| Grouping responses is **also** required | `Please ensure that the number of function response parts is equal to the number of function call parts` |

## What is fixed

**`_to_gemini_contents` now reconstructs a turn's structure instead of flattening it.** Several function calls
produced by one model response are replayed as **one** `Content` with several parts, and their results as
**one** `Content` with several parts. Verified:

```
[0] role=user  parts=['text']                                    sigs=['-']
[1] role=model parts=['call:read_file','call:list_dir']          sigs=['sig','-']
[2] role=user  parts=['resp:read_file','resp:list_dir']          sigs=['-','-']
call parts=2  response parts=2  balanced=True
```

Each stored call and result now carries `_gemini_turn`, the id of the response that produced it, so the
grouping is reconstructed from the history rather than guessed from adjacency.

## What is NOT fixed

**A turn can still fail with the same error, and the cause is now pinned to Vertex rather than to us.**

The failing payload was CAPTURED from a real run rather than reasoned about, by recording every request the SDK
sends. That is the ground truth, and it shows something the grouping could not have addressed:

```
[2.0] model functionCall:request_tools    sig=YES 2764
[3.0] user  functionResponse:request_tools
[4.0] model functionCall:write_file       sig=NONE     <- a SEPARATE turn, unsigned
[5.0] user  functionResponse:write_file
```

Those are **two different turns**, not one response. So grouping was correct and irrelevant here: the
`write_file` turn simply arrived with **no signature at all**, and nothing in the history can be reassembled to
invent one.

### Why the earlier hypotheses were wrong

The signature is not being lost by the harness. Three attempts at each path, and the SDK reports it every time:

| Path | Signature |
|---|---|
| streaming, 3 attempts | YES 332 / 338 / 364 |
| non-streaming, 3 attempts | YES 335 / 339 / 373 |

The harness assigns nothing to `part.thought_signature` — it stores what the SDK hands it. And the SDK's value
is not corrupted by chunk accumulation: a stream emits 2 chunks for one call and the second carries no part of
the signature.

### What the 2764-character value actually was

Not a repeat. A signature scales with the **tool catalogue**: the same call returns 332 chars against a
one-tool catalogue and **737 chars against a six-tool catalogue**. `request_tools` is offered alongside every
deferred tool, so its signature is large — 2764 chars is plausible for that turn's schema, not a duplication
artifact. That also means the earlier `2764 / 332 ≈ 8.3` arithmetic, which suggested repetition, was a
coincidence and not a finding.

### What that leaves

Vertex returns a signature on some turns and not others, for reasons not exposed to the caller. This is
consistent with the API's own wording — *"missing thought_signature may lead to degraded model performance"* —
which reads as a warning about a case it sometimes produces rather than one it always prevents.

**The fix is therefore a policy, not a reassembly**, and it is not implemented:

* **Option A — do not replay an unsigned functionCall.** Rewrite that turn's history as text ("the model asked
  to call `write_file`; it wrote the file") so the request stays valid. Honest, and it loses the tool-call
  structure for that turn.
* **Option B — summarise the turn.** Replace the call and its result with one text part. Same idea, coarser.
* **Option C — detect it and stop cleanly**, telling the user the run cannot continue on this model rather than
  dying with a 400 mid-objective.

**A is the recommended one** and is a contained change to `_to_gemini_contents`: it already walks the history
and already knows which calls have a signature.

**Not yet tested:** whether the omission correlates with the catalogue size, with the specific tool, or with the
number of prior turns. One experiment — vary the tool count and measure how often a call comes back unsigned —
would decide whether Option A is a rare fallback or the common case on Gemini 3.x.

## How to reproduce

```powershell
$env:GOOGLE_CLOUD_PROJECT = "<your project>"
$env:GOOGLE_GENAI_USE_VERTEXAI = "true"
$env:GOOGLE_CLOUD_LOCATION = "global"
python -m myriaxon --vendor gemini --model gemini-3.8-flash loci --run
```

with a Locus whose objective needs two files. The objective is completed and the run then fails.

## Also fixed in the same investigation

`vertex_auth.project()` ignored `GOOGLE_CLOUD_PROJECT` — the variable `docs/VERTEX-SETUP.md` tells you to set —
so the documented setup produced `projects//locations/...` and `400 RESOURCE_PROJECT_INVALID`. Introduced by
removing the hardcoded project default for the 0.0.1 release; the default had been hiding it. Now resolved in
the order Myriaxon-specific → `GOOGLE_CLOUD_PROJECT` → `gcloud`.
