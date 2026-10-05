"""Outlook (Microsoft Graph) draft creation — app-only client credentials.

Creates a draft in a specified thelsa.com mailbox and never sends. Mirrors the
Thelsa Automation Library's Graph mailer. Used to place a lead's outreach draft
into the assigned rep's Outlook Drafts folder.

Env vars required (same Azure app registration as the Library):
  GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET
Optional:
  ALLOWED_MAILBOXES  (comma-separated; defaults to the three Thelsa reps)
"""
from __future__ import annotations

import os
import threading
import time

import requests

GRAPH = "https://graph.microsoft.com/v1.0"

# App-only tokens last ~1h; cache them instead of requesting one per Graph call
# (the tracker used to request dozens per cycle and hit AAD throttling).
_TOKEN_CACHE = {"token": None, "expires_at": 0.0}
_TOKEN_LOCK = threading.Lock()

# Mailboxes the app is permitted to draft into (the assignable reps).
ALLOWED_MAILBOXES = [
    m.strip().lower()
    for m in os.environ.get(
        "ALLOWED_MAILBOXES",
        "bbrill@thelsa.com,armandosilveyra@thelsa.com,gustavogonzalez@thelsa.com",
    ).split(",")
    if m.strip()
]


def _token() -> str:
    with _TOKEN_LOCK:
        if _TOKEN_CACHE["token"] and _TOKEN_CACHE["expires_at"] > time.time() + 120:
            return _TOKEN_CACHE["token"]
        tok, ttl = _fetch_token()
        _TOKEN_CACHE["token"] = tok
        _TOKEN_CACHE["expires_at"] = time.time() + ttl
        return tok


def _fetch_token() -> tuple[str, float]:
    missing = [k for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET")
               if not os.environ.get(k, "").strip()]
    if missing:
        raise RuntimeError(f"Microsoft Graph not configured — missing env var(s): {', '.join(missing)}")
    tenant = os.environ["GRAPH_TENANT_ID"].strip()
    client_id = os.environ["GRAPH_CLIENT_ID"].strip()
    secret = os.environ["GRAPH_CLIENT_SECRET"].strip()
    r = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "client_id": client_id,
            "client_secret": secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    if r.status_code != 200:
        detail = ""
        try:
            j = r.json()
            detail = j.get("error_description", "") or j.get("error", "")
        except Exception:
            detail = r.text[:300]
        raise RuntimeError(
            f"Microsoft token request failed [{r.status_code}]: "
            f"{detail.splitlines()[0] if detail else r.text[:200]}"
        )
    j = r.json()
    return j["access_token"], float(j.get("expires_in", 3600))


def graph_request(method: str, url: str, *, retries: int = 3, **kwargs) -> requests.Response:
    """Call Graph with a cached token, retrying 429 / 5xx and honouring Retry-After."""
    last = None
    base_headers = dict(kwargs.pop("headers", {}) or {})
    timeout = kwargs.pop("timeout", 30)
    for attempt in range(retries):
        headers = dict(base_headers)
        headers["Authorization"] = f"Bearer {_token()}"
        r = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        if r.status_code == 401 and attempt == 0:
            with _TOKEN_LOCK:
                _TOKEN_CACHE["token"] = None  # token revoked/expired early — refresh once
            last = r
            continue
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            wait = float(r.headers.get("Retry-After", 2 ** attempt))
            time.sleep(min(wait, 30))
            last = r
            continue
        return r
    return last


