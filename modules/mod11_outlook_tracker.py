"""MOD-11: Outlook tracking — sent detection, reply detection, and working-day
Day-2 / Day-5 follow-ups, all via the app-only Microsoft Graph credentials
(same Azure app as graph_outlook). Drafts only — never sends.

Called by the scheduler/cron. Uses the existing Lead status machine:
APPROVED / DRAFTED --(sent detected)--> DRAFTED (initial_sent_at set)
DRAFTED --(Day-2 working)--> FOLLOWED_UP_D2
FOLLOWED_UP_D2 --(Day-5 working)--> FOLLOWED_UP_D5
any --(reply)--> RESPONDED (stops follow-ups)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from db import get_db
from models import Lead, EmailDraft, transition_status
from modules.graph_outlook import GRAPH, _token


def _get(url: str) -> dict:
    r = requests.get(url, headers={"Authorization": f"Bearer {_token()}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def _parse(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _add_working_days(dt: datetime, n: int) -> datetime:
    """Add n working days (Mon-Fri), skipping weekends."""
    d = dt
    added = 0
    while added < n:
        d = d + timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def _list_folder(mailbox: str, folder: str, date_field: str, since_iso: str) -> list:
    url = (
        f"{GRAPH}/users/{mailbox}/mailFolders/{folder}/messages"
        f"?$select=subject,toRecipients,from,{date_field},conversationId"
        f"&$top=200&$filter={date_field} ge {since_iso}"
    )
    items: list = []
    try:
        # Follow @odata.nextLink so a wide look-back window is not truncated at 200.
        while url and len(items) < 2000:
            data = _get(url)
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return items
    except Exception as e:
        print(f"[MOD-11] list {folder} failed for {mailbox}: {e}")
        return items


def run_outlook_tracking() -> dict:
    now = datetime.now(timezone.utc)
    # 90-day look-back so past leads (not just the last 3 weeks) get backfilled.
    since = (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = {"sent": 0, "replies": 0, "d2": 0, "d5": 0, "errors": 0}

    sent_cache: dict = {}
    inbox_cache: dict = {}

    def sent_for(mb):
        if mb not in sent_cache:
            sent_cache[mb] = _list_folder(mb, "SentItems", "sentDateTime", since)
        return sent_cache[mb]

    def inbox_for(mb):
        if mb not in inbox_cache:
            inbox_cache[mb] = _list_folder(mb, "Inbox", "receivedDateTime", since)
        return inbox_cache[mb]

    # 1) SENT DETECTION — match the initial draft's subject OR recipient in the rep's Sent folder
    with get_db() as db:
        cands = (
            db.query(Lead)
            .filter(Lead.initial_sent_at.is_(None),
                    Lead.status.in_([Lead.STATUS_APPROVED, Lead.STATUS_DRAFTED]))
            .all()
        )
        rows = []
        for lead in cands:
            u = lead.assigned_to
            mb = (u.email_outlook or "").strip().lower() if u else ""
            d = (db.query(EmailDraft)
                 .filter_by(lead_id=lead.id, draft_type="INITIAL", language="EN", provider="outlook")
                 .first())
            to_addr = (lead.contact.email or "").strip().lower() if (lead.contact and lead.contact.email) else ""
            if mb and d and d.subject_line:
                rows.append((lead.id, mb, d.subject_line.strip().lower(), to_addr))
    for lead_id, mb, subj, to_addr in rows:
        try:
            def _matches(m, subj=subj, to_addr=to_addr):
                # Primary: exact subject match. Fallback: same recipient (covers edited subjects).
                if (m.get("subject") or "").strip().lower() == subj:
                    return True
                if to_addr:
                    for rcp in (m.get("toRecipients") or []):
                        addr = ((rcp.get("emailAddress") or {}).get("address") or "").lower()
                        if addr == to_addr:
                            return True
                return False
            match = next((m for m in sent_for(mb) if _matches(m)), None)
            if not match:
                continue
            to = ((match.get("toRecipients") or [{}])[0].get("emailAddress") or {})
            sent_dt = _parse(match.get("sentDateTime")) or now
            with get_db() as db:
                lead = db.query(Lead).filter_by(id=lead_id).first()
                lead.initial_sent_at = sent_dt
                lead.sent_to_email = to.get("address")
                lead.sent_to_name = to.get("name")
                lead.sent_conversation_id = match.get("conversationId")
                lead.followup_d2_scheduled = _add_working_days(sent_dt, 2)
                lead.followup_d5_scheduled = _add_working_days(sent_dt, 5)
                transition_status(db, lead, Lead.STATUS_DRAFTED, "system",
                                  f"Initial email sent to {to.get('address') or '?'}")
            stats["sent"] += 1
        except Exception as e:
            stats["errors"] += 1
            print(f"[MOD-11] sent-detect error lead {lead_id}: {e}")

    # 2) REPLY DETECTION — same conversation, or a message from the recipient
    with get_db() as db:
        sent_leads = (
            db.query(Lead)
            .filter(Lead.initial_sent_at.isnot(None), Lead.reply_detected == False)
            .all()
        )
        rows = [(l.id,
                 (l.assigned_to.email_outlook or "").strip().lower() if l.assigned_to else "",
                 l.sent_conversation_id,
                 (l.sent_to_email or "").lower()) for l in sent_leads]
    for lead_id, mb, conv, sent_to in rows:
        if not mb:
            continue
        try:
            found = False
            for m in inbox_for(mb):
                if conv and m.get("conversationId") == conv:
                    found = True
                    break
                frm = (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
                if sent_to and frm == sent_to:
                    found = True
                    break
            if found:
                with get_db() as db:
                    lead = db.query(Lead).filter_by(id=lead_id).first()
                    lead.reply_detected = True
                    lead.reply_detected_at = now
                    transition_status(db, lead, Lead.STATUS_RESPONDED, "system",
                                      "Reply detected in Outlook")
                stats["replies"] += 1
        except Exception as e:
            stats["errors"] += 1
            print(f"[MOD-11] reply-detect error lead {lead_id}: {e}")

    # 3) FOLLOW-UPS at Day-2 and Day-5 (working days)
    stats["d2"] = _run_followups(now, "FOLLOWUP_D2")
    stats["d5"] = _run_followups(now, "FOLLOWUP_D5")
    return stats


def _run_followups(now: datetime, draft_type: str) -> int:
    if draft_type == "FOLLOWUP_D2":
        from_status = Lead.STATUS_DRAFTED
        sched_col = Lead.followup_d2_scheduled
        sent_col = Lead.followup_d2_sent_at
        to_status = Lead.STATUS_FOLLOWED_UP_D2
    else:
        from_status = Lead.STATUS_FOLLOWED_UP_D2
        sched_col = Lead.followup_d5_scheduled
        sent_col = Lead.followup_d5_sent_at
        to_status = Lead.STATUS_FOLLOWED_UP_D5

    with get_db() as db:
        due = (db.query(Lead)
               .filter(Lead.status == from_status, sched_col <= now,
                       sent_col.is_(None), Lead.reply_detected == False)
               .all())
        rows = [(l.id, (l.assigned_to.email_outlook or "").strip() if l.assigned_to else "") for l in due]

    count = 0
    for lead_id, mb in rows:
        if not mb:
            continue
        try:
            _create_followup_outlook(lead_id, mb, draft_type)
            with get_db() as db:
                lead = db.query(Lead).filter_by(id=lead_id).first()
                setattr(lead, "followup_d2_sent_at" if draft_type == "FOLLOWUP_D2" else "followup_d5_sent_at", now)
                transition_status(db, lead, to_status, "system",
                                  f"{draft_type} draft created in Outlook")
            count += 1
        except Exception as e:
            print(f"[MOD-11] {draft_type} error lead {lead_id}: {e}")
    return count


def _create_followup_outlook(lead_id: int, mailbox: str, draft_type: str) -> None:
    from modules.mod07_drafter import _build_context, _generate_email, _save_draft
    from modules.graph_outlook import create_outlook_draft

    with get_db() as db:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        ctx = _build_context(lead, lead.company, lead.contact)
        to_email = lead.sent_to_email or (lead.contact.email if lead.contact else "")
        subject, body = _generate_email(ctx, draft_type, "EN")
        res = create_outlook_draft(mailbox=mailbox, to_email=to_email, subject=subject, body=body)
        _save_draft(lead_id, draft_type, "EN", subject, body, res.get("id", ""), "outlook")
