"""MOD-05: Contact Enricher

Finds the correct contact for outreach using ZoomInfo API.
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


_ZOOMINFO_TOKEN_CACHE = {"token": None, "expires_at": 0}


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
    lead_ids = []
    for candidate in candidates:
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
    zoominfo_result = _zoominfo_search(
        company_name=company_name,
        domain=domain,
        titles=["Logistics Manager", "Operations Manager", "Supply Chain Manager",
                "VP Operations", "Director of Logistics", "CEO", "Owner", "President"],
    )
    if zoominfo_result:
        return zoominfo_result

    # Fallback: web search
    return _web_search_contact(company_name, domain, industry, contact_type="direct")


def _find_rmc_contact(rmc_name: str) -> dict:
    """Find supply chain / network manager at the RMC via ZoomInfo, fallback to web search."""
    zoominfo_result = _zoominfo_search(
        company_name=rmc_name,
        domain="",
        titles=["Supply Chain Manager", "Network Manager", "Account Manager",
                "Director Supply Chain", "VP Supply Chain", "Operations Manager"],
    )
    if zoominfo_result:
        return zoominfo_result

    return _web_search_contact(rmc_name, "", "relocation", contact_type="rmc")


def _zoominfo_search(company_name: str, domain: str, titles: list[str]) -> dict | None:
    """Search ZoomInfo API for a contact. Returns dict or None."""
    import config

    if not config.ZOOMINFO_CLIENT_ID or not config.ZOOMINFO_CLIENT_SECRET:
        return None  # Credentials not configured — use fallback

    try:
        token = _get_zoominfo_token()
        if not token:
            return None

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "TMS-LeadGen/1.0",
        }

        # Search for person by company
        payload = {
            "outputFields": ["firstName", "lastName", "jobTitle", "email", "phone", "companyName"],
            "searchValues": {
                "companyName": [company_name],
                "jobTitle": titles[:5],
            },
            "sortBy": "relevance",
            "rpp": 1,
            "page": 1,
        }

        if domain:
            payload["searchValues"]["companyWebsite"] = [domain]

        resp = requests.post(
            f"{config.ZOOMINFO_BASE_URL}/search/contact",
            headers=headers,
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        results = data.get("data", {}).get("outputFields", [])
        if not results:
            return None

        person = results[0]
        full_name = f"{person.get('firstName', '')} {person.get('lastName', '')}".strip()
        if not full_name:
            return None

        return {
            "full_name": full_name,
            "title": person.get("jobTitle", ""),
            "email": person.get("email", ""),
            "phone": person.get("phone", ""),
            "enrichment_source": "zoominfo",
            "enrichment_raw": person,
        }

    except requests.exceptions.HTTPError as e:
        print(f"[MOD-05] ZoomInfo HTTP error: {e}")
        return None
    except Exception as e:
        print(f"[MOD-05] ZoomInfo error: {e}")
        return None


def _get_zoominfo_token() -> str | None:
    """Get a valid ZoomInfo OAuth token, refreshing if expired."""
    import config

    now = time.time()
    if _ZOOMINFO_TOKEN_CACHE["token"] and _ZOOMINFO_TOKEN_CACHE["expires_at"] > now + 60:
        return _ZOOMINFO_TOKEN_CACHE["token"]

    try:
        resp = requests.post(
            config.ZOOMINFO_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": config.ZOOMINFO_CLIENT_ID,
                "client_secret": config.ZOOMINFO_CLIENT_SECRET,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token", "")
        expires_in = data.get("expires_in", 3600)
        _ZOOMINFO_TOKEN_CACHE["token"] = token
        _ZOOMINFO_TOKEN_CACHE["expires_at"] = now + expires_in
        return token
    except Exception as e:
        print(f"[MOD-05] ZoomInfo token error: {e}")
        return None


def _web_search_contact(
    company_name: str, domain: str, industry: str, contact_type: str
) -> dict:
    """Fallback: find contact via web search + AI extraction."""
    from modules.mod08_search import search

    if contact_type == "rmc":
        query = f"{company_name} supply chain manager network manager contact email"
    else:
        query = f"{company_name} logistics operations manager contact email LinkedIn"

    results = search(query, num_results=5)
    if not results:
        return _empty_contact()

    results_text = "\n".join([f"- {r.title}: {r.snippet}" for r in results[:5]])

    if contact_type == "rmc":
        role_hint = "supply chain manager or network manager"
    else:
        role_hint = "logistics, operations, or owner-level contact"

    prompt = f"""Extract contact information for a {role_hint} at {company_name} from these search results.

{results_text}

Respond with ONLY a JSON object:
{{"full_name": "Jane Smith", "title": "Supply Chain Manager", "email": "jane@company.com", "phone": "+1-555-0000"}}

If you cannot find a real contact, return:
{{"full_name": "", "title": "", "email": "", "phone": ""}}"""

    try:
        import anthropic
        import config
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        text = message.content[0].text.strip()
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"```$", "", text).strip()
        data = json.loads(text)
        data["enrichment_source"] = "web_search"
        data["enrichment_raw"] = None
        return data
    except Exception as e:
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
