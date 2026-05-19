"""
dashboard.py — Agent approval web interface (Flask).
The agent opens this in a browser to review, edit, approve, or reject
AI-drafted emails before anything is sent.

Run with: python run.py dashboard
Opens at: http://localhost:5050
"""

import json
import logging
import os

from flask import Flask, request, redirect, url_for, session

from engine.config import AGENT_NAME, COMPANY_NAME, CLIENT_NAME
from engine.database import (
    get_pending_approvals,
    approve_email,
    reject_email,
    get_db,
    add_to_suppression,
)
from engine.gmail_drafts import create_gmail_draft

logger = logging.getLogger(__name__)
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "tms-lead-engine-secret-change-in-prod")

# ── Google OAuth config ───────────────────────────────────────────────────────
_GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
_GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
_OAUTH_REDIRECT_URI   = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:5050/oauth2callback")
_OAUTH_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.compose",
]


# ── HTML Templates ────────────────────────────────────────────────────────────

BASE_STYLE = """
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         margin: 0; background: #f4f6f9; color: #1a1a2e; }
  .nav { background: #0d3b6e; color: white; padding: 14px 24px;
         display: flex; align-items: center; justify-content: space-between; }
  .nav h1 { margin: 0; font-size: 16px; font-weight: 700; }
  .nav span { font-size: 12px; opacity: 0.7; }
  .nav a { color: #8ec5f7 !important; }
  .container { max-width: 920px; margin: 24px auto; padding: 0 16px; }
  .card { background: white; border-radius: 10px; padding: 20px;
          margin-bottom: 20px; border: 1px solid #e8e8e8; }
  .badge { display: inline-block; padding: 3px 10px; border-radius: 20px;
           font-size: 11px; font-weight: 700; text-transform: uppercase; }
  .badge-hot     { background: #fee2e2; color: #991b1b; }
  .badge-good    { background: #d1fae5; color: #047857; }
  .badge-med     { background: #fef3c7; color: #92400e; }
  .badge-mexico  { background: #fde68a; color: #78350f; }
  .badge-usca    { background: #dbeafe; color: #1e40af; }
  .badge-func    { background: #ede9fe; color: #5b21b6; }
  .label { font-size: 11px; font-weight: 700; color: #888;
           text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
  .value { font-size: 14px; color: #1a1a2e; margin-bottom: 12px; }
  textarea { width: 100%; border: 1px solid #ddd; border-radius: 6px;
             padding: 10px; font-size: 13px; line-height: 1.6;
             font-family: inherit; resize: vertical; }
  .btn { display: inline-block; padding: 9px 20px; border-radius: 6px;
         font-size: 13px; font-weight: 600; cursor: pointer; border: none;
         text-decoration: none; margin-right: 8px; }
  .btn-approve { background: #059669; color: white; }
  .btn-reject  { background: #dc2626; color: white; }
  .btn-skip    { background: #f3f4f6; color: #555; border: 1px solid #ddd; }
  .btn:hover   { opacity: 0.85; }
  .flash-ok  { background: #d1fae5; color: #065f46; padding: 12px 16px;
               border-radius: 8px; margin-bottom: 16px; font-size: 13px; }
  .flash-err { background: #fee2e2; color: #991b1b; padding: 12px 16px;
               border-radius: 8px; margin-bottom: 16px; font-size: 13px; }
  .empty     { text-align: center; padding: 60px 0; color: #aaa; font-size: 15px; }
  .meta-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 24px; }
  .tabs      { display: flex; gap: 4px; margin-bottom: 10px; }
  .tab-btn   { padding: 6px 14px; border-radius: 6px; font-size: 12px;
               font-weight: 600; cursor: pointer; border: 1px solid #ddd;
               background: #f3f4f6; color: #555; }
  .tab-btn.active { background: #0d3b6e; color: white; border-color: #0d3b6e; }
  .tab-pane  { display: none; }
  .tab-pane.active { display: block; }
  .stat-row  { display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
  .stat-box  { flex: 1; min-width: 100px; background: white; border-radius: 10px;
               padding: 16px; border: 1px solid #e8e8e8; text-align: center; }
  .stat-num  { font-size: 28px; font-weight: 800; color: #0d3b6e; }
  .stat-lbl  { font-size: 11px; color: #888; margin-top: 2px; }
  .warning   { background: #fffbf0; border: 1px solid #fde68a; border-radius: 8px;
               padding: 10px 14px; font-size: 12px; color: #92400e; margin-bottom: 12px; }
  .dir-badge-mexico { display:inline-block; padding:2px 8px; border-radius:12px;
                      background:#fef3c7; color:#92400e; font-size:11px; font-weight:700; }
  .dir-badge-usca   { display:inline-block; padding:2px 8px; border-radius:12px;
                      background:#dbeafe; color:#1e40af; font-size:11px; font-weight:700; }
  .dir-badge-unk    { display:inline-block; padding:2px 8px; border-radius:12px;
                      background:#f3f4f6; color:#666; font-size:11px; font-weight:700; }
</style>
<script>
function switchTab(emailId, lang) {
  document.querySelectorAll('#email-' + emailId + ' .tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('#email-' + emailId + ' .tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('pane-' + emailId + '-' + lang).classList.add('active');
  document.getElementById('btn-' + emailId + '-' + lang).classList.add('active');
}
</script>
"""


