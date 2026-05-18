"""
email_drafter.py — AI email generation using Claude Sonnet (drafting tier).
Generates bilingual (English + Spanish) outreach emails personalised to each
company's expansion direction, industry, and expansion stage.

For US/Canada → Mexico expansions: leads with immigration/work permit complexity
for employees moving to Mexico.

For Mexico → US/Canada expansions: leads with US/Canada visa and relocation support.

A professional sender footer and unsubscribe link are appended to every draft.
"""

import logging
from typing import Optional

import anthropic

from engine.config import (
    ANTHROPIC_API_KEY,
    DRAFTING_MODEL,
    MAX_AI_CALLS_PER_DAY,
    AGENT_NAME,
    AGENT_TITLE,
    AGENT_EMAIL,
    AGENCY_NAME,
    AGENCY_ADDRESS,
    COMPANY_NAME,
    CLIENT_NAME,
    CLIENT_WEBSITE,
    UNSUBSCRIBE_URL,
)
from engine.database import (
    get_counter,
    increment_counter,
    create_email_draft,
)
from engine.gmail_drafts import create_gmail_draft

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Email footer (sender identity + unsubscribe link) ────────────────────────

def _compliance_footer_en() -> str:
    return f"""
---
{AGENT_NAME} | {AGENT_TITLE}
{COMPANY_NAME}
Email: {AGENT_EMAIL}

This email is a commercial introduction on behalf of {CLIENT_NAME} ({CLIENT_WEBSITE}).
{CLIENT_NAME} is a FIDI/ISO/LACMA/WERC certified corporate relocation and immigration
services company. Service terms and availability subject to engagement agreement.

To unsubscribe from future emails: {UNSUBSCRIBE_URL if UNSUBSCRIBE_URL else '[UNSUBSCRIBE_LINK]'}
""".strip()


def _compliance_footer_es() -> str:
    return f"""
---
{AGENT_NAME} | {AGENT_TITLE}
{COMPANY_NAME}
Email: {AGENT_EMAIL}

Este correo es una presentación comercial en nombre de {CLIENT_NAME} ({CLIENT_WEBSITE}).
{CLIENT_NAME} es una empresa de relocation corporativa y servicios de inmigración con
certificaciones FIDI/ISO/LACMA/WERC. Los términos del servicio están sujetos a acuerdo.

Para cancelar su suscripción: {UNSUBSCRIBE_URL if UNSUBSCRIBE_URL else '[ENLACE_CANCELAR]'}
""".strip()


# ── Direction-specific service context ────────────────────────────────────────

def _get_service_context(expansion_direction: str, industry: str) -> str:
    """
    Return a relevant service emphasis based on expansion direction.
    """
    if expansion_direction == "to_mexico":
        return (
            "immigration support (Mexican work permits, FM3 visas, temporary residency for "
            "expatriate employees), full household relocation to Mexico, school search and "
            "settling-in services for employee families, commercial office move management, "
            "and ongoing global mobility program administration for Mexico-based assignees"
        )
    elif expansion_direction == "to_us_canada":
        return (
            "US and Canadian work authorization (TN visas, H-1B support, L-1 intracompany "
            "transfers, Canadian work permits), full household relocation to the US or Canada, "
            "destination settling-in services, commercial office and equipment moving, "
            "and ongoing global mobility program support for North America-based assignees"
        )
    else:
        return (
            "immigration support (work permits, visas, residency), full household relocation, "
            "school search and settling-in services, commercial office moving, "
            "and ongoing global mobility program administration"
        )


# ── Draft prompt ──────────────────────────────────────────────────────────────

