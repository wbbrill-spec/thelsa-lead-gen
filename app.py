"""TMS Corp Lead Gen Engine — Web Application.

Multi-user Flask app. Each team member signs in with their own Google account;
Gmail drafts are created in their mailbox. Auth pattern matches the Thelsa
Library exactly.

Routes
------
GET  /                      → dashboard (redirects to /login if not signed in)
GET  /login                 → sign-in page
GET  /auth/google           → begin Google OAuth flow
GET  /auth/callback         → OAuth callback; stores credentials; redirects to /
GET  /logout                → clears session; redirects to /login
GET  /leads                 → all leads view
GET  /leads/<id>            → lead detail view
POST /leads/<id>/approve    → approve lead, trigger email drafting
POST /leads/<id>/skip       → skip lead
POST /leads/<id>/assign     → reassign lead to another user
POST /leads/<id>/mark-sent  → mark initial email as sent, schedule follow-ups
GET  /pipeline/run          → trigger a discovery run (GET for simplicity in dashboard)
GET  /health                → health check (used by Render)
"""

from __future__ import annotations

import os
from datetime import timedelta

from flask import (
    Flask, flash, jsonify, redirect,
    render_template, request, session, url_for,
)

import config
from models import create_all_tables, User, Lead, Company, Contact, EmailDraft
from db import get_db
from web_auth import WebAuthFlow, WebAuthError

# ── App setup ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
app.permanent_session_lifetime = timedelta(days=7)

# ── Auth helpers (matching Library pattern exactly) ────────────────────────────

def _require_auth():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return None


def _get_credentials():
    token_json = session.get("token_json")
    if not token_json:
        return None
    try:
        creds = WebAuthFlow.credentials_from_token(token_json)
        session["token_json"] = creds.to_json()
        return creds
    except WebAuthError:
        return None


def _current_user_db(db) -> User | None:
    """Return the User record for the currently logged-in session user."""
    return db.query(User).filter_by(id=session.get("user_id"), is_active=True).first()


# ── Routes: Auth ───────────────────────────────────────────────────────────────

@app.route("/login")
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/auth/google")
def auth_google():
    flow = WebAuthFlow(url_for("auth_callback", _external=True))
    auth_url, state, code_verifier = flow.authorization_url()
    session["oauth_state"] = state
    session["code_verifier"] = code_verifier
    return redirect(auth_url)


@app.route("/auth/callback")
def auth_callback():
    try:
        flow = WebAuthFlow(url_for("auth_callback", _external=True))
        creds = flow.exchange_code(
            authorization_response=request.url,
            expected_state=session.get("oauth_state"),
            code_verifier=session.get("code_verifier", ""),
        )
        user_info = flow.get_user_info(creds)
    except WebAuthError as exc:
        flash(f"Sign-in failed: {exc}", "error")
        return redirect(url_for("login"))

    # Auto-create user on first login - open to anyone with a Google account
    email = user_info.get("email", "").lower()
    name = user_info.get("name", email)
    with get_db() as db:
        user = db.query(User).filter(
            (User.email_gmail == email) | (User.email_outlook == email),
        ).first()
        if not user:
            user = User(full_name=name, email_gmail=email, oauth_provider="google", is_active=True)
            db.add(user)
            db.flush()
        user.oauth_token = creds.to_json()
        user.oauth_provider = "google"
        if not user.email_gmail:
            user.email_gmail = email

    session.clear()
    session.permanent = True
    session["user_id"] = user.id
    session["user_email"] = email
    session["user_name"] = user_info.get("name", email)
    session["token_json"] = creds.to_json()

    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Routes: Dashboard ──────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    if redir := _require_auth():
        return redir

    with get_db() as db:
        # Pending action leads (default view)
        pending_statuses = [Lead.STATUS_NEW, Lead.STATUS_APPROVED]
        pending_leads = (
            db.query(Lead)
            .join(Company)
            .filter(Lead.status.in_(pending_statuses))
            .order_by(Lead.created_at.desc())
            .all()
        )

        # Stats for header
        total_leads = db.query(Lead).count()
        responded = db.query(Lead).filter_by(status=Lead.STATUS_RESPONDED).count()
        call_required = db.query(Lead).filter_by(status=Lead.STATUS_CALL_REQUIRED).count()

    return render_template(
        "dashboard.html",
        leads=pending_leads,
        view="pending",
        total_leads=total_leads,
        responded=responded,
        call_required=call_required,
        user_email=session["user_email"],
        user_name=session.get("user_name", ""),
    )


