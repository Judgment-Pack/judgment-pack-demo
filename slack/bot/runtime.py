"""The model-free demo engine: the `jpack` binary, run for real.

Every disposition this app shows comes out of one of these calls. The model is
not in this code path, and cannot be: the functions here take documents and
return payloads.

Each user gets their own copy of the baked demo project, so their evaluations
land in their own audit trail (that trail is use case 4's whole content) and
their authoring flow cannot register a pack into anybody else's project.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

log = logging.getLogger("jpack-slack.runtime")


class RuntimeUnavailable(Exception):
    """The binary is missing or refused to run at all."""


@dataclass
class Ran:
    """One subprocess result, kept whole so a refusal can be quoted verbatim."""

    argv: List[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    payload: Optional[Dict[str, Any]] = None

    @property
    def ok(self):
        return self.returncode == 0 and not self.timed_out

    def message(self):
        if self.timed_out:
            return "the command exceeded its timeout and was killed:\n$ " + " ".join(self.argv)
        text = (self.stderr or "").strip() or (self.stdout or "").strip()
        return text or "(exit {} with no output)".format(self.returncode)


def _decode(raw):
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


class JpackRuntime:
    """Thin, timeout-bounded wrapper over the CLI."""

    def __init__(self, config):
        self.config = config

    # --- session projects -------------------------------------------------

    def session_dir(self, user_id):
        safe = "".join(c for c in user_id if c.isalnum() or c in "-_") or "anon"
        return os.path.join(self.config.session_root, "session-" + safe)

    def ensure_project(self, session):
        """Copy the baked demo project into this user's scratch directory.

        Idempotent, and safe to call twice at once: the copy is built in a
        temporary directory and moved into place, so a half-copied project is
        never visible and a lost race costs a copy rather than a crash. (Turns
        are serialized per session, so the race is defence in depth.)

        The stored `scratch_dir` is a HINT that has been through an external
        datastore, and this function deletes what it points at. A hint outside
        the configured scratch root is refused and replaced with the path this
        process would have chosen — which also makes a changed SESSION_ROOT
        take effect on restored sessions instead of being silently ignored.
        """
        from .state import inside  # local import: state imports runtime's config only

        default = self.session_dir(session.user_id)
        path = session.scratch_dir or default
        if path != default and not inside(path, self.config.session_root):
            log.warning(
                "ignoring the stored scratch path %r for %s: it is not inside %r",
                path,
                session.user_id,
                self.config.session_root,
            )
            path = default
        session.scratch_dir = path
        if os.path.isdir(os.path.join(path, "packs")):
            return path
        source = self.config.demo_project
        if not os.path.isdir(source):
            raise RuntimeUnavailable("the baked demo project is missing at " + source)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        staging = tempfile.mkdtemp(prefix=".staging-", dir=os.path.dirname(path) or None)
        built = os.path.join(staging, "project")
        shutil.copytree(source, built)
        # A fresh copy starts with an empty decision book: the audit trail the
        # user reads in use case 4 must be theirs, not the image author's.
        for stale in ("audit", "attested", "gateway"):
            target = os.path.join(built, stale)
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
        os.makedirs(os.path.join(built, "slack-inputs"), exist_ok=True)
        try:
            os.rename(built, path)
        except OSError:
            # Another turn won the race and the project is already there.
            if not os.path.isdir(os.path.join(path, "packs")):
                raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return path

    def read_pack(self, project, decision_id):
        """The pack document a decision id names, as parsed JSON.

        Narration is grounded in the payload AND the document it came from;
        a narrator asked to quote a condition must be given the condition.
        """
        try:
            with open(os.path.join(project, "jpack.json")) as handle:
                declared = json.load(handle).get("packs") or {}
            entry = declared.get(decision_id) or {}
            path = entry.get("path")
            if not path:
                return None
            with open(os.path.join(project, path)) as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    # --- process plumbing -------------------------------------------------

    def _env(self, project, extra=None):
        """An allowlisted environment plus this run's config path.

        No Slack token, no signing secret, no API key: the runtime is
        keyless, opens no network connection, and never needs any of them.
        """
        explicit = {}
        if project:
            explicit["JPACK_CONFIG"] = os.path.join(project, "jpack.json")
        if extra:
            explicit.update(extra)
        return self.config.child_env(explicit)

    def run(self, args, project=None, stdin=None, timeout=None, env_extra=None):
        argv = [self.config.jpack_bin] + list(args)
        timeout = timeout or self.config.subprocess_timeout
        try:
            proc = subprocess.run(
                argv,
                cwd=project,
                input=None if stdin is None else stdin.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                env=self._env(project, env_extra),
            )
        except FileNotFoundError as error:
            raise RuntimeUnavailable(
                "cannot run {}: {}".format(self.config.jpack_bin, error)
            )
        except subprocess.TimeoutExpired:
            return Ran(argv=argv, returncode=124, stdout="", stderr="", timed_out=True)
        ran = Ran(
            argv=argv,
            returncode=proc.returncode,
            stdout=_decode(proc.stdout),
            stderr=_decode(proc.stderr),
        )
        text = ran.stdout.strip()
        if text.startswith("{"):
            try:
                ran.payload = json.loads(text)
            except ValueError:
                ran.payload = None
        return ran

    # --- the surfaces the flows use ---------------------------------------

    def version(self):
        return self.run(["version"]).stdout.strip()

    def evaluate(self, project, pack_id, facts, evidence=None, label="run"):
        """`jpack experimental evaluate --pack-id …` on this user's project."""
        inputs = os.path.join(project, "slack-inputs")
        os.makedirs(inputs, exist_ok=True)
        facts_path = os.path.join(inputs, label + "-facts.json")
        with open(facts_path, "w") as handle:
            json.dump(facts, handle, indent=2, sort_keys=True)
        args = [
            "experimental",
            "evaluate",
            "--pack-id",
            pack_id,
            "--facts",
            os.path.relpath(facts_path, project),
            "--format",
            "json",
        ]
        if evidence is not None:
            evidence_path = os.path.join(inputs, label + "-evidence.json")
            with open(evidence_path, "w") as handle:
                json.dump(evidence, handle, indent=2, sort_keys=True)
            args += ["--evidence", os.path.relpath(evidence_path, project)]
        return self.run(args, project=project)

    def evaluate_file(self, project, pack_path, facts_path, evidence_path=None):
        args = [
            "experimental",
            "evaluate",
            pack_path,
            "--facts",
            facts_path,
            "--format",
            "json",
        ]
        if evidence_path:
            args += ["--evidence", evidence_path]
        return self.run(args, project=project)

    def graph_evaluate(self, project, graph_path, inputs_path):
        return self.run(
            [
                "experimental",
                "graph",
                "evaluate",
                graph_path,
                "--inputs",
                inputs_path,
                "--format",
                "json",
            ],
            project=project,
        )

    def spec_validate_text(self, text):
        """Validate a draft document that is not (yet) a file anywhere."""
        return self.run(["spec", "validate", "-", "--format", "json"], stdin=text)

    def packs_lock(self, project):
        """Declare the project's current documents as its reviewed set.

        Only ever run right after this app registers a pack the user authored
        — which IS an amendment, made on purpose, by the person making it.
        Without it the next evaluation refuses with `JPS-LOCK-VERIFY`, because
        a project carrying a lock refuses to decide under law that left the
        reviewed set. That refusal is correct; running this command is the
        honest answer to it, and it leaves a diff a reviewer can read.
        """
        return self.run(["packs", "lock", "--format", "json"], project=project)

    def packs_verify(self, project):
        return self.run(["packs", "verify", "--format", "json"], project=project)

    def has_lock(self, project):
        return os.path.isfile(os.path.join(project, "jpack.lock.json"))

    def packs_validate(self, project):
        return self.run(["packs", "validate", "--format", "json"], project=project)

    def packs_list(self, project):
        return self.run(["packs", "list", "--format", "json"], project=project)

    # --- the decision book ------------------------------------------------

    def audit_path(self, project):
        return os.path.join(project, "audit", "evaluations.jsonl")

    def audit_records(self, project):
        """Every line of the user's own decision book, oldest first."""
        path = self.audit_path(project)
        records = []
        if not os.path.isfile(path):
            return records
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
        return records


def diagnostics_summary(payload, limit=6):
    """Render `spec validate` diagnostics the way the validator states them."""
    if not payload:
        return "(no diagnostics payload)"
    lines = []
    for diagnostic in (payload.get("diagnostics") or [])[:limit]:
        lines.append(
            "{} at {} — {}".format(
                diagnostic.get("code", "?"),
                diagnostic.get("instancePath") or "/",
                diagnostic.get("message", ""),
            )
        )
    if not lines:
        return "(status {} with no diagnostics)".format(payload.get("status"))
    remaining = len(payload.get("diagnostics") or []) - len(lines)
    if remaining > 0:
        lines.append("… and {} more".format(remaining))
    return "\n".join(lines)
