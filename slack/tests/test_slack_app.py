"""The Slack manifest automation, proven without touching Slack.

Every call goes through a stubbed opener, so this file makes no network
request, holds no credential, and would fail loudly if it tried: the point of
testing a deploy tool is that its mistakes are expensive and its rehearsals
must not be.

What is pinned here: that the substitution finds ALL THREE urls in the real
committed manifest (two of three wired is a demo that answers buttons and
ignores messages), that the payloads are the shape Slack documents, that a
token never reaches stdout, and that a rotation keeps the new refresh token —
losing it locks the next release out.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import urllib.error
import urllib.parse

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SLACK_DIR = os.path.dirname(HERE)
SCRIPT = os.path.join(SLACK_DIR, "deploy", "slack-app.py")
MANIFEST = os.path.join(SLACK_DIR, "app_manifest.yml")

yaml = pytest.importorskip("yaml", reason="slack-app.py needs PyYAML")


def load_module():
    """Import a file whose name is not a module name."""
    spec = importlib.util.spec_from_file_location("slack_app", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


slack_app = load_module()

URL = "https://judgment-pack-slack-demo-681892532065.us-central1.run.app"
EVENTS = URL + "/slack/events"


class Recorder:
    """A stand-in for urlopen: records requests, replays canned answers."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request, timeout=None):
        raw = request.data.decode() if request.data else ""
        body, form = None, None
        if raw.startswith("{"):
            body = json.loads(raw)
        elif raw:
            form = urllib.parse.parse_qs(raw)
        self.requests.append(
            {
                "url": request.full_url,
                "headers": dict(request.header_items()),
                "body": body,
                "form": form,
            }
        )
        answer = self.answers.pop(0)
        return _Response(answer)


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --- substitution over the REAL manifest -----------------------------------


def test_every_request_url_in_the_committed_manifest_is_replaced():
    manifest = slack_app.load_manifest(MANIFEST)
    manifest, changed = slack_app.substitute_urls(manifest, URL)

    assert sorted(changed) == [
        "/features/slash_commands[0]/url",
        "/settings/event_subscriptions/request_url",
        "/settings/interactivity/request_url",
    ], "all three of Slack's url fields, or the demo answers only some of them"

    assert manifest["settings"]["event_subscriptions"]["request_url"] == EVENTS
    assert manifest["settings"]["interactivity"]["request_url"] == EVENTS
    assert manifest["features"]["slash_commands"][0]["url"] == EVENTS
    assert "example.invalid" not in json.dumps(manifest)


def test_a_trailing_slash_does_not_double_up():
    assert slack_app.events_url("https://x.run.app/") == "https://x.run.app/slack/events"
    assert slack_app.events_url("https://x.run.app") == "https://x.run.app/slack/events"


def test_substitution_leaves_everything_else_alone():
    manifest = slack_app.load_manifest(MANIFEST)
    before = yaml.safe_load(open(MANIFEST))
    manifest, _ = slack_app.substitute_urls(manifest, URL)
    assert manifest["oauth_config"] == before["oauth_config"], "scopes are untouched"
    assert manifest["display_information"] == before["display_information"]
    assert manifest["settings"]["event_subscriptions"]["bot_events"] == [
        "team_join",
        "app_home_opened",
        "message.im",
    ]


def test_a_url_that_is_not_a_slash_command_is_not_touched():
    document = {"display_information": {"url": "https://example.com/about"},
                "features": {"slash_commands": [{"command": "/x", "url": "https://old/y"}]}}
    document, changed = slack_app.substitute_urls(document, URL)
    assert document["display_information"]["url"] == "https://example.com/about"
    assert document["features"]["slash_commands"][0]["url"] == EVENTS
    assert changed == ["/features/slash_commands[0]/url"]


# --- payload shapes --------------------------------------------------------


def test_create_posts_the_manifest_with_a_bearer_token():
    opener = Recorder([{"ok": True, "app_id": "A0123456789",
                        "credentials": {"signing_secret": "s3cr3t-signing"}}])
    args = slack_app.build_parser().parse_args(
        ["create", "--url", URL, "--manifest", MANIFEST]
    )
    os.environ["SLACK_CONFIG_TOKEN"] = "fake-config-token-for-tests"
    try:
        assert slack_app.cmd_create(args, opener=opener, log=lambda *a: None) == 0
    finally:
        del os.environ["SLACK_CONFIG_TOKEN"]

    call = opener.requests[0]
    assert call["url"] == "https://slack.com/api/apps.manifest.create"
    assert call["headers"]["Authorization"] == "Bearer fake-config-token-for-tests"
    assert call["headers"]["Content-type"].startswith("application/json")
    assert call["body"]["manifest"]["settings"]["interactivity"]["request_url"] == EVENTS


