"""
database.py — SQLite schema and data access layer.
All engine state (leads, contacts, emails, events, suppression) lives here.
The database file is stored in data/leads.db inside your project folder.
"""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager
from typing import Optional, List
from engine.config import DB_PATH

logger = logging.getLogger(__name__)


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
-- Companies identified as potential leads
CREATE TABLE IF NOT EXISTS companies (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    domain               TEXT,                   -- e.g. acme.com
    industry             TEXT,
    description          TEXT,
    expansion_direction  TEXT DEFAULT 'unknown', -- to_mexico | to_us_canada | unknown
    source_url           TEXT,                   -- article/filing that surfaced this company
    source_name          TEXT,                   -- e.g. "Globe and Mail Business"
    raw_snippet          TEXT,                   -- original text from source
    qualification_score  INTEGER DEFAULT 0,      -- 0-10, set by Haiku classifier
    qualification_reason TEXT,                   -- why this score was assigned
    status               TEXT DEFAULT 'new',     -- new | qualified | disqualified | contacted | closed
    created_at           TEXT DEFAULT (datetime('now')),
    updated_at           TEXT DEFAULT (datetime('now')),
    UNIQUE(name, domain)                         -- deduplication key
);

-- Decision-maker contacts at each company
CREATE TABLE IF NOT EXISTS contacts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id        INTEGER NOT NULL REFERENCES companies(id),
    first_name        TEXT,
    last_name         TEXT,
    title             TEXT,                      -- e.g. "HR Director", "Global Mobility Manager"
    target_function   TEXT,                      -- hr | global_mobility | comp_benefits | procurement
    email             TEXT,
    linkedin_url      TEXT,
    phone             TEXT,
    email_verified    INTEGER DEFAULT 0,         -- 1 if verified by enrichment API
    enrichment_source TEXT,                      -- "hunter", "apollo", "manual"
    do_not_contact    INTEGER DEFAULT 0,         -- 1 if on suppression list
    created_at        TEXT DEFAULT (datetime('now')),
    UNIQUE(email)
);

-- Draft and sent emails
CREATE TABLE IF NOT EXISTS emails (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id       INTEGER NOT NULL REFERENCES contacts(id),
    company_id       INTEGER NOT NULL REFERENCES companies(id),
    subject          TEXT,
    body_english     TEXT,
    body_spanish     TEXT,
    language_sent    TEXT,                       -- "english", "spanish", "both"
    status           TEXT DEFAULT 'draft',       -- draft | pending_approval | approved | sent | rejected
    sendgrid_msg_id  TEXT,                       -- for tracking
    approved_by      TEXT,                       -- agent name
    approved_at      TEXT,
    sent_at          TEXT,
    sequence_num     INTEGER DEFAULT 1,          -- 1=initial, 2=followup1, 3=followup2
    pipeline_run_id  TEXT,                       -- groups emails from the same pipeline run
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now'))
);

-- Tracking events (opens, clicks, bounces, replies, unsubscribes)
CREATE TABLE IF NOT EXISTS email_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id     INTEGER NOT NULL REFERENCES emails(id),
    event_type   TEXT NOT NULL,                  -- open | click | bounce | spam | unsubscribe | reply
    event_data   TEXT,                           -- JSON blob of webhook payload
    occurred_at  TEXT DEFAULT (datetime('now'))
);

-- Suppression / opt-out list
CREATE TABLE IF NOT EXISTS suppression_list (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    email     TEXT NOT NULL UNIQUE,
    reason    TEXT,                              -- unsubscribe | bounce | spam | manual
    added_at  TEXT DEFAULT (datetime('now'))
);

-- Scrape cache — prevents re-fetching the same URLs within TTL
CREATE TABLE IF NOT EXISTS scrape_cache (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash    TEXT NOT NULL UNIQUE,            -- MD5 of the URL
    url         TEXT NOT NULL,
    content     TEXT,
    fetched_at  TEXT DEFAULT (datetime('now'))
);

