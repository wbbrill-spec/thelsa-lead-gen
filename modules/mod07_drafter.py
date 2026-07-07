"""MOD-07: Email Drafter

Generates personalized bilingual email drafts and creates them in the
assigned user's Gmail Drafts folder.

Handles INITIAL, FOLLOWUP_D2, and FOLLOWUP_D5 draft types.
Always generates both EN and ES versions simultaneously.
Spanish version is culturally adapted for Mexican business context.
"""

from __future__ import annotations
import base64
import json
from datetime import datetime, timezone
from email.mime.text import MIMEText

import anthropic
import config
from db import get_db
from models import Lead, Company, Contact, User, EmailDraft


# ── Thelsa service description (sourced from thelsa.com) ──────────────────────

_THELSA_SERVICES_EN = """Thelsa is the largest and most established relocation and moving company in Mexico, with over 30 years of experience. Our services include:
- Household goods and personal effects moving
- Micro shipments
- Office and commercial moving
- Destination and settling-in services
- Immigration support services
We specialize in cross-border moves between Mexico and the United States."""

_THELSA_SERVICES_ES = """Thelsa es la empresa de mudanzas y reubicación más grande y establecida de México, con más de 30 años de experiencia. Nuestros servicios incluyen:
- Mudanza de enseres domésticos y efectos personales
- Micro envíos
- Mudanza de oficinas y comercial
- Servicios de destino e integración
- Servicios de apoyo en inmigración
Somos especialistas en mudanzas transfronterizas entre México y los Estados Unidos."""


# ── Prompt templates ───────────────────────────────────────────────────────────

_INITIAL_SMB_EN = """Write a professional outreach email in English from Thelsa to {contact_name} at {company_name}.

Context: {company_name} is {expansion_detail}.

Thelsa background: {services}

Requirements:
- Max 150 words
- Professional but warm, direct company-to-company tone
- Reference their specific expansion
- Introduce Thelsa and its services briefly
- CTA: request a 15-minute call
- Do NOT use subject line in body

Return ONLY a JSON object:
{{"subject": "...", "body": "..."}}"""

_INITIAL_RMC_EN = """Write a professional outreach email in English from Thelsa to {contact_name}, {contact_title} at {rmc_name}.

Context: {company_name} is expanding cross-border and appears to work with {rmc_name} for relocation management.

Thelsa background: {services}

Requirements:
- Max 150 words
- B2B partner-to-partner tone
- Introduce Thelsa as a potential partner to support their clients moving in/out of Mexico
- CTA: request a 20-minute call to explore how we can work together to benefit their clients
- Do NOT use subject line in body

Return ONLY a JSON object:
{{"subject": "...", "body": "..."}}"""

_INITIAL_SMB_ES = """Escribe un correo electrónico de presentación profesional en español para {contact_name} en {company_name}, de parte de Thelsa.

Contexto: {company_name} {expansion_detail}.

Información de Thelsa: {services}

Requisitos:
- Máximo 150 palabras
- Tono profesional y directo, de empresa a empresa
- Saludo formal mexicano (Estimado/a {contact_first_name})
- Menciona específicamente su expansión
- Presenta brevemente a Thelsa y sus servicios
- CTA: solicitar una llamada de 15 minutos
- Cierre formal apropiado para el contexto empresarial mexicano
- NO incluyas el asunto en el cuerpo del correo

Responde ÚNICAMENTE con un objeto JSON:
{{"subject": "...", "body": "..."}}"""

_INITIAL_RMC_ES = """Escribe un correo electrónico de presentación profesional en español para {contact_name}, {contact_title} en {rmc_name}, de parte de Thelsa.

Contexto: {company_name} tiene presencia transfronteriza y aparentemente trabaja con {rmc_name} para gestión de reubicaciones.

Información de Thelsa: {services}

Requisitos:
- Máximo 150 palabras
- Tono de socio a socio, B2B
- Saludo formal mexicano (Estimado/a {contact_first_name})
- Presenta a Thelsa como socio potencial para apoyar a sus clientes con mudanzas en/desde México
- CTA: solicitar una llamada de 20 minutos para explorar cómo trabajar juntos en beneficio de sus clientes
- Cierre formal apropiado para el contexto empresarial mexicano
- NO incluyas el asunto en el cuerpo del correo

Responde ÚNICAMENTE con un objeto JSON:
{{"subject": "...", "body": "..."}}"""

_D2_EN = """Write a brief, warm follow-up email in English. This is Day 2 follow-up after an initial outreach to {contact_name} at {company_name} about Thelsa's cross-border relocation services.

Requirements:
- 2-3 sentences maximum
- Gentle bump — assume the email got buried
- Reference the original email briefly
- Same CTA: {cta}
- Do NOT use subject line in body

Return ONLY a JSON object:
{{"subject": "Re: {original_subject}", "body": "..."}}"""

