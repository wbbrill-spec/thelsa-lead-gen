"""
main.py — Pipeline orchestrator.
Ties all modules together into one scheduled run:
  1. Scrape -> 2. Classify -> 3. Enrich -> 4. Draft -> 5. (Agent approves) -> 6. Send -> 7. Follow-up
"""

import logging
import sys
import uuid
from datetime import datetime
from typing import List

from engine.config import validate_config, LOG_DIR, LOG_LEVEL
from engine.database import (
    init_db, get_counter, get_qualified_companies, get_db,
    get_large_multinational_companies, update_company_rmc,
)
from engine.scraper import run_scraper
from engine.classifier import process_signals
from engine.contact_enrichment import enrich_company, get_enrichment_status
from engine.email_drafter import create_and_save_draft, create_and_save_large_company_draft
from engine.rmc_scanner import scan_for_rmc
from engine.tracker import run_followup_scheduler, print_pipeline_summary


# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"engine_{datetime.utcnow().strftime('%Y%m%d')}.log"
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(log_file)),
        ],
    )


logger = logging.getLogger(__name__)


# ── Pipeline steps ────────────────────────────────────────────────────────────

def step_scrape() -> List[dict]:
    logger.info("-- Step 1: Scraping intelligence sources --")
    signals = run_scraper()
    logger.info(f"   Found {len(signals)} relevant signals.")
    return signals


def step_classify(signals: List[dict]) -> List[int]:
    logger.info("-- Step 2: AI qualification (Claude Haiku) --")
    if not signals:
        logger.info("   No signals to classify.")
        return []
    qualified_ids = process_signals(signals)
    logger.info(f"   {len(qualified_ids)} new companies qualified.")
    return qualified_ids


def step_enrich_and_draft(company_ids: List[int] = None, pipeline_run_id: str = None):
    """
    For all qualified companies without contacts/drafts yet:
    enrich contact data, then create an email draft.
    """
    logger.info("-- Step 3 & 4: Contact enrichment + email drafting --")

    enrich_status = get_enrichment_status()
    logger.info(f"   Enrichment mode: {enrich_status['mode']}")

    # Get all qualified companies that don't yet have a pending draft or sent email
    with get_db() as conn:
        rows = conn.execute("""
            SELECT c.*
            FROM companies c
            WHERE c.status = 'qualified'
              AND NOT EXISTS (
                SELECT 1 FROM contacts ct
                JOIN emails e ON e.contact_id = ct.id
                WHERE ct.company_id = c.id
                  AND e.status IN ('pending_approval','approved','sent')
              )
            ORDER BY c.qualification_score DESC
            LIMIT 20
        """).fetchall()

    if not rows:
        logger.info("   No companies ready for enrichment/drafting.")
        return

    drafts_created = 0
    for company in rows:
        cid               = company["id"]
        cname             = company["name"]
        domain            = company["domain"]
        expansion_dir     = company["expansion_direction"] if "expansion_direction" in company.keys() else "unknown"

        logger.info(f"   Processing: {cname} (score {company['qualification_score']}/10, "
                    f"direction: {expansion_dir})")

        # Try API enrichment first
        contact_ids = enrich_company(cid, cname, domain)

        # If no contacts found via API, check for manually entered contacts
        if not contact_ids:
            with get_db() as conn:
                manual = conn.execute(
                    "SELECT id FROM contacts WHERE company_id=? AND do_not_contact=0",
                    (cid,)
                ).fetchall()
            contact_ids = [r["id"] for r in manual]

        if not contact_ids:
            logger.info(f"   No contacts for {cname} — will appear in dashboard for manual entry.")
            continue

        # Create a draft for the first available contact
        with get_db() as conn:
            contact = conn.execute(
                "SELECT * FROM contacts WHERE id=?", (contact_ids[0],)
            ).fetchone()

        if not contact:
            continue

        target_func = contact["target_function"] if "target_function" in contact.keys() else "hr"

        email_id = create_and_save_draft(
            contact_id=contact["id"],
            company_id=cid,
            company_name=cname,
            contact_first_name=contact["first_name"],
            contact_title=contact["title"],
            industry=company["industry"],
            expansion_stage=company["description"],
            source_snippet=company["raw_snippet"],
            expansion_direction=expansion_dir,
            target_function=target_func,
            sequence_num=1,
            pipeline_run_id=pipeline_run_id,
            contact_email=contact["email"],
        )

        if email_id:
            drafts_created += 1
            # Mark company as contacted
            with get_db() as conn:
                conn.execute(
                    "UPDATE companies SET status='contacted', updated_at=datetime('now') WHERE id=?",
                    (cid,)
                )

    logger.info(f"   {drafts_created} email drafts created and queued for approval.")