def _score_badge(score):
    if score >= 8:
        return '<span class="badge badge-hot">Hot Lead</span>'
    elif score >= 6:
        return '<span class="badge badge-good">Qualified</span>'
    return '<span class="badge badge-med">Moderate</span>'


def _direction_badge(direction):
    if direction == "to_mexico":
        return '<span class="dir-badge-mexico">to Mexico</span>'
    elif direction == "to_us_canada":
        return '<span class="dir-badge-usca">to US/Canada</span>'
    return '<span class="dir-badge-unk">direction unknown</span>'


def _function_badge(func):
    labels = {
        "hr":             "HR",
        "global_mobility": "Global Mobility",
        "comp_benefits":  "C&B",
        "procurement":    "Procurement",
    }
    label = labels.get(func or "hr", func or "HR")
    return f'<span class="badge badge-func">{label}</span>'


# ── Google OAuth helpers ──────────────────────────────────────────────────────

def _build_oauth_flow():
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_config(
        {
            "web": {
                "client_id":     _GOOGLE_CLIENT_ID,
                "client_secret": _GOOGLE_CLIENT_SECRET,
                "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                "token_uri":     "https://oauth2.googleapis.com/token",
                "redirect_uris": [_OAUTH_REDIRECT_URI],
            }
        },
        scopes=_OAUTH_SCOPES,
        redirect_uri=_OAUTH_REDIRECT_URI,
    )


@app.route("/login")
def login():
    """Redirect to Google OAuth consent screen."""
    if not _GOOGLE_CLIENT_ID or not _GOOGLE_CLIENT_SECRET:
        return (
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>OAuth not configured</h2>"
            "<p>GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set as environment variables.</p>"
            "</body></html>"
        ), 500
    flow = _build_oauth_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/oauth2callback")
def oauth2callback():
    """Handle Google OAuth callback, store token in session."""
    try:
        flow = _build_oauth_flow()
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        session["google_token"] = json.loads(creds.to_json())
        session["google_email"] = ""
        # Try to get the user's email from the token
        try:
            import google.oauth2.id_token
            import google.auth.transport.requests
            req = google.auth.transport.requests.Request()
            id_info = google.oauth2.id_token.verify_oauth2_token(
                creds.id_token, req, _GOOGLE_CLIENT_ID
            )
            session["google_email"] = id_info.get("email", "")
        except Exception:
            pass
    except Exception as exc:
        logger.warning("OAuth callback failed: %s", exc)
        flash_custom("Google login failed. Please try again.", "err")
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.pop("google_token", None)
    session.pop("google_email", None)
    return redirect(url_for("index"))


