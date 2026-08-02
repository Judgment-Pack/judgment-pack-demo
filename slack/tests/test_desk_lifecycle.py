"""A spawned desk is always reachable by the reaper — or already dead.

The failure this guards against is the quiet one: a gateway that came up (or
half came up), failed its readiness check, and then kept running with nothing
holding a reference to it. Each retry would add another, on an instance sized
for one.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from bot.config import Config
from bot.desk import Desk, DeskError, DeskManager
from bot.state import Session

SLEEPER = "import time; time.sleep(120)"


def _config(tmp_path, gateway_bin):
    return Config.from_env(
        {
            "GATEWAY_BIN": gateway_bin,
            "SESSION_ROOT": str(tmp_path),
            "SUBPROCESS_TIMEOUT": "10",
        }
    )


@pytest.fixture
def fake_gateway(tmp_path):
    """A stand-in that provisions a key and then never binds a port."""
    path = tmp_path / "gateway-stub"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "keygen" ]; then\n'
        '  : > "$2"\n'
        "  echo 'seed written'\n"
        "  echo 'publicKey 00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff'\n"
        "  echo 'keyId     0011223344556677'\n"
        "  exit 0\n"
        "fi\n"
        "exec {} -c '{}'\n".format(sys.executable, SLEEPER)
    )
    path.chmod(0o755)
    return str(path)


def _session():
    now = time.time()
    return Session(user_id="U1", created_at=now, last_seen=now)


def test_a_desk_that_never_answers_is_killed_not_leaked(tmp_path, fake_gateway, monkeypatch):
    config = _config(tmp_path, fake_gateway)
    manager = DeskManager(config)
    session = _session()
    project = tmp_path / "project"
    project.mkdir()

    spawned = []
    real_popen = subprocess.Popen

    def watch(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", watch)
    # Readiness is never going to happen; make the wait short.
    monkeypatch.setattr(DeskManager, "_await_ready", lambda self, desk, timeout=0.3: (_ for _ in ()).throw(DeskError("never answered")))

    with pytest.raises(DeskError):
        manager.start(session, str(project), attempts=2)

    assert spawned, "the test never spawned anything to check"
    for process in spawned:
        assert process.poll() is not None, "a gateway was left running with no owner"
    assert "desk" not in session.data


def test_a_started_desk_is_recorded_before_readiness(tmp_path, fake_gateway, monkeypatch):
    """The reaper can reach the process even mid-startup."""
    config = _config(tmp_path, fake_gateway)
    manager = DeskManager(config)
    session = _session()
    project = tmp_path / "project"
    project.mkdir()

    seen = {}

    def peek(self, desk, timeout=1):
        seen["recorded"] = isinstance(session.data.get("desk"), Desk)
        seen["alive"] = desk.alive()
        return True

    monkeypatch.setattr(DeskManager, "_await_ready", peek)
    desk = manager.start(session, str(project))
    try:
        assert seen == {"recorded": True, "alive": True}
        assert session.data["desk"] is desk
    finally:
        manager.stop(session)
    assert desk.process.poll() is not None
    assert "desk" not in session.data


def test_stop_is_terminate_then_wait_then_kill(tmp_path, fake_gateway, monkeypatch):
    config = _config(tmp_path, fake_gateway)
    manager = DeskManager(config)
    session = _session()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(DeskManager, "_await_ready", lambda self, desk, timeout=1: True)

    desk = manager.start(session, str(project))
    assert desk.alive()
    assert manager.stop(session) is True
    assert desk.process.poll() is not None
    # Idempotent: reaping an already-reaped session is not an error.
    assert manager.stop(session) is False


def test_provisioning_refuses_a_half_identity(tmp_path, fake_gateway):
    """A seed without its pin can never be verified: refuse, do not serve."""
    config = _config(tmp_path, fake_gateway)
    manager = DeskManager(config)
    seed = tmp_path / "gateway.seed"
    seed.write_text("")
    with pytest.raises(DeskError):
        manager._provision(str(seed), str(tmp_path / "missing.pubkey"))


def test_missing_binary_is_a_desk_error_not_a_crash(tmp_path):
    config = _config(tmp_path, str(tmp_path / "nope"))
    manager = DeskManager(config)
    session = _session()
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(DeskError):
        manager.start(session, str(project))
    assert "desk" not in session.data
    assert not os.path.exists(str(tmp_path / "nope"))
