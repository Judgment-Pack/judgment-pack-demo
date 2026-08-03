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

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

if __package__ in (None, ""):  # allow `python3 slack/bot/app.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import blocks, content, flows, lead  # noqa: E402
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
# The lead modal bypasses the turn lock (a trigger_id dies in ~3s), so it
# gets its own small meter: an unmetered open/submit loop would let one
# person page the triage channel.
HUMAN_LIMITER = BACKEND.limiter("human", 6)
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

# The human buttons render only when their promise has a recipient.
blocks.configure_human_handoff(bool(CONFIG.triage_channel))


# --- helpers ---------------------------------------------------------------


def _dm_channel(client, user_id):
    opened = client.conversations_open(users=user_id)
    return opened["channel"]["id"]


class Delivery:
    """Where one turn's replies go, with one honest fallback.

    Most turns target the user's DM. Interactive payloads, though, carry the
    channel of the message whose button was clicked, and nothing guarantees
    the bot can post there (`not_in_channel` / `channel_not_found` — and no
    prefix test identifies these up front, since every DM id starts with `D`,
    a colleague's included). The first refused post switches the rest of the
    turn to the user's DM, tells the invoking surface once via the command's
    response_url when there is one, and re-sends the reply that failed. The
    user never sees silence.
    """

    def __init__(self, client, user_id, channel, respond=None):
        self.client = client
        self.user_id = user_id
        self.target = channel  # None resolves to the DM on first use
        self.respond = respond
        self._dm = None

    def dm(self):
        if self._dm is None:
            self._dm = _dm_channel(self.client, self.user_id)
        return self._dm

    def _channel(self):
        if self.target is None:
            self.target = self.dm()
        return self.target

    def _send(self, block_list, text):
        self.client.chat_postMessage(channel=self._channel(), blocks=block_list, text=text)

    def post_one(self, block_list, text):
        try:
            self._send(block_list, text)
            return
        except Exception as error:  # noqa: BLE001 - inspected, re-raised when foreign
            code = _slack_error_code(error)
            if code not in ("not_in_channel", "channel_not_found") or self.target == self.dm():
                raise
            log.info(
                "cannot post in %s (%s); the rest of this turn goes to the DM",
                self.target,
                code,
            )
        self.target = self.dm()
        if self.respond is not None:
            try:
                self.respond(
                    text="I cannot post in this conversation, so I answered in our DM."
                )
            except Exception:  # noqa: BLE001 - the pointer is a courtesy
                log.warning("could not post the ephemeral pointer")
            self.respond = None
        self._send(block_list, text)

    def post(self, replies):
        for one in replies:
            self.post_one(one.blocks, one.text)

    def say(self, markdown):
        self.post_one([blocks.section(markdown)], "Judgment Pack demo")


def _slack_error_code(error):
    """The `error` member of a Slack API refusal, whatever SDK raised it."""
    response = getattr(error, "response", None)
    try:
        return (response or {}).get("error", "")
    except AttributeError:
        return ""


def _user_tag(user_id):
    """A stable non-PII handle for the metrics line: enough to count distinct
    users and follow one user's funnel, nothing to look one up by."""
    return hashlib.sha256((user_id or "?").encode()).hexdigest()[:8]


def _run(job, *args, **kwargs):
    """Do the slow part off the request thread, and never die silently."""

    def wrapped():
        try:
            job(*args, **kwargs)
        except Exception:  # noqa: BLE001
            log.error("handler failed:\n%s", traceback.format_exc())

    POOL.submit(wrapped)