_D2_ES = """Escribe un breve correo de seguimiento en español. Es el seguimiento del Día 2 después de una presentación inicial a {contact_name} en {company_name} sobre los servicios transfronterizos de Thelsa.

Requisitos:
- Máximo 2-3 oraciones
- Tono amable, no invasivo — asumir que el correo anterior se perdió entre otros
- Saludo formal mexicano breve
- Referencia brevemente el correo anterior
- Mismo CTA: {cta}
- Cierre breve pero formal
- NO incluyas el asunto en el cuerpo del correo

Responde ÚNICAMENTE con un objeto JSON:
{{"subject": "Re: {original_subject}", "body": "..."}}"""

_D5_SMB_EN = """Write an urgent, value-forward follow-up email in English to {contact_name} at {company_name}.

This is the Day 5 follow-up. No response has been received.

Key Thelsa credentials to include:
- Largest, most well-established company in Mexico
- 30+ years in business
- Full services: household goods, personal effects, micro shipments, office and commercial moving, destination/settling-in services, immigration
- Core message: "Your employees deserve the best — we need to talk"

Requirements:
- Professional but urgent tone
- Confident, not desperate
- Clear final CTA: 15-minute call
- Do NOT use subject line in body

Return ONLY a JSON object:
{{"subject": "...", "body": "..."}}"""

_D5_RMC_EN = """Write an urgent, value-forward follow-up email in English to {contact_name}, {contact_title} at {rmc_name}.

This is the Day 5 follow-up. No response has been received.

Key Thelsa credentials to include:
- Largest, most well-established company in Mexico
- 30+ years in business
- Full services: household goods, personal effects, micro shipments, office and commercial moving, destination/settling-in services, immigration
- Core message: "Your clients deserve the best — we need to talk"

Requirements:
- Professional but urgent tone
- B2B partner framing
- Confident, not desperate
- Clear final CTA: 20-minute call
- Do NOT use subject line in body

Return ONLY a JSON object:
{{"subject": "...", "body": "..."}}"""

_D5_SMB_ES = """Escribe un correo de seguimiento urgente y con propuesta de valor en español para {contact_name} en {company_name}.

Este es el seguimiento del Día 5. No se ha recibido respuesta.

Credenciales clave de Thelsa a incluir:
- La empresa de mudanzas más grande y consolidada de México
- Más de 30 años en el negocio
- Servicios completos: enseres domésticos, efectos personales, micro envíos, mudanza de oficinas y comercial, servicios de destino e integración, inmigración
- Mensaje central: "Sus empleados merecen lo mejor — necesitamos hablar"

Requisitos:
- Tono profesional pero urgente, con confianza — no desesperado
- Saludo formal mexicano
- CTA claro: llamada de 15 minutos
- Cierre formal apropiado
- NO incluyas el asunto en el cuerpo del correo

Responde ÚNICAMENTE con un objeto JSON:
{{"subject": "...", "body": "..."}}"""

_D5_RMC_ES = """Escribe un correo de seguimiento urgente y con propuesta de valor en español para {contact_name}, {contact_title} en {rmc_name}.

Este es el seguimiento del Día 5. No se ha recibido respuesta.

Credenciales clave de Thelsa a incluir:
- La empresa de mudanzas más grande y consolidada de México
- Más de 30 años en el negocio
- Servicios completos: enseres domésticos, efectos personales, micro envíos, mudanza de oficinas y comercial, servicios de destino e integración, inmigración
- Mensaje central: "Sus clientes merecen lo mejor — necesitamos hablar"

Requisitos:
- Tono profesional pero urgente, B2B
- Saludo formal mexicano
- CTA claro: llamada de 20 minutos
- Cierre formal apropiado
- NO incluyas el asunto en el cuerpo del correo

Responde ÚNICAMENTE con un objeto JSON:
{{"subject": "...", "body": "..."}}"""


# ── Main functions ─────────────────────────────────────────────────────────────

def create_initial_drafts(lead_id: int, credentials, user_email: str) -> list[int]:
    """Create EN and ES initial drafts for a lead in the user's Gmail.

    Returns list of EmailDraft IDs created.
    """
    with get_db() as db:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            raise ValueError(f"Lead {lead_id} not found")

        company = lead.company
        contact = lead.contact
        user = lead.assigned_to

        # Build context
        ctx = _build_context(lead, company, contact)

    draft_ids = []
    for lang in ["EN", "ES"]:
        subject, body = _generate_email(ctx, "INITIAL", lang)
        draft_id = _create_gmail_draft(
            credentials=credentials,
            user_email=user_email,
            to_email=contact.email if contact else "",
            subject=subject,
            body=body,
        )
        db_draft_id = _save_draft(lead_id, "INITIAL", lang, subject, body, draft_id, "gmail")
        draft_ids.append(db_draft_id)

    # Update lead status to APPROVED -> draft creation pending
    with get_db() as db:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        from models import transition_status
        transition_status(db, lead, Lead.STATUS_APPROVED, "system", "Drafts created in Gmail")

    return draft_ids


