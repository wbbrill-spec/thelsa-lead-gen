"""SQLAlchemy models for the TMS Lead Gen Engine.

Seven tables:
  users               — authorized Thelsa team members
  companies           — deduplication anchor; one record per unique company
  contacts            — enriched contact per company (ZoomInfo or web fallback)
  leads               — central operational record; one per company per cycle
  email_drafts        — one per draft per language (EN + ES × 3 types = up to 6 per lead)
  lead_status_history — append-only audit log of every status change
  discovery_runs      — one record per pipeline execution
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Integer, String, Text, ForeignKey,
    create_engine, event
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import JSON

import config


# ── Base ───────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Use JSONB on Postgres, JSON on SQLite
def _json_column():
    if "sqlite" in config.DATABASE_URL:
        return JSON
    return JSONB


# ── Table 1: users ─────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email_gmail: Mapped[Optional[str]] = mapped_column(String(150), unique=True, nullable=True)
    email_outlook: Mapped[Optional[str]] = mapped_column(String(150), unique=True, nullable=True)
    specialty: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    oauth_provider: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # 'google' or 'microsoft'
    oauth_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)           # encrypted
    oauth_refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # encrypted
    oauth_token_expiry: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    # Relationships
    generated_leads: Mapped[list["Lead"]] = relationship(
        "Lead", foreign_keys="Lead.generated_by_user_id", back_populates="generated_by"
    )
    assigned_leads: Mapped[list["Lead"]] = relationship(
        "Lead", foreign_keys="Lead.assigned_to_user_id", back_populates="assigned_to"
    )
    discovery_runs: Mapped[list["DiscoveryRun"]] = relationship("DiscoveryRun", back_populates="run_by_user")

    def __repr__(self) -> str:
        return f"<User {self.full_name} ({self.email_gmail or self.email_outlook})>"

    @property
    def active_email(self) -> str:
        """Return the current active email based on oauth_provider."""
        if self.oauth_provider == "microsoft":
            return self.email_outlook or ""
        return self.email_gmail or ""


# ── Table 2: companies ─────────────────────────────────────────────────────────

class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    domain: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)  # dedup key
    industry: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    country_of_origin: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)   # 'MX' or 'US'
    expansion_direction: Mapped[Optional[str]] = mapped_column(String(20), nullable=True) # 'MX_to_US' or 'US_to_MX'
    estimated_revenue: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    size_tier: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)            # 'SMB' or 'LARGE_CORP'
    rmc_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    rmc_detected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_snippet: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    first_discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    # Relationships
    contacts: Mapped[list["Contact"]] = relationship("Contact", back_populates="company")
    leads: Mapped[list["Lead"]] = relationship("Lead", back_populates="company")

    def __repr__(self) -> str:
        return f"<Company {self.name} ({self.domain})>"


# ── Table 3: contacts ──────────────────────────────────────────────────────────

class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(Integer, ForeignKey("companies.id"), nullable=False)
    contact_type: Mapped[str] = mapped_column(String(20), nullable=False)  # 'DIRECT' or 'RMC'
    full_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    enrichment_source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # 'zoominfo', 'web_search', 'manual'
    enrichment_raw: Mapped[Optional[dict]] = mapped_column(_json_column()(), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    # Relationships
    company: Mapped["Company"] = relationship("Company", back_populates="contacts")
    leads: Mapped[list["Lead"]] = relationship("Lead", back_populates="contact")

    def __repr__(self) -> str:
        return f"<Contact {self.full_name} ({self.contact_type}) @ {self.company_id}>"


# ── Table 4: leads ─────────────────────────────────────────────────────────────

class Lead(Base):
    __tablename__ = "leads"

    # Status values
    STATUS_NEW = "NEW"
    STATUS_APPROVED = "APPROVED"
    STATUS_SKIPPED = "SKIPPED"
    STATUS_DRAFTED = "DRAFTED"
    STATUS_FOLLOWED_UP_D2 = "FOLLOWED_UP_D2"
    STATUS_FOLLOWED_UP_D5 = "FOLLOWED_UP_D5"
    STATUS_RESPONDED = "RESPONDED"
    STATUS_CALL_REQUIRED = "CALL_REQUIRED"
    STATUS_CLOSED = "CLOSED"

    ALL_STATUSES = [
        STATUS_NEW, STATUS_APPROVED, STATUS_SKIPPED, STATUS_DRAFTED,
        STATUS_FOLLOWED_UP_D2, STATUS_FOLLOWED_UP_D5, STATUS_RESPONDED,
        STATUS_CALL_REQUIRED, STATUS_CLOSED,
    ]

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(Integer, ForeignKey("companies.id"), nullable=False)
    contact_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("contacts.id"), nullable=True)
    generated_by_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    assigned_to_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)

    qualification_score: Mapped[int] = mapped_column(Integer, nullable=False)
    qualification_reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(30), nullable=False, default=STATUS_NEW)

    reply_detected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reply_detected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    initial_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    followup_d2_scheduled: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    followup_d5_scheduled: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    followup_d2_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    followup_d5_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    call_required_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_to_email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    sent_to_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    sent_conversation_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    # Relationships
    company: Mapped["Company"] = relationship("Company", back_populates="leads")
    contact: Mapped[Optional["Contact"]] = relationship("Contact", back_populates="leads")
    generated_by: Mapped["User"] = relationship("User", foreign_keys=[generated_by_user_id], back_populates="generated_leads")
    assigned_to: Mapped["User"] = relationship("User", foreign_keys=[assigned_to_user_id], back_populates="assigned_leads")
    email_drafts: Mapped[list["EmailDraft"]] = relationship("EmailDraft", back_populates="lead")
    status_history: Mapped[list["LeadStatusHistory"]] = relationship("LeadStatusHistory", back_populates="lead", order_by="LeadStatusHistory.changed_at")

    def __repr__(self) -> str:
        return f"<Lead {self.id} {self.company.name if self.company else self.company_id} [{self.status}]>"


# ── Table 5: email_drafts ──────────────────────────────────────────────────────

class EmailDraft(Base):
    __tablename__ = "email_drafts"

    # Draft type values
    TYPE_INITIAL = "INITIAL"
    TYPE_FOLLOWUP_D2 = "FOLLOWUP_D2"
    TYPE_FOLLOWUP_D5 = "FOLLOWUP_D5"

    # Language values
    LANG_EN = "EN"
    LANG_ES = "ES"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(Integer, ForeignKey("leads.id"), nullable=False)
    draft_type: Mapped[str] = mapped_column(String(20), nullable=False)
    language: Mapped[str] = mapped_column(String(5), nullable=False)
    subject_line: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    body_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    provider_draft_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # 'gmail' or 'outlook'
    created_in_drafts_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    # Relationships
    lead: Mapped["Lead"] = relationship("Lead", back_populates="email_drafts")

    def __repr__(self) -> str:
        return f"<EmailDraft {self.draft_type}/{self.language} lead={self.lead_id}>"


# ── Table 6: lead_status_history ───────────────────────────────────────────────

class LeadStatusHistory(Base):
    __tablename__ = "lead_status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(Integer, ForeignKey("leads.id"), nullable=False)
    changed_by: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # 'system' or user full_name
    from_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # Relationships
    lead: Mapped["Lead"] = relationship("Lead", back_populates="status_history")

    def __repr__(self) -> str:
        return f"<StatusHistory lead={self.lead_id} {self.from_status}→{self.to_status}>"


# ── Table 7: discovery_runs ────────────────────────────────────────────────────

class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_by_user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    search_queries_used: Mapped[Optional[list]] = mapped_column(_json_column()(), nullable=True)
    companies_discovered: Mapped[int] = mapped_column(Integer, default=0)
    companies_skipped_dupe: Mapped[int] = mapped_column(Integer, default=0)
    leads_qualified: Mapped[int] = mapped_column(Integer, default=0)
    leads_disqualified: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # 'RUNNING', 'COMPLETED', 'FAILED'
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    run_by_user: Mapped[Optional["User"]] = relationship("User", back_populates="discovery_runs")

    def __repr__(self) -> str:
        return f"<DiscoveryRun {self.id} [{self.status}] {self.started_at}>"


# ── Database helpers ───────────────────────────────────────────────────────────

def get_engine():
    return create_engine(
              config.DATABASE_URL,
              echo=False,
              pool_pre_ping=True,
              pool_recycle=280,
    )

def create_all_tables():
    """Create all tables. Safe to call multiple times (no-op if tables exist)."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    return engine


def transition_status(session, lead: Lead, new_status: str, changed_by: str, reason: str = None):
    """Update lead status and write audit log entry atomically."""
    old_status = lead.status
    lead.status = new_status
    lead.updated_at = _now()

    history = LeadStatusHistory(
        lead_id=lead.id,
        changed_by=changed_by,
        from_status=old_status,
        to_status=new_status,
        reason=reason,
    )
    session.add(history)
    return history
