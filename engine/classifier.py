"""
classifier.py — AI lead qualification using Claude Haiku (cheap screening tier).
Only leads that score >= QUALIFICATION_SCORE_THRESHOLD are escalated to drafting.
This two-tier design keeps Claude API costs low.

Identifies cross-border expansion signals in BOTH directions:
  - US/Canada companies expanding to Mexico
  - Mexico companies expanding to US/Canada

SMB focus: deliberately down-scores Fortune 500 and large multinationals.
Target company size: 50–5,000 employees, under $500M revenue.
"""

import json
import logging
from typing import Optional, List

import anthropic

from engine.config import (
    ANTHROPIC_API_KEY,
    SCREENING_MODEL,
    QUALIFICATION_SCORE_THRESHOLD,
    MAX_AI_CALLS_PER_DAY,
    TARGET_INDUSTRIES,
)
from engine.database import (
    upsert_company,
    update_company_qualification,
    get_counter,
    increment_counter,
    get_db,
)

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Qualification prompt ──────────────────────────────────────────────────────

QUALIFY_SYSTEM = """You are a lead qualification assistant for TMS Corp, a company
that introduces Thelsa — a 30+ year certified corporate relocation and immigration
services firm — to companies undergoing cross-border expansion between the US/Canada
and Mexico.

Your job is to analyse a news article or business filing snippet and determine:
1. Whether it describes a REAL company that is expanding, opening, or establishing
   operations across the US-Mexico or Canada-Mexico border.
2. The DIRECTION of the expansion:
   - "to_mexico": a US or Canadian company expanding into Mexico
   - "to_us_canada": a Mexican company expanding into the US or Canada
   - "unknown": direction cannot be determined
3. Whether the company is SMB-sized (50–5,000 employees, under $500M revenue).
   IMPORTANT: Large multinationals and Fortune 500 companies are NOT good leads
   because they already have established relocation vendors. Score them 0–3.

Score the lead 0–10:
  0-3:  Not relevant OR large multinational/Fortune 500 (already has vendors),
        wrong geography, purely domestic, or speculative/rumour
  4-5:  Possible but weak signal (vague mention, no confirmed cross-border move,
        or company size is unclear/possibly large)
  6-7:  Clear SMB cross-border expansion signal — company is clearly SMB-sized
        and is opening offices, facilities, or relocating employees cross-border
  8-10: Confirmed SMB with active cross-border expansion — entity registrations,
        lease signings, hiring announcements, or employee relocation confirmed

Also identify:
- company_name: best-guess company name from the text
- domain: company website domain if mentioned (else null)
- industry: one of the target industries if identifiable (else "unknown")
- expansion_direction: "to_mexico" | "to_us_canada" | "unknown"
- expansion_stage: "announced" | "in_progress" | "operational" | "unknown"
- estimated_size: "small" | "medium" | "large" (large = Fortune 500 / major multinational)
- reason: 1-2 sentence explanation of your score

Respond ONLY with valid JSON matching this schema:
{
  "score": <int 0-10>,
  "company_name": <string or null>,
  "domain": <string or null>,
  "industry": <string>,
  "expansion_direction": <"to_mexico" | "to_us_canada" | "unknown">,
  "expansion_stage": <string>,
  "estimated_size": <"small" | "medium" | "large">,
  "reason": <string>
}"""


def qualify_signal(signal: dict) -> Optional[dict]:
    """
    Send one scraped signal to Claude Haiku for qualification.
    Returns parsed JSON result, or None if cap is hit or API fails.
    """
    # ── Daily cap check ──────────────────────────────────────────────────────
    current = get_counter("ai_calls")
    if current >= MAX_AI_CALLS_PER_DAY:
        logger.warning(f"AI call daily cap reached ({MAX_AI_CALLS_PER_DAY}). Queuing signal.")
        return None

    snippet = f"""
Source: {signal.get('source_name', 'Unknown')}
Title: {signal.get('title', '')}
Content: {signal.get('snippet', '')}
URL: {signal.get('url', '')}
"""

    try:
        response = client.messages.create(
            model=SCREENING_MODEL,
            max_tokens=500,
            system=QUALIFY_SYSTEM,
            messages=[{"role": "user", "content": snippet}],
        )
        increment_counter("ai_calls")
        text = response.content[0].text.strip()

        # Strip markdown code fences if model adds them
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        result = json.loads(text)
        result["source_url"]  = signal.get("url", "")
        result["source_name"] = signal.get("source_name", "")
        result["raw_snippet"] = signal.get("snippet", "")[:500]
        return result

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error from classifier: {e}")
        return None
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error: {e}")
        return None