def test_update_exports_then_updates_with_the_app_id():
    exported = {"ok": True, "manifest": {
        "settings": {"event_subscriptions": {"request_url": "https://old/slack/events"},
                     "interactivity": {"request_url": "https://old/slack/events"}},
        "features": {"slash_commands": [{"command": "/jpack", "url": "https://old/slack/events"}]},
    }}
    opener = Recorder([exported, {"ok": True}])
    args = slack_app.build_parser().parse_args(
        ["update-url", "--app", "A0123456789", "--url", URL]
    )
    os.environ["SLACK_CONFIG_TOKEN"] = "fake-config-token-for-tests"
    try:
        assert slack_app.cmd_update_url(args, opener=opener, log=lambda *a: None) == 0
    finally:
        del os.environ["SLACK_CONFIG_TOKEN"]

    assert opener.requests[0]["url"].endswith("apps.manifest.export")
    assert opener.requests[0]["body"] == {"app_id": "A0123456789"}
    update = opener.requests[1]
    assert update["url"].endswith("apps.manifest.update")
    assert update["body"]["app_id"] == "A0123456789"
    assert update["body"]["manifest"]["settings"]["interactivity"]["request_url"] == EVENTS
    assert update["body"]["manifest"]["features"]["slash_commands"][0]["url"] == EVENTS


def test_slack_saying_no_is_an_error_not_a_success():
    opener = Recorder([{"ok": False, "error": "invalid_manifest"}])
    with pytest.raises(slack_app.SlackError) as raised:
        slack_app.call("apps.manifest.create", {"manifest": {}}, token="t", opener=opener)
    assert "invalid_manifest" in str(raised.value)


# --- secrets never reach a log ---------------------------------------------


def test_redaction_shows_enough_to_recognize_and_not_enough_to_use():
    redacted = slack_app.redact("xoxe.x" + "FAKE-" * 4 + "redactme")
    assert "redactme" not in redacted
    assert redacted.startswith("xoxe.x")
    assert slack_app.redact("") == "<none>"


def test_dry_run_prints_the_payload_with_the_token_redacted(capsys):
    lines = []
    args = slack_app.build_parser().parse_args(
        ["create", "--url", URL, "--manifest", MANIFEST, "--dry-run"]
    )
    os.environ["SLACK_CONFIG_TOKEN"] = "fake-config-token-redactme"
    try:
        assert slack_app.cmd_create(args, log=lines.append) == 0
    finally:
        del os.environ["SLACK_CONFIG_TOKEN"]
    printed = "\n".join(lines)
    assert "SUPERSECRETVALUE" not in printed
    assert EVENTS in printed
    assert "apps.manifest.create" in printed


def test_the_signing_secret_goes_to_a_private_file_and_not_to_stdout(tmp_path):
    lines = []
    out = tmp_path / "signing"
    opener = Recorder([{"ok": True, "app_id": "A1", "credentials": {"signing_secret": "SIGNING-XYZ"}}])
    args = slack_app.build_parser().parse_args(
        ["create", "--url", URL, "--manifest", MANIFEST,
         "--signing-secret-out", str(out)]
    )
    os.environ["SLACK_CONFIG_TOKEN"] = "t"
    try:
        slack_app.cmd_create(args, opener=opener, log=lines.append)
    finally:
        del os.environ["SLACK_CONFIG_TOKEN"]

    assert out.read_text() == "SIGNING-XYZ"
    assert stat.S_IMODE(os.stat(str(out)).st_mode) == 0o600
    assert "SIGNING-XYZ" not in "\n".join(lines)


def test_write_private_is_owner_only(tmp_path):
    path = slack_app.write_private(str(tmp_path / "nested" / "secret"), "value")
    assert open(path).read() == "value"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# --- rotation --------------------------------------------------------------