DRAFT_SYSTEM = f"""You are an expert corporate relocation and global mobility consultant
writing outreach emails on behalf of {COMPANY_NAME} to introduce {CLIENT_NAME}
({CLIENT_WEBSITE}) — a 30+ year FIDI/ISO/LACMA/WERC certified corporate relocation
and immigration services company — to companies undergoing cross-border expansion
between the US/Canada and Mexico.

Your emails must:
- Be warm, consultative, and relationship-first (never pushy or salesy)
- Acknowledge the company's cross-border expansion as a significant milestone
- Highlight the specific complexity of cross-border employee moves that Thelsa solves:
  immigration paperwork, work permits, visa processing, family relocation logistics,
  settling-in support, and commercial move management
- Position {CLIENT_NAME} as a trusted specialist with 30+ years of FIDI/ISO certified
  expertise, not as a generic vendor
- Reference {CLIENT_WEBSITE} as the resource for learning more
- Be tailored to the contact's function: HR/Global Mobility contacts get a message about
  employee experience and compliance; Procurement/C&B contacts get a message about
  program cost management and vendor consolidation
- Be concise: subject line under 65 chars, body 160-210 words
- Never make guarantees about specific outcomes, timelines, or pricing
- Never include a signature or footer (that is added automatically)

Writing tone: consultative, human, and culturally aware. Respect the significance of
the company's cross-border venture. For Spanish versions, use formal "usted" register."""


