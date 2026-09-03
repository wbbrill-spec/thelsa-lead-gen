"""MOD-05: Contact Enricher

Finds the correct contact for outreach using the ZoomInfo GTM API
(modules/zoominfo.py: contact search → enrich by personId).
Falls back to web search + AI extraction if ZoomInfo returns nothing.
Writes Company, Contact, and Lead records to DB.
"""

from __future__ import annotations
import json
import re
import time
import requests
from dataclasses import dataclass
from modules.mod04_segmentation import SegmentedCandidate
from modules.llm import ask_json, LLMError

# Per-run enrichment stats, reset by the pipeline runner.
STATS = {"zoominfo_hits": 0, "web_hits": 0, "empty": 0, "zoominfo_http_errors": 0}


def enrich_contacts(
    candidates: list[SegmentedCandidate],
    run_id: int,
    generated_by_user_id: int,
) -> list[int]:
    """Enrich each candidate with contact data and write to DB.

    Args:
        candidates: Segmented candidates from MOD-04
        run_id: Current discovery run ID
        generated_by_user_id: User who triggered the run

    Returns:
        List of new Lead IDs created
    """
    from pipeline import heartbeat
    lead_ids = []
    for candidate in candidates:
        heartbeat()
        lead_id = _process_one(candidate, generated_by_user_id)
        if lead_id:
            lead_ids.append(lead_id)
    return lead_ids


def _process_one(candidate: SegmentedCandidate, generated_by_user_id: int) -> int | None:
    """Enrich one candidate and create DB records."""
    c = candidate.scored.candidate

    # Determine search target based on flow
    if candidate.effective_flow == "RMC":
        contact = _find_rmc_contact(candidate.rmc_name)
        contact_type = "RMC"
    else:
        contact = _find_direct_contact(c.name, c.domain, c.industry)
        contact_type = "DIRECT"

    # Write to DB
    return _write_to_db(candidate, contact, contact_type, generated_by_user_id)


def _find_direct_contact(company_name: str, domain: str, industry: str) -> dict:
    """Find a direct contact at the company via ZoomInfo, fallback to web search."""
    # Try ZoomInfo first
    zoominfo_result = _zoominfo_enrich(
        company_name=company_name,
        domain=domain,
        titles=["Logistics Manager", "Operations Manager", "Supply Chain Manager",
                "VP Operations", "Director of Logistics", "CEO", "Owner", "President"],
    )
    if zoominfo_result:
        STATS["zoominfo_hits"] += 1
        return zoominfo_result

    # Fallback: web search
    return _count(_web_search_contact(company_name, domain, industry, contact_type="direct"))


def _find_rmc_contact(rmc_name: str) -> dict:
    """Find supply chain / network manager at the RMC via ZoomInfo, fallback to web search."""
    zoominfo_result = _zoominfo_enrich(
        company_name=rmc_name,
        domain="",
        titles=["Supply Chain Manager", "Network Manager", "Account Manager",
                "Director Supply Chain", "VP Supply Chain", "Operations Manager"],
    )
    if zoominfo_result:
        STATS["zoominfo_hits"] += 1
        return zoominfo_result

    return _count(_web_search_contact(rmc_name, "", "relocation", contact_type="rmc"))


def _count(contact: dict) -> dict:
    if contact.get("full_name") or contact.get("email"):
        STATS["web_hits"] += 1
    else:
        STATS["empty"] += 1
    return contact


def _zoominfo_enrich(company_name: str, domain: str, titles: list[str]) -> dict | None:
    """Search ZoomInfo for the best contact at the company and enrich it.
    See modules/zoominfo.py for the two-step search→enrich flow."""
    from modules import zoominfo
    if not zoominfo.configured():
        return None
    errors_before = zoominfo.STATS["http_errors"]
    result = zoominfo.find_contact(company_name, domain, titles)
    STATS["zoominfo_http_errors"] += zoominfo.STATS["http_errors"] - errors_before
    return result


