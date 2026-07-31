#!/usr/bin/env python3
"""The graph's hand-run input files must match the suite's inlined rows.

`graphs/vendor-onboarding.rows.json` inlines each case's `inputs`, because the
runtime's graph matrix format has no way to reference a file (`RowCase.Inputs` is
inline JSON, and the parser sets DisallowUnknownFields so the pairing cannot even
be annotated there). The standalone `graphs/inputs-*.json` files exist for a
different consumer: DEMO.md Act 3 walks a presenter through
`jpack experimental graph evaluate --inputs graphs/inputs-<story>.json`, one
scenario at a time, so each needs to be a real file on disk.

So the same four documents live in two places with nothing linking them, and
`graph test` reads only one of them. That drifted the first time anyone edited a
fixture: a spend corrected in the standalone file left the suite's copy stale, and
every check stayed green because the suite never reads the file it disagreed with.

This is the link. The rows file is the source of truth -- it is what the suite
actually executes.
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
GRAPHS = HERE / "projects" / "enterprise-demo" / "graphs"

# The pairing has to live somewhere, and it cannot live in the rows file.
PAIRS = {
    "clear-approves": "inputs-northwind-clear.json",
    "match-rejects": "inputs-meridian-match.json",
    "unresolved-screening-escalates": "inputs-unresolved-screening.json",
    "committee-threshold-escalates": "inputs-committee-threshold.json",
}


def main():
    rows = json.loads((GRAPHS / "vendor-onboarding.rows.json").read_text())
    cases = {case["id"]: case for case in rows["cases"]}

    problems = []
    if set(cases) != set(PAIRS):
        problems.append(
            "the suite's case ids %s do not match the pairing this check knows %s -- "
            "a case was added or renamed without its hand-run file"
            % (sorted(cases), sorted(PAIRS)))

    for case_id, filename in sorted(PAIRS.items()):
        path = GRAPHS / filename
        if case_id not in cases:
            continue
        if not path.exists():
            problems.append("%s is missing, but case %r expects it" % (filename, case_id))
            continue
        inline = json.dumps(cases[case_id]["inputs"], sort_keys=True)
        on_disk = json.dumps(json.loads(path.read_text()), sort_keys=True)
        if inline != on_disk:
            problems.append(
                "%s has drifted from case %r in vendor-onboarding.rows.json.\n"
                "    the suite runs the INLINED copy, so this file can be wrong "
                "while every test stays green.\n"
                "    rows: %s\n    file: %s" % (filename, case_id, inline, on_disk))

    for problem in problems:
        print("FAIL %s" % problem, file=sys.stderr)
    if problems:
        print("\n%d graph input file(s) disagree with the suite they illustrate."
              % len(problems), file=sys.stderr)
        return 1
    print("graph inputs: %d hand-run files agree with the suite's inlined rows" % len(PAIRS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
