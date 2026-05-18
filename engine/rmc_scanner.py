"""
rmc_scanner.py — Identifies whether a large multinational company works with
a Relocation Management Company (RMC) for employee moves.

Strategy:
  1. Build targeted search queries combining the company name with known RMC names.
  2. Fetch the top search results (DuckDuckGo HTML search, no API key required).
  3. Pass the result snippets to Claude Haiku to identify any confirmed RMC relationship.
  4. Return the RMC name and domain if found, or None if no relationship detected.

Known RMCs tracked:
  Cartus, BGRS, Sirva, Weichert, Plus Relocation, Crown Relocations,
  Santa Fe Relocation, Graebel, AIReS, Altair Global, NEI Global Relocation,
  UniGroup (United Van Lines / Mayflower), Atlas Van Lines (corporate),
  Brookfield GRS, Executive Relocation, Paragon Relocation.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
import anthropic

from engine.config import ANTHROPIC_API_KEY, SCREENING_MODEL

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Known RMCs ────────────────────────────────────────────────────────────────

KNOWN_RMCS = [
    {"name": "Cartus",              "domain": "cartus.com"},
    {"name": "BGRS",                "domain": "bgrs.com"},
    {"name": "Sirva",               "domain": "sirva.com"},
    {"name": "Weichert Workforce Mobility", "domain": "weichertworkforcemobility.com"},
    {"name": "Plus Relocation",     "domain": "plusrelo.com"},
    {"name": "Crown Relocations",   "domain": "crownrelo.com"},
    {"name": "Santa Fe Relocation", "domain": "santaferelo.com"},
    {"name": "Graebel",             "domain": "graebel.com"},
    {"name": "AIReS",               "domain": "aires.com"},
    {"name": "Altair Global",       "domain": "altairglobal.com"},
    {"name": "NEI Global Relocation","domain": "neirelo.com"},
    {"name": "UniGroup",            "domain": "unigroup.com"},
    {"name": "Atlas Van Lines",     "domain": "atlasvanlines.com"},
    {"name": "Paragon Relocation",  "domain": "paragonrelocation.com"},
    {"name": "Brookfield GRS",      "domain": "brookfieldgrs.com"},
    {"name": "Executive Relocation","domain": "executive-relocation.com"},
]

RMC_NAMES_FOR_SEARCH = " OR ".join(
    f'"{r["name"]}"' for r in KNOWN_RMCS
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

_SCAN_SYSTEM = """You are a corporate relocation research analyst.
You will be given web search snippets about a large company and asked to determine
whether that company has a contracted Relocation Management Company (RMC) relationship.

RMCs are companies that manage corporate employee relocation programs on behalf of
large employers. Common RMCs include: Cartus, BGRS, Sirva, Weichert Workforce Mobility,
Plus Relocation, Crown Relocations, Santa Fe Relocation, Graebel, AIReS, Altair Global,
NEI Global Relocation, UniGroup, Atlas Van Lines, Paragon Relocation, Brookfield GRS.

Analyse the snippets and respond ONLY with valid JSON:
{
  "rmc_found": true | false,
  "rmc_name": "<exact RMC company name>" | null,
  "rmc_domain": "<domain.com>" | null,
  "confidence": "high" | "medium" | "low",
  "evidence": "<one sentence describing where the relationship was mentioned>"
}

If multiple RMCs appear, pick the one with the strongest evidence of a current contract.
If no RMC relationship is found, set rmc_found to false and the rest to null."""


# ── Public API ────────────────────────────────────────────────────────────────

def scan_for_rmc(company_name: str, domain: Optional[str] = None) -> dict:
    """
    Search the web to identify whether `company_name` works with an RMC.

    Returns a dict:
        {
            "rmc_found": bool,
            "rmc_name": str | None,
            "rmc_domain": str | None,
            "confidence": str | None,
            "evidence": str | None,
        }
    """
    snippets = _fetch_search_snippets(company_name)
    if not snippets:
        logger.info(f"  No web results found for {company_name} RMC scan.")
        return _no_rmc()

    result = _classify_with_claude(company_name, snippets)
    logger.info(
        f"  RMC scan for {company_name}: "
        f"{'FOUND ' + result['rmc_name'] if result['rmc_found'] else 'none found'} "
        f"(confidence: {result.get('confidence')})"
    )
    return result


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fetch_search_snippets(company_name: str) -> str:
    """
    Run two DuckDuckGo searches and return combined result text snippets:
      1. company + known RMC names
      2. company + "relocation management company" / "global mobility"
    Returns a single text block of up to ~2000 chars.
    """
    queries = [
        f'"{company_name}" relocation management company RMC Cartus BGRS Sirva Weichert',
        f'"{company_name}" global mobility employee relocation vendor partner',
    ]
    all_snippets: list[str] = []

    for q in queries:
        try:
            url = f"https://html.duckduckgo.com/html/?q={quote_plus(q)}"
            resp = requests.get(url, headers=_HEADERS, timeout=10)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for result in soup.select(".result__snippet")[:5]:
                text = result.get_text(separator=" ", strip=True)
                if text:
                    all_snippets.append(text)
            time.sleep(1.5)   # be polite between requests
        except Exception as e:
            logger.warning(f"  Search request failed: {e}")
            continue

    if not all_snippets:
        return ""

    combined = "\n\n".join(all_snippets)
    return combined[:3000]  # cap to keep token cost low


def _classify_with_claude(company_name: str, snippets: str) -> dict:
    """Ask Claude Haiku to identify an RMC from the search snippets."""
    prompt = (
        f"Company being researched: {company_name}\n\n"
        f"Web search result snippets:\n{snippets}"
    )
    try:
        response = client.messages.create(
            model=SCREENING_MODEL,
            max_tokens=300,
            system=_SCAN_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text)
        # Fill in domain from our known list if Claude didn't provide one
        if result.get("rmc_found") and result.get("rmc_name") and not result.get("rmc_domain"):
            for rmc in KNOWN_RMCS:
                if rmc["name"].lower() in result["rmc_name"].lower():
                    result["rmc_domain"] = rmc["domain"]
                    break
        return result
    except Exception as e:
        logger.error(f"  RMC Claude classification failed: {e}")
        return _no_rmc()


def _no_rmc() -> dict:
    return {
        "rmc_found": False,
        "rmc_name": None,
        "rmc_domain": None,
        "confidence": None,
        "evidence": None,
    }
