---
name: graphify
description: "Use for any question about this codebase's architecture, file relationships, or cross-file structure -- especially when graphify-out/graph.json already exists, in which case treat the question as a graphify query first instead of grepping/reading files manually. Builds a local, deterministic knowledge graph of the project (via tree-sitter, no LLM calls, nothing leaves the machine) that can be queried instead of re-reading files every time."
---

# graphify

Turns the current project (or any folder of code/docs) into a queryable knowledge graph instead
of re-reading and re-grepping files on every question. Requires the `graphifyy` package
(`pip install graphifyy` or `uv tool install graphifyy`) -- if the `graphify` command isn't
found, tell the user how to install it and stop; don't attempt a workaround.

## Fast path -- graph already exists

Before doing anything else, check whether `graphify-out/graph.json` exists in the project root
(via a quick `read_file`/`glob_search` check, not a full directory listing). If it exists AND
the request is a natural-language question about the codebase ("How does X work?", "What calls
Y?", "Trace how Z flows through the system") rather than an explicit rebuild request, skip
straight to **Querying** below -- do not rebuild, do not run `detect`.

## Building the graph

For a first-time build or an explicit rebuild request, use `run_bash` to invoke:

```
graphify .                        # full pipeline on the current directory
graphify . --update               # incremental -- only re-extract new/changed files
graphify . --mode deep             # thorough extraction, richer inferred edges
graphify . --no-viz                # skip the HTML visualization, just report + JSON
```

This produces three files under `graphify-out/`:
- `graph.json` -- the full graph, queried by every command below
- `GRAPH_REPORT.md` -- key concepts, detected communities, suggested questions
- `graph.html` -- interactive visualization (open in a browser; not directly useful to you)

After a build finishes, read `GRAPH_REPORT.md` once to orient yourself before answering the
user's original question.

## Querying

Once `graph.json` exists, prefer these over manual file exploration:

```
graphify query "<question>"                 # broad, scoped subgraph for a plain-language question
graphify query "<question>" --dfs           # trace a specific path instead of broad context
graphify path "<ConceptA>" "<ConceptB>"     # shortest path between two named concepts
graphify explain "<ConceptName>"            # plain-language explanation of one node + its edges
```

Every edge in the output is tagged `EXTRACTED` (read directly from the source) or `INFERRED`
(resolved by graphify) -- when citing a connection to the user, keep that distinction rather
than presenting an inferred edge as if it were explicit in the code.

## When not to use this

Don't reach for graphify for a single, already-known file path (just `read_file` it) or for a
literal string search (`grep_search` is faster and doesn't need a prebuilt graph). Graphify is
for structural/relationship questions across multiple files -- "what depends on this," "how do
these two things connect," "what are the main subsystems here."
