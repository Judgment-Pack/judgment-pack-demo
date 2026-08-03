"""Drive the whole demo from a terminal, with no Slack and no API key.

    python3 slack/bot/dryrun.py --script     # the scripted 1 -> 3 -> 4 -> 2 path
    python3 slack/bot/dryrun.py              # an interactive REPL over the same menu

This is the headless proof that the app's decisions do not need a model: with
JPACK_DRYRUN_FAKE_MODEL=1 every narration is canned, and use cases 1, 3 and 4
still run entirely for real against the `jpack` binary and the gateway. Use
case 2 runs with a canned draft — the drafting is the one step that is genuine
model labor.

Environment (a workstation, not the container):

    JPACK_BIN     path to a built jpack binary          (required)
    GATEWAY_BIN   path to a built gateway binary        (required for use case 3)
    DERIVE_CLI    experiments repo derivation-rule/derive_cli.py
    DERIVE_RULE   experiments repo derivation-rule/rules/screening.rule.json

The last two default to a sibling `judgment-pack-evaluator-experiments`
checkout beside this repository, and the demo project, `attest`, the OFAC
source and the watchlist default to this repository's own copies.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))  # .../<demo repo>
if os.path.dirname(HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(HERE))  # .../slack

from bot import blocks, flows  # noqa: E402
from bot.config import Config  # noqa: E402
from bot.desk import DeskManager  # noqa: E402
from bot.flows.base import Deps, Turn  # noqa: E402
from bot.model import build_model  # noqa: E402
from bot.runtime import JpackRuntime  # noqa: E402
from bot.state import RateLimiter, SessionStore  # noqa: E402


def workstation_defaults(scratch_root):
    """Point the baked paths at this checkout — when this IS a checkout.

    Run inside the container, none of the repository-relative paths exist, so
    nothing is set and the image's own baked defaults stand. That way one
    command proves the same flows in both places.
    """
    experiments = os.path.join(
        os.path.dirname(REPO), "judgment-pack-evaluator-experiments", "derivation-rule"
    )
    if_present = {
        "DEMO_PROJECT": os.path.join(REPO, "projects", "enterprise-demo"),
        "ATTEST_SCRIPT": os.path.join(REPO, "attestation", "attest.py"),
        "OFAC_SOURCE": os.path.join(REPO, "attestation", "ofac-screening-source.py"),
        "OFAC_WATCHLIST": os.path.join(REPO, "attestation", "watchlist.json"),
        "DERIVE_CLI": os.path.join(experiments, "derive_cli.py"),
        "DERIVE_RULE": os.path.join(experiments, "rules", "screening.rule.json"),
    }
    for key, value in if_present.items():
        if key not in os.environ and os.path.exists(value):
            os.environ[key] = value
    for key, value in (
        ("SESSION_ROOT", scratch_root),
        ("GATEWAY_AUTHORITY", "gateway:judgment-pack-slack-dryrun"),
        ("JPACK_DRYRUN_FAKE_MODEL", "1"),
    ):
        os.environ.setdefault(key, value)


# --- rendering -------------------------------------------------------------


def render(block):
    kind = block.get("type")
    if kind == "header":
        return "\n=== " + block["text"]["text"] + " ==="
    if kind == "section":
        return block["text"]["text"]
    if kind == "context":
        return "  " + " ".join(e.get("text", "") for e in block.get("elements", []))
    if kind == "divider":
        return "-" * 60
    if kind == "actions":
        return "  [ " + " | ".join(
            "{} -> {}".format(e["text"]["text"], e["action_id"]) for e in block["elements"]
        ) + " ]"
    return "  (" + str(kind) + ")"


def show(replies, out=sys.stdout):
    text = []
    for one in replies:
        for block in one.blocks:
            text.append(render(block))
    body = "\n".join(text)
    out.write(body + "\n")
    out.flush()
    return body


# --- wiring ----------------------------------------------------------------


def build(scratch_root):
    workstation_defaults(scratch_root)
    config = Config.from_env()
    runtime = JpackRuntime(config)
    desk = DeskManager(config)
    limiter = RateLimiter(config.model_calls_per_hour)
    deps = Deps(config=config, runtime=runtime, desk=desk, model=build_model(config, limiter))
    store = SessionStore(config, on_evict=desk.stop)
    return deps, store


def preflight(deps):
    """Refuse to pretend: say exactly which piece is missing."""
    problems = []
    version = deps.runtime.run(["version"])
    if not version.ok:
        problems.append("JPACK_BIN ({}) did not answer `version`".format(deps.config.jpack_bin))
    gateway = shutil.which(deps.config.gateway_bin) or (
        deps.config.gateway_bin if os.path.exists(deps.config.gateway_bin) else None
    )
    if not gateway:
        problems.append("GATEWAY_BIN is not runnable: " + deps.config.gateway_bin)
    for label, path in (
        ("DEMO_PROJECT", deps.config.demo_project),
        ("ATTEST_SCRIPT", deps.config.attest_script),
        ("DERIVE_CLI", deps.config.derive_cli),
        ("DERIVE_RULE", deps.config.derive_rule),
    ):
        if not os.path.exists(path):
            problems.append("{} does not exist: {}".format(label, path))
    return problems, version.stdout.strip()


# --- the scripted path -----------------------------------------------------

SCRIPT = [
    ("start", "judge", ["reviewed", "cannot decide"]),
    ("step", "case", ["approve", "trace"]),
    ("step", "case", ["reject", "sanctions-match-hard-stop"]),
    ("step", "case", ["unresolved", "missing-required-evidence", "unknown"]),
    ("start", "attested", ["screening desk"]),
    ("step", "screen", ["acquired", "verify", "reject"]),
    ("step", "tamper", ["WITHHELD", "unresolved", "Vendor risk committee"]),
    ("start", "book", ["record", "digest"]),
    ("step", "replay", ["identical"]),
    ("start", "author", ["policy"]),
    ("step", "canned", ["valid", "packs validate", "Registered"]),
    ("step", "judge", ["accept"]),
    ("step", "unknown", ["unresolved"]),
]


def run_script(deps, store, out=sys.stdout):
    user = "U-DRYRUN"
    session = store.get(user)
    failures = []
    started = time.time()

    replies = [flows.welcome(session)]
    show(replies, out)

    for kind, token, expected in SCRIPT:
        out.write("\n>>> {} {}\n".format(kind, token))
        turn = Turn(user_id=user, session=session, action=None, text=None)
        if kind == "start":
            replies = flows.start(turn, deps, token)
        else:
            turn.action = token
            replies = flows.dispatch(turn, deps)
        if replies is None:
            failures.append("{} {}: no active flow to advance".format(kind, token))
            continue
        body = show(replies, out)
        lowered = body.lower()
        for needle in expected:
            if needle.lower() not in lowered:
                failures.append("{} {}: expected {!r} in the reply".format(kind, token, needle))

    if session.completed != {"judge", "author", "attested", "book"}:
        failures.append("completed set is {}, expected all four".format(sorted(session.completed)))
    if session.active_flow is not None:
        failures.append("a flow is still active at the end: " + str(session.active_flow))

    out.write("\n" + "=" * 60 + "\n")
    out.write("dry run finished in {:.1f}s\n".format(time.time() - started))
    out.write("completed: {}\n".format(", ".join(sorted(session.completed))))
    if failures:
        out.write("FAILURES ({}):\n".format(len(failures)))
        for failure in failures:
            out.write("  - " + failure + "\n")
    else:
        out.write("all scripted expectations met\n")
    return failures


# --- the REPL --------------------------------------------------------------

HELP = """commands:
  1 | 2 | 3 | 4   start that use case (judge, author, attested, book)
  next [token]    advance the active flow (token defaults to the flow's own button)
  menu            show the menu
  about           show the About surface
  <anything else> sent as a message into the active flow (or as glue)
  quit            leave (and reap the session's desk and scratch copy)
"""


def repl(deps, store):
    user = "U-DRYRUN"
    session = store.get(user)
    show([flows.welcome(session)])
    sys.stdout.write("\n" + HELP)
    while True:
        try:
            line = input("\ndemo> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ("quit", "exit"):
            break
        if line == "help":
            sys.stdout.write(HELP)
            continue
        if line == "menu":
            show([flows.menu(session)])
            continue
        if line == "about":
            show([flows.about(session)])
            continue
        turn = Turn(user_id=user, session=session)
        if line in ("1", "2", "3", "4"):
            flow = flows.CATALOGUE[int(line) - 1]
            show(flows.start(turn, deps, flow.ID))
            continue
        if line.startswith("next"):
            parts = line.split(None, 1)
            turn.action = parts[1].strip() if len(parts) > 1 else "next"
        else:
            turn.text = line
        replies = flows.dispatch(turn, deps)
        if replies is None:
            answer = deps.model.glue(user, line, situation="at the menu")
            if answer.ok:
                # Same attributed, escaped surface the app uses: model prose
                # is never shown bare, not even on a terminal.
                show([flows.reply(blocks.model_blocks(answer.text, deps.model.name, label="Host reply"))])
            show([flows.menu(session)])
        else:
            show(replies)
    store.drop(user)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dryrun", description=__doc__)
    parser.add_argument("--script", action="store_true", help="run the scripted 1->3->4->2 path")
    parser.add_argument(
        "--scratch",
        default=os.environ.get("DRYRUN_SCRATCH", "/tmp/jpack-slack-dryrun"),
        help="where session copies are made",
    )
    parser.add_argument("--keep", action="store_true", help="keep the session directory")
    args = parser.parse_args(argv)

    os.makedirs(args.scratch, exist_ok=True)
    deps, store = build(args.scratch)
    problems, version = preflight(deps)
    if problems:
        sys.stderr.write("dryrun cannot start:\n")
        for problem in problems:
            sys.stderr.write("  - " + problem + "\n")
        sys.stderr.write("\n" + (__doc__ or "") + "\n")
        return 2
    sys.stdout.write("runtime: {}\nmodel:   {}\n".format(version, deps.model.name))

    try:
        if args.script:
            failures = run_script(deps, store)
            return 1 if failures else 0
        repl(deps, store)
        return 0
    finally:
        if not args.keep:
            for session in store.all():
                store.drop(session.user_id)


if __name__ == "__main__":
    sys.exit(main())
