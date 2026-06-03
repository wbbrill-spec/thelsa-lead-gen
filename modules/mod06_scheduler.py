"""MOD-06: Follow-Up Scheduler

Daily job that checks all active leads and:
1. Creates Day 2 follow-up drafts when due (if no reply)
2. Creates Day 5 follow-up drafts when due (if no reply)
3. Sends Call Required notification after Day 5 with no reply
4. Always checks for replies before creating any draft

Run as a Render Background Worker via scheduler.py.
"""

from __future__ import annotations
from datetime import datetime, timezone
from dataclasses import dataclass, field
from db import get_db
from models import Lead, transition_status


@dataclass
class SchedulerResult:
    d2_drafts_created: int = 0
    d5_drafts_created: int = 0
    replies_detected: int = 0
    call_required_sent: int = 0
    errors: list[str] = field(default_factory=list)


def run_scheduler() -> SchedulerResult:
    """Main scheduler entry point. Run once per day."""
    result = SchedulerResult()
    now = datetime.now(timezone.utc)

    with get_db() as db:
        # Leads due for Day 2 follow-up
        d2_due = db.query(Lead).filter(
            Lead.status == Lead.STATUS_DRAFTED,
            Lead.followup_d2_scheduled <= now,
            Lead.followup_d2_sent_at.is_(None),
            Lead.reply_detected == False,
        ).all()
        d2_lead_ids = [l.id for l in d2_due]

        # Leads due for Day 5 follow-up
        d5_due = db.query(Lead).filter(
            Lead.status == Lead.STATUS_FOLLOWED_UP_D2,
            Lead.followup_d5_scheduled <= now,
            Lead.followup_d5_sent_at.is_(None),
            Lead.reply_detected == False,
        ).all()
        d5_lead_ids = [l.id for l in d5_due]

        # Leads past Day 5 with no reply and not yet notified
        call_req = db.query(Lead).filter(
            Lead.status == Lead.STATUS_FOLLOWED_UP_D5,
            Lead.reply_detected == False,
            Lead.call_required_notified_at.is_(None),
        ).all()
        call_req_ids = [l.id for l in call_req]

    # Process Day 2
    for lead_id in d2_lead_ids:
        try:
            _process_d2(lead_id, now, result)
        except Exception as e:
            result.errors.append(f"D2 error lead {lead_id}: {e}")

    # Process Day 5
    for lead_id in d5_lead_ids:
        try:
            _process_d5(lead_id, now, result)
        except Exception as e:
            result.errors.append(f"D5 error lead {lead_id}: {e}")

    # Process Call Required
    for lead_id in call_req_ids:
        try:
            _process_call_required(lead_id, now, result)
        except Exception as e:
            result.errors.append(f"Call required error lead {lead_id}: {e}")

    return result


def _process_d2(lead_id: int, now: datetime, result: SchedulerResult):
    """Handle Day 2 follow-up for a single lead."""
    from modules.mod10_reply_detector import check_for_reply
    from modules.mod07_drafter import create_followup_drafts

    # Check for reply first
    reply = check_for_reply(lead_id)
    if reply:
        result.replies_detected += 1
        return  # Suppressed — reply already handled by check_for_reply

    # Create D2 drafts
    create_followup_drafts(lead_id, "FOLLOWUP_D2")

    with get_db() as db:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if lead:
            lead.followup_d2_sent_at = now
            transition_status(
                db, lead, Lead.STATUS_FOLLOWED_UP_D2,
                changed_by="system",
                reason="Day 2 follow-up drafts created automatically",
            )

    result.d2_drafts_created += 1


def _process_d5(lead_id: int, now: datetime, result: SchedulerResult):
    """Handle Day 5 follow-up for a single lead."""
    from modules.mod10_reply_detector import check_for_reply
    from modules.mod07_drafter import create_followup_drafts

    # Check for reply first
    reply = check_for_reply(lead_id)
    if reply:
        result.replies_detected += 1
        return

    # Create D5 drafts
    create_followup_drafts(lead_id, "FOLLOWUP_D5")

    with get_db() as db:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if lead:
            lead.followup_d5_sent_at = now
            transition_status(
                db, lead, Lead.STATUS_FOLLOWED_UP_D5,
                changed_by="system",
                reason="Day 5 follow-up drafts created automatically",
            )

    result.d5_drafts_created += 1


def _process_call_required(lead_id: int, now: datetime, result: SchedulerResult):
    """Send call required notification to rep."""
    from modules.mod10_reply_detector import check_for_reply
    from modules.mod07_drafter import send_call_required_notification

    # One final reply check
    reply = check_for_reply(lead_id)
    if reply:
        result.replies_detected += 1
        return

    # Send notification
    sent = send_call_required_notification(lead_id)

    if sent:
        with get_db() as db:
            lead = db.query(Lead).filter_by(id=lead_id).first()
            if lead:
                lead.call_required_notified_at = now
                transition_status(
                    db, lead, Lead.STATUS_CALL_REQUIRED,
                    changed_by="system",
                    reason="No reply after Day 5 — rep notified to call",
                )

        result.call_required_sent += 1