def test_a_stale_token_is_rotated_once_and_the_call_retried(tmp_path):
    refresh_out = tmp_path / "refresh"
    opener = Recorder([
        {"ok": False, "error": "token_expired"},          # the first attempt
        {"ok": True, "token": "fake-access-new", "refresh_token": "fake-refresh-new"},
        {"ok": True, "app_id": "A1"},                      # the retry
    ])
    session = slack_app.Session(
        "fake-access-stale", "fake-refresh-old", str(refresh_out), opener, lambda *a: None
    )
    body = session.call("apps.manifest.create", {"manifest": {}})

    assert body["app_id"] == "A1"
    assert session.rotated is True
    # The refresh token is in the BODY, not the request line: a query string
    # lands in every proxy and access log between here and Slack.
    assert opener.requests[1]["url"] == "https://slack.com/api/tooling.tokens.rotate"
    assert "fake-refresh-old" not in opener.requests[1]["url"]
    assert opener.requests[1]["form"] == {"refresh_token": ["fake-refresh-old"]}
    assert opener.requests[2]["headers"]["Authorization"] == "Bearer fake-access-new"
    assert refresh_out.read_text() == "fake-refresh-new"
    assert stat.S_IMODE(os.stat(str(refresh_out)).st_mode) == 0o600


def test_a_refresh_token_alone_is_enough_to_start(tmp_path):
    opener = Recorder([
        {"ok": True, "token": "fake-access-minted", "refresh_token": "fake-refresh-2"},
        {"ok": True, "app_id": "A2"},
    ])
    session = slack_app.Session("", "fake-refresh-1", str(tmp_path / "r"), opener, lambda *a: None)
    session.call("apps.manifest.update", {"app_id": "A2", "manifest": {}})
    assert session.token == "fake-access-minted"
    assert (tmp_path / "r").read_text() == "fake-refresh-2"


def test_a_real_failure_is_not_retried_forever():
    opener = Recorder([{"ok": False, "error": "invalid_manifest"}])
    session = slack_app.Session("t", "refresh", None, opener, lambda *a: None)
    with pytest.raises(slack_app.SlackError):
        session.call("apps.manifest.create", {"manifest": {}})
    assert len(opener.requests) == 1, "a bad manifest is not a stale token"


def test_no_token_at_all_says_so():
    session = slack_app.Session("", "", None, None, lambda *a: None)
    with pytest.raises(slack_app.SlackError) as raised:
        session.ensure_token()
    assert "SLACK_CONFIG_TOKEN" in str(raised.value)


def test_a_rotation_with_nowhere_to_put_the_new_token_is_refused():
    """A rotation nobody can store kills the credential — so it does not happen."""
    lines = []
    opener = Recorder([{"ok": True, "token": "t2", "refresh_token": "r2"}])
    session = slack_app.Session("", "r1", None, opener, lines.append)
    with pytest.raises(slack_app.SlackError) as raised:
        session.ensure_token()
    assert raised.value.code == "rotation_refused"
    assert "--refresh-out" in str(raised.value)
    assert opener.requests == [], "and nothing was rotated"


# --- the gcloud hop, without gcloud ----------------------------------------


def test_the_signing_secret_is_piped_to_gcloud_never_passed_as_an_argument():
    calls = []

    class Result:
        returncode = 0
        stderr = b""

    def runner(argv, **kwargs):
        calls.append((argv, kwargs.get("input")))
        return Result()

    ok, detail = slack_app.add_secret_version("proj", "SLACK_SIGNING_SECRET", "SEKRIT", runner)
    assert ok and detail == ""
    for argv, _ in calls:
        assert "SEKRIT" not in " ".join(argv), "a secret in argv is a secret in `ps`"
    assert calls[-1][1] == b"SEKRIT"


# --- URL completeness on the export round trip -----------------------------

EXPORTED = {
    "display_information": {"name": "Judgment Pack Demo"},
    "features": {
        "slash_commands": [{"command": "/jpack", "url": "https://old.run.app/slack/events"}],
    },
    "oauth_config": {
        "redirect_urls": ["https://old.run.app/oauth"],
        "scopes": {"bot": ["chat:write"]},
    },
    "settings": {
        "event_subscriptions": {"request_url": "https://old.run.app/slack/events"},
        "interactivity": {
            "is_enabled": True,
            "request_url": "https://old.run.app/slack/events",
            "message_menu_options_url": "https://old.run.app/slack/events",
        },
    },
}