def create_followup_drafts(lead_id: int, draft_type: str) -> list[int]:
    """Create EN and ES follow-up drafts. Called by scheduler.

    draft_type: 'FOLLOWUP_D2' or 'FOLLOWUP_D5'
    """
    with get_db() as db:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            raise ValueError(f"Lead {lead_id} not found")

        company = lead.company
        contact = lead.contact
        assigned_user = lead.assigned_to
        ctx = _build_context(lead, company, contact)

        # Get assigned user credentials
        token_json = assigned_user.oauth_token
        if not token_json:
            raise ValueError(f"No OAuth token for user {assigned_user.full_name}")

    from web_auth import WebAuthFlow
    credentials = WebAuthFlow.credentials_from_token(token_json)
    user_email = assigned_user.active_email

    draft_ids = []
    for lang in ["EN", "ES"]:
        subject, body = _generate_email(ctx, draft_type, lang)
        draft_id = _create_gmail_draft(
            credentials=credentials,
            user_email=user_email,
            to_email=contact.email if contact else "",
            subject=subject,
            body=body,
        )
        db_draft_id = _save_draft(lead_id, draft_type, lang, subject, body, draft_id, "gmail")
        draft_ids.append(db_draft_id)

    return draft_ids


def send_call_required_notification(lead_id: int) -> bool:
    """Send internal notification email to rep — time to make a phone call."""
    with get_db() as db:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            return False

        company = lead.company
        contact = lead.contact
        assigned_user = lead.assigned_to
        token_json = assigned_user.oauth_token

        if not token_json:
            return False

        contact_name = contact.full_name if contact else "the contact"
        contact_phone = contact.phone if contact else "No phone on file"
        contact_email = contact.email if contact else ""
        company_name = company.name if company else "the company"

    from web_auth import WebAuthFlow
    credentials = WebAuthFlow.credentials_from_token(token_json)
    user_email = assigned_user.active_email

    subject = f"📞 Time to call: {contact_name} at {company_name}"
    body = f"""Hi {assigned_user.full_name.split()[0]},

The automated outreach sequence for {contact_name} at {company_name} is complete — no response was received after the initial email, Day 2 follow-up, and Day 5 follow-up.

It's time to make a phone call.

Contact details:
  Name: {contact_name}
  Phone: {contact_phone}
  Email: {contact_email}
  Company: {company_name}

Good luck!

— Thelsa Lead Gen System"""

    try:
        _create_gmail_draft(
            credentials=credentials,
            user_email=user_email,
            to_email=user_email,  # to the rep themselves
            subject=subject,
            body=body,
        )
        return True
    except Exception as e:
        print(f"[MOD-07] Call required notification error: {e}")
        return False


# ── Internal helpers ───────────────────────────────────────────────────────────

def _build_context(lead, company, contact) -> dict:
    """Build template context dict from DB objects."""
    contact_name = contact.full_name if contact else "there"
    contact_first_name = contact_name.split()[0] if contact_name != "there" else "there"
    contact_title = contact.title if contact else ""
    contact_type = contact.contact_type if contact else "DIRECT"

    # Get original subject from initial draft if available
    original_subject = ""
    if lead.email_drafts:
        initial = next((d for d in lead.email_drafts if d.draft_type == "INITIAL" and d.language == "EN"), None)
        if initial:
            original_subject = initial.subject_line or ""

    expansion_detail = company.source_snippet[:200] if company else ""

    return {
        "contact_name": contact_name,
        "contact_first_name": contact_first_name,
        "contact_title": contact_title,
        "contact_type": contact_type,
        "company_name": company.name if company else "",
        "rmc_name": company.rmc_name if company and company.rmc_name else "",
        "expansion_detail": expansion_detail,
        "effective_flow": "RMC" if (company and company.rmc_detected) else "SMB",
        "original_subject": original_subject,
        "cta_en": "15-minute call" if contact_type == "DIRECT" else "20-minute call",
        "cta_es": "llamada de 15 minutos" if contact_type == "DIRECT" else "llamada de 20 minutos",
    }


