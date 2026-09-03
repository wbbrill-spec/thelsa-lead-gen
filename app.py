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
POST /pipeline/run          → start a discovery run in the background
GET  /runs                  → run history / progress page
GET  /runs/<id>.json        → run status (polled by the runs page)
GET  /health                → health check (used by Render) incl. config warnings
"""

from __future__ import annotations

import os
from datetime import timedelta

from sqlalchemy.orm import joinedload
from flask import (
    Flask, flash, jsonify, redirect,
    render_template, request, session, url_for,
)

import config
from models import create_all_tables, User, Lead, Company, Contact, EmailDraft, DiscoveryRun
from db import get_db
from web_auth import WebAuthFlow, WebAuthError
import pipeline

# ── App setup ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
create_all_tables()  # runs on gunicorn import — creates tables / adds new columns
app.secret_key = config.FLASK_SECRET_KEY
app.permanent_session_lifetime = timedelta(days=7)

CONFIG_WARNINGS = config.log_config_warnings("APP")
try:
    pipeline.reap_stale_runs()  # a deploy kills in-flight runs; mark them FAILED
except Exception as _exc:  # never block boot on this
    print(f"[APP] reap_stale_runs failed: {_exc}")

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

    # Auto-create user on first login — open to anyone with a Google account
    email = user_info.get("email", "").lower()
    name = user_info.get("name", email)
    with get_db() as db:
        user = db.query(User).filter(
            (User.email_gmail == email) | (User.email_outlook == email),
        ).first()

        if not user:
            user = User(
                full_name=name,
                email_gmail=email,
                oauth_provider="google",
                is_active=True,
            )
            db.add(user)
            db.flush()

        # Store token for scheduler access
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
            .options(joinedload(Lead.company), joinedload(Lead.contact), joinedload(Lead.assigned_to), joinedload(Lead.generated_by))
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
        last_run=pipeline.last_run_status(),
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

        q = q.options(joinedload(Lead.company), joinedload(Lead.contact), joinedload(Lead.assigned_to), joinedload(Lead.generated_by))
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
        lead = (
            db.query(Lead)
            .options(
                joinedload(Lead.company),
                joinedload(Lead.contact),
                joinedload(Lead.email_drafts),
                joinedload(Lead.status_history),
                joinedload(Lead.assigned_to),
                joinedload(Lead.generated_by),
            )
            .filter_by(id=lead_id)
            .first()
        )
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
    from modules.mod07_drafter import draft_on_assign
    draft_on_assign(lead.id, new_user.email_outlook, new_user.full_name, session["user_name"])
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
    """Start a discovery run in the background and return immediately.

    The run takes 5–20 minutes; progress is on the Runs page. A second click
    while a run is in progress is refused instead of starting a duplicate.
    """
    if redir := _require_auth():
        return redir

    try:
        run_id = pipeline.start_run(user_id=session["user_id"], triggered_by=f"dashboard:{session.get('user_name', '')}"[:40])
    except pipeline.RunAlreadyInProgress as exc:
        flash(f"{exc}. Wait for it to finish — progress is shown below.", "warning")
        return redirect(url_for("runs"))
    except Exception as exc:
        flash(f"Could not start discovery run: {exc}", "error")
        return redirect(url_for("dashboard"))

    flash(f"Discovery run #{run_id} started. It usually takes 5–20 minutes; "
          f"this page refreshes automatically.", "success")
    return redirect(url_for("runs"))


# ── Routes: Runs ───────────────────────────────────────────────────────────────

@app.route("/runs")
def runs():
    if redir := _require_auth():
        return redir
    pipeline.reap_stale_runs()
    with get_db() as db:
        run_rows = (
            db.query(DiscoveryRun)
            .options(joinedload(DiscoveryRun.run_by_user))
            .order_by(DiscoveryRun.started_at.desc())
            .limit(50)
            .all()
        )
    any_running = any(r.status == DiscoveryRun.STATUS_RUNNING for r in run_rows)
    return render_template(
        "runs.html",
        runs=run_rows,
        any_running=any_running,
        config_warnings=CONFIG_WARNINGS,
        model=config.CLAUDE_MODEL,
        user_email=session["user_email"],
        user_name=session.get("user_name", ""),
    )


@app.route("/runs/<int:run_id>.json")
def run_json(run_id: int):
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    with get_db() as db:
        r = db.query(DiscoveryRun).filter_by(id=run_id).first()
        if not r:
            return jsonify({"error": "not found"}), 404
        return jsonify({
            "id": r.id, "status": r.status, "stage": r.stage,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            "heartbeat_at": r.heartbeat_at.isoformat() if r.heartbeat_at else None,
            "companies_discovered": r.companies_discovered,
            "companies_skipped_dupe": r.companies_skipped_dupe,
            "leads_qualified": r.leads_qualified,
            "leads_disqualified": r.leads_disqualified,
            "leads_created": r.leads_created,
            "contacts_found": r.contacts_found,
            "contacts_zoominfo": r.contacts_zoominfo,
            "error_message": r.error_message,
            "summary": r.summary,
        })


# ── Routes: Utility ────────────────────────────────────────────────────────────

@app.route("/trigger")
def trigger():
    """Entry point from Thelsa Library — redirect to dashboard."""
    return redirect(url_for("dashboard"))

@app.route("/cron/run", methods=["GET", "POST"])
def cron_run():
    """Token-guarded trigger for the daily scheduler cycle (called by a Render Cron Job)."""
    import os
    import threading
    token = os.environ.get("CRON_TOKEN", "")
    if not token or request.args.get("token") != token:
        return ("forbidden", 403)
    import scheduler
    if not scheduler.CYCLE_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "started": False, "reason": "a cycle is already running"}), 409

    def _run():
        try:
            scheduler.run_cycle(triggered_by="cron")
        finally:
            scheduler.CYCLE_LOCK.release()

    threading.Thread(target=_run, name="cron-cycle", daemon=False).start()
    return jsonify({"ok": True, "started": True})


@app.route("/admin/zoominfo-test", methods=["GET"])
def zoominfo_test():
    """Token-guarded one-shot ZoomInfo enrich test — returns the raw API response
    so we can confirm the new GTM enrich call works (or see the exact error)."""
    import os
    import requests as _rq
    import config
    from modules.mod05_enricher import _get_zoominfo_token, _ZI_OUTPUT_FIELDS
    token = os.environ.get("CRON_TOKEN", "")
    if not token or request.args.get("token") != token:
        return ("forbidden", 403)
    out = {
        "base_url": config.ZOOMINFO_BASE_URL,
        "token_url": config.ZOOMINFO_TOKEN_URL,
        "has_client_id": bool(config.ZOOMINFO_CLIENT_ID),
        "has_client_secret": bool(config.ZOOMINFO_CLIENT_SECRET),
    }
    tok = _get_zoominfo_token()
    out["token_ok"] = bool(tok)
    if not tok:
        return jsonify(out)
    company = request.args.get("company", "Bimbo Bakeries USA")
    title = request.args.get("title", "Logistics Manager")
    url = f"{config.ZOOMINFO_BASE_URL}/data/v1/contacts/enrich"
    payload = {"data": {"matchPersonInput": [{"companyName": company, "jobTitle": title}],
                        "outputFields": _ZI_OUTPUT_FIELDS}}
    try:
        r = _rq.post(url, headers={"Authorization": f"Bearer {tok}",
                                   "Content-Type": "application/json",
                                   "Accept": "application/json",
                                   "User-Agent": "TMS-LeadGen/1.0"},
                     json=payload, timeout=15)
        out["enrich_url"] = url
        out["status_code"] = r.status_code
        out["response"] = r.text[:3000]
    except Exception as exc:
        out["error"] = str(exc)
    return jsonify(out)


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "tms-leadgen",
        "model": config.CLAUDE_MODEL,
        "config_warnings": CONFIG_WARNINGS,
    })


# ── Startup ────────────────────────────────────────────────────────────────────

def create_app():
    create_all_tables()
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = config.FLASK_ENV == "development"
    create_app().run(host="0.0.0.0", port=port, debug=debug)
