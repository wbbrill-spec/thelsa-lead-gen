"""
email_sender.py — SendGrid integration with circuit breaker and guardrails.
Handles sending, bounce tracking, and spam complaint processing.
"""

import json
import logging
from typing import Optional, Tuple

import sendgrid
from sendgrid.helpers.mail import Mail, Email, To, Content

from engine.config import (
    SENDGRID_API_KEY,
    AGENT_EMAIL,
    AGENT_NAME,
    MAX_EMAILS_SENT_PER_DAY,
    MAX_EMAILS_PER_CONTACT,
    RECONTACT_LOCKOUT_DAYS,
    MAX_BOUNCE_RATE_PCT,
    MAX_SPAM_COMPLAINT_RATE_PCT,
)
from engine.database import (
    get_db,
    mark_email_sent,
    log_email_event,
    add_to_suppression,
    get_counter,
    increment_counter,
    get_contact_send_count,
    days_since_last_send,
    is_suppressed,
)

logger = logging.getLogger(__name__)


# ── Circuit breaker ───────────────────────────────────────────────────────────

def _check_circuit_breaker() -> Tuple[bool, str]:
    """
    Check if deliverability metrics are within safe thresholds.
    Returns (is_safe, reason_if_not_safe).
    """
    with get_db() as conn:
        sent    = conn.execute("SELECT COUNT(*) FROM emails WHERE status='sent'").fetchone()[0]
        bounces = conn.execute(
            "SELECT COUNT(*) FROM email_events WHERE event_type='bounce'"
        ).fetchone()[0]
        spam    = conn.execute(
            "SELECT COUNT(*) FROM email_events WHERE event_type='spam'"
        ).fetchone()[0]

    if sent < 10:
        return True, ""   # not enough data to trip circuit breaker

    bounce_rate = (bounces / sent) * 100
    spam_rate   = (spam / sent) * 100

    if bounce_rate > MAX_BOUNCE_RATE_PCT:
        return False, f"Bounce rate {bounce_rate:.1f}% exceeds {MAX_BOUNCE_RATE_PCT}% threshold"
    if spam_rate > MAX_SPAM_COMPLAINT_RATE_PCT:
        return False, f"Spam complaint rate {spam_rate:.2f}% exceeds {MAX_SPAM_COMPLAINT_RATE_PCT}% threshold"

    return True, ""


# ── Pre-send validation ───────────────────────────────────────────────────────

def _can_send_to_contact(contact_id: int, contact_email: str) -> Tuple[bool, str]:
    """
    Run all pre-send checks. Returns (can_send, reason_if_not).
    """
    if is_suppressed(contact_email):
        return False, "Contact is on suppression list"

    send_count = get_contact_send_count(contact_id)
    if send_count >= MAX_EMAILS_PER_CONTACT:
        return False, f"Contact has already received {send_count} emails (max {MAX_EMAILS_PER_CONTACT})"

    days_since = days_since_last_send(contact_id)
    if days_since is not None and days_since < RECONTACT_LOCKOUT_DAYS:
        return False, f"Last email was {days_since} days ago (lockout: {RECONTACT_LOCKOUT_DAYS} days)"

    daily_count = get_counter("emails_sent")
    if daily_count >= MAX_EMAILS_SENT_PER_DAY:
        return False, f"Daily email cap reached ({MAX_EMAILS_SENT_PER_DAY})"

    safe, reason = _check_circuit_breaker()
    if not safe:
        return False, f"Circuit breaker active: {reason}"

    return True, ""


# ── SendGrid sender ───────────────────────────────────────────────────────────

def _send_via_sendgrid(to_email: str, to_name: str, subject: str,
                        body_html: str) -> Optional[str]:
    """
    Send one email via SendGrid. Returns message ID on success, None on failure.
    """
    if not SENDGRID_API_KEY:
        logger.error("SENDGRID_API_KEY not configured.")
        return None

    try:
        sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)
        message = Mail(
            from_email=Email(AGENT_EMAIL, AGENT_NAME),
            to_emails=To(to_email, to_name),
            subject=subject,
            html_content=Content("text/html", body_html),
        )
        # Enable tracking
        message.tracking_settings = {
            "click_tracking": {"enable": True},
            "open_tracking":  {"enable": True},
        }
        response = sg.send(message)

        if response.status_code in (200, 202):
            msg_id = response.headers.get("X-Message-Id", "unknown")
            logger.info(f"Email sent to {to_email} — SendGrid ID: {msg_id}")
            return msg_id
        else:
            logger.error(f"SendGrid error {response.status_code}: {response.body}")
            return None

    except Exception as e:
        logger.error(f"SendGrid exception: {e}")
        return None


