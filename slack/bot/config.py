"""Configuration for the Slack self-serve demo.

Every value comes from the environment. Secrets are read here and nowhere
else: they are never logged, never rendered into a Slack message, and never
written to a session directory. The container's baked layout supplies the
defaults; the dry run overrides the binary and project paths so the same code
runs on a workstation with locally built binaries.

The model/runtime boundary this whole app is built around: the paths below
that decide anything (JPACK_BIN, GATEWAY_BIN, the demo project) are
model-free. GEMINI_* only ever buys narration and drafting labor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

# The demo's pinned presenter model. The lite models paraphrase instead of
# reading the payload they are handed.
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"

# The only variables a demo subprocess inherits. Everything else — every
# secret this app holds included — is built explicitly per call site by
# Config.child_env(). An allowlist rather than a denylist, because the next
# secret this app gains should be excluded by default.
CHILD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


def _int_env(env, name, default):
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool_env(env, name, default=False):
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    """Resolved runtime configuration. Construct with :meth:`from_env`."""

    # --- Slack ------------------------------------------------------------
    slack_bot_token: Optional[str] = None
    slack_signing_secret: Optional[str] = None

    # --- Gemini (model labor only) ---------------------------------------
    gemini_api_key: Optional[str] = None
    gemini_model: str = DEFAULT_GEMINI_MODEL
    fake_model: bool = False

    # --- The model-free demo engine --------------------------------------
    jpack_bin: str = "jpack"
    gateway_bin: str = "gateway"
    attest_script: str = "/usr/local/bin/attest"
    ofac_source: str = "/usr/local/libexec/ofac-screening-source.py"
    ofac_watchlist: str = "/usr/local/share/ofac/watchlist.json"
    derive_cli: str = "/usr/local/share/derivation-rule/derive_cli.py"
    derive_rule: str = "/usr/local/share/derivation-rule/rules/screening.rule.json"
    demo_project: str = "/opt/judgment-pack/enterprise-demo"
    gateway_authority: str = "gateway:judgment-pack-slack"

    # --- Where session metadata lives -------------------------------------
    # "memory" (default) forgets everything when this process ends. "firestore"
    # makes a user's PROGRESS durable across restarts; it does not make the
    # demo multi-instance, because the scratch project and the live desk are
    # inherently local (see bot/store.py and slack/DESIGN.md).
    state_backend: str = "memory"
    firestore_collection: str = "slack-demo-sessions"
    firestore_project: Optional[str] = None
    firestore_database: Optional[str] = None
    # How long a turn lease is claimed for. A seam for a multi-instance
    # version; the single instance is serialized by a threading.Lock.
    lease_seconds: int = 120
    dedupe_ttl_seconds: int = 900

    # --- Session hygiene --------------------------------------------------
    session_root: str = "/tmp"
    session_ttl_seconds: int = 2 * 60 * 60
    max_sessions: int = 24
    subprocess_timeout: int = 90

    # --- Politeness limits ------------------------------------------------
    model_calls_per_hour: int = 20
    model_timeout_seconds: int = 60
    # Turns are cheap for the user and expensive for the instance: a turn runs
    # jpack, sometimes a gateway, sometimes both. Generous, but not unbounded.
    turns_per_hour: int = 60
    max_concurrent_work: int = 4

    # --- HTTP -------------------------------------------------------------
    port: int = 8080

    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env

        def get(name, default):
            value = env.get(name)
            return default if value is None or value.strip() == "" else value

        return cls(
            slack_bot_token=env.get("SLACK_BOT_TOKEN") or None,
            slack_signing_secret=env.get("SLACK_SIGNING_SECRET") or None,
            gemini_api_key=env.get("GEMINI_API_KEY") or None,
            gemini_model=get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
            fake_model=_bool_env(env, "JPACK_DRYRUN_FAKE_MODEL", False),
            jpack_bin=get("JPACK_BIN", "jpack"),
            gateway_bin=get("GATEWAY_BIN", "gateway"),
            attest_script=get("ATTEST_SCRIPT", "/usr/local/bin/attest"),
            ofac_source=get("OFAC_SOURCE", "/usr/local/libexec/ofac-screening-source.py"),
            ofac_watchlist=get("OFAC_WATCHLIST", "/usr/local/share/ofac/watchlist.json"),
            derive_cli=get("DERIVE_CLI", "/usr/local/share/derivation-rule/derive_cli.py"),
            derive_rule=get(
                "DERIVE_RULE",
                "/usr/local/share/derivation-rule/rules/screening.rule.json",
            ),
            demo_project=get("DEMO_PROJECT", "/opt/judgment-pack/enterprise-demo"),
            gateway_authority=get("GATEWAY_AUTHORITY", "gateway:judgment-pack-slack"),
            state_backend=get("STATE_BACKEND", "memory"),
            firestore_collection=get("FIRESTORE_COLLECTION", "slack-demo-sessions"),
            firestore_project=env.get("FIRESTORE_PROJECT") or env.get("GOOGLE_CLOUD_PROJECT") or None,
            firestore_database=env.get("FIRESTORE_DATABASE") or None,
            lease_seconds=_int_env(env, "LEASE_SECONDS", 120),
            dedupe_ttl_seconds=_int_env(env, "DEDUPE_TTL_SECONDS", 900),
            session_root=get("SESSION_ROOT", "/tmp"),
            session_ttl_seconds=_int_env(env, "SESSION_TTL_SECONDS", 2 * 60 * 60),
            max_sessions=_int_env(env, "MAX_SESSIONS", 24),
            subprocess_timeout=_int_env(env, "SUBPROCESS_TIMEOUT", 90),
            model_calls_per_hour=_int_env(env, "MODEL_CALLS_PER_HOUR", 20),
            model_timeout_seconds=_int_env(env, "MODEL_TIMEOUT_SECONDS", 60),
            turns_per_hour=_int_env(env, "TURNS_PER_HOUR", 60),
            max_concurrent_work=_int_env(env, "MAX_CONCURRENT_WORK", 4),
            port=_int_env(env, "PORT", 8080),
        )

    def require_slack(self):
        """Refuse to boot without the two Slack secrets.

        Bolt verifies every request's signature with the signing secret; a
        server that starts without one would accept unsigned traffic, so the
        absence is fatal rather than a warning.
        """
        missing = [
            name
            for name, value in (
                ("SLACK_BOT_TOKEN", self.slack_bot_token),
                ("SLACK_SIGNING_SECRET", self.slack_signing_secret),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "refusing to start without " + ", ".join(missing)
                + " — see slack/SETUP.md (the values are never logged or echoed)"
            )

    def model_available(self):
        return bool(self.fake_model or self.gemini_api_key)

    def child_env(self, extra=None):
        """The environment a demo subprocess gets: an allowlist, not a copy.

        Every child here is a trusted binary, but the bot token can post as
        the app anywhere it is a member and the signing secret is the app's
        entire request authentication — so neither is ever handed to a
        process that has no use for it, and the runtime stays keyless by
        construction rather than by intention. Anything a child genuinely
        needs is passed explicitly in `extra`.
        """
        env = {}
        for name in CHILD_ENV_ALLOWLIST:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        if extra:
            env.update({k: v for k, v in extra.items() if v is not None})
        return env

    def redacted(self):
        """A dict safe to log: presence, never value."""
        return {
            "gemini_model": self.gemini_model,
            "fake_model": self.fake_model,
            "has_slack_bot_token": bool(self.slack_bot_token),
            "has_slack_signing_secret": bool(self.slack_signing_secret),
            "has_gemini_api_key": bool(self.gemini_api_key),
            "jpack_bin": self.jpack_bin,
            "gateway_bin": self.gateway_bin,
            "demo_project": self.demo_project,
            "session_root": self.session_root,
            "state_backend": self.state_backend,
            "firestore_collection": self.firestore_collection,
            "model_calls_per_hour": self.model_calls_per_hour,
            "turns_per_hour": self.turns_per_hour,
            "max_concurrent_work": self.max_concurrent_work,
        }
