"""Slack wiring: events, interactivity, the slash command.

The only module that imports slack_bolt — everything else (flows, runtime,
desk, model, blocks) stays importable without it, which is why the tests and
the dry run can exercise the whole demo with no Slack at all.

Request handling shape, and why:

* Bolt verifies every request's signature with the signing secret before a
  handler sees it, and this process refuses to start without one.
* Slack wants a 200 within three seconds and retries otherwise, so each
  handler acknowledges immediately and does the real work — evaluations,
  gateway calls, model calls — on a thread pool.
* Every retry carries the same `event_id`, so the de-duplicator drops it: a
  retry must never run a second evaluation or post a second narration.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

if __package__ in (None, ""):  # allow `python3 slack/bot/app.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import blocks, content, flows  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.desk import DeskManager  # noqa: E402
from bot.flows.base import Deps, Turn  # noqa: E402
from bot.model import build_model  # noqa: E402
from bot.reconcile import Reconciler  # noqa: E402
from bot.runtime import JpackRuntime  # noqa: E402
from bot.state import SessionStore, TurnLock  # noqa: E402
from bot.store import build_backend  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("jpack-slack")

CONFIG = Config.from_env()
CONFIG.require_slack()  # a server that cannot verify signatures must not serve

RUNTIME = JpackRuntime(CONFIG)
DESK = DeskManager(CONFIG)
# Where session metadata lives, decided once. A Firestore backend that cannot
# be reached raises here, before the server starts: a demo configured to
# remember that silently forgets is worse than one that refuses to boot.
BACKEND = build_backend(CONFIG)
# Two budgets: the vendor-metered one over model calls, and a generous one
# over turns, because a flood of free subprocesses sinks one small instance
# just as surely as a flood of paid tokens. Both live behind the same
# interface as the sessions, so neither app.py nor a flow knows which is live.
LIMITER = BACKEND.limiter("model", CONFIG.model_calls_per_hour)
TURN_LIMITER = BACKEND.limiter("turns", CONFIG.turns_per_hour)
DEPS = Deps(config=CONFIG, runtime=RUNTIME, desk=DESK, model=build_model(CONFIG, LIMITER))
SESSIONS = SessionStore(CONFIG, on_evict=DESK.stop, backend=BACKEND)
# Rebuilds the local half of a session this process has never run — and says
# so plainly when something (a signing desk, a decision book) cannot be
# rebuilt rather than resuming a flow whose evidence is gone.
RECONCILER = Reconciler(RUNTIME, DESK)
SEEN = BACKEND.dedupe()
WORKERS = int(os.environ.get("WORKERS", "8"))
POOL = ThreadPoolExecutor(max_workers=WORKERS)
# Strictly below the pool size: there is always a worker free to answer with,
# even when every heavy turn is busy.
WORK = threading.BoundedSemaphore(max(1, min(CONFIG.max_concurrent_work, WORKERS - 2)))
WORK_WAIT_SECONDS = 20

from slack_bolt import App  # noqa: E402
from slack_bolt.adapter.wsgi import SlackRequestHandler  # noqa: E402

app = App(
    token=CONFIG.slack_bot_token,
    signing_secret=CONFIG.slack_signing_secret,
    # Request signature verification is never optional. Token verification is
    # a boot-time auth.test call, which a smoke test with a placeholder token
    # cannot make: SLACK_TOKEN_VERIFICATION=0 skips only that.
    request_verification_enabled=True,
    token_verification_enabled=os.environ.get("SLACK_TOKEN_VERIFICATION", "1") != "0",
)

EVENTS_PATH = os.environ.get("SLACK_EVENTS_PATH", "/slack/events")


# --- helpers ---------------------------------------------------------------


def _post(client, channel, replies):
    for one in replies:
        client.chat_postMessage(channel=channel, blocks=one.blocks, text=one.text)


def _say(client, channel, markdown):
    client.chat_postMessage(
        channel=channel, blocks=[blocks.section(markdown)], text="Judgment Pack demo"
    )


def _dm_channel(client, user_id):
    opened = client.conversations_open(users=user_id)
    return opened["channel"]["id"]


def _run(job, *args, **kwargs):
    """Do the slow part off the request thread, and never die silently."""

    def wrapped():
        try:
            job(*args, **kwargs)
        except Exception:  # noqa: BLE001
            log.error("handler failed:\n%s", traceback.format_exc())

    POOL.submit(wrapped)


def _turn(client, user_id, channel, build, publish_home=False):
    """Run one user's turn: serialized, budgeted, streamed, and never silent.

    Four guards, in order, and each one answers rather than swallowing:

    1. THE TURN LOCK. One turn per user at a time. Slack's interactive
       payloads carry no event id, so de-duplication cannot see a
       double-click; this can. A second click is told the first is still
       running instead of racing it through the same session and the same
       scratch project.
    2. THE TURN BUDGET. Every step starts subprocesses on one small instance,
       so turns are metered generously per user — free work floods a single
       CPU as easily as paid work.
    3. THE WORK SEMAPHORE. Concurrent heavy turns are capped below the pool
       size, so the pool always has a thread left to answer with.
    4. THE SINK. Replies a flow flushes go out the moment they exist, which
       is how a disposition reaches Slack without waiting for a narration.

    And, once per restored session, RECONCILIATION: a turn whose session came
    back from durable state into a container that has never run it gets its
    rebuildable parts rebuilt first, and one plain line when something cannot
    be rebuilt. That line is posted before the flow's own output, because the
    user is owed the reason before the consequence.
    """
    session = SESSIONS.get(user_id)
    target = channel or _dm_channel(client, user_id)

    with TurnLock(session) as held:
        if not held:
            _say(client, target, content.BUSY)
            log.info("dropped a concurrent turn for %s", user_id)
            return
        if not TURN_LIMITER.allow(user_id):
            wait = TURN_LIMITER.retry_after(user_id)
            _say(
                client,
                target,
                content.TURNS_LIMITED.format(
                    n=CONFIG.turns_per_hour, minutes=max(1, wait // 60)
                ),
            )
            return
        if not WORK.acquire(timeout=WORK_WAIT_SECONDS):
            _say(client, target, content.BUSY_GLOBAL)
            log.info("work semaphore full; refused a turn for %s", user_id)
            return
        try:
            notice = RECONCILER.reconcile(session)
            if notice:
                _say(client, target, notice)
            deps = replace(DEPS, sink=lambda replies: _post(client, target, replies))
            replies = build(session, deps) or []
            _post(client, target, replies)
        except Exception:  # noqa: BLE001 - a dropped turn is worse than an ugly one
            log.error("turn for %s failed:\n%s", user_id, traceback.format_exc())
            try:
                _say(client, target, content.TURN_FAILED)
            except Exception:  # noqa: BLE001
                log.error("could not even report the failure to %s", user_id)
        finally:
            # Whatever the turn did to the session — advanced a step, finished
            # a flow, reset one — is written back before the lock is released.
            SESSIONS.save(session)
            WORK.release()
        if publish_home:
            try:
                client.views_publish(user_id=user_id, view=_home_view(session))
            except Exception:  # noqa: BLE001 - the message already landed
                log.warning("could not refresh the home tab for %s", user_id)


def _home_view(session):
    body = [
        blocks.header(content.WELCOME_HEADER),
        blocks.section(content.WELCOME),
        blocks.divider(),
    ]
    body += blocks.menu_blocks(flows.CATALOGUE, session.completed)
    body.append(blocks.divider())
    body += blocks.about_blocks()
    return {"type": "home", "blocks": body[:100]}


# --- entry points ----------------------------------------------------------


def welcome_once(session, deps):
    """The welcome is a greeting, not a habit: exactly one per session."""
    if session.welcomed:
        return [flows.menu(session)]
    session.welcomed = True
    return [flows.welcome(session)]


@app.event("team_join")
def on_team_join(body, event, client):
    """A new member joined: DM them the welcome and the menu."""
    if SEEN.seen(body.get("event_id")):
        return
    user = event.get("user")
    user_id = user.get("id") if isinstance(user, dict) else user
    if not user_id:
        return
    _run(_turn, client, user_id, None, welcome_once)


@app.event("app_home_opened")
def on_home_opened(body, event, client):
    if SEEN.seen(body.get("event_id")):
        return
    user_id = event.get("user")
    tab = event.get("tab")
    channel = event.get("channel")
    if not user_id:
        return

    if tab == "home":

        def publish():
            session = SESSIONS.get(user_id)
            client.views_publish(user_id=user_id, view=_home_view(session))

        _run(publish)
        return

    # The Messages tab. Greet once — every open is a distinct event, so the
    # de-duplicator cannot help, and re-greeting somebody mid-demo is noise.
    def build(session, deps):
        if session.welcomed:
            return []
        session.welcomed = True
        return [flows.welcome(session)]

    _run(_turn, client, user_id, channel, build)


@app.event("message")
def on_message(body, event, client):
    """Direct messages: route into the active flow, or answer at the menu."""
    if SEEN.seen(body.get("event_id")):
        return
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id") or event.get("subtype"):
        return
    user_id = event.get("user")
    channel = event.get("channel")
    text = (event.get("text") or "").strip()
    if not user_id or not channel:
        return

    def build(session, deps):
        session.welcomed = True
        lowered = text.lower()
        if lowered in ("menu", "help", "start", "hi", "hello"):
            return [flows.menu(session)]
        if lowered in ("about", "what is this"):
            return [flows.about(session)]
        replies = flows.dispatch(Turn(user_id=user_id, session=session, text=text), deps)
        if replies is not None:
            return replies
        # Nothing running: conversational glue, attributed like every other
        # model utterance, then the menu.
        glue = deps.model.glue(user_id, text, situation="at the menu, nothing running")
        replies = []
        if glue.ok:
            replies.append(
                flows.reply(
                    blocks.model_blocks(glue.text, deps.model.name, label="Host reply"),
                    text="Hello",
                )
            )
        replies.append(flows.menu(session))
        return replies

    _run(_turn, client, user_id, channel, build)


@app.command("/jpack")
def on_command(ack, body, client):
    ack()
    user_id = body.get("user_id")
    channel = body.get("channel_id")
    argument = (body.get("text") or "").strip().lower()
    if not user_id:
        return

    def build(session, deps):
        session.welcomed = True
        if argument == "about":
            return [flows.about(session)]
        if argument in flows.BY_ID:
            return flows.start(Turn(user_id=user_id, session=session), deps, argument)
        return [flows.menu(session)]

    # The slash command answers in a DM when the app is not in the channel it
    # was typed in: a flow is a conversation, not a one-shot reply.
    _run(_turn, client, user_id, channel, build)


@app.action(blocks.ACTION_MENU)
def on_menu(ack, body, client):
    ack()
    _respond_with(body, client, lambda session, deps: [flows.menu(session)])


@app.action(blocks.ACTION_ABOUT)
def on_about(ack, body, client):
    ack()
    _respond_with(body, client, lambda session, deps: [flows.about(session)])


@app.action(re.compile(r"^jp:start:"))
def on_start(ack, body, client):
    ack()
    action_id = body["actions"][0]["action_id"]
    flow_id = action_id[len(blocks.ACTION_START):]
    user_id = (body.get("user") or {}).get("id")

    def build(session, deps):
        return flows.start(Turn(user_id=user_id, session=session), deps, flow_id)

    _respond_with(body, client, build)


@app.action(re.compile(r"^jp:step:"))
def on_step(ack, body, client):
    ack()
    action_id = body["actions"][0]["action_id"]
    rest = action_id[len(blocks.ACTION_STEP):]
    flow_id, _, token = rest.partition(":")
    user_id = (body.get("user") or {}).get("id")

    def build(session, deps):
        if session.active_flow != flow_id:
            # A button from an earlier message: re-enter that flow rather than
            # dropping the click on the floor.
            return flows.start(Turn(user_id=user_id, session=session), deps, flow_id)
        turn = Turn(user_id=user_id, session=session, action=token)
        return flows.dispatch(turn, deps) or [flows.menu(session)]

    _respond_with(body, client, build)


def _respond_with(body, client, build):
    user_id = (body.get("user") or {}).get("id")
    channel = (body.get("channel") or {}).get("id")
    from_home = (body.get("view") or {}).get("type") == "home"
    if not user_id:
        return
    _run(_turn, client, user_id, channel, build, from_home)


# --- boot ------------------------------------------------------------------


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """One thread per request, so a slow post cannot delay an ack.

    Bolt's own `App.start()` is a single-threaded development server; this is
    the same stdlib machinery with threading mixed in, which is honest for a
    one-instance demo. Gunicorn in front of the same WSGI app is the named
    upgrade, and it needs no code change here.
    """

    daemon_threads = True


def wsgi_app(environ, start_response):
    """Slack's path goes to bolt; everything else gets a boring 200/404."""
    path = environ.get("PATH_INFO", "/")
    if path == EVENTS_PATH:
        return SLACK_HANDLER(environ, start_response)
    if path in ("/", "/healthz"):
        start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"judgment-pack slack demo: up. Slack posts to " + EVENTS_PATH.encode()]
    start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
    return [b"not found"]


SLACK_HANDLER = SlackRequestHandler(app, path=EVENTS_PATH)


def main():
    log.info("configuration: %s", CONFIG.redacted())
    log.info(
        "state: %s — session progress %s restarts; scratch projects and screening "
        "desks are rebuilt per container either way",
        BACKEND.name,
        "survives" if BACKEND.durable else "does NOT survive",
    )
    version = RUNTIME.run(["version"])
    if version.ok:
        log.info("runtime: %s", version.stdout.strip().replace("\n", " "))
    else:
        log.error("the jpack binary did not answer `version` — use cases will refuse loudly")
    SESSIONS.start_sweeper()  # expire on a timer, not on the next visitor
    server = make_server("0.0.0.0", CONFIG.port, wsgi_app, ThreadingWSGIServer)
    log.info("listening on :%s%s", CONFIG.port, EVENTS_PATH)
    server.serve_forever()


if __name__ == "__main__":
    main()
