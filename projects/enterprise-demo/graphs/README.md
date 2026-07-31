# What is in this directory

Three kinds of file, and only two of them are written by hand.

| File | What it is | Pack-world analogue |
|---|---|---|
| `vendor-onboarding.graph.json` | **The artifact.** Nodes (which packs), edges (screening's outcome lands at `/vendor/sanctionsScreening/status`, and its resolution state becomes the `sanctions-screening` evidence), and the result node. | `packs/*.pack.json` |
| `vendor-onboarding.rows.json` | **Its regression suite.** Four cases, each with inputs and the disposition it expects. `jpack.json` declares it, which is why `jpack experimental graph test` takes no arguments. | `packs/*.matrix.json` |
| `inputs-*.json` | **Generated.** Hand-run fixtures for DEMO.md Act 3. | *nothing* |

## Why the inputs files exist at all

They serve a different consumer from the suite. Act 3 walks a presenter through

```
jpack experimental graph evaluate --inputs graphs/inputs-northwind-clear.json
```

one scenario at a time, so each scenario has to be a real file on disk. `graph test`
never reads them — it runs the copy inlined in `vendor-onboarding.rows.json`.

The packs directory has no equivalent, because nothing there is hand-run: in Acts
3–5 the *agent* builds the facts document from the request and the evidence, so
there is only ever one copy of anything.

## Do not edit `inputs-*.json`

They are generated from the suite:

```
python3 scripts/gen-graph-inputs.py          # regenerate
python3 scripts/gen-graph-inputs.py --check  # what CI runs
```

To change a scenario, edit its case in `vendor-onboarding.rows.json` and re-run
that. A hand edit here is overwritten on the next run, and CI fails in the
meantime.

This is not bureaucracy. The same four documents used to live in two places with
nothing linking them, and `graph test` reads only one of them — so a fixture
corrected in the standalone file left the suite's copy stale while every check
stayed green. That is what happened the first time anyone edited one. Deriving the
files means the two cannot disagree, which is a stronger guarantee than checking
that they match.
