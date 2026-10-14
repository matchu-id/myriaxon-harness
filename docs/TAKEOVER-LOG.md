# Takeover log

A running record of the work that finished the project after the incident of 2026-10-08, kept in git so that the
position can always be recovered from the repository itself: what is done, what is in progress, what is next, and how
to resume. Updated with every milestone; each line points at the commit that did it (`git log --oneline`).

## Where things are

| What | Where |
|---|---|
| The working clone (outside OneDrive, so a cloud rollback cannot touch it) | `C:\Users\arian\dev\Myriaxon-work` |
| The line of work | branch `integrate` (the merge of the two lines below, plus every fix after it) |
| The rescued original code, as of 8 Oct 02:49 | branch `snapshot-0249` |
| The DeepSeek agent's 44-commit rebuild, plus the verifier fixes | branch `takeover` |
| The shared repository in OneDrive | `C:\Users\arian\OneDrive\ドキュメント\Myriaxon` (receives every branch above) |
| Offline backups | `C:\Users\arian\dev\backups\` (git bundles, and a copy of `Myriaxon-RESCUE-2026-10-08`) |

To resume: `git log --oneline integrate -20`, read the "In progress" and "Next" lists below, then run
`python tools/run_checks.py` to see the state of every check.

## Why there were two lines

On 2026-10-08 at about 03:46 most of the working tree was deleted (`tools/verify_packaging.py` removed the parent of its
temporary folder, and in the sandbox that parent was the project folder). Git only held the 2 Oct commit, so the work
since then existed in two partial forms: a source distribution built at 02:49 (the original code, without the Loci
companion stack) and a rebuild written afterwards by another agent (the Loci companion, without 26 of the original
agent-side modules). `integrate` combines them. See `docs/RECOVERY.md` for the full account.

## Done

- Rescued the 02:49 package from a temp folder before it was cleaned, and committed it as `snapshot-0249`.
- Merged both lines: original agent-side code kept, Loci character and companion stack taken from the rebuild.
- Fixed the verifier that caused the deletion (`rmtree(outdir.parent)`) and made 53 verifiers independent of where the
  repository lives.
- `tools/run_checks.py`: one command runs every verifier and the pytest suite, each in its own sandbox.
- Fixed five NameErrors that only fire on rarely-run paths (`myriaxon loci --watch`, hosted MCP servers, interactive
  locus delete, dashboard lease release, tool catalog), the duplicate `_run_loci_command`, the stale `dots` test fixture
  and the mangled comments.
- Release artifacts are built from a clean copy of the checkout (`tools/_artifacts.py`).
- The four built-in agent roles exist and ship in the wheel.

## In progress

- GitHub Actions workflow (`.github/workflows/ci.yml`), checked by `tools/verify_ci.py`.
- A static check that finds the whole class of "name that does not exist" bugs (`tools/verify_names.py`).
- A dynamic sweep that calls every tool and every command (`tools/verify_tools_work.py`, `tools/verify_commands_work.py`).

- Fixes the streaming reader that silently discarded every SSE line from a chunked response
  (`_Response.iter_lines` de-chunked by hand because a comment claimed `http.client` returns raw
  chunked bodies; it does not). Measured before the fix: every turn against a local Ollama model
  reported "Empty response from the model" and the server cancelled each request after 2 tokens.
  This broke every OpenAI-compatible provider -- Ollama, DeepSeek, OpenRouter, Netra -- not one of
  them. The Ollama test double answered with `Connection: close` instead of chunked framing, which
  is why no test caught it; it now streams chunked, like the real Ollama, and
  `tests/test_direct_stream.py` pins both the framing and the aggregation.

## Next

1. Dashboard: the Loci list reads `dots` while the server sends `loci` (so it always says "No bots yet"); test every
   dashboard view in a real browser against the offline mock provider.
2. Setup: one command to install, one to check (`install.ps1`, `install.sh`, `myriaxon doctor`), a quickstart that works
   with no API key (Ollama or the offline demo provider).
3. Documentation: generated command and tool references (so they cannot drift), README and INSTALL corrected to the real
   numbers, `docs/RECOVERY.md` corrected, CHANGELOG entry.
4. Hand over: update the OneDrive repository so `main` is `integrate`, tag the release, clean up temp folders.
   As of 2026-10-08 the working clone (`C:\Users\arian\dev\Myriaxon-work`, branch `integrate`) is 60 commits ahead of
   `origin/main` and 0 behind, so this is a fast-forward, not a merge: no work exists anywhere that `integrate` lacks.

## Known issues not fixed yet

- `tools/` holds about 200 scripts for another project (Certivo content checks) and one-off probes that import modules
  that no longer exist (`myriaxon.dots`). They are not part of the Myriaxon checks and are listed in `tools/README.md`.
