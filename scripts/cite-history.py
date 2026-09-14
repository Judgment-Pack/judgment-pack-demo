#!/usr/bin/env python3
"""Give every row of a history matrix the receipt its records came under.

Act 7's ledger rows were transcribed from records the desk acquired under a
page receipt (Act 4). This script reads the desk's store, takes the newest
receipt of the newest session, and writes it as each row's `cites` -- the
gateway's citation shape, {sessionId, callIndex, signature} -- so that the
matrix declares matrixVersion "3" and `packs test` carries the citation on
every row's result. The runtime records a citation as given and verifies
nothing; the gateway's verify is what resolves one.

Usage: scripts/cite-history.py [<store>] [<matrix>]
  store   the desk's store root (default gateway-state/public/store)
  matrix  the history matrix (default projects/history-replay/packs/history.matrix.json)
"""
import json, os, sys

store = sys.argv[1] if len(sys.argv) > 1 else "gateway-state/public/store"
matrix_path = sys.argv[2] if len(sys.argv) > 2 else "projects/history-replay/packs/history.matrix.json"
receipts = os.path.join(store, "receipts")
sessions = sorted(d for d in os.listdir(receipts)) if os.path.isdir(receipts) else []
if not sessions:
    sys.exit("no session in the desk's store; run Act 4's attest first so there is a receipt to cite")
session = sessions[-1]
files = sorted((int(f[:-5]), f) for f in os.listdir(os.path.join(receipts, session)) if f.endswith(".json") and f[:-5].isdigit())
if not files:
    sys.exit("the newest session holds no receipt")
index, name = files[-1]
receipt = json.load(open(os.path.join(receipts, session, name)))
citation = {"sessionId": receipt["sessionId"], "callIndex": receipt["callIndex"], "signature": receipt["signature"]}
matrix = json.load(open(matrix_path))
matrix["matrixVersion"] = "3"
for row in matrix["cases"]:
    row["cites"] = [citation]
with open(matrix_path, "w") as out:
    json.dump(matrix, out, indent=1)
    out.write("\n")
print("every row of %s now cites %s/%d" % (matrix_path, citation["sessionId"], citation["callIndex"]))
