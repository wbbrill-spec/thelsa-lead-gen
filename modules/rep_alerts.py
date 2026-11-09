"""Rep alerts — tell Gustavo / Armando when new drafts are waiting in their Outlook.

Runs at the end of every scheduler cycle (and after a dashboard-triggered
discovery). For each rep with Outlook drafts created since the last alert,
sends ONE email listing the companies, from bbrill@thelsa.com.

Delivery: Microsoft Graph ``sendMail`` from ALERT_FROM (needs application
permission Mail.Send). Until that is granted, falls back to the app's Gmail
integration (Bill's token, already used for the morning summary) with
Reply-To set to ALERT_FROM, so the alert still goes out.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from db import get_db
from models import EmailDraft, Lead, User, Company

ALERT_FROM = os.environ.get("REP_ALERT_FROM", "bbrill@thelsa.com")
ALERT_CC = [a.strip() for a in os.environ.get("REP_ALERT_CC", "").split(",") if a.strip()]
DASHBOARD = os.environ.get("DASHBOARD_URL", "https://thelsa.inflectionpointnow.com")
_LOOKBACK_HOURS = int(os.environ.get("REP_ALERT_LOOKBACK_HOURS", "26"))

_LABEL = {"INITIAL": "Initial outreach", "FOLLOWUP_D2": "Follow-up 1",
          "FOLLOWUP_D5": "Follow-up 2", "FOLLOWUP_D20": "Follow-up 3 (final)"}


def pending_drafts_by_rep(since: datetime) -> dict:
    """{rep_user: [(company, draft_type, created_at, lead_id), ...]}"""
    out: dict = {}
    with get_db() as db:
        rows = (db.query(EmailDraft, Lead, Company, User)
                .join(Lead, EmailDraft.lead_id == Lead.id)
                .join(Company, Lead.company_id == Company.id)
                .join(User, Lead.assigned_to_user_id == User.id)
                .filter(EmailDraft.provider == "outlook", EmailDraft.created_at >= since,
                        User.email_outlook.isnot(None))
                .order_by(User.id, EmailDraft.created_at.desc()).all())
        for d, lead, co, user in rows:
            key = (user.id, user.full_name, (user.email_outlook or "").strip().lower())
            out.setdefault(key, []).append((co.name, d.draft_type, d.created_at, lead.id))
    return out


def _body(rep_first: str, items: list) -> tuple[str, str]:
    n = len(items)
    lines = [f"Hola {rep_first},", "",
             f"Tienes {n} borrador{'es' if n != 1 else ''} nuevo{'s' if n != 1 else ''} de prospección en tu carpeta de Borradores de Outlook, listo{'s' if n != 1 else ''} para revisar y enviar:", ""]
    for co, dtype, created, lead_id in items:
        lines.append(f"  • {co} — {_LABEL.get(dtype, dtype)}  ({DASHBOARD}/leads/{lead_id})")
    lines += ["", "Revisa cada uno, ajusta lo que quieras y envíalo desde tu buzón. Si ya lo enviaste, el sistema lo detecta solo y programa los seguimientos.",
              "", f"Panel: {DASHBOARD}", "", "Gracias,", "Bill"]
    subject = f"Thelsa Lead Gen — {n} borrador{'es' if n != 1 else ''} nuevo{'s' if n != 1 else ''} para enviar"
    return subject, "\n".join(lines)


def send_rep_alerts(since: datetime | None = None) -> dict:
    """Send one alert per rep with new drafts. Returns {rep_email: n_drafts | 'error: ...'}."""
    since = since or (datetime.now(timezone.utc) - timedelta(hours=_LOOKBACK_HOURS))
    results: dict = {}
    for (uid, name, mailbox), items in pending_drafts_by_rep(since).items():
        if not mailbox or mailbox == ALERT_FROM.lower():
            continue  # don't alert Bill about his own drafts
        first = (name or "").split()[0] if name else "equipo"
        subject, body = _body(first, items)
        try:
            _deliver(mailbox, subject, body)
            results[mailbox] = len(items)
            print(f"[ALERT] {mailbox}: {len(items)} new draft(s) — alert sent")
        except Exception as exc:
            results[mailbox] = f"error: {exc}"
            print(f"[ALERT] {mailbox}: failed to send alert: {exc}")
    return results


def _deliver(to_email: str, subject: str, body: str) -> None:
    from modules.graph_outlook import send_new_mail
    try:
        send_new_mail(ALERT_FROM, to_email, subject, body, bcc=None)
        return
    except PermissionError as e:
        print(f"[ALERT] Graph Mail.Send not granted yet ({e}); falling back to Gmail with Reply-To {ALERT_FROM}")
    _gmail_fallback(to_email, subject, body)


def _gmail_fallback(to_email: str, subject: str, body: str) -> None:
    import base64
    from email.mime.text import MIMEText
    from web_auth import WebAuthFlow
    from googleapiclient.discovery import build
    with get_db() as db:
        u = db.query(User).filter(User.email_gmail == "wbbrill@gmail.com").first()
        token = u.oauth_token if u else None
    if not token:
        raise RuntimeError("no Gmail token on file for the fallback sender")
    creds = WebAuthFlow.credentials_from_token(token)
    msg = MIMEText(body, _charset="utf-8")
    msg["to"] = to_email
    if ALERT_CC:
        msg["cc"] = ", ".join(ALERT_CC)
    msg["from"] = f"Bill Brill <wbbrill@gmail.com>"
    msg["reply-to"] = ALERT_FROM
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    build("gmail", "v1", credentials=creds, cache_discovery=False).users().messages().send(
        userId="me", body={"raw": raw}).execute()