def create_outlook_draft(mailbox: str, to_email: str, subject: str, body: str) -> dict:
    """Create a draft in ``mailbox``'s Outlook Drafts folder. Returns {'id','webLink'}.

    Drafts only — never sends. Raises if the mailbox is not in ALLOWED_MAILBOXES.
    """
    mailbox = (mailbox or "").strip()
    if mailbox.lower() not in ALLOWED_MAILBOXES:
        raise ValueError(f"mailbox not allowed: {mailbox}")

    msg = {"subject": subject, "body": {"contentType": "Text", "content": body}}
    if to_email:
        msg["toRecipients"] = [{"emailAddress": {"address": to_email}}]

    r = graph_request(
        "POST",
        f"{GRAPH}/users/{mailbox}/messages",
        headers={"Content-Type": "application/json"},
        json=msg,
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Graph create_draft failed [{r.status_code}]: {r.text[:400]}")
    d = r.json()
    return {"id": d.get("id", ""), "webLink": d.get("webLink", "")}


# ── Follow-ups: reply in the original thread ──────────────────────────────────

def create_reply_draft(mailbox: str, message_id: str, body: str,
                       bcc: list[str] | None = None, subject: str | None = None) -> dict:
    """Create a reply DRAFT to ``message_id`` in ``mailbox`` (threaded: same
    conversation, "Re:" subject, original quoted below). Returns {'id','webLink'}."""
    mailbox = (mailbox or "").strip()
    if mailbox.lower() not in ALLOWED_MAILBOXES:
        raise ValueError(f"mailbox not allowed: {mailbox}")
    r = graph_request("POST", f"{GRAPH}/users/{mailbox}/messages/{message_id}/createReply",
                      headers={"Content-Type": "application/json"}, json={}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Graph createReply failed [{r.status_code}]: {r.text[:400]}")
    draft = r.json()
    draft_id = draft.get("id", "")
    # Prepend our text to the quoted original (keep Graph's quoted thread below).
    quoted = (draft.get("body") or {}).get("content") or ""
    ctype = ((draft.get("body") or {}).get("contentType") or "HTML")
    if ctype.lower() == "html":
        new_body = _text_to_html(body) + quoted
    else:
        new_body = body + "\n\n" + quoted
    patch: dict = {"body": {"contentType": ctype, "content": new_body}}
    if subject:
        patch["subject"] = subject
    if bcc:
        patch["bccRecipients"] = [{"emailAddress": {"address": a}} for a in bcc if a]
    r = graph_request("PATCH", f"{GRAPH}/users/{mailbox}/messages/{draft_id}",
                      headers={"Content-Type": "application/json"}, json=patch, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Graph patch reply failed [{r.status_code}]: {r.text[:400]}")
    d = r.json()
    return {"id": d.get("id", draft_id), "webLink": d.get("webLink", ""),
            "subject": d.get("subject", ""), "conversationId": d.get("conversationId", "")}


def send_draft(mailbox: str, draft_id: str) -> None:
    """Send an existing draft. Requires application permission Mail.Send."""
    r = graph_request("POST", f"{GRAPH}/users/{mailbox}/messages/{draft_id}/send", timeout=30)
    if r.status_code == 403:
        raise PermissionError(
            "Microsoft Graph refused to send (403). The Azure app needs the application "
            "permission Mail.Send with admin consent — ask IT, or set FOLLOWUP_MODE=draft."
        )
    if r.status_code >= 300:
        raise RuntimeError(f"Graph send failed [{r.status_code}]: {r.text[:400]}")


def send_new_mail(mailbox: str, to_email: str, subject: str, body: str,
                  bcc: list[str] | None = None) -> None:
    """Send a brand-new message (used when there is no original to reply to)."""
    mailbox = (mailbox or "").strip()
    if mailbox.lower() not in ALLOWED_MAILBOXES:
        raise ValueError(f"mailbox not allowed: {mailbox}")
    msg = {"subject": subject, "body": {"contentType": "Text", "content": body},
           "toRecipients": [{"emailAddress": {"address": to_email}}]}
    if bcc:
        msg["bccRecipients"] = [{"emailAddress": {"address": a}} for a in bcc if a]
    r = graph_request("POST", f"{GRAPH}/users/{mailbox}/sendMail",
                      headers={"Content-Type": "application/json"},
                      json={"message": msg, "saveToSentItems": True}, timeout=30)
    if r.status_code == 403:
        raise PermissionError("Microsoft Graph refused to send (403) — Mail.Send permission missing.")
    if r.status_code >= 300:
        raise RuntimeError(f"Graph sendMail failed [{r.status_code}]: {r.text[:400]}")


def _text_to_html(text: str) -> str:
    import html
    paras = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    return "".join(
        "<p style=\"font-family:Calibri,Arial,sans-serif;font-size:11pt;margin:0 0 12px 0\">"
        + html.escape(p).replace("\n", "<br>") + "</p>"
        for p in paras
    ) + "<br>"
