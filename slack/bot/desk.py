"""The screening desk: a reference attestation gateway, per session.

One gateway process per active session, on loopback, on a port nobody outside
this container can reach. Each desk gets its own signing identity, its own
seal registry, and its own store, so one user's tamper beat cannot withhold
another user's screening.

The trust boundary the compose demo draws with containers is drawn here with
directories, and the same rules are honored:

* the pin is written from `gateway keygen`'s own stdout — the provisioning
  ceremony — never from `GET /publickey`, because asking the gateway you are
  about to audit for its own key proves consistency, not authenticity;
* the seal registry lives beside the seed, and the verifier fetches it from
  the key holder over HTTP rather than reading it out of the store under
  audit;
* verification is delegated to the reference `gateway verify` binary and the
  verdict is read from its JSON findings, never from an exit code;
* a failed verification derives `screening-record: unknown`. Withholding is
  the answer, not an error.

The model is not in this code path either: `attest` is the demo's own
deterministic glue, and what lands in the inputs document is a pure function
of the derivation rule and the store bytes a verified receipt covers.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

try:  # stdlib only
    from urllib.request import urlopen
except ImportError:  # pragma: no cover
    urlopen = None


log = logging.getLogger("jpack-slack.desk")


class DeskError(Exception):
    """The desk could not be provisioned or reached. Say so; never fake it."""


@dataclass
class Desk:
    root: str
    store: str
    private: str
    pin_file: str
    port: int
    key_id: str
    public_key: str
    process: object = None

    @property
    def url(self):
        return "http://127.0.0.1:{}".format(self.port)

    def alive(self):
        return self.process is not None and self.process.poll() is None


@dataclass
class AttestRun:
    verb: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def narration(self):
        return ((self.stdout or "") + ("\n" if self.stdout and self.stderr else "") + (self.stderr or "")).strip()


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class DeskManager:
    """Starts, addresses, and reaps one gateway per session."""

    def __init__(self, config):
        self.config = config

    # --- lifecycle --------------------------------------------------------

    def start(self, session, project, attempts=3):
        existing = session.data.get("desk")
        if isinstance(existing, Desk) and existing.alive():
            return existing

        root = os.path.join(project, "gateway")
        private = os.path.join(root, "private")
        pin_dir = os.path.join(root, "pin")
        store = os.path.join(root, "store")
        for path in (private, pin_dir, store):
            os.makedirs(path, exist_ok=True)

        seed = os.path.join(private, "gateway.seed")
        pin_file = os.path.join(pin_dir, "pinned.pubkey")
        key_id, public_key = self._provision(seed, pin_file)

        last = None
        for attempt in range(1, attempts + 1):
            # A probed port is a guess: the probe socket is closed before the
            # gateway binds, so another process can take it in between. Losing
            # that race is a bind failure, not a corrupt desk — re-probe and
            # try again rather than reporting a desk that never came up.
            port = _free_port()
            desk = self._spawn(session, project, private, store, pin_file, port,
                               key_id, public_key, root)
            if desk is not None:
                return desk
            last = "port {} was taken before the desk could bind".format(port)
            log.info("desk start attempt %d/%d failed: %s", attempt, attempts, last)
        raise DeskError("the desk could not be started after {} attempts ({})".format(attempts, last))

    def _spawn(self, session, project, private, store, pin_file, port, key_id, public_key, root):
        """Start one gateway, or reap it. Never both, never neither.

        The Popen is recorded on the session BEFORE anything that can fail,
        and any failure after the spawn kills the child before returning —
        so no path through this function can leave a gateway running with
        nothing holding a reference to it.
        """
        registry = os.path.join(private, "registry.jsonl")
        argv = [
            self.config.gateway_bin,
            "serve",
            store,
            os.path.join(private, "gateway.seed"),
            self.config.gateway_authority,
            registry,
            "--source",
            "ofac-screening={} {}".format(sys.executable, self.config.ofac_source),
            "--port",
            str(port),
        ]
        # The source program runs inside the gateway's own process tree and
        # reads its own copy of the baked world. No secret is in this
        # environment: the desk needs none.
        env = self.config.child_env({"WATCHLIST": self.config.ofac_watchlist})
        handle = open(os.path.join(private, "gateway.log"), "ab", buffering=0)
        try:
            process = subprocess.Popen(argv, stdout=handle, stderr=handle, env=env, cwd=project)
        except FileNotFoundError as error:
            handle.close()
            raise DeskError("cannot run {}: {}".format(self.config.gateway_bin, error))

        desk = Desk(
            root=root,
            store=store,
            private=private,
            pin_file=pin_file,
            port=port,
            key_id=key_id,
            public_key=public_key,
            process=process,
        )
        # Reachable by the reaper from this instant on.
        session.data["desk"] = desk
        try:
            self._await_ready(desk)
        except DeskError:
            reason = self._exit_reason(desk)
            self._terminate(process)
            session.data.pop("desk", None)
            handle.close()
            if reason == "bind":
                return None  # the caller re-probes and retries
            raise
        except BaseException:
            self._terminate(process)
            session.data.pop("desk", None)
            handle.close()
            raise
        return desk

    def _exit_reason(self, desk):
        """Why did readiness fail: a taken port, or something else?"""
        if desk.process is not None and desk.process.poll() is None:
            return "timeout"
        try:
            with open(os.path.join(desk.private, "gateway.log")) as handle:
                tail = handle.read()[-2000:]
        except OSError:
            return "exited"
        lowered = tail.lower()
        if "address already in use" in lowered or "bind" in lowered:
            return "bind"
        return "exited"

    @staticmethod
    def _terminate(process, grace=5):
        """terminate → wait → kill. A desk is never left half-dead."""
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=grace)
        except Exception:  # noqa: BLE001 - reaping must never raise
            try:
                process.kill()
                process.wait(timeout=grace)
            except Exception:
                pass

    def _provision(self, seed, pin_file):
        """Mint the identity if this session has none, and pin it from keygen."""
        if os.path.isfile(seed) and os.path.isfile(pin_file):
            with open(pin_file) as handle:
                return "(pinned earlier)", handle.read().strip()
        if os.path.isfile(seed) != os.path.isfile(pin_file):
            # Seed and pin are one artifact; a surviving half cannot regenerate
            # the other, so refuse rather than serve an uncheckable identity.
            raise DeskError(
                "the desk's seed and pin disagree — one exists without the other; "
                "this session's desk cannot be provisioned"
            )
        try:
            out = subprocess.run(
                [self.config.gateway_bin, "keygen", seed],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.config.subprocess_timeout,
                env=self.config.child_env(),
            )
        except FileNotFoundError as error:
            raise DeskError("cannot run {}: {}".format(self.config.gateway_bin, error))
        except subprocess.TimeoutExpired:
            raise DeskError("gateway keygen timed out")
        if out.returncode != 0:
            raise DeskError("gateway keygen refused: " + out.stderr.decode("utf-8", "replace"))
        key = ""
        key_id = ""
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("publicKey "):
                key = line.split(None, 1)[1].strip()
            elif line.startswith("keyId"):
                key_id = line.split(None, 1)[1].strip()
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            try:
                os.remove(seed)
            except OSError:
                pass
            raise DeskError("could not parse a public key out of keygen's output; not provisioning")
        with open(pin_file, "w") as handle:
            handle.write(key + "\n")
        os.chmod(pin_file, 0o444)
        return key_id, key

    def _await_ready(self, desk, timeout=15.0):
        """Wait for the desk to answer. Liveness only — never the pin.

        `GET /publickey` is used here as a readiness probe and for nothing
        else; the key this session verifies under is the one written at
        provisioning time.
        """
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if not desk.alive():
                raise DeskError("the desk exited while starting; see gateway.log in the session")
            try:
                with urlopen(desk.url + "/publickey", timeout=2) as response:
                    response.read()
                    return True
            except Exception as error:  # noqa: BLE001 - readiness probe
                last = error
                time.sleep(0.2)
        raise DeskError("the desk did not answer on {} ({})".format(desk.url, last))

    def stop(self, session):
        desk = session.data.pop("desk", None)
        if not isinstance(desk, Desk) or desk.process is None:
            return False
        self._terminate(desk.process)
        return True

    # --- attest -----------------------------------------------------------

    def _attest_env(self, desk):
        explicit = {
            "GATEWAY_URL": desk.url,
            "GATEWAY_AUTHORITY": self.config.gateway_authority,
            "GATEWAY_STORE": desk.store,
            "GATEWAY_PIN": desk.pin_file,
            "DERIVE_CLI": self.config.derive_cli,
            "DERIVE_RULE": self.config.derive_rule,
        }
        env = self.config.child_env(explicit)
        # `attest` delegates verification to the reference binary by name.
        gateway_dir = os.path.dirname(os.path.abspath(self.config.gateway_bin))
        if os.sep in self.config.gateway_bin and os.path.isdir(gateway_dir):
            env["PATH"] = gateway_dir + os.pathsep + env.get("PATH", "")
        return env

    def attest(self, session, project, verb, *args):
        desk = session.data.get("desk")
        if not isinstance(desk, Desk) or not desk.alive():
            raise DeskError("this session has no running desk — start it first")
        argv = [sys.executable, self.config.attest_script, verb] + [str(a) for a in args]
        try:
            proc = subprocess.run(
                argv,
                cwd=project,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.config.subprocess_timeout,
                env=self._attest_env(desk),
            )
        except FileNotFoundError as error:
            raise DeskError("cannot run attest: {}".format(error))
        except subprocess.TimeoutExpired:
            return AttestRun(verb=verb, returncode=124, stdout="", stderr="", timed_out=True)
        return AttestRun(
            verb=verb,
            returncode=proc.returncode,
            stdout=proc.stdout.decode("utf-8", "replace"),
            stderr=proc.stderr.decode("utf-8", "replace"),
        )
