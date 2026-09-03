"""MOD-03: Qualification Scorer

Scores each net-new company 1-10.
Only companies scoring >= 7 proceed downstream.
Uses Claude to score and provide reasoning.
"""

from __future__ import annotations
from dataclasses import dataclass
from modules.mod01_discovery import RawCandidate
from modules.llm import ask_json, LLMError

# Last-run stats, read by the pipeline runner for the run summary.
STATS = {"errors": 0, "skipped_names": [], "last_error": ""}


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
    STATS["errors"] = 0
    STATS["skipped_names"] = []
    STATS["last_error"] = ""
    from pipeline import heartbeat

    for candidate in candidates:
        heartbeat()
        try:
            score, reasoning = _score_one(candidate)
        except LLMError as e:
            # Transient API failure: skip WITHOUT writing the company to the DB,
            # so it can be rediscovered and scored on the next run.
            STATS["errors"] += 1
            STATS["skipped_names"].append(candidate.name)
            STATS["last_error"] = str(e)
            print(f"[MOD-03] Scoring error for {candidate.name} — left for next run: {e}")
            continue
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

    if candidates and STATS["errors"] == len(candidates):
        raise LLMError(f"Scoring failed for every candidate ({STATS['errors']}); last error: {STATS['last_error']}")

    print(f"[MOD-03] qualified={len(qualified)} disqualified={disqualified_count} errors={STATS['errors']}")
    return qualified


def _score_one(candidate: RawCandidate) -> tuple[int, str]:
    """Score a single candidate using Claude. Raises LLMError on API/parse failure."""
    prompt = _SCORING_PROMPT.format(
        name=candidate.name,
        domain=candidate.domain,
        country_of_origin=candidate.country_of_origin,
        expansion_direction=candidate.expansion_direction,
        industry=candidate.industry,
        source_snippet=candidate.source_snippet[:500],
    )
    data = ask_json(prompt, max_tokens=500, expect="object", tag="MOD-03")
    try:
        score = max(1, min(10, int(data.get("score", 1))))
    except (TypeError, ValueError) as e:
        raise LLMError(f"score was not a number: {data.get('score')!r}") from e
    reasoning = str(data.get("reasoning", ""))
    return score, reasoning


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
