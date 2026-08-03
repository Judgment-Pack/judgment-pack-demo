#!/usr/bin/env python3
"""Create or re-point the Slack app from the committed manifest.

Three subcommands:

    slack-app.py create --url https://…run.app \
        --signing-secret-out ./signing --refresh-out ./slack-refresh-token
    slack-app.py update-url --app A0123456789 --url https://…run.app
    slack-app.py rotate --refresh-out ./slack-refresh-token --token-out ./slack-token

`create` posts the manifest to `apps.manifest.create` with every placeholder
URL replaced. `update-url` exports the app's current manifest, replaces the
same URLs, and puts it back — what a redeploy to a new URL needs. `rotate`
trades a refresh token for a fresh pair and writes both down, which is the
only safe way to use one unattended.

ORDERING MATTERS, and it is not a preference: Slack accepts the manifest
without probing the URL (observed 2026-08-03 — a successful `update` produced
zero requests to the endpoint), but it holds EVENT DELIVERY until the URL is
verified, and the one verification path that exists — the Retry button on the
Event Subscriptions page — only succeeds against a live, healthy service. So
the service must be up FIRST. The release workflow runs this after the
candidate revision has been smoke-tested and promoted, for exactly that
reason.

Secrets, and where they are allowed to go:

* The config token comes from `SLACK_CONFIG_TOKEN` or `--token-file`. It is
  never printed, and `--dry-run` redacts it.
* A refresh token is sent in the request BODY, never a query string — a query
  string is the HTTP equivalent of argv, and lands in every proxy and access
  log on the path.
* `create` returns the app's signing secret. It is written to the mode-0600
  file named by `--signing-secret-out` and nowhere else — never stdout, never
  a log line, never an argv a `ps` could read. With `--gcp-project` it is also
  added as a Secret Manager version, piped to gcloud's stdin.
* Config tokens expire after twelve hours, and **each rotation invalidates the
  previous refresh token**. A rotation whose output nobody stores has locked
  you out of the next run, so `--refresh-out` is required to rotate at all,
  and `--no-rotate` lets a caller that cannot store a new token refuse to mint
  one (exit 3) rather than burn the one it has.

PyYAML is needed only to read the local manifest for `create`. `update-url`
consumes the JSON Slack exports, so the CI path installs nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

SLACK_API = "https://slack.com/api/"
EVENTS_PATH = "/slack/events"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MANIFEST = os.path.join(os.path.dirname(HERE), "app_manifest.yml")

# The Slack errors that mean "your config token is stale", as opposed to "your
# manifest is wrong". Matched against the STRUCTURED error member, never the
# rendered message: a substring scan over prose is not a control decision.
STALE_TOKEN_ERRORS = ("token_expired", "invalid_auth", "not_authed")

# Every manifest member that carries a URL Slack will call. `request_url` and
# a slash command's `url` are the three in the committed file; the others
# appear in what `apps.manifest.export` returns once somebody has touched the
# app in the UI, and a missed one is a Slack callback going to the old
# revision.
URL_KEYS = ("request_url", "message_menu_options_url")
URL_LIST_KEYS = ("redirect_urls",)
# Anything ending in these is url-bearing enough to be worth checking after a
# substitution, even where this tool does not rewrite it.
URL_SUFFIXES = ("_url", "_urls", "_uri", "_uris")

EXIT_ROTATION_REFUSED = 3


class SlackError(Exception):
    """Slack answered, and the answer was no.

    Carries the structured `error` member when there is one, so callers decide
    on a code rather than on the text of a sentence.
    """

    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code = code
        self.status = status


# --- the manifest ----------------------------------------------------------


def load_manifest(path=DEFAULT_MANIFEST):
    """Read the committed YAML manifest. Only `create` needs this."""
    try:
        import yaml
    except ImportError:
        raise SlackError(
            "reading {} needs PyYAML (pip install pyyaml). Only `create` reads the "
            "local manifest; `update-url` works from Slack's JSON export and needs "
            "nothing.".format(path)
        )
    with open(path) as handle:
        return yaml.safe_load(handle)


def events_url(service_url):
    """The one URL Slack needs, from the service URL Cloud Run printed."""
    return service_url.rstrip("/") + EVENTS_PATH


def substitute_urls(manifest, service_url):
    """Point every URL Slack would call at this service.

    Returns (manifest, [paths that changed]). The three in the committed file
    are the event subscription, the interactivity endpoint and the slash
    command; an EXPORTED manifest can also carry a message-menu options URL and
    OAuth redirect URLs, configured in the UI by somebody who is not here.
    Those are rewritten too: a demo that answers buttons while sending its
    menus to a dead host is worse than one that fails loudly.
    """
    target = events_url(service_url)
    changed = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                where = path + "/" + str(key)
                if key in URL_KEYS and isinstance(value, str):
                    node[key] = target
                    changed.append(where)
                elif key == "url" and isinstance(value, str) and "command" in node:
                    # A slash command's endpoint. Other `url` members (an app's
                    # homepage, say) are left alone.
                    node[key] = target
                    changed.append(where)
                elif key in URL_LIST_KEYS and isinstance(value, list):
                    for index, entry in enumerate(value):
                        if isinstance(entry, str):
                            value[index] = target
                            changed.append("{}[{}]".format(where, index))
                else:
                    walk(value, where)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, "{}[{}]".format(path, index))

    walk(manifest, "")
    return manifest, changed


def foreign_urls(manifest, service_url):
    """Every url-bearing member still pointing somewhere else.

    The substitution above knows the members Slack documents today. This is
    the check that the knowledge was enough: anything url-shaped left pointing
    at another host is reported by path, so a member this tool has never heard
    of announces itself instead of quietly keeping the old URL.
    """
    host = urllib.parse.urlsplit(events_url(service_url)).netloc
    found = []

    def check(value, where):
        if not isinstance(value, str) or "://" not in value:
            return
        if urllib.parse.urlsplit(value).netloc != host:
            found.append((where, value))

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                where = path + "/" + str(key)
                url_shaped = key == "url" or key.endswith(URL_SUFFIXES)
                if url_shaped and isinstance(value, str):
                    check(value, where)
                elif url_shaped and isinstance(value, list):
                    for index, entry in enumerate(value):
                        check(entry, "{}[{}]".format(where, index))
                else:
                    walk(value, where)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, "{}[{}]".format(path, index))

    walk(manifest, "")
    return found


# --- tokens ----------------------------------------------------------------


def read_token(args):
    """The config token: from a file if given, else the environment."""
    if getattr(args, "token_file", None):
        with open(args.token_file) as handle:
            return handle.read().strip()
    return (os.environ.get("SLACK_CONFIG_TOKEN") or "").strip()


def read_refresh_token(args):
    if getattr(args, "refresh_token_file", None):
        with open(args.refresh_token_file) as handle:
            return handle.read().strip()
    return (os.environ.get("SLACK_CONFIG_REFRESH_TOKEN") or "").strip()


def redact(value):
    """Enough to recognize a token in a log, never enough to use one."""
    if not value:
        return "<none>"
    return "{}…{} ({} chars)".format(value[:6], value[-2:], len(value))


def write_private(path, value):
    """Write a secret to a file only its owner can read."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(value)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


