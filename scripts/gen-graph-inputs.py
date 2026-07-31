#!/usr/bin/env python3
"""Generate the graph's hand-run input files from the suite that owns them.

`graphs/vendor-onboarding.rows.json` inlines each case's `inputs`, because the
runtime's graph matrix format has no way to reference a file: `RowCase.Inputs` is
inline JSON, and the parser sets DisallowUnknownFields, so the pairing cannot even
be annotated there.

The standalone `graphs/inputs-*.json` files exist for a different consumer. DEMO.md
Act 3 walks a presenter through
`jpack experimental graph evaluate --inputs graphs/inputs-<story>.json`, one
scenario at a time, so each scenario has to be a real file on disk.

That left the same four documents in two places with nothing linking them, and
`graph test` reads only one of them -- so a fixture corrected in the standalone
file left the suite's copy stale while every check stayed green. That is exactly
what happened the first time anyone edited one.

So the files are DERIVED. The rows file is the source of truth, because it is what
the suite actually executes. Two documents cannot disagree when one is produced
from the other, which is a stronger guarantee than checking that they match.

    python3 scripts/gen-graph-inputs.py            # write the files
    python3 scripts/gen-graph-inputs.py --check    # fail if they are stale (CI)

To change a scenario, edit the case in `vendor-onboarding.rows.json` and re-run
this. Editing an `inputs-*.json` by hand accomplishes nothing: the next run
overwrites it, and CI fails in the meantime.
"""

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
GRAPHS = ROOT / "projects" / "enterprise-demo" / "graphs"
ROWS = GRAPHS / "vendor-onboarding.rows.json"

# Case id -> the story-named file DEMO.md hands the presenter. The names differ on
# purpose: a case id says what it asserts, a filename says which story it tells,
# and the demo reads better for it. This mapping is the one place the two meet.
PAIRS = {
    "clear-approves": "inputs-northwind-clear.json",
    "match-rejects": "inputs-meridian-match.json",
    "unresolved-screening-escalates": "inputs-unresolved-screening.json",
    "committee-threshold-escalates": "inputs-committee-threshold.json",
}


def render(case):
    return json.dumps(case["inputs"], indent=2) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="do not write; exit non-zero if any file is stale")
    args = parser.parse_args()

    cases = {case["id"]: case for case in json.loads(ROWS.read_text())["cases"]}
    if set(cases) != set(PAIRS):
        print("FAIL the suite's case ids %s do not match this script's mapping %s -- "
              "a case was added or renamed without deciding what its hand-run file "
              "is called." % (sorted(cases), sorted(PAIRS)), file=sys.stderr)
        return 1

    stale = []
    for case_id, filename in sorted(PAIRS.items()):
        path = GRAPHS / filename
        wanted = render(cases[case_id])
        if args.check:
            if not path.exists():
                stale.append("%s is missing" % filename)
            elif path.read_text() != wanted:
                stale.append("%s is stale: %r no longer matches case %r"
                             % (filename, path.name, case_id))
        else:
            path.write_text(wanted)

    if args.check:
        for entry in stale:
            print("FAIL %s" % entry, file=sys.stderr)
        if stale:
            print("\n%d generated graph input file(s) are out of date. The suite is the "
                  "source of truth: edit the case in vendor-onboarding.rows.json, then "
                  "run scripts/gen-graph-inputs.py." % len(stale), file=sys.stderr)
            return 1
        print("graph inputs: %d generated files are current" % len(PAIRS))
    else:
        print("graph inputs: wrote %d files from vendor-onboarding.rows.json" % len(PAIRS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
