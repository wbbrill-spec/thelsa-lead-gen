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

import json
import logging
import re
import time
from typing import Optional, List
from urllib.parse import quote_plus

import anthropic
import requests
from bs4 import BeautifulSoup

from engine.config import (
    HUNTER_API_KEY,
    APOLLO_API_KEY,
    MAX_ENRICHMENT_LOOKUPS_PER_RUN,
    ANTHROPIC_API_KEY,
    SCREENING_MODEL,
)
from engine.database import (
    upsert_contact,
    get_counter,
    increment_counter,
)

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

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


# ── Free web-search enrichment (no API key required) ─────────────────────────

_CONTACT_SYSTEM = """You are a corporate contact research analyst.
You will receive web search snippets about a company and its HR / Global Mobility team.
Extract the best available contact and respond ONLY with valid JSON:

{
  "found": true | false,
  "first_name": "<first name>" | null,
  "last_name": "<last name>" | null,
  "title": "<job title>" | null,
  "email": "<email address if explicitly visible in snippets>" | null,
  "email_pattern": "<guessed pattern: firstname.lastname | firstnamelastname | f.lastname | firstname>" | null,
  "linkedin_url": "<linkedin profile URL if visible>" | null,
  "confidence": "high" | "medium" | "low"
}

Rules:
- Prefer HR Director, Global Mobility Manager, Relocation Manager, VP HR titles.
- Only set email if you can see it explicitly in the snippets — do not invent emails.
- Set email_pattern to the most common pattern you can infer from any visible email addresses at this domain, or default to "firstname.lastname".
- confidence = high if you have name + title from a credible source (LinkedIn, company site).
- confidence = low if you are guessing from indirect evidence."""


def _ddg_search(query: str, num_results: int = 5) -> str:
    """Fetch DuckDuckGo HTML results and return combined snippet text."""
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        resp = requests.get(url, headers=_HEADERS, timeout=10)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        snippets = []
        for r in soup.select(".result__snippet")[:num_results]:
            text = r.get_text(separator=" ", strip=True)
            if text:
                snippets.append(text)
        time.sleep(1.5)
        return "\n\n".join(snippets)[:2500]
    except Exception as exc:
        logger.warning(f"DuckDuckGo search failed: {exc}")
        return ""


def lookup_web_free(company_name: str, domain: Optional[str] = None) -> List[dict]:
    """
    Find HR / Global Mobility contacts using free DuckDuckGo search + Claude Haiku.
    No Hunter or Apollo API key required.  Less precise than paid APIs but always available.
    """
    logger.info(f"  Web-search enrichment for: {company_name}")

    queries = [
        f'site:linkedin.com "{company_name}" "HR Director" OR "Global Mobility" OR "Relocation Manager"',
        f'"{company_name}" "HR Director" OR "Human Resources Director" email contact',
        f'"{company_name}" "Global Mobility" OR "Relocation Manager" contact',
    ]
    if domain:
        queries.append(f'site:{domain} contact OR team OR "human resources" OR "global mobility"')

    all_snippets: list[str] = []
    for q in queries[:3]:          # cap at 3 searches to stay polite
        text = _ddg_search(q)
        if text:
            all_snippets.append(text)

    if not all_snippets:
        logger.info(f"  No web results found for {company_name}")
        return []

    combined = "\n\n---\n\n".join(all_snippets)[:4000]
    prompt = f"Company: {company_name}\nDomain: {domain or 'unknown'}\n\nSearch snippets:\n{combined}"

    try:
        response = _client.messages.create(
            model=SCREENING_MODEL,
            max_tokens=400,
            system=_CONTACT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
    except Exception as exc:
        logger.warning(f"  Web enrichment Claude call failed: {exc}")
        return []

    if not data.get("found") or not data.get("first_name"):
        logger.info(f"  Web enrichment: no usable contact found for {company_name}")
        return []

    first  = (data.get("first_name") or "").strip()
    last   = (data.get("last_name")  or "").strip()
    title  = data.get("title")
    email  = data.get("email")

    # If no email visible, construct one from the guessed pattern + domain
    if not email and domain and first:
        pattern = data.get("email_pattern") or "firstname.lastname"
        if pattern == "firstname.lastname" and last:
            email = f"{first.lower()}.{last.lower()}@{domain}"
        elif pattern == "firstnamelastname" and last:
            email = f"{first.lower()}{last.lower()}@{domain}"
        elif pattern == "f.lastname" and last:
            email = f"{first[0].lower()}.{last.lower()}@{domain}"
        elif pattern == "firstname":
            email = f"{first.lower()}@{domain}"
        elif last:
            email = f"{first.lower()}.{last.lower()}@{domain}"
        else:
            email = f"hr@{domain}"

    if not email:
        logger.info(f"  Web enrichment: could not construct email for {company_name}")
        return []

    confidence = data.get("confidence", "low")
    logger.info(
        f"  Web enrichment found: {first} {last} <{email}> — {title} "
        f"(confidence: {confidence})"
    )

    return [{
        "first_name":        first or None,
        "last_name":         last or None,
        "email":             email,
        "title":             title,
        "target_function":   _infer_target_function(title or ""),
        "linkedin_url":      data.get("linkedin_url"),
        "email_verified":    False,   # not verified — constructed or extracted from web
        "enrichment_source": "web_search",
        "is_target_title":   True,
        "confidence":        50 if confidence == "medium" else (30 if confidence == "low" else 70),
    }]


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

    # Free web-search fallback — always runs when paid APIs return nothing
    if not contacts_found:
        contacts_found = lookup_web_free(company_name, domain)

    if not contacts_found:
        logger.info(f"No contacts found for {company_name} via any method.")
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