-- Daily usage counters (for enforcing hard caps)
CREATE TABLE IF NOT EXISTS daily_counters (
    date          TEXT NOT NULL,
    counter_name  TEXT NOT NULL,                 -- "ai_calls", "emails_sent", "enrichment_lookups"
    count         INTEGER DEFAULT 0,
    PRIMARY KEY (date, counter_name)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_companies_status   ON companies(status);
CREATE INDEX IF NOT EXISTS idx_companies_domain   ON companies(domain);
CREATE INDEX IF NOT EXISTS idx_contacts_company   ON contacts(company_id);
CREATE INDEX IF NOT EXISTS idx_contacts_email     ON contacts(email);
CREATE INDEX IF NOT EXISTS idx_emails_contact     ON emails(contact_id);
CREATE INDEX IF NOT EXISTS idx_emails_status      ON emails(status);
CREATE INDEX IF NOT EXISTS idx_events_email       ON email_events(email_id);
CREATE INDEX IF NOT EXISTS idx_suppression_email  ON suppression_list(email);
CREATE INDEX IF NOT EXISTS idx_cache_hash         ON scrape_cache(url_hash);
"""


# ── Connection ─────────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    """Context manager for database connections. Auto-commits or rolls back."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # better concurrent read performance
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables if they don't exist. Safe to call on every startup."""
    with get_db() as conn:
        conn.executescript(SCHEMA)
        # Migrations: add columns if they don't exist yet
        email_cols = [r[1] for r in conn.execute("PRAGMA table_info(emails)").fetchall()]
        if "pipeline_run_id" not in email_cols:
            conn.execute("ALTER TABLE emails ADD COLUMN pipeline_run_id TEXT")
            logger.info("Migration: added pipeline_run_id column to emails table.")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_run_id ON emails(pipeline_run_id)")

        co_cols = [r[1] for r in conn.execute("PRAGMA table_info(companies)").fetchall()]
        if "company_tier" not in co_cols:
            conn.execute("ALTER TABLE companies ADD COLUMN company_tier TEXT DEFAULT 'smb'")
            logger.info("Migration: added company_tier column to companies table.")
        if "rmc_partner" not in co_cols:
            conn.execute("ALTER TABLE companies ADD COLUMN rmc_partner TEXT")
            logger.info("Migration: added rmc_partner column to companies table.")
        if "rmc_domain" not in co_cols:
            conn.execute("ALTER TABLE companies ADD COLUMN rmc_domain TEXT")
            logger.info("Migration: added rmc_domain column to companies table.")
    logger.info(f"Database initialised at {DB_PATH}")


# ── Daily Counter Helpers ──────────────────────────────────────────────────────

def increment_counter(counter_name: str, amount: int = 1) -> int:
    """Increment a daily counter and return the new value."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with get_db() as conn:
        conn.execute("""
            INSERT INTO daily_counters (date, counter_name, count)
            VALUES (?, ?, ?)
            ON CONFLICT(date, counter_name) DO UPDATE SET count = count + ?
        """, (today, counter_name, amount, amount))
        row = conn.execute(
            "SELECT count FROM daily_counters WHERE date=? AND counter_name=?",
            (today, counter_name)
        ).fetchone()
    return row["count"] if row else 0


def get_counter(counter_name: str) -> int:
    """Get today's value for a named counter."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with get_db() as conn:
        row = conn.execute(
            "SELECT count FROM daily_counters WHERE date=? AND counter_name=?",
            (today, counter_name)
        ).fetchone()
    return row["count"] if row else 0


# ── Company Helpers ────────────────────────────────────────────────────────────

def upsert_company(name: str, domain: Optional[str] = None, **kwargs) -> Optional[int]:
    """
    Insert a new company or skip if already exists (deduplication).
    Returns the company ID, or None if skipped.
    """
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM companies WHERE name=? OR (domain IS NOT NULL AND domain=?)",
            (name, domain)
        ).fetchone()
        if existing:
            logger.debug(f"Duplicate company skipped: {name}")
            return None
        cursor = conn.execute("""
            INSERT INTO companies (name, domain, industry, description,
                                   expansion_direction, source_url, source_name, raw_snippet)
            VALUES (:name, :domain, :industry, :description,
                    :expansion_direction, :source_url, :source_name, :raw_snippet)
        """, {
            "name":               name,
            "domain":             domain,
            "industry":           kwargs.get("industry"),
            "description":        kwargs.get("description"),
            "expansion_direction": kwargs.get("expansion_direction", "unknown"),
            "source_url":         kwargs.get("source_url"),
            "source_name":        kwargs.get("source_name"),
            "raw_snippet":        kwargs.get("raw_snippet"),
        })
        return cursor.lastrowid


