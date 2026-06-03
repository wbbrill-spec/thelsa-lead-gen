"""Configuration — loads all environment variables for the TMS Lead Gen Engine."""

from __future__ import annotations
import os
from pathlib import Path

# ── Flask ──────────────────────────────────────────────────────────────────────
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-CHANGE-IN-PRODUCTION")
FLASK_ENV = os.environ.get("FLASK_ENV", "development")

# ── Database ───────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///tms_leadgen_dev.db")

# Render sets DATABASE_URL with postgres:// prefix; SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ── Google OAuth + Gmail API ───────────────────────────────────────────────────
# Full JSON credentials for the Web application OAuth client.
# Set GOOGLE_WEB_CREDENTIALS_JSON in Render dashboard (paste the JSON string).
# Falls back to web_credentials.json at project root for local dev.
GOOGLE_WEB_CREDENTIALS_JSON = os.environ.get("GOOGLE_WEB_CREDENTIALS_JSON", "")

# ── Token encryption ───────────────────────────────────────────────────────────
# Fernet key for encrypting OAuth tokens at rest in the users table.
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
TOKEN_ENCRYPTION_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY", "")

# ── Anthropic ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Web Search ─────────────────────────────────────────────────────────────────
SEARCH_API_KEY = os.environ.get("SEARCH_API_KEY", "")
SEARCH_PROVIDER = os.environ.get("SEARCH_PROVIDER", "serpapi")  # serpapi or perplexity

# ── ZoomInfo ───────────────────────────────────────────────────────────────────
ZOOMINFO_CLIENT_ID = os.environ.get("ZOOMINFO_CLIENT_ID", "")
ZOOMINFO_CLIENT_SECRET = os.environ.get("ZOOMINFO_CLIENT_SECRET", "")
ZOOMINFO_BASE_URL = "https://api.zoominfo.com/gtm"
ZOOMINFO_TOKEN_URL = "https://api.zoominfo.com/gtm/oauth/v1/token"

# ── Thelsa Library ─────────────────────────────────────────────────────────────
THELSA_LIBRARY_URL = os.environ.get("THELSA_LIBRARY_URL", "https://thelsa.inflectionpointnow.com")

# ── Dev helpers ────────────────────────────────────────────────────────────────
if FLASK_ENV != "production":
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
