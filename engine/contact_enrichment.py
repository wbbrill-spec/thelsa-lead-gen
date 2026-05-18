"""
contact_enrichment.py — BYOAK (Bring Your Own API Key) contact discovery.
Supports Hunter.io and Apollo.io. The customer provides their own API key.
Falls back gracefully to manual entry when no key is configured.

Target functions for TMS Corp Lead Gen Engine:
  - HR Director, HR Manager, HR Business Partner
  - Global Mobility Manager, Mobility Specialist, Relocation Manager
  - Compensation & Benefits Manager, Total Rewards Manager
  - Procurement Manager, Vendor Manager, Supply Chain Manager
"""

import logging
import requests
from typing import Optional, List

from engine.config import (
    HUNTER_API_KEY,
    APOLLO_API_KEY,
    MAX_ENRICHMENT_LOOKUPS_PER_RUN,
)
from engine.database import (
    upsert_contact,
    get_counter,
    increment_counter,
)

logger = logging.getLogger(__name__)


# ── Target titles by function ─────────────────────────────────────────────────

HR_TITLES = [
    "hr director", "human resources director", "director of human resources",
    "hr manager", "human resources manager", "hr business partner",
    "hrbp", "chief people officer", "cpo", "vp human resources",
    "vp hr", "head of hr", "head of human resources",
    "people operations director", "people operations manager",
    "director de recursos humanos", "gerente de recursos humanos",
]

GLOBAL_MOBILITY_TITLES = [
    "global mobility manager", "global mobility director", "global mobility specialist",
    "relocation manager", "relocation specialist", "mobility specialist",
    "expatriate manager", "expat manager", "international hr manager",
    "international mobility", "director of global mobility",
    "gerente de movilidad global", "coordinador de reubicación",
]

COMP_BENEFITS_TITLES = [
    "compensation and benefits manager", "c&b manager", "total rewards manager",
    "director of compensation", "benefits manager", "compensation director",
    "total rewards director", "vp total rewards", "vp compensation",
    "gerente de compensaciones", "gerente de beneficios",
]

PROCUREMENT_TITLES = [
    "procurement manager", "vendor manager", "supply chain manager",
    "purchasing manager", "director of procurement", "vp procurement",
    "supply chain director", "vendor relations manager",
    "gerente de compras", "gerente de adquisiciones",
]

ALL_TARGET_TITLES = (
    HR_TITLES + GLOBAL_MOBILITY_TITLES + COMP_BENEFITS_TITLES + PROCUREMENT_TITLES
)


def _infer_target_function(title: str) -> str:
    """Infer the target_function value from a contact title."""
    if not title:
        return "hr"
    t = title.lower()
    if any(k in t for k in ["mobility", "relocation", "expat", "movilidad", "reubicación"]):
        return "global_mobility"
    if any(k in t for k in ["compensation", "benefits", "total rewards", "c&b",
                              "compensaciones", "beneficios"]):
        return "comp_benefits"
    if any(k in t for k in ["procurement", "purchasing", "vendor", "supply chain",
                              "compras", "adquisiciones"]):
        return "procurement"
    return "hr"


# ── Hunter.io ─────────────────────────────────────────────────────────────────

