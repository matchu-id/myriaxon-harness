# OPEN: `the revision advances` in `verify_always_on.py`

**Status: unresolved after three attempts, with the cause narrowed by measurement rather than by reading.**

Do not start this from the code. Start from the numbers below, which were produced by instrumenting the verifier
itself (copied, a print injected into its own `run` closure, and run) — the only method that shows the objects the
checks actually hold. Three earlier attempts reasoned from the docstring and were all wrong.

## The check

```python
holds, result, state2 = run('echo pass', 1010.0, state)
holds, result, state3 = run('echo fail', 1020.0, state2)
check("  the revision advances", state3.revision == state2.revision + 1)
```

## What the call trace shows — and it is CORRECT

`C.should_run` wrapped so every call is logged:

```
PROBE3 call#5 kind=changed holds=False rev=0     <- baseline
PROBE3 call#6 kind=changed holds=False rev=0     <- unchanged, identical observation
PROBE3 call#7 kind=changed holds=True  rev=1     <- changed
```

So `should_run` returns revisions **0, 0, 1** for the three `changed` calls. The check wants `state3 == state2 + 1`,
i.e. `1 == 0 + 1`. **The trace already satisfies it.**

## What the verifier reads instead

```
PROBE2 state=0  state2=1  state3=1  state2is3=False
```

`state2.revision` is **1**, while call #6 returned an object whose revision was **0**. The verifier's `state2` is not
the object `run` handed back from call #6.

`state2is3=False` is the odd part and rules out the obvious explanation: `state2` and `state3` are *different*
objects, yet both read 1, and `state2` is not what call #6 returned.

## Three shapes tried, all with the same result

| Shape | `should_run` returns | Verifier reads | Outcome |
|---|---|---|---|
| live object, mutated in place | same object every call | `state2=1 state3=1` | fails |
| `dataclasses.replace` (shallow copy) | 0, 0, 1 | `state2=1 state3=1` | fails |
| `copy.deepcopy` | 0, 0, 1 | `state2=1 state3=1` | fails |

A deep copy cannot share state with the original, so the aliasing hypothesis is **disproved**. The remaining
candidate is that something advances the state between the calls and the assertion — but the wrapper on
`C.should_run` logs every call it makes, and no extra `changed` call appears between #6 and the check.

That leaves the possibility that this check cannot pass against a correct implementation as written, which is why it
is recorded rather than chased further. **It is one check out of 131; the other 130 pass.**

## The shape that was KEPT, and why it is right independently of this check

`should_run` **mutates `carried` in place and returns a copy**:

- the object the caller **passed in** carries the new fingerprint, which the verifier needs — it reads
  `state.observation` off the object it supplied;
- the object the caller **receives** cannot be moved by a later call, which is what a snapshot means, and what the
  "a failed run must not consume the change" rule depends on: that rule compares states before and after, which is
  meaningless if they are one object.

## If you pick this up

1. Run the probe method, not a reconstruction: copy the verifier, inject a print into its `run` closure, run it.
2. Check whether `state2` is rebound before the assertion — the trace says the object returned by call #6 has
   revision 0, so if the verifier's `state2` has 1, the binding is the thing to watch, not `should_run`.
3. Do not change `should_run` to make the number match. It already returns the right numbers.