@app.route("/get_token")
def get_token():
    """
    One-time helper: shows the current session token as JSON so you can
    paste it into Render as GMAIL_REFRESH_TOKEN.  After that, no one on
    the team ever needs to sign in again — the env var is used automatically.
    """
    token = session.get("google_token")
    email = session.get("google_email", "unknown")
    if not token:
        return redirect(url_for("login"))
    token_json = json.dumps(token, indent=2)
    return f"""<!DOCTYPE html><html><head><title>Save Gmail Token</title>{BASE_STYLE}</head><body>
    <div class="nav"><h1>Save Gmail Token to Render</h1><span><a href="/">← Back to Dashboard</a></span></div>
    <div class="container"><div class="card">
      <p style="font-size:14px">You are signed in as <strong>{email}</strong>.</p>
      <p style="font-size:14px">
        Copy the entire JSON block below and paste it into your Render Lead Gen app as the
        environment variable <code style="background:#f3f4f6;padding:2px 6px;border-radius:4px">GMAIL_REFRESH_TOKEN</code>.
        After that, click <em>Save &amp; Deploy</em> on Render.  The "Sign in with Google" prompt
        will disappear for everyone on your team — Gmail drafts will just work.
      </p>
      <div class="label">GMAIL_REFRESH_TOKEN value (copy everything below)</div>
      <textarea rows="18" onclick="this.select()" style="font-family:monospace;font-size:12px">{token_json}</textarea>
      <p style="font-size:12px;color:#888;margin-top:10px">
        ℹ️  This token has a refresh token embedded, so it never expires as long as it is used
        at least once every 6 months.  You only need to do this once.
      </p>
    </div></div></body></html>"""