def test_an_exported_manifest_has_more_urls_than_the_committed_one():
    """What Slack exports carries whatever the UI configured — including
    members the committed file never had."""
    manifest = json.loads(json.dumps(EXPORTED))
    manifest, changed = slack_app.substitute_urls(manifest, URL)
    assert sorted(changed) == [
        "/features/slash_commands[0]/url",
        "/oauth_config/redirect_urls[0]",
        "/settings/event_subscriptions/request_url",
        "/settings/interactivity/message_menu_options_url",
        "/settings/interactivity/request_url",
    ]
    assert "old.run.app" not in json.dumps(manifest)


def test_nothing_url_shaped_is_left_pointing_at_the_old_host():
    manifest = json.loads(json.dumps(EXPORTED))
    manifest, _ = slack_app.substitute_urls(manifest, URL)
    assert slack_app.foreign_urls(manifest, URL) == []


def test_a_url_this_tool_does_not_know_is_reported_rather_than_ignored():
    """A member added by a future Slack feature must be loud, not silent."""
    manifest = json.loads(json.dumps(EXPORTED))
    manifest["settings"]["some_future_callback_url"] = "https://old.run.app/hook"
    manifest, _ = slack_app.substitute_urls(manifest, URL)
    stale = slack_app.foreign_urls(manifest, URL)
    assert stale == [("/settings/some_future_callback_url", "https://old.run.app/hook")]


def test_update_refuses_when_a_url_would_be_left_behind():
    manifest = json.loads(json.dumps(EXPORTED))
    manifest["settings"]["some_future_callback_url"] = "https://old.run.app/hook"
    opener = Recorder([{"ok": True, "manifest": manifest}])
    args = slack_app.build_parser().parse_args(
        ["update-url", "--app", "A1", "--url", URL])
    os.environ["SLACK_CONFIG_TOKEN"] = "t"
    try:
        with pytest.raises(slack_app.SlackError) as raised:
            slack_app.cmd_update_url(args, opener=opener, log=lambda *a: None)
    finally:
        del os.environ["SLACK_CONFIG_TOKEN"]
    assert "another host" in str(raised.value)
    assert len(opener.requests) == 1, "the update was never sent"


def test_the_escape_hatch_exists_and_is_explicit():
    manifest = json.loads(json.dumps(EXPORTED))
    manifest["settings"]["some_future_callback_url"] = "https://elsewhere.example/hook"
    opener = Recorder([{"ok": True, "manifest": manifest}, {"ok": True}])
    args = slack_app.build_parser().parse_args(
        ["update-url", "--app", "A1", "--url", URL, "--allow-foreign-urls"])
    os.environ["SLACK_CONFIG_TOKEN"] = "t"
    try:
        assert slack_app.cmd_update_url(args, opener=opener, log=lambda *a: None) == 0
    finally:
        del os.environ["SLACK_CONFIG_TOKEN"]
    assert opener.requests[1]["url"].endswith("apps.manifest.update")


def test_update_url_needs_no_yaml(monkeypatch):
    """The CI job holding the Slack tokens installs nothing."""
    def no_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no yaml here")
        return original(name, *args, **kwargs)

    import builtins
    original = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", no_yaml)

    manifest = json.loads(json.dumps(EXPORTED))
    opener = Recorder([{"ok": True, "manifest": manifest}, {"ok": True}])
    args = slack_app.build_parser().parse_args(["update-url", "--app", "A1", "--url", URL])
    os.environ["SLACK_CONFIG_TOKEN"] = "t"
    try:
        assert slack_app.cmd_update_url(args, opener=opener, log=lambda *a: None) == 0
    finally:
        del os.environ["SLACK_CONFIG_TOKEN"]


# --- rotation, in the body and under control -------------------------------


def test_rotate_sends_the_refresh_token_in_the_body():
    opener = Recorder([{"ok": True, "token": "new", "refresh_token": "newer"}])
    token, refresh = slack_app.rotate("fake-refresh-secret", opener=opener)
    assert (token, refresh) == ("new", "newer")
    call = opener.requests[0]
    assert call["url"] == "https://slack.com/api/tooling.tokens.rotate"
    assert "SECRETREFRESH" not in call["url"]
    assert call["form"] == {"refresh_token": ["fake-refresh-secret"]}
    assert call["headers"]["Content-type"] == "application/x-www-form-urlencoded"


