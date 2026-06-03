"""Standalone scheduler process for TMS Lead Gen Engine.

Runs as a Render Background Worker (see render.yaml).
Executes the follow-up scheduler once per day, then sleeps.
"""

from __future__ import annotations
import time
import logging
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SCHEDULER] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

_RUN_INTERVAL_SECONDS = 24 * 60 * 60  # 24 hours


def main():
    log.info("TMS Lead Gen Scheduler starting.")

    # Ensure DB tables exist
    from models import create_all_tables
    create_all_tables()
    log.info("Database tables verified.")

    while True:
        log.info("Running follow-up scheduler...")
        try:
            from modules.mod06_scheduler import run_scheduler
            result = run_scheduler()
            log.info(
                f"Scheduler complete — "
                f"D2 drafts: {result.d2_drafts_created}, "
                f"D5 drafts: {result.d5_drafts_created}, "
                f"Replies detected: {result.replies_detected}, "
                f"Call required notifications: {result.call_required_sent}, "
                f"Errors: {len(result.errors)}"
            )
            if result.errors:
                for err in result.errors:
                    log.error(f"  - {err}")
        except Exception as e:
            log.error(f"Scheduler run failed: {e}", exc_info=True)

        log.info(f"Sleeping {_RUN_INTERVAL_SECONDS // 3600} hours until next run.")
        time.sleep(_RUN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