# --- the API ---------------------------------------------------------------


def call(method, payload, token=None, opener=None):
    """One Slack API call. Raises SlackError when Slack says no.

    An HTTP error keeps Slack's body: a 429 with a reason, or a 401 naming
    `token_expired`, is diagnosis the caller needs — and the retry logic keys
    on that structured code, so discarding it would silently disable rotation
    wherever Slack answers with a status rather than `ok:false`.
    """
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(SLACK_API + method, data=data, headers=headers)
    send = opener or urllib.request.urlopen
    try:
        with send(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail, code = "", None
        try:
            answer = json.loads(error.read().decode("utf-8"))
            code = answer.get("error")
            detail = json.dumps(answer)
        except Exception:  # noqa: BLE001 - a body that is not JSON is still a clue
            detail = ""
        explain = code or detail
        raise SlackError(
            "{} refused: HTTP {}{}".format(
                method, error.code, " — " + explain if explain else ""
            ),
            code=code,
            status=error.code,
        )
    if not body.get("ok"):
        raise SlackError(
            "{} refused: {}".format(method, body.get("error", "unknown")),
            code=body.get("error"),
        )
    return body


def rotate(refresh_token, opener=None):
    """Trade a refresh token for a fresh config token and a NEW refresh token.

    The refresh token goes in the BODY. In the query string it would land in
    every TLS terminator, proxy and access log between here and Slack — the
    same reason the signing secret is piped to gcloud's stdin rather than
    passed as an argument.
    """
    body = urllib.parse.urlencode({"refresh_token": refresh_token}).encode("utf-8")
    request = urllib.request.Request(
        SLACK_API + "tooling.tokens.rotate",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    send = opener or urllib.request.urlopen
    with send(request, timeout=60) as response:
        answer = json.loads(response.read().decode("utf-8"))
    if not answer.get("ok"):
        raise SlackError(
            "tooling.tokens.rotate refused: " + str(answer.get("error")),
            code=answer.get("error"),
        )
    return answer["token"], answer.get("refresh_token", "")


class Session(object):
    """A config token that renews itself once — if it is allowed to."""

    def __init__(self, token, refresh_token="", refresh_out=None, opener=None,
                 log=print, allow_rotation=True):
        self.token = token
        self.refresh_token = refresh_token
        self.refresh_out = refresh_out
        self.opener = opener
        self.log = log
        self.allow_rotation = allow_rotation
        self.rotated = False

    def ensure_token(self):
        if self.token:
            return self.token
        if not self.refresh_token:
            raise SlackError(
                "no config token: set SLACK_CONFIG_TOKEN (or --token-file), or give a "
                "refresh token so one can be minted"
            )
        self._rotate()
        return self.token

    def _rotate(self):
        if not self.allow_rotation:
            raise SlackError(
                "the config token is expired and rotation is disabled here, because "
                "nothing in this environment can store the new refresh token — and a "
                "rotation nobody stores kills the credential. Rotate where you can "
                "keep it:\n"
                "    python3 slack/deploy/slack-app.py rotate \\\n"
                "      --refresh-out ./slack-refresh-token --token-out ./slack-token\n"
                "then update the stored pair.",
                code="rotation_refused",
            )
        if not self.refresh_out:
            raise SlackError(
                "refusing to rotate without --refresh-out: the new refresh token would "
                "be the only copy, and it replaces the one you have.",
                code="rotation_refused",
            )
        token, refresh_token = rotate(self.refresh_token, opener=self.opener)
        self.token = token
        self.rotated = True
        if refresh_token:
            self.refresh_token = refresh_token
            write_private(self.refresh_out, refresh_token)
            self.log("wrote the new refresh token to {} (mode 600). The previous one is "
                     "now dead — store this before the next run.".format(self.refresh_out))

    def call(self, method, payload):
        """Call, and on a STALE-TOKEN code rotate once and try again."""
        self.ensure_token()
        try:
            return call(method, payload, token=self.token, opener=self.opener)
        except SlackError as error:
            if not self.refresh_token or error.code not in STALE_TOKEN_ERRORS:
                raise
            self.log("the config token was stale; rotating and retrying once")
            self._rotate()
            return call(method, payload, token=self.token, opener=self.opener)


# --- gcloud (optional) -----------------------------------------------------


def add_secret_version(project, name, value, runner=subprocess.run):
    """Pipe a secret to gcloud's stdin. It never appears in argv."""
    create = runner(
        ["gcloud", "secrets", "create", name, "--replication-policy=automatic",
         "--project", project, "--quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if create.returncode != 0:
        detail = (create.stderr or b"").decode("utf-8", "replace")
        if "already exists" not in detail.lower():
            return False, detail.strip()
    added = runner(
        ["gcloud", "secrets", "versions", "add", name, "--data-file=-",
         "--project", project, "--quiet"],
        input=value.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if added.returncode != 0:
        return False, (added.stderr or b"").decode("utf-8", "replace").strip()
    return True, ""


# --- subcommands -----------------------------------------------------------


def _session(args, opener, log):
    return Session(
        read_token(args),
        read_refresh_token(args),
        getattr(args, "refresh_out", None),
        opener,
        log,
        allow_rotation=not getattr(args, "no_rotate", False),
    )


def _point_urls(manifest, args, log):
    manifest, changed = substitute_urls(manifest, args.url)
    log("pointed {} url(s) at {}".format(len(changed), events_url(args.url)))
    for where in changed:
        log("  " + where)
    stale = foreign_urls(manifest, args.url)
    if stale:
        log("URLs still pointing somewhere else:")
        for where, value in stale:
            log("  {} = {}".format(where, value))
    return manifest, changed, stale


def cmd_create(args, opener=None, log=print):
    manifest, _, stale = _point_urls(load_manifest(args.manifest), args, log)
    if stale and not args.allow_foreign_urls:
        raise SlackError(
            "the manifest still has url-bearing members pointing at another host "
            "(listed above). Fix them, or pass --allow-foreign-urls if they are "
            "deliberately somewhere else."
        )

    payload = {"manifest": manifest}
    if args.dry_run:
        log("DRY RUN — would POST apps.manifest.create")
        log("  Authorization: Bearer " + redact(read_token(args)))
        log(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    session = _session(args, opener, log)
    body = session.call("apps.manifest.create", payload)
    app_id = body.get("app_id", "")
    log("created app {}".format(app_id))

    credentials = body.get("credentials") or {}
    signing_secret = credentials.get("signing_secret", "")
    if signing_secret and args.signing_secret_out:
        write_private(args.signing_secret_out, signing_secret)
        log("wrote the signing secret to {} (mode 600, never printed)".format(
            args.signing_secret_out))
        if args.gcp_project:
            ok, detail = add_secret_version(
                args.gcp_project, args.secret_name, signing_secret
            )
            log("Secret Manager {}: {}".format(
                args.secret_name, "new version added" if ok else "FAILED — " + detail))
    elif signing_secret:
        log("NOTE: the app has a signing secret and no --signing-secret-out was given, "
            "so it was discarded rather than printed. Copy it from "
            "api.slack.com/apps → Basic Information, or re-run with the flag.")

    log("")
    log("Next, and only a human can do it: open api.slack.com/apps → {} → Install App"
        .format(app_id))
    log("and install it to the workspace. That consent is what mints the bot token.")
    return 0


def cmd_update_url(args, opener=None, log=print):
    """Re-point an existing app. Reads Slack's JSON export — no YAML needed."""
    session = None
    if args.dry_run:
        manifest = load_manifest(args.manifest)
    else:
        session = _session(args, opener, log)
        exported = session.call("apps.manifest.export", {"app_id": args.app})
        manifest = exported["manifest"]

    manifest, _, stale = _point_urls(manifest, args, log)
    if stale and not args.allow_foreign_urls:
        raise SlackError(
            "the exported manifest still has url-bearing members pointing at another "
            "host (listed above) — a Slack callback would go to the old revision. Fix "
            "them in the app's settings, or pass --allow-foreign-urls if they belong "
            "somewhere else."
        )

    payload = {"app_id": args.app, "manifest": manifest}
    if args.dry_run:
        log("DRY RUN — would POST apps.manifest.export then apps.manifest.update")
        log("  Authorization: Bearer " + redact(read_token(args)))
        log(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    body = session.call("apps.manifest.update", payload)
    log("updated app {}".format(args.app))
    for warning in body.get("warnings") or []:
        log("Slack warning: {}".format(json.dumps(warning)))
    log("The manifest API SETS the request URL without firing a url_verification "
        "challenge (observed 2026-08-03: a successful update produced zero requests "
        "to the endpoint), and Slack holds event delivery until the URL is verified "
        "once. If events do not arrive, open api.slack.com/apps -> Event "
        "Subscriptions and click Retry next to the URL -- the running service "
        "answers the challenge, and delivery starts.")
    return 0


def cmd_rotate(args, opener=None, log=print):
    """Mint a fresh config token pair and write both down."""
    refresh_token = read_refresh_token(args)
    if not refresh_token:
        raise SlackError(
            "no refresh token: set SLACK_CONFIG_REFRESH_TOKEN or --refresh-token-file"
        )
    if args.dry_run:
        log("DRY RUN — would POST tooling.tokens.rotate (refresh token in the body)")
        log("  refresh_token: " + redact(refresh_token))
        return 0
    token, new_refresh = rotate(refresh_token, opener=opener)
    write_private(args.refresh_out, new_refresh)
    log("wrote the new refresh token to {} (mode 600)".format(args.refresh_out))
    if args.token_out:
        write_private(args.token_out, token)
        log("wrote the new access token to {} (mode 600)".format(args.token_out))
    log("The previous pair is dead. Store both of these NOW — a rotation nobody "
        "stores is a credential nobody has.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="slack-app.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def tokens(target):
        target.add_argument("--token-file", help="file holding the Slack config token")
        target.add_argument("--refresh-token-file", help="file holding the refresh token")
        target.add_argument("--refresh-out",
                            help="where to write the NEW refresh token after a rotation")
        target.add_argument("--dry-run", action="store_true",
                            help="print the payloads (token redacted) and call nothing")

    def urls(target):
        target.add_argument("--url", required=True,
                            help="the deployed service URL; /slack/events is appended")
        target.add_argument("--manifest", default=DEFAULT_MANIFEST)
        target.add_argument("--allow-foreign-urls", action="store_true",
                            help="proceed even if a url-bearing member points elsewhere")
        target.add_argument("--no-rotate", action="store_true",
                            help="refuse to rotate an expired token (exit 3) instead of "
                                 "minting one nothing here can store")

    create = sub.add_parser("create", help="create the app from the committed manifest")
    tokens(create)
    urls(create)
    create.add_argument("--signing-secret-out",
                        help="write the new app's signing secret here (mode 600)")
    create.add_argument("--gcp-project",
                        help="also add the signing secret to Secret Manager in this project")
    create.add_argument("--secret-name", default="SLACK_SIGNING_SECRET")
    create.set_defaults(run=cmd_create)

    update = sub.add_parser("update-url", help="re-point an existing app at a new URL")
    tokens(update)
    urls(update)
    update.add_argument("--app", required=True, help="the app id (A0123456789)")
    update.set_defaults(run=cmd_update_url)

    rotate_command = sub.add_parser(
        "rotate", help="mint a fresh config token pair and write both down")
    tokens(rotate_command)
    rotate_command.add_argument("--token-out", help="write the new access token here")
    rotate_command.set_defaults(run=cmd_rotate)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "rotate" and not args.refresh_out:
        sys.stderr.write("slack-app.py rotate needs --refresh-out\n")
        return 2
    try:
        return args.run(args)
    except SlackError as error:
        sys.stderr.write("slack-app.py: {}\n".format(error))
        # A refused rotation is a decision, not a crash: the caller can treat
        # it as "skip the optional wiring" rather than "the release failed".
        return EXIT_ROTATION_REFUSED if error.code == "rotation_refused" else 1
    except OSError as error:
        sys.stderr.write("slack-app.py: {}\n".format(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
