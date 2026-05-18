"""
gmail_drafts.py — Creates a Gmail API draft in the triggering user's mailbox.

Called by email_drafter.py after a draft is saved to SQLite.  Only runs when
PIPELINE_USER_TOKEN_PATH is set in the environment — meaning the pipeline was
triggered by a logged-in team member via the Automation Library.  Failures are
non-fatal: the SQLite draft is always saved regardless.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.compose",
]


def create_gmail_draft(
    *,
    to_email: str,
    subject: str,
    body_english: str,
    body_spanish: Optional[str] = None,
    token_dict: Optional[dict] = None,
) -> Optional[str]:
    """
    Save a draft to the triggering user's Gmail Drafts folder.

    Accepts a token_dict directly (from Flask session) or falls back to reading
    PIPELINE_USER_TOKEN_PATH from the environment.  Returns the Gmail draft ID
    on success, None on any failure (including when no token is available).
    """
    # Resolve credentials: prefer token_dict passed directly, then fall back to file
    resolved_token_data: Optional[dict] = None

    if token_dict:
        resolved_token_data = token_dict
    else:
        token_path_str = os.environ.get("PIPELINE_USER_TOKEN_PATH", "").strip()
        if not token_path_str:
            logger.debug("Gmail draft skipped: no token_dict and PIPELINE_USER_TOKEN_PATH not set")
            return None
        token_path = Path(token_path_str)
        if not token_path.exists():
            logger.debug("Gmail draft skipped: token file not found at %s", token_path)
            return None
        resolved_token_data = json.loads(token_path.read_text())

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_info(resolved_token_data, _SCOPES)

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # If we loaded from a file, persist the refreshed token
            if not token_dict:
                token_path = Path(os.environ.get("PIPELINE_USER_TOKEN_PATH", ""))
                if token_path.exists():
                    token_path.write_text(creds.to_json())

        # Build the email body — both languages separated by a rule
        body_parts = [body_english]
        if body_spanish:
            body_parts.append("─" * 60)
            body_parts.append(body_spanish)
        full_body = "\n\n".join(body_parts)

        msg = MIMEMultipart("alternative")
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(full_body, "plain", "utf-8"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        svc   = build("gmail", "v1", credentials=creds, cache_discovery=False)
        draft = svc.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw}},
        ).execute()

        draft_id = draft.get("id", "")
        user_email = os.environ.get("PIPELINE_USER_EMAIL", "unknown")
        logger.info(
            "Gmail draft saved → %s | to: %s | draft_id: %s",
            user_email, to_email, draft_id,
        )
        return draft_id

    except ImportError:
        logger.debug(
            "gmail_drafts: google-api-python-client not installed — skipping Gmail draft."
        )
        return None
    except Exception as exc:
        logger.warning("Gmail draft creation failed (non-fatal): %s", exc)
        return None
