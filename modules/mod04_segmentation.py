"""MOD-04: Segmentation & RMC Detector

Classifies each qualified company as SMB (<$100M) or LARGE_CORP ($100M+).
For LARGE_CORP, searches for RMC (Relocation Management Company) relationships.
If no RMC found for LARGE_CORP, reverts to SMB flow.
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass
from modules.mod03_scorer import ScoredCandidate
from modules.mod08_search import search


@dataclass
class SegmentedCandidate:
    scored: ScoredCandidate
    size_tier: str        # 'SMB' or 'LARGE_CORP'
    rmc_detected: bool
    rmc_name: str         # RMC name if detected, else ""
    effective_flow: str   # 'SMB' or 'RMC' — drives downstream logic


# Known major RMC companies to search for
_KNOWN_RMCS = [
    "SIRVA", "Cartus", "Crown World Mobility", "Graebel",
    "Santa Fe Relocation", "Weichert Workforce Mobility",
    "Mercer", "Brookfield Global Relocation", "AIReS",
    "NEI Global Relocation", "Altair Global", "Odyssey One Source",
]


def segment_and_detect_rmc(candidates: list[ScoredCandidate]) -> list[SegmentedCandidate]:
    """Segment candidates by size and detect RMC relationships for large corps.

    Args:
        candidates: Qualified scored candidates from MOD-03

    Returns:
        List of SegmentedCandidate with size_tier and RMC data populated
    """
    results = []
    for candidate in candidates:
        segmented = _process_one(candidate)
        results.append(segmented)
    return results


def _process_one(candidate: ScoredCandidate) -> SegmentedCandidate:
    """Classify size and detect RMC for a single candidate."""
    size_tier = _classify_size(candidate)

    if size_tier == "LARGE_CORP":
        rmc_detected, rmc_name = _detect_rmc(candidate)
        if rmc_detected:
            return SegmentedCandidate(
                scored=candidate,
                size_tier="LARGE_CORP",
                rmc_detected=True,
                rmc_name=rmc_name,
                effective_flow="RMC",
            )
        else:
            # No RMC found — revert to SMB flow
            return SegmentedCandidate(
                scored=candidate,
                size_tier="LARGE_CORP",
                rmc_detected=False,
                rmc_name="",
                effective_flow="SMB",
            )
    else:
        return SegmentedCandidate(
            scored=candidate,
            size_tier="SMB",
            rmc_detected=False,
            rmc_name="",
            effective_flow="SMB",
        )


def _classify_size(candidate: ScoredCandidate) -> str:
    """Classify company as SMB or LARGE_CORP using Claude."""
    c = candidate.candidate
    prompt = f"""Classify this company's estimated annual revenue as SMB (under $100M USD) or LARGE_CORP ($100M+ USD).

Company: {c.name}
Domain: {c.domain}
Industry: {c.industry}
Snippet: {c.source_snippet[:300]}

Use your knowledge of the company if you recognize it. If unknown, infer from industry, context clues, and expansion scope.

Respond with ONLY a JSON object:
{{"tier": "SMB", "reasoning": "Small regional manufacturer, no revenue signals"}}
or
{{"tier": "LARGE_CORP", "reasoning": "Recognized Fortune 500 manufacturer with $2B revenue"}}"""

    try:
        import anthropic
        import config
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        text = message.content[0].text.strip()
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"```$", "", text).strip()
        data = json.loads(text)
        return data.get("tier", "SMB")
    except Exception as e:
        print(f"[MOD-04] Size classification error for {c.name}: {e}")
        return "SMB"  # Safe default


def _detect_rmc(candidate: ScoredCandidate) -> tuple[bool, str]:
    """Search for RMC relationship for a large corporation."""
    c = candidate.candidate
    query = f'"{c.name}" relocation management company RMC corporate relocation'
    results = search(query, num_results=5, recency_days=365)

    if not results:
        return False, ""

    # Build context for Claude to analyze
    results_text = "\n".join([
        f"- {r.title}: {r.snippet}"
        for r in results[:5]
    ])

    prompt = f"""Analyze these search results to determine if {c.name} works with a Relocation Management Company (RMC).

Known RMCs include: {', '.join(_KNOWN_RMCS)}

SEARCH RESULTS:
{results_text}

Does {c.name} have a confirmed relationship with an RMC?

Respond with ONLY a JSON object:
{{"rmc_found": true, "rmc_name": "SIRVA", "confidence": "high"}}
or
{{"rmc_found": false, "rmc_name": "", "confidence": "high"}}"""

    try:
        import anthropic
        import config
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        text = message.content[0].text.strip()
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"```$", "", text).strip()
        data = json.loads(text)
        rmc_found = data.get("rmc_found", False)
        rmc_name = data.get("rmc_name", "")
        return rmc_found, rmc_name
    except Exception as e:
        print(f"[MOD-04] RMC detection error for {c.name}: {e}")
        return False, ""
