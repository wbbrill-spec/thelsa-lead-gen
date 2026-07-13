"""Standalone scheduler cycle for the TMS Lead Gen Engine.

Run once per invocation (by a Render Cron Job) — or in a loop by a worker.
Each cycle it:
  1. Runs lead discovery so new leads flow into the dashboard.
  2. Runs Outlook tracking: detect sent emails & replies, and create the
     working-day Day-2 / Day-5 follow-up drafts (modules.mod11_outlook_tracker).
  3. Emails Bill a morning summary of how many new leads await assignment.

Every step is wrapped so a failure in one never stops the others.
"""
from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SCHEDULER] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# When run as a loop (worker), fire once per day at ~14:00 UTC (~9am US Central).
_RUN_HOUR_UTC = 14

ALERT_TO = ["wbbrill@gmail.com"]
ALERT_CC = ["bill.brill@inflectionpointnow.com"]


def _system_user_id(db):
    from models import User
    u = (
        db.query(User).filter(User.email_gmail == "wbbrill@gmail.com").first()
        or db.query(User).filter_by(is_active=True).first()
    )
    return u.id if u else None


def run_discovery():
    from db import get_db
    from models import DiscoveryRun

    with get_db() as db:
        uid = _system_user_id(db)
        if not uid:
            log.error("No user to attribute discovery run; skipping discovery.")
            return
        run = DiscoveryRun(run_by_user_id=uid, status="RUNNING")
        db.add(run)
        db.flush()
        run_id = run.id

    try:
        from modules.mod01_discovery import run_discovery as _discover
        from modules.mod02_deduplication import deduplicate
        from modules.mod03_scorer import score_candidates
        from modules.mod04_segmentation import segment_and_detect_rmc
        from modules.mod05_enricher import enrich_contacts

        candidates = _discover(run_id=run_id)
        net_new = deduplicate(candidates, run_id=run_id)
        qualified = score_candidates(net_new, run_id=run_id)
        segmented = segment_and_detect_rmc(qualified)
        enrich_contacts(segmented, run_id=run_id, generated_by_user_id=uid)

        with get_db() as db:
            r = db.query(DiscoveryRun).filter_by(id=run_id).first()
            if r:
                r.completed_at = datetime.now(timezone.utc)
                r.status = "COMPLETED"
        log.info("Discovery run %s complete.", run_id)
    except Exception as e:
        log.error("Discovery run failed: %s", e, exc_info=True)
        with get_db() as db:
            r = db.query(DiscoveryRun).filter_by(id=run_id).first()
            if r:
                r.status = "FAILED"
                r.error_message = str(e)


def _count_leads():
    from db import get_db
    from models import Lead

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    with get_db() as db:
        new_today = db.query(Lead).filter(Lead.created_at >= cutoff).count()
        pending = (
            db.query(Lead)
            .filter(Lead.status.in_([Lead.STATUS_NEW, Lead.STATUS_APPROVED]))
            .count()
        )
    return new_today, pending


def send_morning_alert(new_today, pending):
    from db import get_db
    from models import User
    from web_auth import WebAuthFlow
    from googleapiclient.discovery import build

    with get_db() as db:
        u = db.query(User).filter(User.email_gmail == "wbbrill@gmail.com").first()
        token = u.oauth_token if u else None
        from_email = u.email_gmail if u else ""

    if not token:
        log.error("No Gmail token available; skipping morning alert.")
        return

    subject = f"Thelsa Lead Gen — {new_today} new lead(s) to assign"
    body = (
        "Good morning,\n\n"
        "The Thelsa lead-gen automation ran successfully.\n\n"
        f"New leads discovered in the last 24 hours: {new_today}\n"
        f"Total leads waiting to be assigned: {pending}\n\n"
        "Review and assign them here: https://thelsa.inflectionpointnow.com\n\n"
        "— Thelsa Lead Gen"
    )
    try:
        creds = WebAuthFlow.credentials_from_token(token)
        msg = MIMEText(body, _charset="utf-8")
        msg["to"] = ", ".join(ALERT_TO)
        msg["cc"] = ", ".join(ALERT_CC)
        msg["from"] = from_email
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        log.info("Morning alert sent (new=%s, pending=%s).", new_today, pending)
    except Exception as e:
        log.error("Morning alert send failed: %s", e, exc_info=True)


def _sleep_until_next_run():
    now = datetime.now(timezone.utc)
    target = now.replace(hour=_RUN_HOUR_UTC, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    secs = (target - now).total_seconds()
    log.info("Sleeping %.1f hours until next run (~9am Central).", secs / 3600)
    time.sleep(secs)


def run_cycle():
    log.info("Running discovery...")
    try:
        run_discovery()
    except Exception as e:
        log.error("Discovery step failed: %s", e, exc_info=True)

    log.info("Running Outlook tracking (sent / replies / follow-ups)...")
    try:
        from modules.mod11_outlook_tracker import run_outlook_tracking
        r = run_outlook_tracking()
        log.info("Outlook tracking — sent: %s, replies: %s, D2: %s, D5: %s, errors: %s",
                 r.get("sent"), r.get("replies"), r.get("d2"), r.get("d5"), r.get("errors"))
    except Exception as e:
        log.error("Outlook tracking failed: %s", e, exc_info=True)

    log.info("Sending morning alert...")
    try:
        new_today, pending = _count_leads()
        send_morning_alert(new_today, pending)
    except Exception as e:
        log.error("Alert step failed: %s", e, exc_info=True)


def main():
    log.info("TMS Lead Gen Scheduler starting.")
    from models import create_all_tables
    create_all_tables()
    log.info("Database tables verified.")

    while True:
        run_cycle()
        _sleep_until_next_run()


if __name__ == "__main__":
    main()
