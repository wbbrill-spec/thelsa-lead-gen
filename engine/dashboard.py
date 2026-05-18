"""
dashboard.py — Agent approval web interface (Flask).
The agent opens this in a browser to review, edit, approve, or reject
AI-drafted emails before anything is sent.

Run with: python run.py dashboard
Opens at: http://localhost:5050
"""

import logging
from flask import Flask, render_template_string, request, redirect, url_for, flash

from engine.config import AGENT_NAME, COMPANY_NAME, CLIENT_NAME
from engine.database import (
    get_pending_approvals,
    approve_email,
    reject_email,
    get_db,
    add_to_suppression,
)
from engine.email_sender import send_approved_email

logger = logging.getLogger(__name__)
app = Flask(__name__)
app.secret_key = "tms-lead-engine-secret-change-in-prod"


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
              <button type="submit" name="action" value="approve" class="btn btn-approve">Approve &amp; Send</button>
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

    html = f"""<!DOCTYPE html><html><head>
    <title>{COMPANY_NAME} — Lead Gen Engine</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    {BASE_STYLE}</head><body>
    <div class="nav">
      <h1>{COMPANY_NAME} — Lead Gen Engine &nbsp;|&nbsp; {CLIENT_NAME} Outreach</h1>
      <span>Agent: {AGENT_NAME} &nbsp;|&nbsp;
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
    language     = request.form.get("language", "english")

    if action == "approve":
        # Save any agent edits back to the DB
        with get_db() as conn:
            conn.execute("""
                UPDATE emails SET body_english=?, body_spanish=?, subject=?
                WHERE id=?
            """, (body_english, body_spanish, subject, email_id))

        approve_email(email_id, AGENT_NAME)
        result = send_approved_email(email_id, language)
        if result:
            flash_custom(f"Email sent successfully (ID {email_id}).", "ok")
        else:
            flash_custom(f"Approval saved but sending failed. Check logs.", "err")

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
