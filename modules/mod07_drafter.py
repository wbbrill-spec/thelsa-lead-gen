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
import re
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
- Greet the recipient by name using the provided contact name; never use a placeholder like "[Name]"
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
- Greet the recipient by name using the provided contact name; never use a placeholder like "[Name]"
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
- Nunca uses un marcador como "[Name]" ni "[Nombre]"; usa el nombre proporcionado
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
- Nunca uses un marcador como "[Name]" ni "[Nombre]"; usa el nombre proporcionado
- NO incluyas el asunto en el cuerpo del correo

Responde ÚNICAMENTE con un objeto JSON:
{{"subject": "...", "body": "..."}}"""

_WHY_THELSA_EN = """Why Thelsa is the right partner (weave ONE or TWO of these in naturally — do not list them all):
- Mexico's largest and most established relocation company, 30+ years, offices across Mexico and a US cross-border team
- One accountable partner for BOTH commercial moves (offices, plants, equipment, employee transfers) AND residential relocations of the people who move with the business
- Bilingual coordinators on both sides of the border, customs and immigration support, destination and settling-in services
- Trusted by multinationals and relocation management companies for Mexico–US moves"""

_WHY_THELSA_ES = """Por qué Thelsa es el socio indicado (integra UNA o DOS de estas ideas de forma natural — no las enlistes todas):
- La empresa de mudanzas y reubicación más grande y consolidada de México, con más de 30 años y presencia en todo el país y en la frontera con EE. UU.
- Un solo socio responsable tanto de mudanzas comerciales (oficinas, plantas, equipo, traslados de personal) como de las reubicaciones residenciales de las personas que se mueven con la empresa
- Coordinadores bilingües a ambos lados de la frontera, apoyo aduanal y migratorio, servicios de destino e integración
- Socio de confianza de multinacionales y empresas de gestión de reubicación para mudanzas México–EE. UU."""

_D2_EN = """Write a short, warm follow-up email in English from {sender_name} at Thelsa to {contact_name} at {company_name}.
It is a reply in the same thread as the original outreach (subject "{original_subject}"), sent two working days later. No reply has come yet.

{why_thelsa}

Requirements:
- 80–120 words, 2 short paragraphs
- Friendly and human but professional — a colleague circling back, never pushy or salesy
- Open by referencing the earlier note (e.g. "I wanted to follow up on my note about…") and their expansion: {expansion_detail}
- Include one concrete reason Thelsa is a great partner for their commercial AND residential relocation needs
- Ask for 15 minutes of their time, offering to work around their schedule
- Greet by first name ("Hi {contact_first_name},"); never use a placeholder like "[Name]"
- Sign off with just the first name of the sender: {sender_first_name}
- Do NOT include the subject line in the body

Return ONLY a JSON object:
{{"subject": "Re: {original_subject}", "body": "..."}}"""

_D2_ES = """Escribe un breve y cordial correo de seguimiento en español de {sender_name}, de Thelsa, para {contact_name} en {company_name}.
Es una respuesta en el mismo hilo del correo original (asunto "{original_subject}"), enviada dos días hábiles después. Aún no hay respuesta.

{why_thelsa}

Requisitos:
- 80–120 palabras, 2 párrafos cortos
- Tono cálido y humano pero profesional, estilo empresarial mexicano — alguien que retoma la conversación, nunca insistente ni de "venta dura"
- Abre haciendo referencia al correo anterior (p. ej. "Quería dar seguimiento a mi mensaje sobre…") y a su expansión: {expansion_detail}
- Incluye una razón concreta por la que Thelsa es un gran socio para sus necesidades de reubicación comercial Y residencial
- Pide 15 minutos de su tiempo, ofreciendo adaptarte a su agenda
- Saluda por nombre ("Hola {contact_first_name}," o "Estimado/a {contact_first_name},"); nunca uses "[Nombre]"
- Firma solo con el nombre de quien envía: {sender_first_name}
- NO incluyas el asunto en el cuerpo

Responde ÚNICAMENTE con un objeto JSON:
{{"subject": "Re: {original_subject}", "body": "..."}}"""

_D5_SMB_EN = """Write a warm, confident final follow-up email in English from {sender_name} at Thelsa to {contact_name} at {company_name}.
It is a reply in the same thread as the original outreach (subject "{original_subject}"), about a week after the first note and after one earlier follow-up. No reply has come.

{why_thelsa}

Requirements:
- 100–140 words, 2–3 short paragraphs
- Friendly and respectful of their time — acknowledge they are busy with the expansion ({expansion_detail}); never guilt-trip
- Briefly reference the earlier emails
- Give one or two specific reasons Thelsa is a great partner for both the commercial move and the residential relocations of the people coming with it
- Ask once more for a 15-minute call, and make it easy: offer two time windows or to simply reply with a convenient day
- Close the loop gracefully: if now isn't the right time, you're happy to reconnect when it is
- Greet by first name ("Hi {contact_first_name},"); never use a placeholder like "[Name]"
- Sign off with just the sender's first name: {sender_first_name}
- Do NOT include the subject line in the body

Return ONLY a JSON object:
{{"subject": "Re: {original_subject}", "body": "..."}}"""

_D5_RMC_EN = """Write a warm, confident final follow-up email in English from {sender_name} at Thelsa to {contact_name}, {contact_title} at {rmc_name} (a relocation management company).
It is a reply in the same thread as the original outreach (subject "{original_subject}"), about a week after the first note and after one earlier follow-up. No reply has come.

{why_thelsa}