def _text_to_html(text: str) -> str:
    """Convert plain text email body to simple HTML."""
    paragraphs = text.strip().split("\n\n")
    html_parts = [
        "<html><body style='font-family:Arial,sans-serif;font-size:14px;"
        "line-height:1.6;color:#333;max-width:600px'>"
    ]
    for para in paragraphs:
        if para.strip().startswith("---"):
            html_parts.append(
                "<hr style='border:none;border-top:1px solid #eee;margin:20px 0'>"
            )
            rest = para.replace("---", "", 1).strip()
            if rest:
                html_parts.append(
                    f"<p style='font-size:11px;color:#888'>{rest.replace(chr(10), '<br>')}</p>"
                )
        else:
            html_parts.append(f"<p>{para.replace(chr(10), '<br>')}</p>")
    html_parts.append("</body></html>")
    return "".join(html_parts)


# ── Main send function ────────────────────────────────────────────────────────

def send_approved_email(email_id: int, language: str = "english") -> bool:
    """
    Send an approved email. Runs all guardrail checks first.
    language: "english" | "spanish" | "both"
    Returns True on success.
    """
    with get_db() as conn:
        row = conn.execute("""
            SELECT e.*, c.email as contact_email,
                   c.first_name, c.last_name, c.id as contact_id
            FROM emails e
            JOIN contacts c ON e.contact_id = c.id
            WHERE e.id = ? AND e.status = 'approved'
        """, (email_id,)).fetchone()

    if not row:
        logger.error(f"Email ID {email_id} not found or not in 'approved' status.")
        return False

    contact_id    = row["contact_id"]
    contact_email = row["contact_email"]
    contact_name  = f"{row['first_name'] or ''} {row['last_name'] or ''}".strip() or contact_email

    # ── Run all pre-send checks ──────────────────────────────────────────────
    can_send, reason = _can_send_to_contact(contact_id, contact_email)
    if not can_send:
        logger.warning(f"Send blocked for {contact_email}: {reason}")
        with get_db() as conn:
            conn.execute(
                "UPDATE emails SET status='draft', updated_at=datetime('now') WHERE id=?",
                (email_id,)
            )
        return False

    # ── Prepare and send ─────────────────────────────────────────────────────
    subject = row["subject"] or "(No subject)"
    success = False

    if language in ("english", "both"):
        body_html = _text_to_html(row["body_english"] or "")
        msg_id = _send_via_sendgrid(contact_email, contact_name, subject, body_html)
        if msg_id:
            increment_counter("emails_sent")
            mark_email_sent(email_id, msg_id, "english")
            success = True

    if language in ("spanish", "both") and row["body_spanish"]:
        subj_es = f"[ES] {subject}"
        body_html_es = _text_to_html(row["body_spanish"])
        msg_id_es = _send_via_sendgrid(contact_email, contact_name, subj_es, body_html_es)
        if msg_id_es:
            increment_counter("emails_sent")
            success = True

    return success


# ── Webhook handler (call from a Flask route to process SendGrid events) ──────

def process_sendgrid_webhook(events: list):
    """
    Process SendGrid event webhook payload.
    Call this from a /webhook/sendgrid POST endpoint in dashboard.py.
    Events include: open, click, bounce, spam_report, unsubscribe.
    """
    for event in events:
        event_type = event.get("event", "")
        sg_msg_id  = event.get("sg_message_id", "").split(".")[0]
        email_addr = event.get("email", "")

        # Find the email record by SendGrid message ID
        with get_db() as conn:
            row = conn.execute(
                "SELECT id FROM emails WHERE sendgrid_msg_id LIKE ?",
                (f"{sg_msg_id}%",)
            ).fetchone()

        if not row:
            logger.debug(f"Webhook event for unknown message ID: {sg_msg_id}")
            continue

        email_id = row["id"]
        event_map = {
            "open":        "open",
            "click":       "click",
            "bounce":      "bounce",
            "spamreport":  "spam",
            "unsubscribe": "unsubscribe",
        }
        mapped = event_map.get(event_type)
        if mapped:
            log_email_event(email_id, mapped, json.dumps(event))
            logger.info(f"Tracked {mapped} event for email ID {email_id}")

            # Auto-suppress bounces and spam
            if mapped in ("bounce", "spam") and email_addr:
                add_to_suppression(email_addr, reason=mapped)
