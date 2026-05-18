"""
Render / production entry point for the TMS Corp Lead Gen Engine dashboard.

Gunicorn imports this module and calls the `app` object directly.
The dashboard database is initialised on startup so the first request
doesn't hit an empty schema.

Environment variables required (set in Render dashboard):
  ANTHROPIC_API_KEY   — from console.anthropic.com
  SENDGRID_API_KEY    — from app.sendgrid.com
  AGENT_EMAIL         — verified SendGrid sender address
  AGENT_NAME          — full name shown on outbound emails
  AGENT_TITLE         — title shown on outbound emails
  AGENCY_ADDRESS      — physical address (CAN-SPAM requirement)
  FLASK_SECRET_KEY    — random secret for session signing
  PORT                — set automatically by Render
"""

import os
from engine.database import init_db
from engine.dashboard import app

# Apply a proper secret key for session signing on Render
app.secret_key = os.environ.get("FLASK_SECRET_KEY", app.secret_key)

# Ensure the SQLite database and tables exist before the first request
init_db()
