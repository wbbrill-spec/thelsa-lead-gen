"""
run.py — Entry point for the TMS Corp Lead Gen Engine.

Usage:
  python run.py pipeline   -- Run one full scrape -> qualify -> draft cycle
  python run.py dashboard  -- Open the agent approval dashboard in your browser
  python run.py followup   -- Run the follow-up scheduler only
  python run.py status     -- Print pipeline summary
  python run.py setup      -- Show setup checklist
  python run.py initdb     -- Initialise the database (run once on first setup)
"""

import sys
import logging


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "help"

    if command == "pipeline":
        from engine.main import setup_logging, run_pipeline
        setup_logging()
        run_pipeline()

    elif command == "dashboard":
        from engine.main import setup_logging
        from engine.database import init_db
        from engine.dashboard import run_dashboard
        setup_logging()
        init_db()
        print("\nOpening approval dashboard at http://localhost:5051")
        print("  Press Ctrl+C to stop.\n")
        run_dashboard(port=5051)

    elif command == "followup":
        from engine.main import setup_logging
        from engine.tracker import run_followup_scheduler, print_pipeline_summary
        setup_logging()
        run_followup_scheduler()
        print_pipeline_summary()

    elif command == "status":
        from engine.database import init_db
        from engine.tracker import print_pipeline_summary
        init_db()
        print_pipeline_summary()

    elif command == "initdb":
        from engine.database import init_db
        init_db()
        print("Database initialised.")

    elif command == "setup":
        from engine.config import validate_config
        print("""
+============================================================+
|   TMS CORP LEAD GEN ENGINE -- SETUP CHECKLIST             |
+============================================================+
|                                                            |
|  1. Copy .env.example to .env                              |
|     cp .env.example .env                                   |
|                                                            |
|  2. Fill in your API keys in .env:                         |
|     * ANTHROPIC_API_KEY  -> console.anthropic.com          |
|     * SENDGRID_API_KEY   -> app.sendgrid.com               |
|     * AGENT_EMAIL        -> your verified sender email     |
|     * AGENT_NAME         -> your full name                 |
|     * AGENT_TITLE        -> your title                     |
|     * AGENCY_ADDRESS     -> physical address (CAN-SPAM)    |
|                                                            |
|  3. (Optional) Add enrichment keys for auto-contacts:      |
|     * HUNTER_API_KEY     -> hunter.io                      |
|     * APOLLO_API_KEY     -> apollo.io                      |
|                                                            |
|  4. Install dependencies:                                  |
|     pip install -r requirements.txt                        |
|                                                            |
|  5. Initialise the database:                               |
|     python run.py initdb                                   |
|                                                            |
|  6. Run a test pass (no live emails sent):                 |
|     python test_run.py                                     |
|                                                            |
|  7. Run your first live pipeline pass:                     |
|     python run.py pipeline                                 |
|                                                            |
|  8. Open the dashboard to review and approve drafts:       |
|     python run.py dashboard                                |
|                                                            |
+============================================================+
|  SENDGRID DOMAIN WARM-UP REMINDER:                         |
|  Do NOT send real emails until your sending domain has     |
|  been warmed up (4-6 weeks). Start with < 20 emails/day   |
|  and increase gradually.                                   |
+============================================================+
        """)
        issues = validate_config()
        if issues:
            print("WARNING  Missing configuration items:")
            for i in issues:
                print(f"   * {i}")
        else:
            print("OK  Configuration complete. Ready to run.")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
