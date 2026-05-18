"""
tracker.py — Lead tracking and automated follow-up scheduler.
Monitors email engagement and queues follow-up drafts at appropriate intervals.
"""

import logging
from typing import List

from engine.config import FOLLOWUP_DELAY_DAYS, MAX_EMAILS_PER_CONTACT
from engine.database import (
    get_db,
    get_contact_send_count,
    days_since_last_send,
    is_suppressed,
)
from engine.email_drafter import create_and_save_draft

logger = logging.getLogger(__name__)


def get_contacts_needing_followup() -> List[dict]:
    """
    Find contacts who:
    - Received an email but haven't replied
    - Are past the follow-up delay window
    - Haven't hit the max send cap
    - Are not suppressed
    """
    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                c.id as contact_id,
                c.email,
                c.first_name,
                c.last_name,
                c.title,
                c.target_function,
                co.id as company_id,
                co.name as company_name,
                co.industry,
                co.description as expansion_stage,
                co.expansion_direction,
                co.raw_snippet,
                co.qualification_score,
                MAX(e.sent_at) as last_sent_at,
                MAX(e.sequence_num) as last_sequence,
                COUNT(DISTINCT CASE WHEN ev.event_type='reply' THEN ev.id END) as reply_count,
                COUNT(DISTINCT CASE WHEN ev.event_type='open'  THEN ev.id END) as open_count
            FROM contacts c
            JOIN companies co ON c.company_id = co.id
            JOIN emails e ON e.contact_id = c.id AND e.status = 'sent'
            LEFT JOIN email_events ev ON ev.email_id = e.id
            WHERE c.do_not_contact = 0
            GROUP BY c.id
            HAVING reply_count = 0
               AND last_sent_at IS NOT NULL
               AND julianday('now') - julianday(last_sent_at) >= ?
        """, (FOLLOWUP_DELAY_DAYS,)).fetchall()

    return [dict(r) for r in rows]


def run_followup_scheduler() -> int:
    """
    Check for contacts needing follow-up and create draft emails.
    Returns the number of follow-up drafts queued.
    """
    logger.info("=== Follow-up scheduler running ===")
    candidates = get_contacts_needing_followup()
    queued = 0

    for c in candidates:
        contact_id    = c["contact_id"]
        contact_email = c["email"]

        # Skip suppressed contacts
        if is_suppressed(contact_email):
            continue

        # Skip if at max send cap
        send_count = get_contact_send_count(contact_id)
        if send_count >= MAX_EMAILS_PER_CONTACT:
            logger.debug(f"Contact {contact_email} at send cap — skipping.")
            continue

        # Check no pending/approved email already exists for this contact
        with get_db() as conn:
            pending = conn.execute("""
                SELECT id FROM emails
                WHERE contact_id=? AND status IN ('draft','pending_approval','approved')
            """, (contact_id,)).fetchone()
        if pending:
            logger.debug(f"Contact {contact_email} already has a pending draft — skipping.")
            continue

        next_sequence = (c["last_sequence"] or 1) + 1
        days_since    = days_since_last_send(contact_id)

        logger.info(f"Queuing follow-up #{next_sequence} for {contact_email} "
                    f"({c['company_name']}) — {days_since} days since last send")

        email_id = create_and_save_draft(
            contact_id=contact_id,
            company_id=c["company_id"],
            company_name=c["company_name"],
            contact_first_name=c["first_name"],
            contact_title=c["title"],
            industry=c["industry"],
            expansion_stage=c["expansion_stage"],
            source_snippet=c["raw_snippet"],
            expansion_direction=c.get("expansion_direction", "unknown"),
            target_function=c.get("target_function", "hr"),
            sequence_num=next_sequence,
        )

        if email_id:
            queued += 1
            logger.info(f"  Follow-up draft queued (email ID {email_id})")

    logger.info(f"=== Follow-up scheduler done: {queued} drafts queued ===")
    return queued


def get_pipeline_summary() -> dict:
    """Return a summary of the current lead pipeline state."""
    with get_db() as conn:
        result = {}
        for status in ["new", "qualified", "contacted", "closed", "disqualified"]:
            row = conn.execute(
                "SELECT COUNT(*) as n FROM companies WHERE status=?", (status,)
            ).fetchone()
            result[status] = row["n"]

        result["to_mexico"] = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE expansion_direction='to_mexico' AND status != 'disqualified'"
        ).fetchone()[0]

        result["to_us_canada"] = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE expansion_direction='to_us_canada' AND status != 'disqualified'"
        ).fetchone()[0]

        result["pending_approval"] = conn.execute(
            "SELECT COUNT(*) FROM emails WHERE status='pending_approval'"
        ).fetchone()[0]

        result["sent_today"] = conn.execute("""
            SELECT COUNT(*) FROM emails
            WHERE status='sent' AND DATE(sent_at) = DATE('now')
        """).fetchone()[0]

        result["hot_leads"] = conn.execute("""
            SELECT COUNT(*) FROM companies
            WHERE status='qualified' AND qualification_score >= 8
        """).fetchone()[0]

    return result


def print_pipeline_summary():
    s = get_pipeline_summary()
    print(f"""
+========================================+
|   TMS CORP LEAD GEN — PIPELINE         |
+========================================+
|  New leads:            {s['new']:>6}          |
|  Qualified:            {s['qualified']:>6}          |
|  Hot leads (8+):       {s['hot_leads']:>6}          |
|  Contacted:            {s['contacted']:>6}          |
|  Closed:               {s['closed']:>6}          |
|  Disqualified:         {s['disqualified']:>6}          |
+========================================+
|  Direction -> Mexico:  {s['to_mexico']:>6}          |
|  Direction -> US/CA:   {s['to_us_canada']:>6}          |
+========================================+
|  Pending approval:     {s['pending_approval']:>6}          |
|  Sent today:           {s['sent_today']:>6}          |
+========================================+
""")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print_pipeline_summary()