def lookup_hunter(domain: str, company_name: str) -> List[dict]:
    """
    Search Hunter.io for email addresses at a given domain.
    Requires HUNTER_API_KEY in .env (customer's own key).
    """
    if not HUNTER_API_KEY:
        logger.debug("Hunter.io API key not configured — skipping.")
        return []

    try:
        resp = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={
                "domain":   domain,
                "api_key":  HUNTER_API_KEY,
                "limit":    10,
                "type":     "personal",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        emails = data.get("emails", [])

        contacts = []
        for e in emails:
            title = (e.get("position") or "").lower()
            is_target = any(t in title for t in ALL_TARGET_TITLES)
            target_func = _infer_target_function(e.get("position", ""))
            contacts.append({
                "first_name":        e.get("first_name"),
                "last_name":         e.get("last_name"),
                "email":             e.get("value"),
                "title":             e.get("position"),
                "target_function":   target_func,
                "email_verified":    e.get("verification", {}).get("status") == "valid",
                "enrichment_source": "hunter",
                "is_target_title":   is_target,
                "confidence":        e.get("confidence", 0),
            })

        # Sort: target titles first, then by confidence
        contacts.sort(key=lambda x: (not x["is_target_title"], -x["confidence"]))
        return contacts

    except requests.RequestException as e:
        logger.warning(f"Hunter.io error for {domain}: {e}")
        return []


# ── Apollo.io ─────────────────────────────────────────────────────────────────

def lookup_apollo(company_name: str, domain: Optional[str] = None) -> List[dict]:
    """
    Search Apollo.io for HR/mobility decision-maker contacts at a company.
    Requires APOLLO_API_KEY in .env (customer's own key).
    """
    if not APOLLO_API_KEY:
        logger.debug("Apollo.io API key not configured — skipping.")
        return []

    try:
        payload = {
            "api_key":             APOLLO_API_KEY,
            "q_organization_name": company_name,
            "person_titles": [
                "HR Director", "HR Manager", "HR Business Partner",
                "Global Mobility Manager", "Global Mobility Director",
                "Relocation Manager", "Mobility Specialist",
                "Compensation and Benefits Manager", "Total Rewards Manager",
                "Procurement Manager", "Vendor Manager", "Supply Chain Manager",
                "Chief People Officer", "VP Human Resources",
            ],
            "per_page": 5,
        }
        if domain:
            payload["q_organization_domains"] = [domain]

        resp = requests.post(
            "https://api.apollo.io/v1/mixed_people/search",
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        people = resp.json().get("people", [])

        contacts = []
        for p in people:
            email = p.get("email")
            if not email:
                continue
            target_func = _infer_target_function(p.get("title", ""))
            contacts.append({
                "first_name":        p.get("first_name"),
                "last_name":         p.get("last_name"),
                "email":             email,
                "title":             p.get("title"),
                "target_function":   target_func,
                "linkedin_url":      p.get("linkedin_url"),
                "email_verified":    True,   # Apollo pre-verifies
                "enrichment_source": "apollo",
                "is_target_title":   True,   # We filtered by title in query
                "confidence":        100,
            })
        return contacts

    except requests.RequestException as e:
        logger.warning(f"Apollo.io error for {company_name}: {e}")
        return []


# ── Main enrichment runner ────────────────────────────────────────────────────

def enrich_company(company_id: int, company_name: str,
                   domain: Optional[str] = None) -> List[int]:
    """
    Try to find HR/mobility decision-maker contacts for a company.
    Returns list of contact IDs saved to the database.
    """
    # ── Per-run cap check ────────────────────────────────────────────────────
    current = get_counter("enrichment_lookups")
    if current >= MAX_ENRICHMENT_LOOKUPS_PER_RUN:
        logger.warning(f"Enrichment lookup cap reached ({MAX_ENRICHMENT_LOOKUPS_PER_RUN}/run).")
        return []

    contacts_found = []

    # Try Apollo first (returns pre-verified emails and LinkedIn)
    if APOLLO_API_KEY:
        contacts_found = lookup_apollo(company_name, domain)
        increment_counter("enrichment_lookups")
        logger.info(f"Apollo found {len(contacts_found)} contacts for {company_name}")

    # Fall back to Hunter if Apollo returned nothing or isn't configured
    if not contacts_found and HUNTER_API_KEY and domain:
        contacts_found = lookup_hunter(domain, company_name)
        increment_counter("enrichment_lookups")
        logger.info(f"Hunter found {len(contacts_found)} contacts for {domain}")

    if not contacts_found:
        logger.info(f"No contacts found via API for {company_name}. "
                    f"Manual entry required in the dashboard.")
        return []

    # Save top contact(s) to database
    saved_ids = []
    for c in contacts_found[:2]:  # save up to 2 contacts per company
        if not c.get("email"):
            continue
        contact_id = upsert_contact(
            company_id=company_id,
            email=c["email"],
            first_name=c.get("first_name"),
            last_name=c.get("last_name"),
            title=c.get("title"),
            target_function=c.get("target_function", "hr"),
            linkedin_url=c.get("linkedin_url"),
            email_verified=c.get("email_verified", False),
            enrichment_source=c.get("enrichment_source", "unknown"),
        )
        if contact_id:
            saved_ids.append(contact_id)
            logger.info(f"  Saved contact: {c.get('first_name')} {c.get('last_name')} "
                        f"<{c['email']}> ({c.get('title')}) [{c.get('target_function')}]")

    return saved_ids


def get_enrichment_status() -> dict:
    """Return which enrichment services are currently configured."""
    return {
        "hunter_configured": bool(HUNTER_API_KEY),
        "apollo_configured": bool(APOLLO_API_KEY),
        "mode": (
            "apollo+hunter" if (APOLLO_API_KEY and HUNTER_API_KEY) else
            "apollo_only"   if APOLLO_API_KEY else
            "hunter_only"   if HUNTER_API_KEY else
            "manual"
        ),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    status = get_enrichment_status()
    print(f"Enrichment mode: {status['mode']}")
    if status["mode"] == "manual":
        print("INFO  No enrichment API keys configured. Contacts must be entered manually.")
        print("   Add HUNTER_API_KEY or APOLLO_API_KEY to your .env file to enable automation.")
