"""Meeting a session this process has never run: rebuild, or say so.

Durable state buys one thing — a user's PROGRESS survives a restart. It does
not move the two things a flow actually runs on:

* the scratch copy of the demo project on this container's disk, which the
  `jpack` subprocesses read;
* the live gateway process for use case 3, with its keypair, its store, and
  the receipts it signed.

So when a turn arrives for a session that was restored from the backend, this
module asks one question per capability the active flow needs: can this
container rebuild it?

    project        yes — re-copy the baked demo project
    registered-pack yes — the drafted pack document persisted with the session
    desk           NO  — a signing key that no longer exists cannot be
                        re-created, and neither can the receipts it signed
    audit-trail    NO  — the decision book was a file in the scratch copy

Whatever can be rebuilt is rebuilt silently (it is the same demo). Whatever
cannot is NOT pretended: the flow is reset to its first step, the user is told
in one plain line that the service restarted and this use case begins again,
and their completed set — the menu they see — is left exactly as they left it.

The alternative, resuming a flow whose evidence is gone, would produce a
screening desk with no receipts or a decision book with no decisions while the
prose claimed otherwise. On this demo, of all demos, that is the one thing
that must not happen.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("jpack-slack.reconcile")

REBUILDABLE = ("project", "registered-pack")
UNREBUILDABLE = ("desk", "audit-trail")

# One line per flow, said once, when that flow cannot be resumed.
RESET_NOTICE = {
    "judge": (
        ":arrows_counterclockwise: *The service restarted while you were mid-run.* Your progress "
        "is here, but the scratch copy of the project went with the old container — so use case 1 "
        "starts again from the top. Nothing you finished was lost."
    ),
    "author": (
        ":arrows_counterclockwise: *The service restarted while you were mid-run,* and the pack "
        "you authored lived in the scratch copy that went with it. Use case 2 begins again — the "
        "policy text is all it needs. Nothing you finished was lost."
    ),
    "attested": (
        ":arrows_counterclockwise: *The service restarted, and the screening desk went with it.* "
        "Its signing key and the receipts it wrote cannot be re-created — that is the point of a "
        "receipt, and pretending otherwise would undo the whole act. Use case 3 begins again, "
        "with a fresh desk. Nothing you finished was lost."
    ),
    "book": (
        ":arrows_counterclockwise: *The service restarted, and your decision book went with it* — "
        "the audit trail was a file in the scratch copy, not a database. Use case 4 begins again; "
        "run use case 1 first if the book comes up empty, because the decisions it recorded were "
        "in that copy. Nothing you finished was lost."
    ),
}

REBUILT_NOTICE = (
    ":arrows_counterclockwise: *The service restarted while you were mid-run.* I rebuilt your "
    "copy of the project{extra}, so you can carry on from where you were."
)


class Reconciler:
    """Rebuild the local half of a restored session, or reset one flow.

    Constructed with the same runtime and desk manager the flows use, so a
    rebuild here is the same code path as a rebuild anywhere else.
    """

    def __init__(self, runtime, desk, catalogue=None):
        self.runtime = runtime
        self.desk = desk
        self._catalogue = catalogue

    def catalogue(self):
        if self._catalogue is None:
            from . import flows  # imported late: flows import nothing from here

            self._catalogue = flows.BY_ID
        return self._catalogue

    def needs(self, flow_id):
        flow = self.catalogue().get(flow_id)
        return tuple(getattr(flow, "NEEDS", ("project",)) or ())

    def reconcile(self, session):
        """Returns one line to post before this turn's replies, or None.

        Called inside the turn lock, before the flow runs, and only does
        anything when the session came back from the backend into a process
        that had never seen it. Kept places (flows the user LEFT, which
        `flows.resume` re-enters mid-step from a later click) are vetted
        here too — a place whose evidence went with the old container is
        dropped now, so resume can never sneak past this module.
        """
        if not getattr(session, "restored", False):
            return None
        session.restored = False

        # A restored session never carries a live desk: process handles do not
        # persist, and a stale handle would be a claim about a running gateway.
        session.data.pop("desk", None)

        flow_id = session.active_flow
        if not flow_id:
            # Between flows: nothing ACTIVE to rebuild — but kept places can
            # still point mid-flow, and they are honored or dropped now.
            log.info("restored %s at the menu; vetting kept places", session.user_id)
            return self._kept_notice(self._vet_kept_places(session))
        notice = self._reconcile_active(session, flow_id)
        dropped_notice = self._kept_notice(self._vet_kept_places(session))
        if notice and dropped_notice:
            return notice + "\n\n" + dropped_notice
        return notice or dropped_notice

    def _reconcile_active(self, session, flow_id):
        needs = self.needs(flow_id)
        rebuilt = []

        if "project" in needs:
            if not self._rebuild_project(session):
                return self._reset(session, flow_id)
            rebuilt.append("project")

        if "registered-pack" in needs and session.step >= 1:
            if self._reregister_pack(session):
                rebuilt.append("pack")
            else:
                return self._reset(session, flow_id)

        blocked = [need for need in needs if need in UNREBUILDABLE]
        if blocked and session.step >= 1:
            # Mid-flow and the evidence is gone: reset rather than resume.
            log.info(
                "cannot rebuild %s for %s (flow %s) — resetting that flow",
                ", ".join(blocked),
                session.user_id,
                flow_id,
            )
            return self._reset(session, flow_id)

        extra = " and re-registered the pack you authored" if "pack" in rebuilt else ""
        log.info("resumed %s in flow %s at step %s", session.user_id, flow_id, session.step)
        return REBUILT_NOTICE.format(extra=extra)

    def _vet_kept_places(self, session):
        """Honor or drop each kept place. Returns the flow ids dropped.

        A place is safe to resume when everything its flow needs can exist in
        this container: a project re-copies lazily on the flow's own
        `ensure_project`, a drafted pack re-registers here and now, but a
        desk's signing key and the decision book's file went with the old
        container — those places are dropped, and the flow starts fresh when
        the user comes back to it.
        """
        kept = session.kept_places()
        dropped = []
        for flow_id in list(kept):
            try:
                step = int(kept[flow_id] or 0)
            except (TypeError, ValueError):
                step = 0
            if step < 1:
                # A place at the intro holds nothing a fresh start would not.
                session.drop_kept_place(flow_id)
                continue
            needs = self.needs(flow_id)
            safe = not any(need in UNREBUILDABLE for need in needs)
            if safe and "registered-pack" in needs:
                safe = self._rebuild_project(session) and self._reregister_pack(session)
            if not safe:
                session.drop_kept_place(flow_id)
                dropped.append(flow_id)
                log.info(
                    "dropped the kept place in %s for %s — its evidence went "
                    "with the old container",
                    flow_id,
                    session.user_id,
                )
        return dropped

    def _kept_notice(self, dropped):
        if not dropped:
            return None
        titles = ", ".join(
            "*{}*".format(getattr(self.catalogue().get(flow_id), "TITLE", flow_id))
            for flow_id in dropped
        )
        return (
            ":arrows_counterclockwise: *The service restarted since you were last "
            "here,* and the place you had kept in {} went with the old container. "
            "{} from the top when you come back — nothing you finished was "
            "lost.".format(
                titles,
                "That use case starts" if len(dropped) == 1 else "Those use cases start",
            )
        )

    # --- the rebuildable half ---------------------------------------------

    def _rebuild_project(self, session):
        try:
            self.runtime.ensure_project(session)
            return True
        except Exception as error:  # noqa: BLE001 - a rebuild failure is a reset
            log.error("could not rebuild the project for %s: %s", session.user_id, error)
            return False

    def _reregister_pack(self, session):
        """Write the drafted pack back into the fresh scratch project.

        The pack document itself persisted with the session, so this is a
        genuine rebuild rather than a story about one: the same bytes, the
        same decision id, registered the same way, and `packs validate` still
        rules on it.
        """
        pack = session.data.get("author_pack") or (
            session.data.get("author_envelope") or {}
        ).get("pack")
        decision_id = session.data.get("author_decision_id")
        project = session.scratch_dir
        if not (pack and decision_id and project):
            return False
        if not os.path.isfile(os.path.join(project, "packs", decision_id + ".pack.json")):
            try:
                from .flows import author

                if not author.register_pack(self.runtime, project, decision_id, pack):
                    return False
            except Exception as error:  # noqa: BLE001
                log.error(
                    "could not re-register %s for %s: %s", decision_id, session.user_id, error
                )
                return False
        # Registered is not the same as accepted. Bytes drafted under one
        # image can fail validation under the next — this branch exists
        # BECAUSE the runtime pin moves — and a rebuild notice for a pack the
        # runtime would refuse is exactly the claim this app must not make.
        ran = self.runtime.packs_validate(project)
        if not ran.ok:
            log.error(
                "the re-registered pack %s does not validate under this image; "
                "resetting the flow instead of claiming a rebuild",
                decision_id,
            )
            return False
        return True

    # --- the honest half ---------------------------------------------------

    def _reset(self, session, flow_id):
        session.step = 0
        session.active_flow = flow_id
        # The reset must stick: a kept place for this flow would resume it
        # right back into the state the reset just declared unresumable.
        session.drop_kept_place(flow_id)
        for key in list(session.data):
            # The user's own policy text survives — the notice promises use
            # case 2 needs nothing else, and re-asking for it would be rude.
            if key in ("author_policy", "author_canned"):
                continue
            if key.startswith("author_") or key == "desk":
                session.data.pop(key, None)
        return RESET_NOTICE.get(flow_id, RESET_NOTICE["judge"])
