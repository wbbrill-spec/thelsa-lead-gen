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
# Single model name for every module. Override in Render without a deploy when
# a model is retired (this has caused two outages before).
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_TIMEOUT_SECONDS = float(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "90"))
CLAUDE_MAX_RETRIES = int(os.environ.get("CLAUDE_MAX_RETRIES", "5"))

# ── Pipeline ───────────────────────────────────────────────────────────────────
# A RUNNING discovery run whose heartbeat is older than this is considered dead
# (process restarted / deploy mid-run) and is marked FAILED by the reaper.
RUN_STALE_MINUTES = int(os.environ.get("RUN_STALE_MINUTES", "45"))
# Refuse to start a discovery run when SerpAPI has fewer searches left than this
# (a run uses ~8 discovery searches + 1 per contact fallback + 1 per large corp).
SEARCH_MIN_LEFT = int(os.environ.get("SEARCH_MIN_LEFT", "15"))

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


# ── Startup validation ─────────────────────────────────────────────────────────
_REQUIRED_IN_PRODUCTION = {
    "ANTHROPIC_API_KEY": "Claude extraction / scoring / drafting will fail",
    "SEARCH_API_KEY": "discovery searches will fail (no mock data in production)",
    "DATABASE_URL": "app would write to an ephemeral SQLite file and lose data on restart",
    "FLASK_SECRET_KEY": "sessions would reset on every restart",
    "GRAPH_TENANT_ID": "Outlook drafts and sent/reply tracking will fail",
    "GRAPH_CLIENT_ID": "Outlook drafts and sent/reply tracking will fail",
    "GRAPH_CLIENT_SECRET": "Outlook drafts and sent/reply tracking will fail",
    "CRON_TOKEN": "the daily cron trigger (/cron/run) is disabled",
}


def config_warnings() -> list[str]:
    """Return human-readable warnings for missing / dangerous configuration."""
    warnings: list[str] = []
    for var, consequence in _REQUIRED_IN_PRODUCTION.items():
        if not os.environ.get(var, "").strip():
            warnings.append(f"{var} is not set — {consequence}")
    if FLASK_ENV == "production" and "sqlite" in DATABASE_URL:
        warnings.append("DATABASE_URL points at SQLite in production")
    if FLASK_SECRET_KEY == "dev-secret-CHANGE-IN-PRODUCTION" and FLASK_ENV == "production":
        warnings.append("FLASK_SECRET_KEY is the insecure default")
    if not ZOOMINFO_CLIENT_ID or not ZOOMINFO_CLIENT_SECRET:
        warnings.append("ZOOMINFO_CLIENT_ID/SECRET not set — contact enrichment uses web search only")
    return warnings


def log_config_warnings(tag: str = "CONFIG") -> list[str]:
    warnings = config_warnings()
    for w in warnings:
        print(f"[{tag}] WARNING: {w}")
    if not warnings:
        print(f"[{tag}] all required environment variables present (model={CLAUDE_MODEL})")
    return warnings