@app.route("/trigger")
def trigger():
    """Run one full pipeline cycle in a background thread, then show the dashboard."""
    import threading
    from engine.main import setup_logging, run_pipeline
    from engine.database import init_db
    def _run():
        try:
            setup_logging()
            init_db()
            run_pipeline()
        except Exception as exc:
            logger.error("Pipeline run failed: %s", exc)
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    flash_custom("Pipeline started — leads and drafts will appear here shortly. Refresh in a minute.", "ok")
    return redirect(url_for("index"))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Dashboard home — shows pending approvals and key stats."""
    pending = get_pending_approvals()

    # Quick stats
    with get_db() as conn:
        total_leads    = conn.execute("SELECT COUNT(*) FROM companies WHERE status != 'disqualified'").fetchone()[0]
        total_sent     = conn.execute("SELECT COUNT(*) FROM emails WHERE status='sent'").fetchone()[0]
        total_opens    = conn.execute("SELECT COUNT(*) FROM email_events WHERE event_type='open'").fetchone()[0]
        total_replies  = conn.execute("SELECT COUNT(*) FROM email_events WHERE event_type='reply'").fetchone()[0]
        to_mexico_ct   = conn.execute("SELECT COUNT(*) FROM companies WHERE expansion_direction='to_mexico' AND status != 'disqualified'").fetchone()[0]
        to_usca_ct     = conn.execute("SELECT COUNT(*) FROM companies WHERE expansion_direction='to_us_canada' AND status != 'disqualified'").fetchone()[0]

    flash_msgs = []
    for msg in get_flashed_messages_with_category():
        flash_msgs.append(msg)

    cards_html = ""
    for row in pending:
        eid       = row["id"]
        score     = row["qualification_score"] if "qualification_score" in row.keys() else 0
        direction = row["expansion_direction"] if "expansion_direction" in row.keys() else "unknown"
        func      = row["target_function"] if "target_function" in row.keys() else "hr"

        cards_html += f"""
        <div class="card" id="email-{eid}">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
            <div>
              <strong style="font-size:15px">{row['company_name']}</strong>
              &nbsp; {_score_badge(score)}
              &nbsp; {_direction_badge(direction)}
              &nbsp; {_function_badge(func)}
            </div>
            <span style="font-size:11px;color:#aaa">Draft #{row['sequence_num']} &nbsp;|&nbsp; {row['created_at'][:10]}</span>
          </div>

          <div class="meta-grid">
            <div>
              <div class="label">Contact</div>
              <div class="value">{row['first_name'] or ''} {row['last_name'] or ''} — {row['title'] or 'Unknown title'}</div>
            </div>
            <div>
              <div class="label">Email</div>
              <div class="value">{row['contact_email']}</div>
            </div>
            <div>
              <div class="label">Industry</div>
              <div class="value">{row['industry'] or 'Unknown'}</div>
            </div>
            <div>
              <div class="label">Expansion Stage</div>
              <div class="value">{row['description'] or 'Unknown'}</div>
            </div>
          </div>

          <form method="POST" action="/review/{eid}">
            <div class="label">Subject Line</div>
            <input name="subject" value="{row['subject'] or ''}"
                   style="width:100%;border:1px solid #ddd;border-radius:6px;padding:8px 10px;
                          font-size:13px;margin-bottom:12px;font-family:inherit">

            <div class="tabs">
              <button type="button" class="tab-btn active" id="btn-{eid}-en"
                      onclick="switchTab({eid}, 'en')">English</button>
              <button type="button" class="tab-btn" id="btn-{eid}-es"
                      onclick="switchTab({eid}, 'es')">Spanish</button>
            </div>

            <div class="tab-pane active" id="pane-{eid}-en">
              <div class="warning">⚠️ The sender footer is included in every email. You may edit the body above but please keep the footer intact.</div>
              <textarea name="body_english" rows="14">{row['body_english'] or ''}</textarea>
            </div>
            <div class="tab-pane" id="pane-{eid}-es">
              <div class="warning">⚠️ El pie de página del remitente se incluye en cada correo. Puede editar el cuerpo del mensaje, pero por favor conserve el pie de página.</div>
              <textarea name="body_spanish" rows="14">{row['body_spanish'] or ''}</textarea>
            </div>

            <div class="label" style="margin-top:14px">Send language</div>
            <select name="language" style="border:1px solid #ddd;border-radius:6px;
                    padding:7px 10px;font-size:13px;margin-bottom:16px;font-family:inherit">
              <option value="english">English only</option>
              <option value="spanish">Spanish only</option>
              <option value="both">Both (English + Spanish)</option>
            </select>

            <div>
              <button type="submit" name="action" value="approve" class="btn btn-approve">Save to Gmail Drafts</button>
              <button type="submit" name="action" value="reject"  class="btn btn-reject">Reject</button>
              <a href="/" class="btn btn-skip">Skip for now</a>
            </div>
          </form>
        </div>
        """

    if not pending:
        cards_html = (
            '<div class="empty">No emails pending approval right now.<br>'
            '<small>Run the engine to generate new leads and drafts.</small></div>'
        )

    # Nav auth indicator: env var token (production) > session token > sign-in prompt
    _env_token = os.environ.get("GMAIL_REFRESH_TOKEN", "").strip()
    google_email = session.get("google_email", "")
    google_token = session.get("google_token")
    if _env_token:
        auth_link = 'Gmail Drafts: <span style="color:#6ee7b7;font-weight:700">✓ Configured</span>'
    elif google_token and google_email:
        auth_link = (f'Signed in as {google_email} &nbsp;|&nbsp; '
                     f'<a href="/get_token">Save token to Render</a> &nbsp;|&nbsp; '
                     f'<a href="/logout">Sign out</a>')
    else:
        auth_link = '<a href="/login">Sign in with Google</a> to enable Gmail Drafts'

    html = f"""<!DOCTYPE html><html><head>
    <title>{COMPANY_NAME} — Lead Gen Engine</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    {BASE_STYLE}</head><body>
    <div class="nav">
      <h1>{COMPANY_NAME} — Lead Gen Engine &nbsp;|&nbsp; {CLIENT_NAME} Outreach</h1>
      <span>{auth_link} &nbsp;|&nbsp;
        <a href="/leads">All Leads</a> &nbsp;|&nbsp;
        <a href="/stats">Stats</a>
      </span>
    </div>
    <div class="container">
      {''.join(f'<div class="flash-ok">{m}</div>' if c == "ok" else f'<div class="flash-err">{m}</div>' for c, m in flash_msgs)}
      <div class="stat-row">
        <div class="stat-box"><div class="stat-num">{len(pending)}</div><div class="stat-lbl">Pending Approval</div></div>
        <div class="stat-box"><div class="stat-num">{total_leads}</div><div class="stat-lbl">Total Leads</div></div>
        <div class="stat-box"><div class="stat-num">{to_mexico_ct}</div><div class="stat-lbl">to Mexico</div></div>
        <div class="stat-box"><div class="stat-num">{to_usca_ct}</div><div class="stat-lbl">to US/Canada</div></div>
        <div class="stat-box"><div class="stat-num">{total_sent}</div><div class="stat-lbl">Sent</div></div>
        <div class="stat-box"><div class="stat-num">{total_opens}</div><div class="stat-lbl">Opens</div></div>
        <div class="stat-box"><div class="stat-num">{total_replies}</div><div class="stat-lbl">Replies</div></div>
      </div>
      {cards_html}
    </div></body></html>"""
    return html


def get_flashed_messages_with_category():
    from flask import session
    msgs = session.pop("_flash_messages", [])
    return msgs


def flash_custom(msg, category="ok"):
    from flask import session
    if "_flash_messages" not in session:
        session["_flash_messages"] = []
    session["_flash_messages"].append((category, msg))


@app.route("/review/<int:email_id>", methods=["POST"])
def review(email_id):
    action       = request.form.get("action")
    body_english = request.form.get("body_english", "")
    body_spanish = request.form.get("body_spanish", "")
    subject      = request.form.get("subject", "")

    if action == "approve":
        # Save any agent edits back to the DB
        with get_db() as conn:
            conn.execute("""
                UPDATE emails SET body_english=?, body_spanish=?, subject=?
                WHERE id=?
            """, (body_english, body_spanish, subject, email_id))

        approve_email(email_id, AGENT_NAME)

        # Look up the contact email for the draft
        with get_db() as conn:
            row = conn.execute("""
                SELECT c.email as contact_email
                FROM emails e
                JOIN contacts c ON e.contact_id = c.id
                WHERE e.id = ?
            """, (email_id,)).fetchone()
        to_email = row["contact_email"] if row else ""

        # Resolve token: env var (pre-stored service token) > session (one-time sign-in)
        token_dict = None
        env_token = os.environ.get("GMAIL_REFRESH_TOKEN", "").strip()
        if env_token:
            try:
                token_dict = json.loads(env_token)
            except Exception:
                pass
        if not token_dict:
            token_dict = session.get("google_token")

        draft_id = create_gmail_draft(
            to_email=to_email,
            subject=subject,
            body_english=body_english,
            body_spanish=body_spanish or None,
            token_dict=token_dict,
        )

        if draft_id:
            flash_custom(
                f"Draft saved to Gmail Drafts folder (email ID {email_id}). "
                f"Open Gmail to review and send.", "ok"
            )
        elif not token_dict:
            flash_custom(
                f"Approval saved but no Gmail draft created — "
                f"please <a href='/login'>sign in with Google</a> to enable Gmail drafts.", "err"
            )
        else:
            flash_custom(
                f"Approval saved but Gmail draft creation failed. Check logs.", "err"
            )

    elif action == "reject":
        reject_email(email_id)
        flash_custom(f"Email ID {email_id} rejected and removed from queue.", "ok")

    return redirect(url_for("index"))


@app.route("/leads")
def leads():
    """View all leads and their pipeline status."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT c.*,
                   COUNT(DISTINCT ct.id) as contact_count,
                   COUNT(DISTINCT e.id) as email_count,
                   MAX(e.sent_at) as last_sent
            FROM companies c
            LEFT JOIN contacts ct ON ct.company_id = c.id
            LEFT JOIN emails e ON e.company_id = c.id AND e.status = 'sent'
            WHERE c.status != 'disqualified'
            GROUP BY c.id
            ORDER BY c.qualification_score DESC, c.created_at DESC
            LIMIT 200
        """).fetchall()

    status_colors = {
        "new":       "#dce4fd",
        "qualified": "#d1fae5",
        "contacted": "#fef3c7",
        "closed":    "#e8e8e8",
    }

    rows_html = ""
    for r in rows:
        color     = status_colors.get(r["status"], "#f3f4f6")
        direction = r["expansion_direction"] if "expansion_direction" in r.keys() else "unknown"
        dir_label = "to Mexico" if direction == "to_mexico" else ("to US/CA" if direction == "to_us_canada" else "unknown")
        rows_html += f"""
        <tr>
          <td style="font-weight:600">{r['name']}</td>
          <td>{r['industry'] or '—'}</td>
          <td><span style="font-size:11px;font-weight:700">{dir_label}</span></td>
          <td><span class="badge" style="background:{color};color:#333">{r['status']}</span></td>
          <td style="text-align:center">{r['qualification_score']}/10</td>
          <td style="text-align:center">{r['contact_count']}</td>
          <td style="text-align:center">{r['email_count']}</td>
          <td>{r['last_sent'][:10] if r['last_sent'] else '—'}</td>
          <td><a href="{r['source_url'] or '#'}" target="_blank" style="color:#4f6ef7;font-size:12px">Source</a></td>
        </tr>"""

    html = f"""<!DOCTYPE html><html><head><title>All Leads — {COMPANY_NAME}</title>{BASE_STYLE}
    <style>table{{width:100%;border-collapse:collapse}}th,td{{padding:10px 12px;border-bottom:1px solid #f0f0f0;font-size:13px;text-align:left}}th{{font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:0.5px}}</style>
    </head><body>
    <div class="nav"><h1>All Leads</h1><span><a href="/">Back to Dashboard</a></span></div>
    <div class="container">
      <div class="card">
        <table>
          <thead><tr><th>Company</th><th>Industry</th><th>Direction</th><th>Status</th><th>Score</th><th>Contacts</th><th>Emails Sent</th><th>Last Sent</th><th>Source</th></tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </div></body></html>"""
    return html


@app.route("/stats")
def stats():
    """Email performance stats."""
    with get_db() as conn:
        sent    = conn.execute("SELECT COUNT(*) FROM emails WHERE status='sent'").fetchone()[0]
        opens   = conn.execute("SELECT COUNT(*) FROM email_events WHERE event_type='open'").fetchone()[0]
        clicks  = conn.execute("SELECT COUNT(*) FROM email_events WHERE event_type='click'").fetchone()[0]
        bounces = conn.execute("SELECT COUNT(*) FROM email_events WHERE event_type='bounce'").fetchone()[0]
        replies = conn.execute("SELECT COUNT(*) FROM email_events WHERE event_type='reply'").fetchone()[0]
        unsubs  = conn.execute("SELECT COUNT(*) FROM suppression_list").fetchone()[0]

    open_rate   = f"{(opens/sent*100):.1f}%" if sent else "—"
    bounce_rate = f"{(bounces/sent*100):.1f}%" if sent else "—"

    html = f"""<!DOCTYPE html><html><head><title>Stats — {COMPANY_NAME}</title>{BASE_STYLE}</head><body>
    <div class="nav"><h1>Campaign Stats</h1><span><a href="/">Back to Dashboard</a></span></div>
    <div class="container">
      <div class="stat-row">
        <div class="stat-box"><div class="stat-num">{sent}</div><div class="stat-lbl">Emails Sent</div></div>
        <div class="stat-box"><div class="stat-num">{opens}</div><div class="stat-lbl">Opens ({open_rate})</div></div>
        <div class="stat-box"><div class="stat-num">{clicks}</div><div class="stat-lbl">Clicks</div></div>
        <div class="stat-box"><div class="stat-num">{replies}</div><div class="stat-lbl">Replies</div></div>
        <div class="stat-box"><div class="stat-num">{bounces}</div><div class="stat-lbl">Bounces ({bounce_rate})</div></div>
        <div class="stat-box"><div class="stat-num">{unsubs}</div><div class="stat-lbl">Unsubscribed</div></div>
      </div>
    </div></body></html>"""
    return html


@app.route("/unsubscribe")
def unsubscribe():
    """Public unsubscribe endpoint."""
    email = request.args.get("email", "").strip()
    if email:
        add_to_suppression(email, reason="unsubscribe")
        return """<html><body style="font-family:sans-serif;text-align:center;padding:60px">
        <h2>You've been unsubscribed.</h2>
        <p>Your email has been removed from our mailing list. You will not receive any further emails from us.</p>
        </body></html>"""
    return "Missing email parameter.", 400


def run_dashboard(port: int = 5050):
    logger.info(f"Starting approval dashboard at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