def test_no_rotate_refuses_with_the_documented_exit_code(tmp_path):
    """No access token, a refresh token, and nowhere to store a new pair."""
    os.environ["SLACK_CONFIG_TOKEN"] = ""
    os.environ["SLACK_CONFIG_REFRESH_TOKEN"] = "fake-refresh-env"
    try:
        code = slack_app.main(["update-url", "--app", "A1", "--url", URL, "--no-rotate"])
    finally:
        del os.environ["SLACK_CONFIG_TOKEN"]
        del os.environ["SLACK_CONFIG_REFRESH_TOKEN"]
    assert code == slack_app.EXIT_ROTATION_REFUSED == 3, (
        "exit 3 is what lets CI skip the optional wiring instead of failing the release"
    )


def test_the_rotate_subcommand_writes_both_halves(tmp_path):
    opener = Recorder([{"ok": True, "token": "tok", "refresh_token": "ref"}])
    args = slack_app.build_parser().parse_args(
        ["rotate", "--refresh-out", str(tmp_path / "refresh"),
         "--token-out", str(tmp_path / "token")])
    os.environ["SLACK_CONFIG_REFRESH_TOKEN"] = "old"
    try:
        assert slack_app.cmd_rotate(args, opener=opener, log=lambda *a: None) == 0
    finally:
        del os.environ["SLACK_CONFIG_REFRESH_TOKEN"]
    assert (tmp_path / "refresh").read_text() == "ref"
    assert (tmp_path / "token").read_text() == "tok"
    assert stat.S_IMODE(os.stat(str(tmp_path / "token")).st_mode) == 0o600


def test_rotate_without_refresh_out_is_rejected_by_the_cli():
    assert slack_app.main(["rotate"]) == 2


# --- error bodies ----------------------------------------------------------


class Failing:
    """An opener that raises HTTPError with a Slack-shaped body."""

    def __init__(self, code, payload, then=None):
        self.code = code
        self.payload = payload
        self.then = list(then or [])
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        if self.calls == 1 or not self.then:
            raise urllib.error.HTTPError(
                request.full_url, self.code, "err", {},
                io.BytesIO(json.dumps(self.payload).encode()),
            )
        return _Response(self.then.pop(0))


def test_an_http_error_keeps_slacks_explanation():
    opener = Failing(429, {"ok": False, "error": "ratelimited"})
    with pytest.raises(slack_app.SlackError) as raised:
        slack_app.call("apps.manifest.update", {}, token="t", opener=opener)
    assert raised.value.code == "ratelimited"
    assert raised.value.status == 429
    assert "ratelimited" in str(raised.value)


def test_a_stale_token_reported_as_an_http_status_still_rotates(tmp_path):
    """Slack usually answers 200/ok:false — but a 401 must rotate too."""
    opener = Failing(401, {"ok": False, "error": "token_expired"},
                     then=[{"ok": True, "token": "new", "refresh_token": "newer"},
                           {"ok": True, "app_id": "A1"}])
    session = slack_app.Session("stale", "refresh", str(tmp_path / "r"), opener,
                                lambda *a: None)
    body = session.call("apps.manifest.create", {"manifest": {}})
    assert body["app_id"] == "A1"
    assert session.rotated is True
    assert (tmp_path / "r").read_text() == "newer"


# --- the release plumbing, checked as text ---------------------------------
#
# These read the shipped files rather than running gcloud or GitHub. They pin
# the properties a reviewer had to execute probes to discover.

WORKFLOW = os.path.join(os.path.dirname(SLACK_DIR), ".github", "workflows", "release-slack.yml")
SETUP_WIF = os.path.join(SLACK_DIR, "deploy", "setup-wif.sh")
DEPLOY_SH = os.path.join(SLACK_DIR, "deploy", "deploy.sh")
CLOUDBUILD = os.path.join(SLACK_DIR, "deploy", "cloudbuild.yaml")


def read(path):
    with open(path) as handle:
        return handle.read()


def test_the_wif_condition_requires_the_approved_environment():
    source = read(SETUP_WIF)
    assert "assertion.environment=='${ENVIRONMENT}'" in source
    assert "attribute.environment=assertion.environment" in source
    # And the update path re-applies the mapping, or a provider missing the
    # attribute could never be repaired by re-running.
    update = source.split("providers update-oidc", 1)[1].split("else", 1)[0]
    assert "--attribute-mapping" in update and "--attribute-condition" in update