Requirements:
- 100–140 words, 2–3 short paragraphs, partner-to-partner tone
- Friendly and respectful of their time; never pushy
- Briefly reference the earlier emails and the Mexico–US move activity of their client {company_name}
- Give one or two specific reasons Thelsa is the right destination/origin partner for both commercial moves and residential relocations in Mexico
- Ask once more for a 15-minute call and make it easy (offer two time windows or "just reply with a day that works")
- Close gracefully: happy to reconnect later if the timing is wrong
- Greet by first name ("Hi {contact_first_name},"); never use a placeholder like "[Name]"
- Sign off with just the sender's first name: {sender_first_name}
- Do NOT include the subject line in the body

Return ONLY a JSON object:
{{"subject": "Re: {original_subject}", "body": "..."}}"""

_D5_SMB_ES = """Escribe un correo de seguimiento final, cálido y seguro, en español de {sender_name}, de Thelsa, para {contact_name} en {company_name}.
Es una respuesta en el mismo hilo del correo original (asunto "{original_subject}"), aproximadamente una semana después del primer mensaje y tras un seguimiento previo. No ha habido respuesta.

{why_thelsa}

Requisitos:
- 100–140 palabras, 2–3 párrafos cortos
- Tono cordial, profesional y respetuoso de su tiempo — reconoce que están ocupados con la expansión ({expansion_detail}); nunca reproches
- Haz referencia breve a los correos anteriores
- Da una o dos razones concretas por las que Thelsa es un gran socio tanto para la mudanza comercial como para las reubicaciones residenciales del personal que llega con ella
- Pide una vez más una llamada de 15 minutos y facilítalo: ofrece dos horarios o que simplemente responda con un día que le convenga
- Cierra con elegancia: si no es el momento, con gusto retomas la conversación más adelante
- Saluda por nombre ("Hola {contact_first_name}," o "Estimado/a {contact_first_name},"); nunca uses "[Nombre]"
- Firma solo con el nombre de quien envía: {sender_first_name}
- NO incluyas el asunto en el cuerpo

Responde ÚNICAMENTE con un objeto JSON:
{{"subject": "Re: {original_subject}", "body": "..."}}"""

_D5_RMC_ES = _D5_SMB_ES

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

_GENERIC_LOCALPARTS = {
    "info", "sales", "contact", "hello", "admin", "marketing", "hr",
    "careers", "jobs", "support", "team", "office", "help", "mail",
    "no-reply", "noreply", "newsletter", "press", "media", "billing",
}


def _name_from_email(email: str) -> str:
    """Derive a human display name from an email address local-part.

    'michel.colon@bimbobakeriesusa.com' -> 'Michel Colon'. Returns '' for
    generic mailboxes (info@, sales@, ...) or when nothing usable is found.
    """
    local = (email or "").split("@")[0].strip().lower()
    if not local or local in _GENERIC_LOCALPARTS:
        return ""
    local = re.sub(r"\d+", "", local)  # drop digits like john.doe2
    parts = [p for p in re.split(r"[._\-]+", local) if p and p not in _GENERIC_LOCALPARTS]
    if not parts:
        return ""
    return " ".join(p.capitalize() for p in parts)


def _build_context(lead, company, contact) -> dict:
    """Build template context dict from DB objects."""
    contact_name = (contact.full_name or "").strip() if contact else ""
    if not contact_name:
        # No stored name — derive one from the recipient email address.
        source_email = (contact.email if (contact and contact.email) else "") or (lead.sent_to_email or "")
        contact_name = _name_from_email(source_email) or "there"
    contact_first_name = contact_name.split()[0] if contact_name and contact_name != "there" else "there"
    contact_title = contact.title if contact else ""
    contact_type = contact.contact_type if contact else "DIRECT"

    # Original subject + language of the initial outreach (prefer the Outlook draft)
    original_subject = ""
    initial_language = (getattr(lead, "sent_language", None) or "").upper() or None
    if lead.email_drafts:
        initials = [d for d in lead.email_drafts if d.draft_type == "INITIAL"]
        initial = (next((d for d in initials if d.provider == "outlook"), None)
                   or next((d for d in initials if d.language == (initial_language or "EN")), None)
                   or (initials[0] if initials else None))
        if initial:
            original_subject = initial.subject_line or ""
            initial_language = initial_language or (initial.language or "EN").upper()

    sender = getattr(lead, "assigned_to", None)
    sender_name = (sender.full_name if sender and sender.full_name else "the Thelsa team").strip()
    sender_first_name = sender_name.split()[0] if sender_name else "Thelsa"

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
        "initial_language": initial_language or "EN",
        "sender_name": sender_name,
        "sender_first_name": sender_first_name,
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
        why_thelsa=_WHY_THELSA_ES if lang == "ES" else _WHY_THELSA_EN,
        sender_name=ctx.get("sender_name", "the Thelsa team"),
        sender_first_name=ctx.get("sender_first_name", "Thelsa"),
    )

    from modules.llm import ask_json, LLMError
    try:
        data = ask_json(prompt, max_tokens=600, expect="object", tag="MOD-07")
    except LLMError as e:
        # Never fall back to a placeholder body — a real Outlook/Gmail draft
        # reading "[Email generation failed]" used to be created from it.
        raise RuntimeError(f"Email generation failed ({draft_type}/{lang}): {e}") from e
    subject = str(data.get("subject", "") or "").strip()
    body = str(data.get("body", "") or "").strip()
    if not subject or not body:
        raise RuntimeError(f"Email generation returned an empty subject/body ({draft_type}/{lang})")
    return subject, body


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
