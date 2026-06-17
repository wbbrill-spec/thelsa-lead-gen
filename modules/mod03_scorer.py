"""MOD-03: Qualification Scorer

Scores each net-new company 1-10.
Only companies scoring >= 7 proceed downstream.
Uses Claude to score and provide reasoning.
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass
from modules.mod01_discovery import RawCandidate


@dataclass
class ScoredCandidate:
    candidate: RawCandidate
    score: int
    reasoning: str


_SCORING_PROMPT = """You are a freight/logistics sales qualification expert for Thelsa, the largest relocation and moving company in Mexico (30+ years, household goods, personal effects, office moving, destination services, immigration).

Score this company as a sales prospect for Thelsa's cross-border Mexico-US services.

COMPANY DATA:
Name: {name}
Domain: {domain}
Country of origin: {country_of_origin}
Expansion direction: {expansion_direction}
Industry: {industry}
Source snippet: {source_snippet}

SCORING RUBRIC (1-10):
+3 Physical operations confirmed in BOTH countries (warehouses, employees, vehicles)
+2 High-value industry (manufacturing, logistics, food distribution, staffing, construction)
+2 Very recent expansion news (within 7 days)
+1 Recent expansion news (within 30 days)
+1 Company size signal available (any revenue or employee count mentioned)
+1 Strong, specific expansion footprint (named city, facility type, headcount)

Score 1-10. Only scores 7+ qualify.

Respond with ONLY a JSON object. No preamble, no markdown.
{{"score": 8, "reasoning": "Manufacturing company confirmed opening a warehouse in Laredo TX with 50 employees. Physical ops in both countries, target industry, specific details."}}"""


def score_candidates(candidates: list[RawCandidate], run_id: int = None) -> list[ScoredCandidate]:
    """Score each candidate and return only those scoring >= 7.

    Args:
        candidates: Net-new candidates from MOD-02
        run_id: Optional DiscoveryRun ID for counter updates

    Returns:
        Qualified candidates (score >= 7) as ScoredCandidate objects
    """
    if not candidates:
        return []

    qualified = []
    disqualified_count = 0

    for candidate in candidates:
        score, reasoning = _score_one(candidate)
        if score >= 7:
            qualified.append(ScoredCandidate(
                candidate=candidate,
                score=score,
                reasoning=reasoning,
            ))
        else:
            disqualified_count += 1
            # Log disqualified to DB
            _log_disqualified(candidate, score, reasoning)

    # Update run counters
    if run_id:
        _update_run_counters(run_id, len(qualified), disqualified_count)

    return qualified


def _score_one(candidate: RawCandidate) -> tuple[int, str]:
    """Score a single candidate using Claude."""
    try:
        import anthropic
        import config

        prompt = _SCORING_PROMPT.format(
            name=candidate.name,
            domain=candidate.domain,
            country_of_origin=candidate.country_of_origin,
            expansion_direction=candidate.expansion_direction,
            industry=candidate.industry,
            source_snippet=candidate.source_snippet[:500],
        )

        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )

        response_text = message.content[0].text.strip()
        response_text = re.sub(r"^```json\s*", "", response_text)
        response_text = re.sub(r"```$", "", response_text).strip()

        data = json.loads(response_text)
        score = max(1, min(10, int(data.get("score", 1))))
        reasoning = data.get("reasoning", "")
        return score, reasoning

    except Exception as e:
        print(f"[MOD-03] Scoring error for {candidate.name}: {e}")
        return 1, f"Scoring failed: {e}"


def _log_disqualified(candidate: RawCandidate, score: int, reasoning: str):
    """Log disqualified candidates to the companies table for deduplication."""
    try:
        from db import get_db
        from models import Company
        with get_db() as db:
            existing = db.query(Company).filter_by(domain=candidate.domain).first()
            if not existing:
                company = Company(
                    name=candidate.name,
                    domain=candidate.domain,
                    industry=candidate.industry,
                    country_of_origin=candidate.country_of_origin,
                    expansion_direction=candidate.expansion_direction,
                    source_url=candidate.source_url,
                    source_snippet=candidate.source_snippet,
                )
                db.add(company)
    except Exception as e:
        print(f"[MOD-03] Failed to log disqualified company: {e}")


def _update_run_counters(run_id: int, qualified: int, disqualified: int):
    try:
        from db import get_db
        from models import DiscoveryRun
        with get_db() as db:
            run = db.query(DiscoveryRun).filter_by(id=run_id).first()
            if run:
                run.leads_qualified = qualified
                run.leads_disqualified = disqualified
    except Exception as e:
        print(f"[MOD-03] Failed to update run counters: {e}")
