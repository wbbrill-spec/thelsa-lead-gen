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
import threading
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

# Prevents two cycles (cron + worker, or a cron retry) from overlapping in one process.
CYCLE_LOCK = threading.Lock()


def _system_user_id(db):
    from models import User
    u = (
        db.query(User).filter(User.email_gmail == "wbbrill@gmail.com").first()
        or db.query(User).filter_by(is_active=True).first()
    )
    return u.id if u else None


def run_discovery(triggered_by: str = "scheduler"):
    """Run one discovery pipeline synchronously via the shared runner.
    Refuses (and logs) if a run is already in progress."""
    import pipeline
    try:
        run_id = pipeline.start_run(user_id=None, triggered_by=triggered_by, background=False)
        log.info("Discovery run %s finished: %s", run_id, pipeline.last_run_status().get("status"))
    except pipeline.RunAlreadyInProgress as e:
        log.warning("Skipping discovery: %s", e)


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

    # Be honest about how the run actually went — a failed run must not read
    # as "ran successfully".
    import pipeline
    last = pipeline.last_run_status()
    if last.get("exists") and last.get("status") == "FAILED":
        subject = f"⚠️ Thelsa Lead Gen — discovery run #{last['id']} FAILED"
        run_line = (
            f"The discovery run FAILED at stage '{last.get('stage')}':\n"
            f"    {last.get('error_message')}\n\n"
            "Details: https://thelsa.inflectionpointnow.com/runs\n\n"
        )
    elif last.get("exists") and last.get("status") == "RUNNING":
        subject = f"Thelsa Lead Gen — run #{last['id']} still in progress"
        run_line = f"The discovery run is still in progress (stage: {last.get('stage')}).\n\n"
    else:
        subject = f"Thelsa Lead Gen — {new_today} new lead(s) to assign"
        run_line = (
            "The Thelsa lead-gen automation ran successfully.\n"
            + (f"Run #{last['id']}: {last.get('companies_discovered', 0)} companies found, "
               f"{last.get('leads_qualified', 0)} qualified, {last.get('leads_created', 0)} new leads.\n\n"
               if last.get("exists") else "\n")
        )
    body = (
        "Good morning,\n\n"
        + run_line +
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


def run_cycle(triggered_by: str = "scheduler"):
    log.info("Running discovery...")
    try:
        run_discovery(triggered_by=triggered_by)
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


def _already_ran_today() -> bool:
    from db import get_db
    from models import DiscoveryRun
    cutoff = datetime.now(timezone.utc) - timedelta(hours=20)
    with get_db() as db:
        return db.query(DiscoveryRun).filter(DiscoveryRun.started_at >= cutoff).count() > 0


def main():
    """Worker loop. Runs once a day at _RUN_HOUR_UTC.

    On boot it does NOT run immediately (every deploy used to trigger a full
    discovery + a duplicate morning email); it only runs on boot if nothing has
    run in the last 20 hours. Use ``python scheduler.py --once`` to force one cycle.
    """
    import sys
    log.info("TMS Lead Gen Scheduler starting.")
    from models import create_all_tables
    import config
    create_all_tables()
    config.log_config_warnings("SCHEDULER")
    log.info("Database tables verified.")

    if "--once" in sys.argv:
        with CYCLE_LOCK:
            run_cycle(triggered_by="manual")
        return

    if not _already_ran_today():
        log.info("No run in the last 20h — running a cycle now.")
        with CYCLE_LOCK:
            run_cycle(triggered_by="worker")

    while True:
        _sleep_until_next_run()
        with CYCLE_LOCK:
            run_cycle(triggered_by="worker")


if __name__ == "__main__":
    main()
