"""Automatic lead assignment — round-robin across the sales reps.

Bill asked (2026-11-09) that he no longer be the default owner of new leads:
every new lead is assigned to Gustavo or Armando alternately, and the initial
outreach draft is created in that rep's Outlook immediately, with no manual
step. A one-shot ``rebalance_legacy()`` reassigns existing leads still owned
by the legacy assignee 50/50.

Rep pool = active users whose ``full_name`` is in ASSIGNABLE_REPS (env var
``ASSIGNABLE_REPS``, comma-separated; default "Gustavo Gonzalez,Armando Silveyra")
and who have an Outlook mailbox on file. Round-robin picks the rep with the
fewest open leads (NEW/APPROVED/DRAFTED), breaking ties by who received the
most recent assignment least recently, so the split stays ~50/50 over time.
"""
from __future__ import annotations

import os

from sqlalchemy import func

from db import get_db
from models import Lead, User, LeadStatusHistory

LEGACY_ASSIGNEE = "Bill Brill"
ASSIGNABLE_REPS = [n.strip() for n in os.environ.get(
    "ASSIGNABLE_REPS", "Gustavo Gonzalez,Armando Silveyra").split(",") if n.strip()]

_OPEN = (Lead.STATUS_NEW, Lead.STATUS_APPROVED, Lead.STATUS_DRAFTED)


def rep_pool(db) -> list[User]:
    reps = (db.query(User)
            .filter(User.is_active == True, User.email_outlook.isnot(None))
            .all())
    pool = [u for u in reps if (u.full_name or "").strip() in ASSIGNABLE_REPS and (u.email_outlook or "").strip()]
    # Keep the configured order so ties break deterministically
    pool.sort(key=lambda u: ASSIGNABLE_REPS.index(u.full_name.strip()))
    return pool


def pick_rep(db) -> User | None:
    """Rep with the fewest open leads; ties → least recently assigned."""
    pool = rep_pool(db)
    if not pool:
        return None
    counts = dict(db.query(Lead.assigned_to_user_id, func.count(Lead.id))
                  .filter(Lead.status.in_(_OPEN), Lead.assigned_to_user_id.in_([u.id for u in pool]))
                  .group_by(Lead.assigned_to_user_id).all())
    last = dict(db.query(Lead.assigned_to_user_id, func.max(Lead.updated_at))
                .filter(Lead.assigned_to_user_id.in_([u.id for u in pool]))
                .group_by(Lead.assigned_to_user_id).all())
    return min(pool, key=lambda u: (counts.get(u.id, 0), last.get(u.id) or 0, pool.index(u)))


def assign_new_lead(lead_id: int, changed_by: str = "system") -> dict:
    """Assign a freshly created lead to the next rep and create the Outlook draft.
    Returns {'rep': name, 'mailbox': ..., 'drafted': bool}. Never raises."""
    from modules.mod07_drafter import draft_on_assign
    try:
        with get_db() as db:
            lead = db.query(Lead).filter_by(id=lead_id).first()
            rep = pick_rep(db)
            if not lead or not rep:
                return {"rep": None, "mailbox": None, "drafted": False,
                        "reason": "no assignable rep with an Outlook mailbox"}
            lead.assigned_to_user_id = rep.id
            db.add(LeadStatusHistory(lead_id=lead.id, changed_by=changed_by,
                                     from_status=lead.status, to_status=lead.status,
                                     reason=f"Auto-assigned to {rep.full_name} (round-robin)"))
            rep_name, mailbox = rep.full_name, rep.email_outlook
        draft_on_assign(lead_id, mailbox, rep_name, changed_by)
        with get_db() as db:
            drafted = db.query(Lead).filter_by(id=lead_id).first().status == Lead.STATUS_DRAFTED
        print(f"[ASSIGN] lead {lead_id} → {rep_name} ({mailbox}); draft {'created' if drafted else 'FAILED'}")
        return {"rep": rep_name, "mailbox": mailbox, "drafted": drafted}
    except Exception as exc:
        print(f"[ASSIGN] lead {lead_id} auto-assign failed: {exc}")
        return {"rep": None, "mailbox": None, "drafted": False, "reason": str(exc)}


def rebalance_legacy(changed_by: str = "system", draft: bool = True) -> dict:
    """Reassign every open lead still owned by LEGACY_ASSIGNEE 50/50 across the
    rep pool (alternating, newest first) and create each rep's outreach draft."""
    from modules.mod07_drafter import draft_on_assign
    with get_db() as db:
        legacy = db.query(User).filter(User.full_name == LEGACY_ASSIGNEE).all()
        legacy_ids = [u.id for u in legacy]
        pool = rep_pool(db)
        if not legacy_ids or not pool:
            return {"moved": 0, "reason": "no legacy user or no rep pool"}
        leads = (db.query(Lead)
                 .filter(Lead.assigned_to_user_id.in_(legacy_ids), Lead.status.in_(_OPEN))
                 .order_by(Lead.created_at.desc()).all())
        plan = []
        for i, lead in enumerate(leads):
            rep = pool[i % len(pool)]
            lead.assigned_to_user_id = rep.id
            db.add(LeadStatusHistory(lead_id=lead.id, changed_by=changed_by,
                                     from_status=lead.status, to_status=lead.status,
                                     reason=f"Rebalanced from {LEGACY_ASSIGNEE} to {rep.full_name}"))
            plan.append((lead.id, rep.full_name, rep.email_outlook, lead.status))
    per_rep: dict = {}
    drafted = 0
    for lead_id, rep_name, mailbox, status in plan:
        per_rep[rep_name] = per_rep.get(rep_name, 0) + 1
        if draft and status == Lead.STATUS_NEW:
            draft_on_assign(lead_id, mailbox, rep_name, changed_by)
            with get_db() as db:
                if db.query(Lead).filter_by(id=lead_id).first().status == Lead.STATUS_DRAFTED:
                    drafted += 1
    print(f"[ASSIGN] rebalanced {len(plan)} legacy leads: {per_rep}; drafts created: {drafted}")
    alerts = {}
    if drafted:
        try:
            from datetime import datetime, timedelta, timezone
            from modules.rep_alerts import send_rep_alerts
            alerts = send_rep_alerts(since=datetime.now(timezone.utc) - timedelta(hours=1))
        except Exception as exc:
            alerts = {"error": str(exc)}
    return {"moved": len(plan), "per_rep": per_rep, "drafts_created": drafted, "alerts": alerts}
