"""
test_run.py — End-to-end pipeline test with no live email sending.

Runs 6 synthetic cross-border expansion scenarios through:
  1. Claude Haiku  — lead qualification (scores each company 0-10)
  2. Claude Sonnet — bilingual email drafting (English + Spanish)

  US/Canada -> Mexico (3 scenarios):
    1. Dallas staffing firm registering entity in Monterrey for maquiladora placements
    2. Toronto logistics company leasing warehouse in Guadalajara for auto supply chain
    3. Phoenix construction firm winning contract for distribution center in Queretaro

  Mexico -> US/Canada (3 scenarios):
    4. Monterrey aerospace parts manufacturer opening engineering office in San Antonio
    5. Guadalajara software firm registering in Austin to serve US clients
    6. Mexico City food brand leasing retail and distribution space in Chicago

Test contacts span all 4 target functions:
  HR Director, Global Mobility Manager, C&B Manager,
  Procurement Manager, HR Business Partner, Relocation Manager

Output is saved to data/test_output.txt so you can review the results.
All drafts are saved to the database as pending_approval and visible in the dashboard.

Usage:
    python test_run.py

Only requires ANTHROPIC_API_KEY in .env — everything else uses test values.
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# ── Bootstrap path so `engine` imports work ──────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

# ── Validate only what we actually need ──────────────────────────────────────
if not os.getenv("ANTHROPIC_API_KEY"):
    print("\nERROR  ANTHROPIC_API_KEY is missing from your .env file.")
    print("    1. Open .env in this project folder")
    print("    2. Paste your key after:  ANTHROPIC_API_KEY=")
    print("    3. Get a key at: https://console.anthropic.com\n")
    sys.exit(1)

from engine.database import (
    init_db, upsert_company, update_company_qualification,
    upsert_contact, create_email_draft, get_db,
)
from engine.classifier import qualify_signal
from engine.email_drafter import draft_email, _compliance_footer_en, _compliance_footer_es

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── Output file ───────────────────────────────────────────────────────────────
OUTPUT_DIR = Path(__file__).resolve().parent / "data"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "test_output.txt"


# ── Test signals — 6 realistic SMB cross-border expansion scenarios ───────────

TEST_SIGNALS = [
    # ── US/Canada -> Mexico (3) ───────────────────────────────────────────────
    {
        # Signal 1: Dallas staffing firm registering in Monterrey
        "source_name": "Texas Secretary of State — Foreign Entity Filings",
        "title": "Apex Workforce Solutions LLC — Foreign Entity Registration / Mexican Subsidiary",
        "snippet": (
            "Apex Workforce Solutions LLC, a Dallas-based staffing and workforce management "
            "company with approximately 320 employees, has filed articles to establish a "
            "Mexican subsidiary entity in Monterrey, Nuevo León. The company's stated purpose "
            "is to recruit and place skilled tradespeople — primarily welders, CNC operators, "
            "and quality-control technicians — at maquiladora facilities in the Monterrey "
            "industrial corridor. The founder, Jack Harmon, said in a press release that the "
            "firm expects to relocate three US-based operations managers to Monterrey within "
            "Q1 to oversee the new placement division. The company currently has no established "
            "immigration or relocation vendor for the employee moves."
        ),
        "url": "https://example.com/apex-workforce-monterrey-filing",
        "expansion_direction_hint": "to_mexico",
    },
    {
        # Signal 2: Toronto logistics company leasing warehouse in Guadalajara
        "source_name": "Globe and Mail Business",
        "title": "Toronto's Redline Logistics Signs Warehouse Lease in Guadalajara to Serve Auto Supply Chain",
        "snippet": (
            "Redline Logistics Corp., a Toronto-based third-party logistics provider with "
            "around 410 employees, has signed a 5-year lease on a 22,000 square-metre "
            "warehouse facility in the Guadalajara logistics park. The company supplies "
            "just-in-time parts to automotive OEM and Tier-1 plants across North America "
            "and says the Guadalajara site will serve the growing cluster of auto suppliers "
            "in Jalisco. CEO Patricia Wexler confirmed the firm plans to post two Canadian "
            "logistics managers to Guadalajara on long-term assignments starting March. "
            "The company has not previously had operations in Mexico and does not yet have "
            "a relocation or immigration services partner."
        ),
        "url": "https://example.com/redline-logistics-guadalajara",
        "expansion_direction_hint": "to_mexico",
    },
    {
        # Signal 3: Phoenix construction firm winning contract in Queretaro
        "source_name": "Arizona Republic Business",
        "title": "Phoenix Contractor Fortis Build Wins $18M Distribution Center Contract in Queretaro",
        "snippet": (
            "Fortis Build Inc., a Phoenix-based commercial construction firm with about 280 "
            "employees, has been awarded an $18 million contract to build a 45,000 square-metre "
            "distribution and fulfillment center in the Queretaro industrial zone on behalf of "
            "a US-based e-commerce client. Project Director Alan Nguyen said Fortis will need "
            "to relocate a site superintendent and two project engineers to Queretaro for the "
            "18-month construction period. Fortis has not previously operated in Mexico and "
            "is actively seeking immigration and short-term relocation support for the assignees."
        ),
        "url": "https://example.com/fortis-build-queretaro-contract",
        "expansion_direction_hint": "to_mexico",
    },

    # ── Mexico -> US/Canada (3) ───────────────────────────────────────────────
    {
        # Signal 4: Monterrey aerospace manufacturer opening office in San Antonio
        "source_name": "Mexico Business News",
        "title": "Aeropartes del Norte Opens San Antonio Engineering Office to Serve US Aerospace Clients",
        "snippet": (
            "Aeropartes del Norte SA de CV, a Monterrey-based precision aerospace components "
            "manufacturer with approximately 650 employees, announced the opening of a sales "
            "and engineering liaison office in San Antonio, Texas. The company, which supplies "
            "structural brackets and machined titanium parts to US aerospace primes, said the "
            "San Antonio office will house four engineers and a commercial director relocated "
            "from Monterrey. General Director Ing. Arturo Villanueva said the company is "
            "actively seeking a US immigration and relocation services firm to handle the "
            "TN visa process and household moves for the four relocating employees and "
            "their families."
        ),
        "url": "https://example.com/aeropartes-norte-san-antonio",
        "expansion_direction_hint": "to_us_canada",
    },
    {
        # Signal 5: Guadalajara software firm registering in Austin
        "source_name": "El Financiero",
        "title": "Empresa tapatia Soluciones Digitales Jalisco se registra en Austin para atender clientes estadounidenses",
        "snippet": (
            "Soluciones Digitales Jalisco SA de CV, empresa de desarrollo de software con sede "
            "en Guadalajara y alrededor de 180 empleados, completó el registro de una entidad "
            "corporativa en Austin, Texas, con el objetivo de ofrecer servicios de manera "
            "directa a clientes del sector fintech y healthtech en Estados Unidos. El director "
            "general, Lic. Claudia Espinoza, indicó que la empresa planea reubicar a dos "
            "desarrolladores senior y un gerente de cuentas de Guadalajara a Austin en los "
            "próximos meses. La empresa no cuenta aún con un proveedor de servicios de "
            "relocation ni de gestión de visas de trabajo en Estados Unidos."
        ),
        "url": "https://example.com/soluciones-digitales-jalisco-austin",
        "expansion_direction_hint": "to_us_canada",
    },
    {
        # Signal 6: Mexico City food brand leasing in Chicago
        "source_name": "Google News: Mexican company expanding USA",
        "title": "Mexico City Food Brand Sabores Auténticos Signs Chicago Distribution and Retail Lease",
        "snippet": (
            "Sabores Auténticos SA, a Mexico City-based specialty food brand with around 95 "
            "employees, has signed a lease on a 6,000 square-foot combined retail and "
            "distribution unit in Chicago's Pilsen neighborhood to serve the US Hispanic "
            "retail market. Founder and CEO María Fernanda Salinas confirmed the company "
            "will relocate its head of US operations and a marketing manager from Mexico City "
            "to Chicago. The company, which currently exports products through a US distributor, "
            "is establishing its first direct US presence and has not yet identified vendors "
            "for US work authorization or employee relocation."
        ),
        "url": "https://example.com/sabores-autenticos-chicago",
        "expansion_direction_hint": "to_us_canada",
    },
]


# ── Synthetic test contacts (one per company, 6 total, all 4 functions covered) ──

TEST_CONTACTS = {
    # Signal 1: Dallas staffing -> Monterrey
    0: {
        "first_name": "Sarah",
        "last_name": "Kovacs",
        "title": "HR Director",
        "target_function": "hr",
        "email": "s.kovacs@test-apexworkforce.com",
    },
    # Signal 2: Toronto logistics -> Guadalajara
    1: {
        "first_name": "Marcus",
        "last_name": "Chen",
        "title": "Global Mobility Manager",
        "target_function": "global_mobility",
        "email": "m.chen@test-redlinelogistics.com",
    },
    # Signal 3: Phoenix construction -> Queretaro
    2: {
        "first_name": "Deborah",
        "last_name": "Flores",
        "title": "Compensation & Benefits Manager",
        "target_function": "comp_benefits",
        "email": "d.flores@test-fortisbuild.com",
    },
    # Signal 4: Monterrey aerospace -> San Antonio
    3: {
        "first_name": "Rodrigo",
        "last_name": "Castellanos",
        "title": "Procurement Manager",
        "target_function": "procurement",
        "email": "r.castellanos@test-aeropartesnorte.com",
    },
    # Signal 5: Guadalajara software -> Austin
    4: {
        "first_name": "Claudia",
        "last_name": "Espinoza",
        "title": "HR Business Partner",
        "target_function": "hr",
        "email": "c.espinoza@test-solucionesdigitales.com",
    },
    # Signal 6: Mexico City food brand -> Chicago
    5: {
        "first_name": "María Fernanda",
        "last_name": "Salinas",
        "title": "Relocation Manager",
        "target_function": "global_mobility",
        "email": "mf.salinas@test-saboresautenticos.com",
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def separator(label: str = "") -> str:
    if label:
        pad = max(0, 60 - len(label) - 2)
        return f"\n{'--'} {label} {'-' * pad}"
    return "-" * 64


def write_output(lines: list):
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info(f"Results saved to: {OUTPUT_FILE}")


# ── Main test run ─────────────────────────────────────────────────────────────

def main():
    logger.info("=" * 64)
    logger.info("TMS CORP LEAD GEN ENGINE — TEST RUN")
    logger.info(f"Time: {datetime.utcnow().isoformat()} UTC")
    logger.info("6 signals: 3x US/Canada->Mexico, 3x Mexico->US/Canada")
    logger.info("No emails will be sent. Output saved to data/test_output.txt")
    logger.info("=" * 64)

    init_db()
    output_lines = [
        "TMS CORP LEAD GEN ENGINE — TEST RUN",
        f"Powered by Thelsa Corporate Relocation & Immigration Services",
        f"Run time: {datetime.utcnow().isoformat()} UTC",
        "=" * 64,
        "",
        "SIGNALS: 3x US/Canada->Mexico | 3x Mexico->US/Canada",
        "=" * 64,
    ]

    passed = 0
    failed = 0
    total  = len(TEST_SIGNALS)

    for i, signal in enumerate(TEST_SIGNALS):
        contact = TEST_CONTACTS[i]
        direction_hint = signal.get("expansion_direction_hint", "unknown")

        direction_label = (
            "US/Canada -> Mexico" if direction_hint == "to_mexico"
            else "Mexico -> US/Canada" if direction_hint == "to_us_canada"
            else "Unknown direction"
        )

        logger.info(f"\n{'='*64}")
        logger.info(f"TEST COMPANY {i+1}/{total}: {signal['title'][:70]}")
        logger.info(f"Direction: {direction_label}")
        logger.info(f"{'='*64}")

        output_lines += [
            "",
            separator(f"COMPANY {i+1}/{total}: {direction_label}"),
            f"Source   : {signal['source_name']}",
            f"Headline : {signal['title']}",
            f"Snippet  : {signal['snippet'][:140]}...",
        ]

        # ── Step 1: Qualification ─────────────────────────────────────────────
        logger.info("Step 1: Sending to Claude Haiku for qualification...")
        result = qualify_signal(signal)

        if not result:
            logger.error("  Qualification failed — skipping this company.")
            output_lines.append("QUALIFICATION: FAILED (API error)")
            failed += 1
            continue

        score               = result.get("score", 0)
        reason              = result.get("reason", "")
        name                = result.get("company_name", "Unknown")
        expansion_direction = result.get("expansion_direction", direction_hint)
        estimated_size      = result.get("estimated_size", "unknown")

        logger.info(f"  Score: {score}/10 | Direction: {expansion_direction} | Size: {estimated_size}")
        logger.info(f"  Reason: {reason}")

        output_lines += [
            "",
            "[QUALIFICATION]",
            f"  Company name       : {name}",
            f"  Industry           : {result.get('industry', 'unknown')}",
            f"  Expansion direction: {expansion_direction}",
            f"  Expansion stage    : {result.get('expansion_stage', 'unknown')}",
            f"  Estimated size     : {estimated_size}",
            f"  Score              : {score}/10",
            f"  Reason             : {reason}",
        ]

        if score < 6:
            logger.info(f"  Score {score} is below threshold (6) — would be disqualified.")
            output_lines.append(f"  -> DISQUALIFIED (score below threshold of 6)")
            failed += 1
            continue

        output_lines.append(f"  -> QUALIFIED")

        # ── Save company to database ──────────────────────────────────────────
        company_id = upsert_company(
            name=name,
            domain=result.get("domain"),
            industry=result.get("industry", "unknown"),
            description=result.get("expansion_stage", "unknown"),
            expansion_direction=expansion_direction,
            source_url=signal.get("url", ""),
            source_name=signal.get("source_name", ""),
            raw_snippet=signal.get("snippet", "")[:500],
        )
        if company_id:
            update_company_qualification(company_id, score, reason, status="qualified")
        else:
            # Already exists — look it up
            with get_db() as conn:
                row = conn.execute("SELECT id FROM companies WHERE name=?", (name,)).fetchone()
                company_id = row["id"] if row else None

        # ── Save contact to database ──────────────────────────────────────────
        contact_id = None
        if company_id:
            contact_id = upsert_contact(
                company_id=company_id,
                email=contact["email"],
                first_name=contact["first_name"],
                last_name=contact["last_name"],
                title=contact["title"],
                target_function=contact["target_function"],
                enrichment_source="test",
            )

        # ── Step 2: Email drafting ────────────────────────────────────────────
        logger.info("Step 2: Sending to Claude Sonnet for bilingual email drafting...")
        draft = draft_email(
            company_name=name,
            contact_first_name=contact["first_name"],
            contact_title=contact["title"],
            industry=result.get("industry", "unknown"),
            expansion_stage=result.get("expansion_stage", "unknown"),
            source_snippet=signal["snippet"],
            expansion_direction=expansion_direction,
            target_function=contact["target_function"],
            sequence_num=1,
        )

        if not draft:
            logger.error("  Email drafting failed.")
            output_lines.append("EMAIL DRAFT: FAILED")
            failed += 1
            continue

        footer_en = _compliance_footer_en()
        footer_es = _compliance_footer_es()

        # ── Save draft to database as pending_approval ────────────────────────
        if company_id and contact_id:
            body_en = draft["english_body"] + "\n\n" + footer_en
            body_es = draft["spanish_body"] + "\n\n" + footer_es
            email_id = create_email_draft(
                contact_id=contact_id,
                company_id=company_id,
                subject=draft["english_subject"],
                body_english=body_en,
                body_spanish=body_es,
                sequence_num=1,
            )
            with get_db() as conn:
                conn.execute(
                    "UPDATE emails SET status='pending_approval' WHERE id=?",
                    (email_id,)
                )
            logger.info(f"  Draft saved to dashboard as pending_approval (email ID {email_id})")
        else:
            logger.warning("  Could not save draft — missing company_id or contact_id.")

        output_lines += [
            "",
            f"[EMAIL DRAFT — ENGLISH]",
            f"  To      : {contact['first_name']} {contact['last_name']} <{contact['email']}>",
            f"  Title   : {contact['title']} [{contact['target_function']}]",
            f"  Subject : {draft['english_subject']}",
            "",
            "  Body:",
        ]
        for line in draft["english_body"].splitlines():
            output_lines.append(f"  {line}")
        output_lines.append("")
        output_lines.append("  [Compliance Footer]")
        for line in footer_en.splitlines():
            output_lines.append(f"  {line}")

        output_lines += [
            "",
            f"[EMAIL DRAFT — SPANISH]",
            f"  Subject : {draft['spanish_subject']}",
            "",
            "  Body:",
        ]
        for line in draft["spanish_body"].splitlines():
            output_lines.append(f"  {line}")
        output_lines.append("")
        output_lines.append("  [Pie de pagina de cumplimiento]")
        for line in footer_es.splitlines():
            output_lines.append(f"  {line}")

        output_lines += [
            "",
            "  -> EMAIL SAVED TO DASHBOARD (not sent — test mode)",
        ]

        logger.info(f"  Draft complete. Subject: {draft['english_subject']}")
        passed += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    to_mexico_passed   = sum(1 for i, s in enumerate(TEST_SIGNALS[:3]) if i < passed)
    to_usca_passed     = passed - min(passed, 3)

    summary_lines = [
        "",
        "=" * 64,
        "SUMMARY",
        f"  Total signals tested        : {total}",
        f"  US/Canada -> Mexico signals : 3",
        f"  Mexico -> US/Canada signals : 3",
        f"  Qualified + drafted         : {passed}",
        f"  Disqualified / failed       : {failed}",
        "",
        "  All passing drafts saved to the database as 'pending_approval'.",
        "  Run 'python run.py dashboard' to review and approve them.",
        "=" * 64,
    ]
    output_lines += summary_lines

    for line in summary_lines:
        logger.info(line.strip())

    write_output(output_lines)
    print(f"\nTest complete. Open data/test_output.txt to review all drafts.")
    print(f"Run 'python run.py dashboard' to review drafts in the browser.\n")


if __name__ == "__main__":
    main()