@app.route("/leads/all")
def all_leads():
    if redir := _require_auth():
        return redir

    # Build filter query from URL params
    status_filter = request.args.get("status", "")
    tier_filter = request.args.get("tier", "")
    country_filter = request.args.get("country", "")
    responded_filter = request.args.get("responded", "")
    call_required_filter = request.args.get("call_required", "")
    assigned_filter = request.args.get("assigned_to", "")

    with get_db() as db:
        q = db.query(Lead).join(Company)

        if status_filter:
            q = q.filter(Lead.status == status_filter)
        if tier_filter:
            q = q.filter(Company.size_tier == tier_filter)
        if country_filter:
            q = q.filter(Company.country_of_origin == country_filter)
        if responded_filter == "yes":
            q = q.filter(Lead.reply_detected == True)
        if call_required_filter == "yes":
            q = q.filter(Lead.status == Lead.STATUS_CALL_REQUIRED)
        if assigned_filter:
            q = q.filter(Lead.assigned_to_user_id == int(assigned_filter))

        leads = q.order_by(Lead.created_at.desc()).all()
        users = db.query(User).filter_by(is_active=True).all()

    return render_template(
        "dashboard.html",
        leads=leads,
        view="all",
        users=users,
        filters={
            "status": status_filter,
            "tier": tier_filter,
            "country": country_filter,
            "responded": responded_filter,
            "call_required": call_required_filter,
            "assigned_to": assigned_filter,
        },
        all_statuses=Lead.ALL_STATUSES,
        user_email=session["user_email"],
        user_name=session.get("user_name", ""),
    )


@app.route("/leads/<int:lead_id>")
def lead_detail(lead_id: int):
    if redir := _require_auth():
        return redir

    with get_db() as db:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            flash("Lead not found.", "error")
            return redirect(url_for("dashboard"))

        users = db.query(User).filter_by(is_active=True).all()

    return render_template(
        "lead_detail.html",
        lead=lead,
        users=users,
        user_email=session["user_email"],
        user_name=session.get("user_name", ""),
    )


# ── Routes: Lead Actions ───────────────────────────────────────────────────────

@app.route("/leads/<int:lead_id>/approve", methods=["POST"])
def approve_lead(lead_id: int):
    if redir := _require_auth():
        return redir

    creds = _get_credentials()
    if not creds or not creds.valid:
        flash("Your Google session has expired. Please sign in again.", "error")
        return redirect(url_for("login"))

    with get_db() as db:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            flash("Lead not found.", "error")
            return redirect(url_for("dashboard"))

        if lead.status != Lead.STATUS_NEW:
            flash("Lead is not in NEW status.", "error")
            return redirect(url_for("lead_detail", lead_id=lead_id))

        # Import here to avoid circular imports
        from models import transition_status
        transition_status(db, lead, Lead.STATUS_APPROVED, changed_by=session["user_name"])

    # Trigger email drafting asynchronously (runs inline for now, async in Phase 6)
    try:
        from modules.mod07_drafter import create_initial_drafts
        create_initial_drafts(lead_id=lead_id, credentials=creds, user_email=session["user_email"])
        flash("Lead approved. Bilingual drafts created in your Gmail.", "success")
    except Exception as exc:
        flash(f"Lead approved but draft creation failed: {exc}", "warning")

    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.route("/leads/<int:lead_id>/skip", methods=["POST"])
def skip_lead(lead_id: int):
    if redir := _require_auth():
        return redir

    reason = request.form.get("reason", "Manually skipped")

    with get_db() as db:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            flash("Lead not found.", "error")
            return redirect(url_for("dashboard"))

        from models import transition_status
        transition_status(db, lead, Lead.STATUS_SKIPPED, changed_by=session["user_name"], reason=reason)

    flash("Lead skipped.", "info")
    return redirect(url_for("dashboard"))


