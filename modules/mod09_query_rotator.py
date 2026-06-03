"""MOD-09: Query Rotator

Manages the search query bank and ensures rotation across runs
to avoid duplicate results. Selects 8 queries per run, avoiding
any used in the previous 3 runs.
"""

from __future__ import annotations
import json
import random
from pathlib import Path
from db import get_db
from models import DiscoveryRun

_QUERY_BANK_PATH = Path(__file__).resolve().parent.parent / "query_bank.json"
_QUERIES_PER_RUN = 8
_AVOID_LAST_N_RUNS = 3


def get_queries_for_run() -> list[str]:
    """Return a set of query strings for the current run.

    Avoids queries used in the previous 3 runs.
    Substitutes current year into {year} placeholders.
    """
    import datetime
    year = str(datetime.datetime.now().year)

    # Load query bank
    bank = _load_query_bank()
    all_queries = {q["id"]: q["template"].replace("{year}", year) for q in bank}

    # Get recently used query IDs
    recently_used = _get_recently_used_ids()

    # Filter out recently used
    available = {k: v for k, v in all_queries.items() if k not in recently_used}

    # If we've used too many, reset (use all)
    if len(available) < _QUERIES_PER_RUN:
        available = all_queries

    # Select a balanced mix: MX_to_US, US_to_MX, BOTH
    selected = _select_balanced(bank, available, _QUERIES_PER_RUN, year)
    return selected


def _load_query_bank() -> list[dict]:
    """Load query templates from query_bank.json."""
    if _QUERY_BANK_PATH.exists():
        data = json.loads(_QUERY_BANK_PATH.read_text(encoding="utf-8"))
        return data.get("queries", [])
    return []


def _get_recently_used_ids() -> set[str]:
    """Get query IDs used in the last N runs."""
    used = set()
    try:
        with get_db() as db:
            recent_runs = (
                db.query(DiscoveryRun)
                .filter(DiscoveryRun.search_queries_used.isnot(None))
                .order_by(DiscoveryRun.started_at.desc())
                .limit(_AVOID_LAST_N_RUNS)
                .all()
            )
            for run in recent_runs:
                if run.search_queries_used:
                    for item in run.search_queries_used:
                        if isinstance(item, dict):
                            used.add(item.get("id", ""))
    except Exception:
        pass
    return used


def _select_balanced(bank: list[dict], available: dict, n: int, year: str) -> list[str]:
    """Select a balanced mix of queries across directions."""
    mx_to_us = [q for q in bank if q["id"] in available and q.get("direction") == "MX_to_US"]
    us_to_mx = [q for q in bank if q["id"] in available and q.get("direction") == "US_to_MX"]
    both = [q for q in bank if q["id"] in available and q.get("direction") == "BOTH"]

    selected_ids = []

    # Aim for: ~4 MX_to_US, ~3 US_to_MX, ~1 BOTH
    pools = [
        (mx_to_us, min(4, len(mx_to_us))),
        (us_to_mx, min(3, len(us_to_mx))),
        (both, min(1, len(both))),
    ]

    for pool, count in pools:
        chosen = random.sample(pool, min(count, len(pool)))
        selected_ids.extend([q["id"] for q in chosen])

    # Fill remaining slots if needed
    remaining = n - len(selected_ids)
    if remaining > 0:
        leftover = [q for q in bank if q["id"] in available and q["id"] not in selected_ids]
        extra = random.sample(leftover, min(remaining, len(leftover)))
        selected_ids.extend([q["id"] for q in extra])

    # Build final query strings
    id_to_template = {q["id"]: q["template"] for q in bank}
    return [id_to_template[qid].replace("{year}", year) for qid in selected_ids[:n]]