def _web_search_contact(
    company_name: str, domain: str, industry: str, contact_type: str
) -> dict:
    """Fallback: find contact via web search + AI extraction."""
    from modules.mod08_search import search

    if contact_type == "rmc":
        query = f"{company_name} supply chain manager network manager contact email"
    else:
        query = f"{company_name} logistics operations manager contact email LinkedIn"

    # No date filter: LinkedIn profile pages are rarely "recent".
    results = search(query, num_results=5, recency_days=0)
    if not results:
        return _empty_contact()

    results_text = "\n".join([f"- {r.title}: {r.snippet}" for r in results[:5]])

    if contact_type == "rmc":
        role_hint = "supply chain manager or network manager"
    else:
        role_hint = "logistics, operations, or owner-level contact"

    prompt = f"""Extract contact information for a {role_hint} at {company_name} from these search results.

{results_text}

These results often come from LinkedIn profile snippets formatted like:
"FirstName LastName - Title at Company | bio text..."
Treat any named individual whose snippet places them at {company_name} (or a clear
subsidiary/division of it) as a usable contact, even if their title isn't an exact
match for {role_hint} — a real name at the company is far more useful than nothing.
Prefer the most senior or most operationally relevant person if multiple appear.

Respond with ONLY a JSON object:
{{"full_name": "Jane Smith", "title": "Supply Chain Manager", "email": "jane@company.com", "phone": "+1-555-0000"}}

Only return all-empty values if NONE of the results name a real person associated with {company_name}:
{{"full_name": "", "title": "", "email": "", "phone": ""}}"""

    try:
        def _ask(p: str) -> dict:
            return ask_json(p, max_tokens=200, expect="object", temperature=0, tag="MOD-05")

        data = _ask(prompt)

        # Retry once with a more permissive nudge if the first pass came back empty
        # but the search results clearly contain named individuals — the model is
        # sometimes overly conservative on the first attempt.
        if not data.get("full_name") and not data.get("email"):
            retry_prompt = prompt + (
                "\n\nReminder: if any search result snippet contains a person's "
                "full name associated with this company, extract them — do not "
                "return all-empty unless every result is generic company/job-board "
                "content with no named individual."
            )
            data = _ask(retry_prompt)

        data = {k: (data.get(k) or "") for k in ("full_name", "title", "email", "phone")}
        data["enrichment_source"] = "web_search"
        data["enrichment_raw"] = None
        return data
    except LLMError as e:
        print(f"[MOD-05] Web search contact extraction error: {e}")
        return _empty_contact()


def _empty_contact() -> dict:
    return {
        "full_name": "",
        "title": "",
        "email": "",
        "phone": "",
        "enrichment_source": "web_search",
        "enrichment_raw": None,
    }


def _write_to_db(
    candidate: SegmentedCandidate,
    contact_data: dict,
    contact_type: str,
    generated_by_user_id: int,
) -> int | None:
    """Write Company, Contact, and Lead records to DB. Returns lead ID."""
    from db import get_db
    from models import Company, Contact, Lead, LeadStatusHistory, transition_status

    c = candidate.scored.candidate
    sc = candidate.scored

    try:
        with get_db() as db:
            # Write company
            company = db.query(Company).filter_by(domain=c.domain).first()
            if not company:
                company = Company(
                    name=c.name,
                    domain=c.domain,
                    industry=c.industry,
                    country_of_origin=c.country_of_origin,
                    expansion_direction=c.expansion_direction,
                    size_tier=candidate.size_tier,
                    rmc_detected=candidate.rmc_detected,
                    rmc_name=candidate.rmc_name or None,
                    source_url=c.source_url,
                    source_snippet=c.source_snippet,
                )
                db.add(company)
                db.flush()

            # Write contact
            contact = None
            if contact_data.get("full_name") or contact_data.get("email"):
                contact = Contact(
                    company_id=company.id,
                    contact_type=contact_type,
                    full_name=contact_data.get("full_name", ""),
                    title=contact_data.get("title", ""),
                    email=contact_data.get("email", ""),
                    phone=contact_data.get("phone", ""),
                    enrichment_source=contact_data.get("enrichment_source", ""),
                    enrichment_raw=contact_data.get("enrichment_raw"),
                    is_primary=True,
                )
                db.add(contact)
                db.flush()

            # Write lead
            lead = Lead(
                company_id=company.id,
                contact_id=contact.id if contact else None,
                generated_by_user_id=generated_by_user_id,
                assigned_to_user_id=generated_by_user_id,  # default: assign to generator
                qualification_score=sc.score,
                qualification_reasoning=sc.reasoning,
                status=Lead.STATUS_NEW,
            )
            db.add(lead)
            db.flush()

            # Write initial status history
            history = LeadStatusHistory(
                lead_id=lead.id,
                changed_by="system",
                from_status=None,
                to_status=Lead.STATUS_NEW,
                reason="Lead discovered by pipeline",
            )
            db.add(history)

            return lead.id

    except Exception as e:
        print(f"[MOD-05] DB write error for {c.name}: {e}")
        return None