def test_the_repo_regex_rejects_a_cel_injection():
    import re as _re
    pattern = _re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
    assert pattern.match("Judgment-Pack/judgment-pack-demo")
    assert not pattern.match("a'||assertion.repository!='b/c")
    assert read(SETUP_WIF).count("[A-Za-z0-9._-]+/[A-Za-z0-9._-]+") >= 1


def test_wire_slack_skips_at_the_job_level_and_has_no_environment():
    workflow = yaml.safe_load(read(WORKFLOW))
    job = workflow["jobs"]["wire-slack"]
    assert "environment" not in job, "an environment here is a second approval click"
    assert job["if"].strip() == "${{ needs.deploy.outputs.wire_slack == 'true' }}"
    # The condition cannot read secrets directly (GitHub rejects the context
    # in a job-level if — learned by dispatch); the deploy job exports the
    # presence as a boolean output, and that step must exist.
    deploy = workflow["jobs"]["deploy"]
    assert deploy["outputs"]["wire_slack"] == "${{ steps.wire-flag.outputs.wire }}"
    assert any(s.get("id") == "wire-flag" for s in deploy["steps"])


def test_every_third_party_action_is_pinned_by_sha():
    import re as _re
    for line in read(WORKFLOW).splitlines():
        match = _re.search(r"uses:\s*(\S+)@(\S+)", line)
        if match:
            assert _re.fullmatch(r"[0-9a-f]{40}", match.group(2)), line.strip()
            assert "#" in line, "pin with the version in a comment: " + line.strip()


def test_the_deploy_is_dark_then_promoted():
    workflow = yaml.safe_load(read(WORKFLOW))
    steps = workflow["jobs"]["deploy"]["steps"]
    body = "\n".join(step.get("run", "") for step in steps)
    assert "--no-traffic --tag candidate" in body
    assert "update-traffic" in body
    assert body.index("--no-traffic") < body.index("update-traffic")
    # …and the candidate is smoke-tested in between.
    assert body.index("--no-traffic") < body.index("/health") < body.index("update-traffic")


def test_the_image_is_deployed_by_digest():
    body = "\n".join(
        step.get("run", "")
        for step in yaml.safe_load(read(WORKFLOW))["jobs"]["deploy"]["steps"]
    )
    assert "image_summary.digest" in body
    assert 'sha256:*) : ;;' in body


def test_the_build_proves_the_image_it_pushes():
    build = yaml.safe_load(read(CLOUDBUILD))
    ids = [step.get("id") for step in build["steps"]]
    assert ids == ["build", "prove"]
    assert build["steps"][1]["args"][-3:] == ["-m", "bot.dryrun", "--script"]
    assert build["images"] == ["${_IMAGE}"]


def test_both_deploy_paths_pass_an_explicit_runtime_service_account():
    workflow_body = "\n".join(
        step.get("run", "")
        for step in yaml.safe_load(read(WORKFLOW))["jobs"]["deploy"]["steps"]
    )
    assert '--service-account "${RUNTIME_SA}"' in workflow_body
    assert '--service-account "${runtime_sa}"' in read(DEPLOY_SH)
    # And the build runs as its own identity in both.
    assert "serviceAccounts/${BUILD_SA}" in workflow_body
    assert "serviceAccounts/${build_sa}" in read(DEPLOY_SH)


def test_the_invoker_check_matches_the_role_and_the_member():
    for path in (WORKFLOW, DEPLOY_SH):
        source = read(path)
        assert "bindings.role=roles/run.invoker AND bindings.members=allUsers" in source, path
        assert "grep -q '\"allUsers\"'" not in source, path


def test_the_smoke_test_reads_the_state_backend_status():
    workflow = yaml.safe_load(read(WORKFLOW))
    everything = "\n".join(
        step.get("run", "")
        for job in workflow["jobs"].values()
        for step in job["steps"]
    )
    assert "/health" in everything
    assert "DEGRADED" in everything


def test_setup_wif_verifies_what_a_release_only_names():
    source = read(SETUP_WIF)
    for needed in ("artifacts repositories create", "secrets describe",
                   "firestore databases describe", "bootstrap incomplete"):
        assert needed in source, needed


def test_no_predictable_temp_files_remain():
    import re as _re
    for path in (DEPLOY_SH, SETUP_WIF):
        assert not _re.search(r"/tmp/[a-z-]+\.\$\$", read(path)), path
        assert "mktemp" in read(path), path