def update_company_qualification(company_id: int, score: int, reason: str,
                                  status: str = "qualified"):
    with get_db() as conn:
        conn.execute("""
            UPDATE companies
            SET qualification_score=?, qualification_reason=?, status=?,
                updated_at=datetime('now')
            WHERE id=?
        """, (score, reason, status, company_id))


def get_large_multinational_companies(limit: int = 50):
    """Return large multinational companies that haven't been contacted yet."""
    with get_db() as conn:
        return conn.execute("""
            SELECT * FROM companies
            WHERE status = 'large_multinational'
              AND NOT EXISTS (
                SELECT 1 FROM contacts ct
                JOIN emails e ON e.contact_id = ct.id
                WHERE ct.company_id = companies.id
                  AND e.status IN ('pending_approval','approved','sent')
              )
            ORDER BY qualification_score DESC, created_at ASC
            LIMIT ?
        """, (limit,)).fetchall()


def update_company_rmc(company_id: int, rmc_partner: Optional[str], rmc_domain: Optional[str]):
    """Store the identified RMC partner (or None) for a large multinational."""
    with get_db() as conn:
        conn.execute("""
            UPDATE companies SET rmc_partner=?, rmc_domain=?, updated_at=datetime('now')
            WHERE id=?
        """, (rmc_partner, rmc_domain, company_id))


def get_qualified_companies(limit: int = 50):
    """Return companies that passed qualification but haven't been contacted yet."""
    with get_db() as conn:
        return conn.execute("""
            SELECT * FROM companies
            WHERE status = 'qualified'
            ORDER BY qualification_score DESC, created_at ASC
            LIMIT ?
        """, (limit,)).fetchall()


# ── Contact Helpers ────────────────────────────────────────────────────────────

def upsert_contact(company_id: int, email: str, **kwargs) -> Optional[int]:
    """Insert contact; skip if email already exists."""
    if is_suppressed(email):
        logger.info(f"Contact {email} is on suppression list — skipped.")
        return None
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM contacts WHERE email=?", (email,)
        ).fetchone()
        if existing:
            return existing["id"]
        cursor = conn.execute("""
            INSERT INTO contacts (company_id, email, first_name, last_name,
                                  title, target_function, linkedin_url, phone,
                                  email_verified, enrichment_source)
            VALUES (:company_id, :email, :first_name, :last_name, :title,
                    :target_function, :linkedin_url, :phone,
                    :email_verified, :enrichment_source)
        """, {
            "company_id":       company_id,
            "email":            email,
            "first_name":       kwargs.get("first_name"),
            "last_name":        kwargs.get("last_name"),
            "title":            kwargs.get("title"),
            "target_function":  kwargs.get("target_function"),
            "linkedin_url":     kwargs.get("linkedin_url"),
            "phone":            kwargs.get("phone"),
            "email_verified":   int(kwargs.get("email_verified", False)),
            "enrichment_source": kwargs.get("enrichment_source", "manual"),
        })
        return cursor.lastrowid


def get_contact_send_count(contact_id: int) -> int:
    """How many emails have been sent to this contact (all time)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM emails WHERE contact_id=? AND status='sent'",
            (contact_id,)
        ).fetchone()
    return row["n"] if row else 0


def days_since_last_send(contact_id: int) -> Optional[int]:
    """Days since the last sent email to this contact, or None if never sent."""
    with get_db() as conn:
        row = conn.execute("""
            SELECT sent_at FROM emails
            WHERE contact_id=? AND status='sent'
            ORDER BY sent_at DESC LIMIT 1
        """, (contact_id,)).fetchone()
    if not row or not row["sent_at"]:
        return None
    last = datetime.fromisoformat(row["sent_at"])
    return (datetime.utcnow() - last).days


# ── Email Helpers ──────────────────────────────────────────────────────────────

def create_email_draft(contact_id: int, company_id: int, subject: str,
                        body_english: str, body_spanish: str,
                        sequence_num: int = 1,
                        pipeline_run_id: Optional[str] = None) -> int:
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO emails (contact_id, company_id, subject,
                                body_english, body_spanish, sequence_num, status,
                                pipeline_run_id)
            VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)
        """, (contact_id, company_id, subject, body_english, body_spanish,
              sequence_num, pipeline_run_id))
        return cursor.lastrowid


