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
    queries = get_queries_for_run()
    all_results: list[SearchResult] = []

    for query in queries:
        results = search(query, num_results=10, recency_days=30)
        for r in results:
            r._query = query  # tag with originating query
        all_results.extend(results)

    # Update discovery run with queries used
    _update_run_queries(run_id, queries)

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

    # Build a compact summary of results for Claude
    results_text = "\n\n".join([
        f"RESULT {i+1}:\nTitle: {r.title}\nURL: {r.url}\nSnippet: {r.snippet}\nDate: {r.date}"
        for i, r in enumerate(results[:20])
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

Only include companies with CLEAR, SPECIFIC expansion signals (opening a facility, new office, warehouse, hiring in the other country). Do not include vague or speculative mentions.

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
            max_tokens=2000,
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


def _update_run_queries(run_id: int, queries: list[str]):
    """Store the queries used in this run."""
    try:
        from db import get_db
        from models import DiscoveryRun
        with get_db() as db:
            run = db.query(DiscoveryRun).filter_by(id=run_id).first()
            if run:
                run.search_queries_used = [{"query": q} for q in queries]
                run.companies_discovered = 0  # will be updated by MOD-02
    except Exception as e:
        print(f"[MOD-01] Failed to update run queries: {e}")
