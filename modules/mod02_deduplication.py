"""MOD-02: Deduplication Gate

Filters out any company already in the companies table.
Domain is the deduplication key.
Updates discovery_run counters.
"""

from __future__ import annotations
from modules.mod01_discovery import RawCandidate
from db import get_db
from models import Company, DiscoveryRun


def deduplicate(candidates: list[RawCandidate], run_id: int = None) -> list[RawCandidate]:
    """Filter candidates against existing companies table.

    Args:
        candidates: Raw candidates from MOD-01
        run_id: Optional DiscoveryRun ID for counter updates

    Returns:
        Net-new candidates only (domain not in companies table)
    """
    if not candidates:
        return []

    domains = [c.domain for c in candidates if c.domain]

    with get_db() as db:
        # Fetch all existing domains in one query
        existing_domains = set(
            row[0] for row in
            db.query(Company.domain).filter(Company.domain.in_(domains)).all()
        )

        net_new = [c for c in candidates if c.domain not in existing_domains]
        skipped = len(candidates) - len(net_new)

        # Update run counters
        if run_id:
            run = db.query(DiscoveryRun).filter_by(id=run_id).first()
            if run:
                run.companies_discovered = len(candidates)
                run.companies_skipped_dupe = skipped

    return net_new