def draft_email(company_name: str, contact_first_name: str, contact_title: str,
                industry: str, expansion_stage: str, source_snippet: str,
                expansion_direction: str = "unknown",
                target_function: str = "hr",
                sequence_num: int = 1) -> Optional[dict]:
    """
    Generate a bilingual email draft using Claude Sonnet.
    Returns dict with english_subject, english_body, spanish_subject, spanish_body
    — or None if cap hit.
    """
    # ── Daily cap check ──────────────────────────────────────────────────────
    current = get_counter("ai_calls")
    if current >= MAX_AI_CALLS_PER_DAY:
        logger.warning("AI call daily cap reached. Cannot draft email.")
        return None

    service_context = _get_service_context(expansion_direction, industry)
    greeting_name = contact_first_name or "there"

    # Direction label for prompt context
    if expansion_direction == "to_mexico":
        direction_label = "expanding from the US/Canada into Mexico"
        language_note = (
            "Lead with immigration and work permit complexity for employees moving to Mexico. "
            "English version is primary. Spanish version should be warm and professional — "
            "this company's decision-makers may be US/Canadian but could have Spanish-speaking "
            "HR partners. Use bilingual subject line if helpful."
        )
    elif expansion_direction == "to_us_canada":
        direction_label = "expanding from Mexico into the US or Canada"
        language_note = (
            "Lead with US/Canadian visa, work authorization, and relocation support. "
            "Spanish version is primary for this direction — Mexican companies may prefer "
            "to receive outreach in Spanish. English version for any US-based decision-makers."
        )
    else:
        direction_label = "undergoing cross-border expansion"
        language_note = "Write both versions with equal weight."

    if sequence_num == 1:
        sequence_context = "This is an initial outreach email — the contact has never heard from us."
    elif sequence_num == 2:
        sequence_context = (
            "This is a first follow-up. The contact received our initial email about a week ago "
            "but has not replied. Briefly and warmly reference the previous message."
        )
    else:
        sequence_context = (
            "This is a final follow-up. Keep it very brief — offer to connect or close the loop."
        )

    # Target function context
    function_context_map = {
        "hr":              "This contact leads HR. Emphasize employee experience, smooth transitions for relocating employees and their families, and immigration compliance.",
        "global_mobility": "This contact manages global mobility. Speak their language: assignment lifecycle, policy compliance, FIDI-certified partners, cost-per-move.",
        "comp_benefits":   "This contact manages compensation and benefits. Emphasize predictable relocation costs, vendor consolidation, and benefit-consistent relocation packages.",
        "procurement":     "This contact handles vendor management. Emphasize Thelsa's 30+ year track record, certifications (FIDI/ISO/LACMA/WERC), consolidated vendor relationship, and transparent pricing.",
    }
    function_context = function_context_map.get(
        (target_function or "hr").lower().replace(" ", "_"),
        function_context_map["hr"]
    )

    prompt = f"""
Company: {company_name}
Contact first name: {contact_first_name or 'not available'}
Contact title: {contact_title or 'Decision Maker'}
Contact function: {target_function or 'hr'}
Industry: {industry or 'unknown'}
Expansion: {direction_label}
Expansion stage: {expansion_stage or 'unknown'}
Relevant services: {service_context}
Context from news/source: {source_snippet[:350] if source_snippet else 'N/A'}
Sequence: {sequence_context}
Function context: {function_context}
Language guidance: {language_note}
Agent: {AGENT_NAME}, {AGENT_TITLE} at {COMPANY_NAME}
Introducing: {CLIENT_NAME} ({CLIENT_WEBSITE})

Write the email in TWO versions:

VERSION 1 — ENGLISH:
Subject: [subject line]
Body: [email body — do NOT include a signature or footer, those are added automatically]

VERSION 2 — SPANISH:
Subject: [subject line in Spanish]
Body: [email body in Spanish — do NOT include firma or footer]

Format your response exactly like this, with these exact labels:
ENGLISH_SUBJECT: ...
ENGLISH_BODY:
...

SPANISH_SUBJECT: ...
SPANISH_BODY:
...
"""

    try:
        response = client.messages.create(
            model=DRAFTING_MODEL,
            max_tokens=1400,
            system=DRAFT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        increment_counter("ai_calls")
        text = response.content[0].text.strip()
        return _parse_draft_response(text)

    except anthropic.APIError as e:
        logger.error(f"Anthropic API error during drafting: {e}")
        return None


def _parse_draft_response(text: str) -> Optional[dict]:
    """Parse Claude's structured response into subject/body fields."""
    try:
        lines = text.split("\n")
        result = {
            "english_subject": "",
            "english_body": "",
            "spanish_subject": "",
            "spanish_body": "",
        }
        current_key = None
        buffer = []

        for line in lines:
            if line.startswith("ENGLISH_SUBJECT:"):
                result["english_subject"] = line.replace("ENGLISH_SUBJECT:", "").strip()
            elif line.startswith("ENGLISH_BODY:"):
                if current_key and buffer:
                    result[current_key] = "\n".join(buffer).strip()
                current_key = "english_body"
                buffer = []
            elif line.startswith("SPANISH_SUBJECT:"):
                if current_key and buffer:
                    result[current_key] = "\n".join(buffer).strip()
                result["spanish_subject"] = line.replace("SPANISH_SUBJECT:", "").strip()
                current_key = None
                buffer = []
            elif line.startswith("SPANISH_BODY:"):
                if current_key and buffer:
                    result[current_key] = "\n".join(buffer).strip()
                current_key = "spanish_body"
                buffer = []
            elif current_key:
                buffer.append(line)

        if current_key and buffer:
            result[current_key] = "\n".join(buffer).strip()

        return result if result["english_body"] else None

    except Exception as e:
        logger.error(f"Failed to parse draft response: {e}")
        return None


def create_and_save_draft(contact_id: int, company_id: int,
                           company_name: str, contact_first_name: str,
                           contact_title: str, industry: str,
                           expansion_stage: str, source_snippet: str,
                           expansion_direction: str = "unknown",
                           target_function: str = "hr",
                           sequence_num: int = 1,
                           pipeline_run_id: Optional[str] = None,
                           contact_email: Optional[str] = None) -> Optional[int]:
    """
    Generate a draft and save it to the database with compliance footers appended.
    Returns email ID on success, None on failure.
    """
    draft = draft_email(
        company_name=company_name,
        contact_first_name=contact_first_name,
        contact_title=contact_title,
        industry=industry,
        expansion_stage=expansion_stage,
        source_snippet=source_snippet,
        expansion_direction=expansion_direction,
        target_function=target_function,
        sequence_num=sequence_num,
    )
    if not draft:
        return None

    # Append sender footer with unsubscribe link
    body_en = draft["english_body"] + "\n\n" + _compliance_footer_en()
    body_es = draft["spanish_body"] + "\n\n" + _compliance_footer_es()
    subject = draft["english_subject"]

    email_id = create_email_draft(
        contact_id=contact_id,
        company_id=company_id,
        subject=subject,
        body_english=body_en,
        body_spanish=body_es,
        sequence_num=sequence_num,
        pipeline_run_id=pipeline_run_id,
    )

    # Move to pending_approval so it shows in the dashboard
    from engine.database import get_db
    with get_db() as conn:
        conn.execute(
            "UPDATE emails SET status='pending_approval' WHERE id=?",
            (email_id,)
        )

    # Also save to the triggering user's Gmail Drafts folder (non-fatal if it fails)
    if contact_email:
        create_gmail_draft(
            to_email=contact_email,
            subject=subject,
            body_english=body_en,
            body_spanish=body_es,
        )

    logger.info(f"Draft created (email ID {email_id}) for contact {contact_id} — awaiting approval.")
    return email_id


# ── RMC outreach email drafting ───────────────────────────────────────────────

_RMC_DRAFT_SYSTEM = """You are an expert B2B sales copywriter for TMS Corp, which
introduces Thelsa Mobility Solutions — a 30+ year certified corporate relocation firm
specialising in Mexico — to Relocation Management Companies (RMCs) and large
multinational corporations.

You are writing to the Head of Supply Chain / Household Goods Operations at an RMC.
The pitch: Thelsa provides premium, competitively priced household goods moving
in and out of Mexico that can help the RMC streamline their Mexico corridor services
and offer their clients better quality and pricing than traditional moving companies.

Key talking points:
- Thelsa has 30+ years specialising exclusively in Mexico cross-border moves
- Certified and compliant with all Mexican customs and relocation regulations
- Competitive pricing vs traditional van lines — meaningful cost savings for clients
- White-glove service with bilingual support (English and Spanish)
- Flexible: works as a supply chain partner alongside the RMC's existing network
- Can handle HHG, vehicle transport, storage, and immigration coordination
- Current RMC clients see 10-20% cost reduction on Mexico corridor moves

Write a short, professional outreach email (4-6 sentences in the body, not counting
greeting/close). Tone: collegial, peer-to-peer — you're one supply chain professional
reaching out to another, not a cold sales pitch.

Respond ONLY with valid JSON:
{
  "english_subject": "<subject line>",
  "english_body": "<email body, no greeting or sign-off, just the body paragraphs>",
  "spanish_subject": "<subject line in Spanish>",
  "spanish_body": "<email body in Spanish, no greeting or sign-off>"
}"""


def draft_rmc_email(
    rmc_name: str,
    contact_first_name: str,
    contact_title: str,
    multinational_company: str,
    expansion_direction: str = "unknown",
) -> Optional[dict]:
    """Draft an outreach email targeting an RMC's supply chain/HHG leader."""
    prompt = (
        f"RMC being targeted: {rmc_name}\n"
        f"Contact: {contact_first_name}, {contact_title}\n"
        f"Context: We identified that {multinational_company} (one of your clients or "
        f"a company in your market) is expanding cross-border "
        f"({'into Mexico' if expansion_direction == 'to_mexico' else 'from Mexico to the US/Canada'}).\n"
        f"This is a natural opening to introduce Thelsa as a Mexico HHG supply chain partner."
    )
    try:
        response = client.messages.create(
            model=DRAFTING_MODEL,
            max_tokens=1000,
            system=_RMC_DRAFT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        increment_counter("ai_calls")
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception as e:
        logger.error(f"RMC email draft failed: {e}")
        return None


_LARGE_COMPANY_DRAFT_SYSTEM = """You are an expert B2B sales copywriter for TMS Corp,
which introduces Thelsa Mobility Solutions — a 30+ year certified corporate relocation
firm specialising in Mexico — to HR and Global Mobility leaders at large multinational
corporations.

You are writing to a senior HR or Global Mobility leader at a large company that is
expanding cross-border between the US/Canada and Mexico.

The pitch: Thelsa provides premium, competitively priced household goods moving
in and out of Mexico. For companies managing employee relocations to/from Mexico,
Thelsa can streamline the Mexico corridor with better service and pricing than
traditional moving companies.

Key talking points:
- 30+ years specialising exclusively in Mexico cross-border moves
- Certified and compliant — no customs surprises for relocating employees
- Competitive pricing — typically 10-20% below traditional van lines on Mexico moves
- Bilingual (English/Spanish) support for transferees
- Full service: HHG, vehicles, storage, immigration coordination
- Easy to integrate alongside or as a supplement to their existing relocation program

Write a short, professional outreach email (4-6 sentences in the body). Tone:
consultative and respectful — acknowledge they likely have an existing program,
and position Thelsa as a specialised Mexico complement, not a full replacement.

Respond ONLY with valid JSON:
{
  "english_subject": "<subject line>",
  "english_body": "<email body, no greeting or sign-off>",
  "spanish_subject": "<subject line in Spanish>",
  "spanish_body": "<email body in Spanish, no greeting or sign-off>"
}"""


def draft_large_company_no_rmc_email(
    company_name: str,
    contact_first_name: str,
    contact_title: str,
    expansion_direction: str = "unknown",
    industry: str = "unknown",
) -> Optional[dict]:
    """Draft an outreach email to a large company's HR/Global Mobility leader
    when no RMC partner was identified."""
    prompt = (
        f"Company: {company_name} (industry: {industry})\n"
        f"Contact: {contact_first_name}, {contact_title}\n"
        f"Expansion direction: "
        f"{'expanding into Mexico' if expansion_direction == 'to_mexico' else 'expanding from Mexico to US/Canada'}\n"
        f"No RMC partner was identified — contact their internal HR/Global Mobility team directly."
    )
    try:
        response = client.messages.create(
            model=DRAFTING_MODEL,
            max_tokens=1000,
            system=_LARGE_COMPANY_DRAFT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        increment_counter("ai_calls")
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception as e:
        logger.error(f"Large company (no RMC) email draft failed: {e}")
        return None


import json  # ensure json is available (already imported at top via anthropic/etc)


def create_and_save_large_company_draft(
    contact_id: int,
    company_id: int,
    company_name: str,
    contact_first_name: str,
    contact_title: str,
    expansion_direction: str,
    industry: str,
    rmc_name: Optional[str] = None,
    multinational_company: Optional[str] = None,
    pipeline_run_id: Optional[str] = None,
    contact_email: Optional[str] = None,
) -> Optional[int]:
    """
    Generate and save a draft email for a large multinational lead.
    If rmc_name is provided, drafts to the RMC's supply chain head.
    Otherwise drafts to the company's HR/Global Mobility leader.
    """
    if rmc_name:
        draft = draft_rmc_email(
            rmc_name=rmc_name,
            contact_first_name=contact_first_name,
            contact_title=contact_title,
            multinational_company=multinational_company or company_name,
            expansion_direction=expansion_direction,
        )
    else:
        draft = draft_large_company_no_rmc_email(
            company_name=company_name,
            contact_first_name=contact_first_name,
            contact_title=contact_title,
            expansion_direction=expansion_direction,
            industry=industry,
        )

    if not draft:
        return None

    body_en = draft["english_body"] + "\n\n" + _compliance_footer_en()
    body_es = draft["spanish_body"] + "\n\n" + _compliance_footer_es()
    subject = draft["english_subject"]

    email_id = create_email_draft(
        contact_id=contact_id,
        company_id=company_id,
        subject=subject,
        body_english=body_en,
        body_spanish=body_es,
        sequence_num=1,
        pipeline_run_id=pipeline_run_id,
    )

    from engine.database import get_db
    with get_db() as conn:
        conn.execute(
            "UPDATE emails SET status='pending_approval' WHERE id=?",
            (email_id,)
        )

    # Also save to the triggering user's Gmail Drafts folder (non-fatal if it fails)
    if contact_email:
        create_gmail_draft(
            to_email=contact_email,
            subject=subject,
            body_english=body_en,
            body_spanish=body_es,
        )

    logger.info(
        f"Large company draft created (email ID {email_id}) — "
        f"{'RMC: ' + rmc_name if rmc_name else 'direct to company HR'}"
    )
    return email_id


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    draft = draft_email(
        company_name="Apex Workforce Solutions",
        contact_first_name="Sarah",
        contact_title="HR Director",
        industry="staffing",
        expansion_stage="in_progress",
        expansion_direction="to_mexico",
        target_function="hr",
        source_snippet="Dallas-based staffing firm registering entity in Monterrey to place workers at maquiladoras.",
    )
    if draft:
        print("=== ENGLISH ===")
        print(f"Subject: {draft['english_subject']}")
        print(draft["english_body"])
        print("\n=== SPANISH ===")
        print(f"Subject: {draft['spanish_subject']}")
        print(draft["spanish_body"])