@app.route("/leads/<int:lead_id>/assign", methods=["POST"])
def assign_lead(lead_id: int):
    if redir := _require_auth():
        return redir

    new_user_id = request.form.get("assigned_to_user_id")
    if not new_user_id:
        flash("No user selected.", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    with get_db() as db:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        new_user = db.query(User).filter_by(id=int(new_user_id), is_active=True).first()

        if not lead or not new_user:
            flash("Lead or user not found.", "error")
            return redirect(url_for("dashboard"))

        lead.assigned_to_user_id = int(new_user_id)

        from models import LeadStatusHistory, _now
        note = LeadStatusHistory(
            lead_id=lead.id,
            changed_by=session["user_name"],
            from_status=lead.status,
            to_status=lead.status,
            reason=f"Reassigned to {new_user.full_name}",
        )
        db.add(note)

    flash(f"Lead assigned to {new_user.full_name}.", "success")
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.route("/leads/<int:lead_id>/mark-sent", methods=["POST"])
def mark_sent(lead_id: int):
    """Rep marks the initial email as sent from Gmail. Schedules follow-ups."""
    if redir := _require_auth():
        return redir

    from datetime import datetime, timezone, timedelta

    with get_db() as db:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            flash("Lead not found.", "error")
            return redirect(url_for("dashboard"))

        now = datetime.now(timezone.utc)
        lead.initial_sent_at = now
        lead.followup_d2_scheduled = now + timedelta(days=2)
        lead.followup_d5_scheduled = now + timedelta(days=5)

        from models import transition_status
        transition_status(
            db, lead, Lead.STATUS_DRAFTED,
            changed_by=session["user_name"],
            reason="Initial email marked as sent by rep"
        )

    flash("Email marked as sent. Follow-ups scheduled for Day 2 and Day 5.", "success")
    return redirect(url_for("lead_detail", lead_id=lead_id))


# ── Routes: Pipeline ───────────────────────────────────────────────────────────

@app.route("/pipeline/run", methods=["POST"])
def run_pipeline():
    if redir := _require_auth():
        return redir

    with get_db() as db:
        from models import DiscoveryRun
        run = DiscoveryRun(
            run_by_user_id=session["user_id"],
            status="RUNNING",
        )
        db.add(run)
        db.flush()
        run_id = run.id

    try:
        from modules.mod01_discovery import run_discovery
        from modules.mod02_deduplication import deduplicate
        from modules.mod03_scorer import score_candidates
        from modules.mod04_segmentation import segment_and_detect_rmc
        from modules.mod05_enricher import enrich_contacts

        candidates = run_discovery(run_id=run_id)
        net_new = deduplicate(candidates)
        qualified = score_candidates(net_new)
        segmented = segment_and_detect_rmc(qualified)
        enrich_contacts(segmented, run_id=run_id, generated_by_user_id=session["user_id"])

        with get_db() as db:
            from models import DiscoveryRun
            run = db.query(DiscoveryRun).filter_by(id=run_id).first()
            if run:
                from datetime import datetime, timezone
                run.completed_at = datetime.now(timezone.utc)
                run.status = "COMPLETED"

        flash(f"Discovery run complete. Check the dashboard for new leads.", "success")
    except Exception as exc:
        with get_db() as db:
            from models import DiscoveryRun
            run = db.query(DiscoveryRun).filter_by(id=run_id).first()
            if run:
                run.status = "FAILED"
                run.error_message = str(exc)
        flash(f"Discovery run failed: {exc}", "error")

    return redirect(url_for("dashboard"))


# ── Routes: Utility ────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "tms-leadgen"})


# ── Startup ────────────────────────────────────────────────────────────────────

def create_app():
    create_all_tables()
    from seed import seed
    seed()
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = config.FLASK_ENV == "development"
    create_app().run(host="0.0.0.0", port=port, debug=debug)
