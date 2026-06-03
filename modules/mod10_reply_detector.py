"""MOD-10: Reply Detector

Checks whether a contact has replied to any email in an outreach thread.
Uses Gmail API (Phase 1) or Outlook API (Phase 2).
Provider-agnostic interface.
"""

from __future__ import annotations
from datetime import datetime, timezone
from db import get_db
from models import Lead, EmailDraft, User


def check_for_reply(lead_id: int) -> bool:
    """Check if the contact has replied to any draft in the outreach thread.

    Updates lead.reply_detected and lead.reply_detected_at if reply found.

    Returns:
        True if reply detected, False otherwise
    """
    with get_db() as db:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            return False

        # Already detected — no need to recheck
        if lead.reply_detected:
            return True

        # Get the initial EN draft's provider_draft_id for thread lookup
        initial_draft = db.query(EmailDraft).filter_by(
            lead_id=lead_id,
            draft_type="INITIAL",
            language="EN",
        ).first()

        if not initial_draft or not initial_draft.provider_draft_id:
            return False

        assigned_user = lead.assigned_to
        if not assigned_user or not assigned_user.oauth_token:
            return False

        provider = initial_draft.provider or "gmail"
        provider_draft_id = initial_draft.provider_draft_id
        user_email = assigned_user.active_email
        token_json = assigned_user.oauth_token

    # Check outside DB session
    if provider == "gmail":
        reply_found = _check_gmail_reply(token_json, user_email, provider_draft_id)
    else:
        reply_found = _check_outlook_reply(token_json, provider_draft_id)

    if reply_found:
        with get_db() as db:
            lead = db.query(Lead).filter_by(id=lead_id).first()
            if lead:
                lead.reply_detected = True
                lead.reply_detected_at = datetime.now(timezone.utc)

                from models import transition_status
                transition_status(
                    db, lead, Lead.STATUS_RESPONDED,
                    changed_by="system",
                    reason="Reply detected in email thread",
                )

    return reply_found


def _check_gmail_reply(token_json: str, user_email: str, draft_id: str) -> bool:
    """Check Gmail thread for a reply from the contact."""
    try:
        from web_auth import WebAuthFlow
        from googleapiclient.discovery import build

        creds = WebAuthFlow.credentials_from_token(token_json)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)

        # Get the draft to find the thread ID
        draft = service.users().drafts().get(
            userId="me",
            id=draft_id,
            format="metadata",
        ).execute()

        thread_id = (draft.get("message") or {}).get("threadId")
        if not thread_id:
            return False

        # Get all messages in the thread
        thread = service.users().threads().get(
            userId="me",
            id=thread_id,
            format="metadata",
            metadataHeaders=["From"],
        ).execute()

        messages = thread.get("messages", [])

        # If more than 1 message in thread, check if any are from someone else
        if len(messages) <= 1:
            return False

        for msg in messages:
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            from_header = headers.get("From", "").lower()
            if user_email.lower() not in from_header:
                return True  # Message from someone other than the user

        return False

    except Exception as e:
        print(f"[MOD-10] Gmail reply check error: {e}")
        return False


def _check_outlook_reply(token_json: str, message_id: str) -> bool:
    """Check Outlook thread for a reply. Phase 2 placeholder."""
    # TODO: Implement via Microsoft Graph API in Phase 8
    # GET /me/messages/{id}/extensions or check conversationId thread
    print("[MOD-10] Outlook reply detection not yet implemented (Phase 8)")
    return False