def step_process_large_multinationals(pipeline_run_id: str = None):
    """
    For large multinational companies identified this run:
      1. Scan for an RMC partner via DuckDuckGo + Claude Haiku.
      2. If found: enrich contacts at the RMC and draft to their supply chain head.
      3. If not found: enrich contacts at the company targeting HR/Global Mobility
         and draft to that person.
    """
    logger.info("-- Step 3b: Large multinational RMC scan + drafting --")

    companies = get_large_multinational_companies(limit=20)
    if not companies:
        logger.info("   No large multinational companies to process.")
        return

    enrich_status = get_enrichment_status()
    drafts_created = 0

    for company in companies:
        cid    = company["id"]
        cname  = company["name"]
        domain = company["domain"]
        exp_dir = company["expansion_direction"] if "expansion_direction" in company.keys() else "unknown"

        logger.info(f"   Large multinational: {cname} — scanning for RMC partner...")

        # ── 1. RMC scan ───────────────────────────────────────────────────────
        rmc_result = scan_for_rmc(cname, domain)
        rmc_name   = rmc_result.get("rmc_name")
        rmc_domain = rmc_result.get("rmc_domain")
        update_company_rmc(cid, rmc_name, rmc_domain)

        # ── 2. Contact enrichment ─────────────────────────────────────────────
        if rmc_name and rmc_domain:
            # Target the RMC's supply chain / vendor relations team
            logger.info(f"   RMC found: {rmc_name} — enriching RMC contacts.")
            contact_ids = enrich_company(cid, rmc_name, rmc_domain)
        else:
            # No RMC — target the company's HR / Global Mobility team directly
            logger.info(f"   No RMC — enriching {cname} HR/Global Mobility contacts.")
            contact_ids = enrich_company(cid, cname, domain)

        # Fall back to any manually entered contacts
        if not contact_ids:
            with get_db() as conn:
                manual = conn.execute(
                    "SELECT id FROM contacts WHERE company_id=? AND do_not_contact=0",
                    (cid,)
                ).fetchall()
            contact_ids = [r["id"] for r in manual]

        if not contact_ids:
            logger.info(f"   No contacts found for {cname} — skipping draft.")
            continue

        # ── 3. Draft email ────────────────────────────────────────────────────
        with get_db() as conn:
            contact = conn.execute(
                "SELECT * FROM contacts WHERE id=?", (contact_ids[0],)
            ).fetchone()

        if not contact:
            continue

        email_id = create_and_save_large_company_draft(
            contact_id=contact["id"],
            company_id=cid,
            company_name=cname,
            contact_first_name=contact["first_name"],
            contact_title=contact["title"] or "",
            expansion_direction=exp_dir,
            industry=company["industry"] or "unknown",
            rmc_name=rmc_name,
            multinational_company=cname,
            pipeline_run_id=pipeline_run_id,
            contact_email=contact["email"],
        )

        if email_id:
            drafts_created += 1
            with get_db() as conn:
                conn.execute(
                    "UPDATE companies SET status='contacted', updated_at=datetime('now') WHERE id=?",
                    (cid,)
                )
            logger.info(f"   Draft created for {cname} (email ID {email_id}).")

    logger.info(f"   {drafts_created} large-multinational drafts queued for approval.")


def step_followup():
    logger.info("-- Step 5: Follow-up scheduler --")
    n = run_followup_scheduler()
    logger.info(f"   {n} follow-up drafts queued.")


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_pipeline():
    """
    Execute one full pipeline run. Designed to be called on a schedule
    (e.g., daily via cron or the Cowork scheduler).
    """
    logger.info("=" * 60)
    logger.info("TMS CORP LEAD GEN ENGINE — PIPELINE RUN STARTED")
    logger.info(f"Time: {datetime.utcnow().isoformat()} UTC")
    logger.info("=" * 60)

    # ── Config validation ────────────────────────────────────────────────────
    issues = validate_config()
    if issues:
        logger.error("Configuration incomplete — cannot run pipeline:")
        for issue in issues:
            logger.error(f"  * {issue}")
        logger.error("Run 'python run.py setup' for setup instructions.")
        return

    # ── Ensure database is ready ─────────────────────────────────────────────
    init_db()

    # ── Check daily AI cap before starting ───────────────────────────────────
    ai_calls_today = get_counter("ai_calls")
    logger.info(f"AI calls today so far: {ai_calls_today}")

    # ── Run pipeline steps ───────────────────────────────────────────────────
    run_id = str(uuid.uuid4())
    logger.info(f"Pipeline run ID: {run_id}")
    signals = step_scrape()
    step_classify(signals)
    step_enrich_and_draft(pipeline_run_id=run_id)
    step_process_large_multinationals(pipeline_run_id=run_id)
    step_followup()

    # ── Summary ──────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("PIPELINE RUN COMPLETE")
    print_pipeline_summary()
    logger.info("Open the dashboard to review and approve pending emails:")
    logger.info("  python run.py dashboard")
    logger.info("=" * 60)
