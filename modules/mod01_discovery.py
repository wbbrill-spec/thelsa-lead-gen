"""MOD-01: Discovery Runner

Executes web searches for cross-border expansion signals and returns
raw company candidates. Does not write to DB — returns raw data only.
Deduplicates candidates within the same run before passing downstream.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from modules.mod08_search import search, SearchResult
from modules.mod09_query_rotator import get_queries_for_run


@dataclass
class RawCandidate:
    name: str
    domain: str
    country_of_origin: str        # 'MX' or 'US'
    expansion_direction: str      # 'MX_to_US' or 'US_to_MX'
    industry: str
    source_url: str
    source_snippet: str
    search_query: str = ""


def run_discovery(run_id: int) -> list[RawCandidate]:
    """Run discovery searches and return raw candidates.

    Args:
        run_id: The ID of the current DiscoveryRun record

    Returns:
        List of RawCandidate objects, deduplicated by domain within this run
    """
    query_pairs = get_queries_for_run()  # list of (id, query_string)
    all_results: list[SearchResult] = []

    for qid, query in query_pairs:
        results = search(query, num_results=10, recency_days=180)
        for r in results:
            r._query = query  # tag with originating query
        all_results.extend(results)

    # Update discovery run with queries used (store IDs for proper rotation)
    _update_run_queries(run_id, query_pairs)

    # Extract candidates from search results
    candidates = _extract_candidates(all_results)

    # Deduplicate within this run by domain
    seen_domains: set[str] = set()
    unique_candidates: list[RawCandidate] = []
    for c in candidates:
        if c.domain and c.domain not in seen_domains:
            seen_domains.add(c.domain)
            unique_candidates.append(c)

    return unique_candidates


def _extract_candidates(results: list[SearchResult]) -> list[RawCandidate]:
    """Use Claude to extract company candidates from search results."""
    if not results:
        return []

    # Deduplicate results by URL (same article can appear from multiple queries)
    seen_urls: set[str] = set()
    unique_results: list[SearchResult] = []
    for r in results:
        if r.url and r.url not in seen_urls:
            seen_urls.add(r.url)
            unique_results.append(r)

    # Build a compact summary of results for Claude (up to 50 unique results)
    results_text = "\n\n".join([
        f"RESULT {i+1}:\nTitle: {r.title}\nURL: {r.url}\nSnippet: {r.snippet}\nDate: {r.date}"
        for i, r in enumerate(unique_results[:50])
    ])

    prompt = f"""You are analyzing web search results to find companies with active US-Mexico cross-border expansion.

SEARCH RESULTS:
{results_text}

For each company you identify with clear cross-border expansion signals, extract:
- company_name: the company name
- domain: the company's website domain (e.g. company.com) — infer from context if not explicit
- country_of_origin: MX (Mexican company) or US (American company)
- expansion_direction: MX_to_US or US_to_MX
- industry: one of: manufacturing, logistics, food_distribution, staffing, construction, other
- source_url: the URL this was found at
- source_snippet: a brief excerpt (max 100 words) describing the expansion

Include companies with clear cross-border expansion signals. Qualifying signals:
- Opening a facility, office, warehouse, plant, or distribution center in the other country
- Significant hiring or employee transfers for physical operations in the other country
- Nearshoring or manufacturing relocation projects establishing physical presence
- Acquiring or partnering to create physical operations in the other country
- Registering a subsidiary or legal entity to operate physically in the other country

Do NOT include: general industry/trade trend articles with no specific company, financial-only operations (no physical footprint), or import/export trade without physical cross-border operations.

Respond with ONLY a JSON array. No preamble, no markdown, no explanation.
Example format:
[
  {{
    "company_name": "Acme Mexico SA",
    "domain": "acmemexico.com",
    "country_of_origin": "MX",
    "expansion_direction": "MX_to_US",
    "industry": "manufacturing",
    "source_url": "https://example.com/article",
    "source_snippet": "Acme Mexico announced the opening of a new warehouse in Laredo, Texas..."
  }}
]

If no qualifying companies are found, return an empty array: []"""

    try:
        import anthropic
        import config
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}]
        )
        response_text = message.content[0].text.strip()

        # Clean any accidental markdown fences
        response_text = re.sub(r"^```json\s*", "", response_text)
        response_text = re.sub(r"```$", "", response_text).strip()

        data = __import__("json").loads(response_text)

        candidates = []
        for item in data:
            if not item.get("company_name") or not item.get("domain"):
                continue
            candidates.append(RawCandidate(
                name=item["company_name"],
                domain=item["domain"].lower().strip(),
                country_of_origin=item.get("country_of_origin", "MX"),
                expansion_direction=item.get("expansion_direction", "MX_to_US"),
                industry=item.get("industry", "other"),
                source_url=item.get("source_url", ""),
                source_snippet=item.get("source_snippet", ""),
            ))
        return candidates

    except Exception as e:
        # Log error but don't crash the run
        print(f"[MOD-01] Extraction error: {e}")
        return []


def _update_run_queries(run_id: int, query_pairs: list[tuple[str, str]]):
    """Store the queries used in this run (with IDs for proper rotation)."""
    try:
        from db import get_db
        from models import DiscoveryRun
        with get_db() as db:
            run = db.query(DiscoveryRun).filter_by(id=run_id).first()
            if run:
                run.search_queries_used = [{"id": qid, "query": q} for qid, q in query_pairs]
                run.companies_discovered = 0  # will be updated by MOD-02
    except Exception as e:
        print(f"[MOD-01] Failed to update run queries: {e}")