def get_pending_approvals():
    """Return emails from the most recent pipeline run awaiting agent approval."""
    with get_db() as conn:
        return conn.execute("""
            SELECT e.*, c.first_name, c.last_name, c.email as contact_email,
                   c.title, c.target_function,
                   co.name as company_name, co.industry, co.description,
                   co.expansion_direction, co.qualification_score
            FROM emails e
            JOIN contacts c ON e.contact_id = c.id
            JOIN companies co ON e.company_id = co.id
            WHERE e.status = 'pending_approval'
              AND (
                e.pipeline_run_id = (
                    SELECT pipeline_run_id FROM emails
                    WHERE status = 'pending_approval'
                      AND pipeline_run_id IS NOT NULL
                    ORDER BY created_at DESC LIMIT 1
                )
                OR (
                    -- fallback: if no run IDs exist yet, show all pending
                    NOT EXISTS (
                        SELECT 1 FROM emails
                        WHERE pipeline_run_id IS NOT NULL
                    )
                )
              )
            ORDER BY e.created_at ASC
        """).fetchall()


def approve_email(email_id: int, agent_name: str):
    with get_db() as conn:
        conn.execute("""
            UPDATE emails SET status='approved', approved_by=?,
                              approved_at=datetime('now'), updated_at=datetime('now')
            WHERE id=?
        """, (agent_name, email_id))


def reject_email(email_id: int):
    with get_db() as conn:
        conn.execute("""
            UPDATE emails SET status='rejected', updated_at=datetime('now')
            WHERE id=?
        """, (email_id,))


def mark_email_sent(email_id: int, sendgrid_msg_id: str, language: str):
    with get_db() as conn:
        conn.execute("""
            UPDATE emails SET status='sent', sendgrid_msg_id=?,
                              language_sent=?, sent_at=datetime('now'),
                              updated_at=datetime('now')
            WHERE id=?
        """, (sendgrid_msg_id, language, email_id))


# ── Suppression Helpers ────────────────────────────────────────────────────────

def add_to_suppression(email: str, reason: str = "manual"):
    with get_db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO suppression_list (email, reason)
            VALUES (?, ?)
        """, (email.lower().strip(), reason))
        # Also mark the contact as do-not-contact
        conn.execute("""
            UPDATE contacts SET do_not_contact=1
            WHERE LOWER(email)=?
        """, (email.lower().strip(),))
    logger.info(f"Added {email} to suppression list (reason: {reason})")


def is_suppressed(email: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM suppression_list WHERE LOWER(email)=?",
            (email.lower().strip(),)
        ).fetchone()
    return row is not None


# ── Scrape Cache Helpers ───────────────────────────────────────────────────────

def get_cached_scrape(url_hash: str, ttl_hours: int = 24) -> Optional[str]:
    with get_db() as conn:
        row = conn.execute("""
            SELECT content FROM scrape_cache
            WHERE url_hash=?
              AND fetched_at > datetime('now', ? || ' hours')
        """, (url_hash, f"-{ttl_hours}")).fetchone()
    return row["content"] if row else None


def set_scrape_cache(url_hash: str, url: str, content: str):
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO scrape_cache (url_hash, url, content, fetched_at)
            VALUES (?, ?, ?, datetime('now'))
        """, (url_hash, url, content))


# ── Event Helpers ──────────────────────────────────────────────────────────────

def log_email_event(email_id: int, event_type: str, event_data: Optional[str] = None):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO email_events (email_id, event_type, event_data)
            VALUES (?, ?, ?)
        """, (email_id, event_type, event_data))
    # Auto-suppress on unsubscribe or spam complaint
    if event_type in ("unsubscribe", "spam"):
        with get_db() as conn:
            row = conn.execute(
                "SELECT c.email FROM emails e JOIN contacts c ON e.contact_id=c.id WHERE e.id=?",
                (email_id,)
            ).fetchone()
        if row:
            add_to_suppression(row["email"], reason=event_type)


if __name__ == "__main__":
    init_db()
    print(f"Database ready at {DB_PATH}")