def _generate_email(ctx: dict, draft_type: str, lang: str) -> tuple[str, str]:
    """Generate email subject and body using Claude."""
    flow = ctx["effective_flow"]
    is_rmc = flow == "RMC"

    if draft_type == "INITIAL":
        if lang == "EN":
            template = _INITIAL_RMC_EN if is_rmc else _INITIAL_SMB_EN
        else:
            template = _INITIAL_RMC_ES if is_rmc else _INITIAL_SMB_ES
    elif draft_type == "FOLLOWUP_D2":
        template = _D2_ES if lang == "ES" else _D2_EN
    elif draft_type == "FOLLOWUP_D5":
        if lang == "EN":
            template = _D5_RMC_EN if is_rmc else _D5_SMB_EN
        else:
            template = _D5_RMC_ES if is_rmc else _D5_SMB_ES
    else:
        raise ValueError(f"Unknown draft_type: {draft_type}")

    prompt = template.format(
        contact_name=ctx["contact_name"],
        contact_first_name=ctx["contact_first_name"],
        contact_title=ctx["contact_title"],
        company_name=ctx["company_name"],
        rmc_name=ctx["rmc_name"],
        expansion_detail=ctx["expansion_detail"],
        services=_THELSA_SERVICES_ES if lang == "ES" else _THELSA_SERVICES_EN,
        original_subject=ctx["original_subject"],
        cta=ctx["cta_es"] if lang == "ES" else ctx["cta_en"],
    )

    try:
        import re
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        text = message.content[0].text.strip()
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"```$", "", text).strip()
        data = json.loads(text)
        return data.get("subject", ""), data.get("body", "")
    except Exception as e:
        print(f"[MOD-07] Email generation error ({draft_type}/{lang}): {e}")
        return f"Following up — Thelsa", f"[Email generation failed: {e}]"


def _create_gmail_draft(
    credentials,
    user_email: str,
    to_email: str,
    subject: str,
    body: str,
) -> str:
    """Create a draft in the user's Gmail. Returns Gmail draft ID."""
    from googleapiclient.discovery import build

    msg = MIMEText(body, _charset="utf-8")
    msg["to"] = to_email
    msg["from"] = user_email
    msg["subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    draft = service.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw}},
    ).execute()

    return draft.get("id", "")


def _save_draft(
    lead_id: int,
    draft_type: str,
    language: str,
    subject: str,
    body: str,
    provider_draft_id: str,
    provider: str,
) -> int:
    """Save EmailDraft record to DB. Returns DB record ID."""
    with get_db() as db:
        draft = EmailDraft(
            lead_id=lead_id,
            draft_type=draft_type,
            language=language,
            subject_line=subject,
            body_text=body,
            provider_draft_id=provider_draft_id,
            provider=provider,
            created_in_drafts_at=datetime.now(timezone.utc),
        )
        db.add(draft)
        db.flush()
        return draft.id


def create_initial_outlook_draft(lead_id: int, mailbox: str, language: str = "EN") -> int:
    """Generate the INITIAL outreach email and create it as a draft in ``mailbox``'s
    Outlook Drafts folder via Microsoft Graph. Reuses the same Claude-generated
    content as the Gmail path; only the delivery differs. Drafts only — never sends.

    Returns the EmailDraft DB id.
    """
    from modules.graph_outlook import create_outlook_draft

    with get_db() as db:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            raise ValueError(f"Lead {lead_id} not found")
        ctx = _build_context(lead, lead.company, lead.contact)
        to_email = lead.contact.email if lead.contact else ""

    subject, body = _generate_email(ctx, "INITIAL", language)
    result = create_outlook_draft(
        mailbox=mailbox,
        to_email=to_email,
        subject=subject,
        body=body,
    )
    return _save_draft(lead_id, "INITIAL", language, subject, body, result.get("id", ""), "outlook")


def draft_on_assign(lead_id: int, mailbox: str, rep_name: str, changed_by: str) -> None:
    """Called when a lead is assigned to a rep: create the INITIAL outreach draft in
    that rep's thelsa.com Outlook Drafts and mark the lead DRAFTED. Never raises —
    logs on failure so it can never break the assign action or the dashboard.
    """
    mailbox = (mailbox or "").strip()
    if not mailbox:
        print(f"[MOD-07] draft_on_assign: no Outlook address on file for {rep_name}; skipped.")
        return
    try:
        create_initial_outlook_draft(lead_id=lead_id, mailbox=mailbox, language="EN")
        with get_db() as db:
            lead = db.query(Lead).filter_by(id=lead_id).first()
            if lead:
                from models import transition_status
                transition_status(db, lead, Lead.STATUS_DRAFTED, changed_by,
                                  f"Outlook draft created for {rep_name}")
    except Exception as exc:
        print(f"[MOD-07] draft_on_assign failed for lead {lead_id} / {mailbox}: {exc}")