def _turn(client, user_id, channel, build, publish_home=False, respond=None):
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
    delivery = Delivery(client, user_id, channel, respond)
    started = time.monotonic()
    outcome = "ok"
    # The funnel line names the turn by where it STARTED: a flow that ends
    # this turn (finished, failed, left) has already cleared these fields by
    # the time the line is written, and a line that only ever showed the
    # after-state could not tell a drop-off from an idle menu visit.
    entry_flow = session.active_flow or "-"
    entry_step = session.step

    try:
        with TurnLock(session) as held:
            if not held:
                outcome = "busy"
                delivery.say(content.BUSY)
                log.info("dropped a concurrent turn for %s", user_id)
                return
            if not TURN_LIMITER.allow(user_id):
                outcome = "limited"
                wait = TURN_LIMITER.retry_after(user_id)
                delivery.say(
                    content.TURNS_LIMITED.format(
                        n=CONFIG.turns_per_hour, minutes=max(1, wait // 60)
                    )
                )
                return
            if not WORK.acquire(timeout=WORK_WAIT_SECONDS):
                outcome = "busy-global"
                delivery.say(content.BUSY_GLOBAL)
                log.info("work semaphore full; refused a turn for %s", user_id)
                return
            try:
                entry_flow = session.active_flow or "-"
                entry_step = session.step
                if not session.persist:
                    # The backend read failed: this turn is served on a blank
                    # session and NOTHING is written for this user until a read
                    # succeeds. Say so — silently pretending they are new is how
                    # progress gets destroyed.
                    delivery.say(content.STATE_UNAVAILABLE)
                notice = RECONCILER.reconcile(session)
                if notice:
                    delivery.say(notice)
                deps = replace(DEPS, sink=delivery.post)
                replies = build(session, deps) or []
                delivery.post(replies)
            except Exception:  # noqa: BLE001 - a dropped turn is worse than an ugly one
                outcome = "failed"
                log.error("turn for %s failed:\n%s", user_id, traceback.format_exc())
                try:
                    delivery.say(content.TURN_FAILED)
                except Exception:  # noqa: BLE001
                    log.error("could not even report the failure to %s", user_id)
            finally:
                # The router and the flows stamp what the turn meant for the
                # funnel (finished, left, flow-failed); an exception outranks
                # the stamp, and the stamp must never persist either way.
                stamped = session.data.pop("funnel_outcome", None)
                if outcome == "ok" and stamped:
                    outcome = stamped
                # Whatever the turn did to the session — advanced a step, finished
                # a flow, reset one — is written back before the lock is released.
                SESSIONS.save(session)
                WORK.release()
            if publish_home:
                try:
                    client.views_publish(user_id=user_id, view=_home_view(session))
                except Exception:  # noqa: BLE001 - the message already landed
                    log.warning("could not refresh the home tab for %s", user_id)
    finally:
        # The one funnel line this service emits: every turn, every outcome.
        # Cloud Logging retains these; log-based metrics count them.
        log.info(
            "event=turn user=%s flow=%s step=%d completed=%d outcome=%s persisted=%d ms=%d",
            _user_tag(user_id),
            entry_flow,
            entry_step,
            len(session.completed),
            outcome,
            int(session.persist),
            int((time.monotonic() - started) * 1000),
        )


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
        # People type "Help!" and "what is this?" — match what they mean,
        # not their punctuation.
        lowered = re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()
        if lowered in ("stop", "quit", "cancel", "exit", "start over", "reset", "menu"):
            if session.active_flow:
                flow = flows.BY_ID.get(session.active_flow)
                title = flow.TITLE if flow else session.active_flow
                session.leave()
                return [
                    flows.menu(
                        session,
                        intro="*Left {}* — it is not marked complete, and your place is "
                        "kept: its own buttons resume it where you stopped, and starting "
                        "it from this menu starts it fresh.".format(title),
                    )
                ]
            return [flows.menu(session)]
        if lowered in ("help", "start", "hi", "hello", "hey"):
            return [flows.menu(session)]
        if lowered in ("about", "what is this", "whats this"):
            return [flows.about(session)]
        replies = flows.dispatch(Turn(user_id=user_id, session=session, text=text), deps)
        if replies is not None:
            return replies
        # Nothing running: conversational glue, attributed like every other
        # model utterance, then the menu. When the model cannot answer, its
        # note says why — a question answered with a bare menu reads as a
        # bot that ignored it.
        glue = deps.model.glue(user_id, text, situation="at the menu, nothing running")
        return [
            flows.reply(
                blocks.model_blocks(glue.text, deps.model.name, label="Host reply", note=glue.note),
                text="Hello",
            ),
            flows.menu(session),
        ]

    _run(_turn, client, user_id, channel, build)


@app.command("/jpack")
def on_command(ack, body, client, respond):
    ack()
    user_id = body.get("user_id")
    channel = body.get("channel_id") or ""
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

    # Every slash turn lives in the DM. Half of this demo is typed input —
    # the pasted policy, questions, the menu keywords — and `message.im` is
    # the only message event the app subscribes to, so a use case parked in
    # a channel would be a conversation that cannot hear the user. When the
    # command came from anywhere else, that surface gets one ephemeral
    # pointer; posting the pointer needs no membership, the response_url
    # carries it.
    def deliver():
        dm = _dm_channel(client, user_id)
        if channel and channel != dm:
            try:
                respond(
                    text="Answered in our DM — this demo runs on typed input too "
                    "(policies, questions), so the conversation lives there."
                )
            except Exception:  # noqa: BLE001 - the pointer is a courtesy
                log.warning("could not post the ephemeral pointer for /jpack")
        _turn(client, user_id, dm, build)

    _run(deliver)


@app.action(blocks.ACTION_MENU)
def on_menu(ack, body, client):
    ack()

    def build(session, deps):
        # "Back to the menu" means it: a menu that renders while the flow
        # stays active is an escape hatch painted on a wall. Leaving credits
        # nothing and KEEPS the place — the flow's own buttons resume it at
        # the step the user left (flows.resume), so looking at the menu
        # never costs them the cases they already ran.
        session.leave()
        return [flows.menu(session)]

    _respond_with(body, client, build)


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
        if session.active_flow != flow_id and not flows.resume(session, flow_id):
            # A button from an earlier message, for a flow the user neither
            # holds nor left: enter it fresh rather than dropping the click.
            # (When they LEFT it, resume() just put them back at their step,
            # and the dispatch below continues from there.)
            return flows.start(Turn(user_id=user_id, session=session), deps, flow_id)
        turn = Turn(user_id=user_id, session=session, action=token)
        return flows.dispatch(turn, deps) or [flows.menu(session)]

    _respond_with(body, client, build)


@app.action(lead.ACTION_OPEN)
def on_human(ack, body, client):
    """Open the lead modal NOW, on this thread.

    The trigger_id is single-use and expires in about three seconds, and the
    turn lock can hold a turn far longer than that — so this is the one
    handler that never goes through _turn.
    """
    ack()
    user_id = (body.get("user") or {}).get("id")
    trigger_id = body.get("trigger_id")
    if not user_id or not trigger_id or not CONFIG.triage_channel:
        return
    if not HUMAN_LIMITER.allow(user_id):
        _run(
            _notify_dm,
            client,
            user_id,
            "A few of these are already on their way — try the button again in a "
            "little while, or just type the note here and I will hold it for you.",
        )
        return
    try:
        client.views_open(trigger_id=trigger_id, view=lead.build_modal())
    except Exception:  # noqa: BLE001 - the form failing must not eat the intent
        log.error("could not open the lead modal:\n%s", traceback.format_exc())
        _run(
            _notify_dm,
            client,
            user_id,
            "The form would not open just now — DM me the note instead; it reaches "
            "the same people.",
        )


def _notify_dm(client, user_id, markdown):
    Delivery(client, user_id, None).say(markdown)


@app.view(lead.CALLBACK_ID)
def on_lead(ack, body, client):
    ack()
    user = body.get("user") or {}
    view = body.get("view") or {}
    _run(_deliver_lead, client, user.get("id"), user.get("username") or user.get("name") or "", view)


def _deliver_lead(client, user_id, username, view):
    """Post the lead to the triage channel, or refuse to lose it quietly.

    Delivery failure DMs the user a plain fallback naming where to file the
    note, and the full payload goes to the error log so the lead survives
    somewhere even then. Repeat notes from the same user thread under their
    first, so the channel stays a queue instead of a pile.
    """
    parsed = lead.parse_submission(view)
    session = SESSIONS.get(user_id, create=False)
    thread_ts = session.data.get("triage_ts") if session is not None else None
    try:
        posted = client.chat_postMessage(
            channel=CONFIG.triage_channel,
            blocks=lead.triage_blocks(user_id, username, parsed, session),
            text="New lead",
            **({"thread_ts": thread_ts} if thread_ts else {}),
        )
        if session is not None and not thread_ts:
            session.data["triage_ts"] = posted.get("ts")
            SESSIONS.save(session)
    except Exception:  # noqa: BLE001 - a lost lead is the one unacceptable outcome
        log.error(
            "LEAD DELIVERY FAILED for %s — payload follows so nothing is lost:\n%s\n%s",
            user_id,
            json.dumps({"username": username, "lead": parsed}, sort_keys=True),
            traceback.format_exc(),
        )
        _notify_dm(client, user_id, lead.FALLBACK.format(issues=content.DEMO_ISSUES))
        return
    _notify_dm(client, user_id, lead.confirmation_text(parsed))


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
    if path in ("/", "/health", "/healthz"):
        # Reports the state backend too: a service that is up but has lost its
        # durable store is a different thing from a healthy one, and an
        # operator should not have to read the logs to find out.
        state = "state={} {}".format(
            SESSIONS.backend_name, "DEGRADED" if SESSIONS.degraded else "ok"
        )
        start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
        return [
            b"judgment-pack slack demo: up. Slack posts to "
            + EVENTS_PATH.encode()
            + b" ("
            + state.encode()
            + b")"
        ]
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
