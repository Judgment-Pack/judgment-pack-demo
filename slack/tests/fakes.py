"""Stand-ins for the three things a flow reaches: runtime, desk, model.

They record what was asked and answer with fixed payloads, so the flow tests
assert on the state machine rather than on the binaries (the dry run is what
proves the binaries).
"""

from __future__ import annotations

import json

from bot.config import Config
from bot.flows.base import Deps
from bot.model import CANNED_DRAFT, FakeModel
from bot.runtime import Ran

APPROVE = {
    "command": "experimental evaluate",
    "packId": "https://example.invalid/judgment-packs/vendor-onboarding",
    "packVersion": "0.1.0",
    "conformanceClaimReference": "CONFORMANCE.md",
    "disposition": {"handoff": {"state": "none"}, "kind": "outcome", "outcomeId": "approve", "reasons": []},
    "trace": [
        {"stage": "applicability", "condition": "true"},
        {"stage": "rule", "id": "approve-standard", "condition": "true", "outcome": "approve"},
    ],
}

UNRESOLVED = {
    "command": "experimental evaluate",
    "packId": "https://example.invalid/judgment-packs/vendor-onboarding",
    "packVersion": "0.1.0",
    "conformanceClaimReference": "CONFORMANCE.md",
    "disposition": {
        "handoff": {"state": "requested", "triggeredBy": ["missing-required-evidence", "unknown"]},
        "kind": "unresolved",
        "reasons": ["missing-required-evidence", "unknown"],
    },
    "handoffTarget": {"kind": "human-role", "name": "Vendor risk committee"},
    "trace": [{"stage": "exception", "id": "sanctions-match-hard-stop", "condition": "unknown", "onUnknown": "escalate"}],
}

RECORD = {
    "recordVersion": "1",
    "run": "0123456789abcdef",
    "at": "2026-08-02T00:00:00Z",
    "kind": "evaluation",
    "surface": "experimental evaluate",
    "pack": {
        "id": "https://example.invalid/judgment-packs/vendor-onboarding",
        "version": "0.1.0",
        "digest": "sha256:" + "a" * 64,
    },
    "inputs": {"facts": {"request": {"type": "new-vendor-onboarding"}}, "evidenceSupplied": True, "evidence": {"tax-form": "present"}},
    "disposition": APPROVE["disposition"],
}

# A graph walk writes one record per NODE — carrying a pack AND a graph — plus
# a composite headline carrying a graph and no pack. Both shapes are fixtures
# here so the decision book's captions stay honest about what it prints.
NODE_RECORD = dict(
    RECORD,
    run="fedcba9876543210",
    surface="experimental graph evaluate",
    graph={"id": "vendor-onboarding-flow", "version": "0.1.0", "resultNode": "onboarding"},
)

COMPOSITE_RECORD = {
    "recordVersion": "1",
    "run": "fedcba9876543210",
    "at": "2026-08-02T00:00:01Z",
    "kind": "graph-composite",
    "surface": "experimental graph evaluate",
    "graph": {"id": "vendor-onboarding-flow", "version": "0.1.0", "resultNode": "onboarding"},
    "disposition": UNRESOLVED["disposition"],
}

PACK_DOCUMENT = {
    "specVersion": "0.2.0-draft",
    "id": "https://example.invalid/judgment-packs/vendor-onboarding",
    "version": "0.1.0",
    "title": "New vendor onboarding approval",
    "rules": [{"id": "approve-standard", "outcome": "approve"}],
}


def ok(payload):
    return Ran(argv=["jpack"], returncode=0, stdout=json.dumps(payload), stderr="", payload=payload)


class FakeRuntime:
    def __init__(self, payloads=None, records=None):
        self.calls = []
        self.evaluations = 0
        self.payloads = list(payloads or [APPROVE, APPROVE, UNRESOLVED])
        self.records = list(records if records is not None else [RECORD])

    def ensure_project(self, session):
        session.scratch_dir = session.scratch_dir or "/tmp/session-fake"
        return session.scratch_dir

    def read_pack(self, project, decision_id):
        self.calls.append(("read-pack", decision_id))
        return PACK_DOCUMENT

    def evaluate(self, project, pack_id, facts, evidence=None, label="run"):
        self.calls.append(("evaluate", pack_id, label))
        payload = self.payloads[min(self.evaluations, len(self.payloads) - 1)]
        self.evaluations += 1
        return ok(payload)

    def graph_evaluate(self, project, graph_path, inputs_path):
        self.calls.append(("graph", graph_path))
        return ok(dict(UNRESOLVED, nodes=[], handoffs=[]))

    def spec_validate_text(self, text):
        self.calls.append(("spec-validate",))
        return ok({"status": "valid", "diagnostics": []})

    def packs_validate(self, project):
        self.calls.append(("packs-validate",))
        return ok({"status": "valid", "summary": {"total": 5, "passed": 5, "failed": 0}})

    def packs_list(self, project):
        return ok(
            {
                "packs": [
                    {"id": "vendor-onboarding", "packId": RECORD["pack"]["id"]},
                    {"id": "gifts-hospitality", "packId": CANNED_DRAFT["pack"]["id"]},
                ]
            }
        )

    def audit_records(self, project):
        return list(self.records)

    def run(self, args, **kwargs):
        return Ran(argv=["jpack"] + list(args), returncode=0, stdout="jpack test", stderr="")


class FakeDesk:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.verbs = []

    def start(self, session, project):
        self.started = True

        class _Desk:
            url = "http://127.0.0.1:9999"
            key_id = "deadbeef"

        session.data["desk"] = _Desk()
        return session.data["desk"]

    def attest(self, session, project, verb, *args):
        self.verbs.append(verb)

        class _Run:
            returncode = 3 if verb == "check" and "tamper" in self.verbs else 0
            narration = "acquired / verify / WITHHELD" if verb == "check" else verb + " ok"

        return _Run()

    def stop(self, session):
        self.stopped = True
        session.data.pop("desk", None)
        return True


def deps(runtime=None, desk=None, config=None):
    config = config or Config.from_env({"JPACK_DRYRUN_FAKE_MODEL": "1"})
    return Deps(
        config=config,
        runtime=runtime or FakeRuntime(),
        desk=desk or FakeDesk(),
        model=FakeModel(config),
    )