def process_signals(signals: List[dict]) -> List[int]:
    """
    Qualify a list of scraped signals and save qualifying companies to the DB.
    Returns list of company IDs that passed qualification.
    """
    qualified_ids = []

    for signal in signals:
        result = qualify_signal(signal)
        if not result:
            continue

        score               = result.get("score", 0)
        company_name        = result.get("company_name")
        domain              = result.get("domain")
        industry            = result.get("industry", "unknown")
        reason              = result.get("reason", "")
        expansion_direction = result.get("expansion_direction", "unknown")
        source_url          = result.get("source_url", "")
        source_name         = result.get("source_name", "")
        raw_snippet         = result.get("raw_snippet", "")

        if not company_name:
            logger.debug("No company name extracted — skipping signal.")
            continue

        logger.info(f"Score {score}/10: {company_name} [{expansion_direction}] — {reason[:60]}")

        estimated_size = result.get("estimated_size", "small")
        is_large = estimated_size == "large"

        # Large multinationals get their own tier regardless of score —
        # we'll scan for their RMC partner and draft accordingly.
        if is_large:
            logger.info(f"  Large multinational detected: {company_name} — routing to RMC scan.")
            company_id = upsert_company(
                name=company_name,
                domain=domain,
                industry=industry,
                description=result.get("expansion_stage"),
                expansion_direction=expansion_direction,
                source_url=source_url,
                source_name=source_name,
                raw_snippet=raw_snippet,
            )
            if company_id:
                update_company_qualification(company_id, score, reason, status="large_multinational")
                # Tag the tier
                with get_db() as conn:
                    conn.execute(
                        "UPDATE companies SET company_tier='large_multinational' WHERE id=?",
                        (company_id,)
                    )
            continue

        if score < QUALIFICATION_SCORE_THRESHOLD:
            logger.debug(f"  Below threshold ({QUALIFICATION_SCORE_THRESHOLD}). Skipping.")
            # Still save to DB as disqualified to prevent re-processing
            company_id = upsert_company(
                name=company_name,
                domain=domain,
                industry=industry,
                description=result.get("expansion_stage"),
                expansion_direction=expansion_direction,
                source_url=source_url,
                source_name=source_name,
                raw_snippet=raw_snippet,
            )
            if company_id:
                update_company_qualification(company_id, score, reason, status="disqualified")
            continue

        # Passed threshold — save as qualified SMB
        company_id = upsert_company(
            name=company_name,
            domain=domain,
            industry=industry,
            description=result.get("expansion_stage"),
            expansion_direction=expansion_direction,
            source_url=source_url,
            source_name=source_name,
            raw_snippet=raw_snippet,
        )

        if company_id:
            update_company_qualification(company_id, score, reason, status="qualified")
            qualified_ids.append(company_id)
            logger.info(f"  Qualified SMB saved: {company_name} (ID {company_id})")
        else:
            logger.debug(f"  Duplicate — {company_name} already in database.")

    logger.info(f"Qualification complete: {len(qualified_ids)} new leads qualified.")
    return qualified_ids


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Quick test with a synthetic signal
    test_signal = {
        "title": "Dallas staffing firm Apex Workforce registers entity in Monterrey to serve maquiladoras",
        "snippet": (
            "Apex Workforce Solutions, a Dallas-based staffing company with around 300 employees, "
            "has registered a legal entity in Monterrey, Nuevo Leon, as part of its plan to place "
            "skilled workers at maquiladora facilities in the Monterrey industrial corridor. The "
            "company, which serves manufacturing clients across Texas, expects to place 50-100 "
            "workers in Mexico within its first year of operations there."
        ),
        "url": "https://example.com/apex-workforce-monterrey",
        "source_name": "Test",
    }
    result = qualify_signal(test_signal)
    print(json.dumps(result, indent=2))
