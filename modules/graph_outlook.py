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
