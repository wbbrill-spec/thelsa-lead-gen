"""Google OAuth 2.0 web flow for the TMS Lead Gen Engine.

Matches the pattern from the Thelsa Library web_auth.py exactly.
Store credentials one of two ways:
  1. Env:  set GOOGLE_WEB_CREDENTIALS_JSON to the full JSON string (Render)
  2. File: save downloaded JSON as web_credentials.json at project root (local dev)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent

WEB_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
]


class WebAuthError(RuntimeError):
    """Raised when Google OAuth fails in the web flow."""


def _load_client_config() -> dict:
    """Load Web application OAuth client config.

    Prefers GOOGLE_WEB_CREDENTIALS_JSON env var (Render).
    Falls back to web_credentials.json at project root (local dev).
    """
    env_json = os.environ.get("GOOGLE_WEB_CREDENTIALS_JSON")
    if env_json:
        try:
            return json.loads(env_json)
        except json.JSONDecodeError as exc:
            raise WebAuthError("GOOGLE_WEB_CREDENTIALS_JSON is not valid JSON.") from exc

    path = _PROJECT_ROOT / "web_credentials.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    raise WebAuthError(
        "Web OAuth credentials not found. "
        "Create a 'Web application' OAuth client in Google Cloud Console, "
        "download the JSON, and set GOOGLE_WEB_CREDENTIALS_JSON env var."
    )


class WebAuthFlow:
    """Manages a single Google OAuth 2.0 authorization round-trip."""

    def __init__(self, redirect_uri: str) -> None:
        try:
            from google_auth_oauthlib.flow import Flow
        except ImportError as exc:
            raise WebAuthError(
                "google-auth-oauthlib is required. Run: pip install google-auth-oauthlib"
            ) from exc

        config = _load_client_config()
        self._flow = Flow.from_client_config(
            config, scopes=WEB_SCOPES, redirect_uri=redirect_uri
        )

    def authorization_url(self) -> tuple[str, str, str]:
        """Return (auth_url, state, code_verifier) for a PKCE-protected sign-in."""
        import hashlib
        import base64
        import secrets

        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()

        url, state = self._flow.authorization_url(
            access_type="offline",
            prompt="consent",
            code_challenge=code_challenge,
            code_challenge_method="S256",
        )
        return url, state, code_verifier

    def exchange_code(
        self,
        *,
        authorization_response: str,
        expected_state: str | None,
        code_verifier: str,
    ):
        """Exchange the authorization code for credentials (PKCE flow)."""
        try:
            self._flow.fetch_token(
                authorization_response=authorization_response,
                code_verifier=code_verifier,
            )
        except Exception as exc:
            raise WebAuthError(f"Token exchange failed: {exc}") from exc
        return self._flow.credentials

    def get_user_info(self, credentials) -> dict:
        """Fetch signed-in user's profile (id, email, name) from Google."""
        try:
            from googleapiclient.discovery import build
            svc = build("oauth2", "v2", credentials=credentials, cache_discovery=False)
            return svc.userinfo().get().execute()
        except Exception as exc:
            raise WebAuthError(f"Failed to get user info: {exc}") from exc

    @staticmethod
    def credentials_from_token(token_json: str):
        """Reload credentials from a stored token JSON string."""
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request

            creds = Credentials.from_authorized_user_info(
                json.loads(token_json), WEB_SCOPES
            )
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            return creds
        except Exception as exc:
            raise WebAuthError(f"Failed to load/refresh credentials: {exc}") from exc
