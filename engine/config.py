"""
config.py — Central configuration and guardrails for the TMS Corp Lead Gen Engine.
All cost controls, rate limits, and API settings live here.
Edit this file to tune the engine's behavior without touching any other code.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).resolve().parent.parent
DATA_DIR   = BASE_DIR / "data"
CACHE_DIR  = DATA_DIR / "cache"
LOG_DIR    = DATA_DIR / "logs"
DB_PATH    = DATA_DIR / "leads.db"

# Load .env from project root
load_dotenv(BASE_DIR / ".env")


# ── API Keys (loaded from .env — never hard-code these) ───────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
SENDGRID_API_KEY   = os.getenv("SENDGRID_API_KEY", "")

# Contact enrichment — customer supplies their own key (BYOAK)
HUNTER_API_KEY     = os.getenv("HUNTER_API_KEY", "")    # hunter.io
APOLLO_API_KEY     = os.getenv("APOLLO_API_KEY", "")    # apollo.io


# ── Agent / Sender Identity ───────────────────────────────────────────────────
AGENT_NAME         = os.getenv("AGENT_NAME", "Your Name")
AGENT_TITLE        = os.getenv("AGENT_TITLE", "Your Title")
AGENT_EMAIL        = os.getenv("AGENT_EMAIL", "")          # must be verified in SendGrid
AGENCY_NAME        = os.getenv("AGENCY_NAME", "TMS Corp")
AGENCY_ADDRESS     = os.getenv("AGENCY_ADDRESS", "")       # included in email footer
COMPANY_NAME       = os.getenv("COMPANY_NAME", "TMS Corp")
CLIENT_NAME        = os.getenv("CLIENT_NAME", "Thelsa")
CLIENT_WEBSITE     = os.getenv("CLIENT_WEBSITE", "https://www.thelsa.com")


# ── Claude Model Selection (cost control) ─────────────────────────────────────
# Haiku: cheap screening (~$0.00025/1K tokens). Sonnet: drafting (~$0.003/1K).
SCREENING_MODEL    = "claude-haiku-4-5-20251001"   # used for lead qualification
DRAFTING_MODEL     = "claude-sonnet-4-6"            # used for email drafting only


# ── Hard Daily Caps (GUARDRAILS — do not remove) ──────────────────────────────
MAX_AI_CALLS_PER_DAY       = 50    # total Claude API calls across all runs today
MAX_EMAILS_SENT_PER_DAY    = 100   # stays within SendGrid free tier (100/day)
MAX_SCRAPE_SOURCES_PER_RUN = 35    # publications/feeds checked per scheduled run
MAX_ENRICHMENT_LOOKUPS_PER_RUN = 20  # Hunter/Apollo API calls per run (customer's quota)


# ── Per-Contact Send Limits (prevents runaway loops) ─────────────────────────
MAX_EMAILS_PER_CONTACT     = 3     # max total emails ever sent to one contact
RECONTACT_LOCKOUT_DAYS     = 30    # days before a contact can be emailed again after last send
FOLLOWUP_DELAY_DAYS        = 7     # days to wait before sending a follow-up


# ── Scraping & Caching ────────────────────────────────────────────────────────
SCRAPE_DELAY_SECONDS       = 3     # polite delay between HTTP requests
CACHE_TTL_HOURS            = 24    # how long to keep cached scrape results
MAX_ARTICLES_PER_SOURCE    = 20    # cap articles ingested per source per run


# ── Lead Qualification Threshold ─────────────────────────────────────────────
# Haiku assigns a relevance score 0–10. Only leads >= this threshold
# are escalated to Sonnet for email drafting (saves AI cost).
QUALIFICATION_SCORE_THRESHOLD = 6


# ── Circuit Breaker (email deliverability protection) ─────────────────────────
# If bounce rate or spam rate exceeds these thresholds in any 24-hour window,
# the engine stops sending and alerts the agent.
MAX_BOUNCE_RATE_PCT        = 2.0   # percent — industry safe threshold
MAX_SPAM_COMPLAINT_RATE_PCT = 0.1  # percent — Google/Yahoo enforcement threshold


# ── Target HR / Mobility contact functions ────────────────────────────────────
TARGET_FUNCTIONS = [
    "human resources",
    "global mobility",
    "compensation benefits",
    "procurement",
    "relocation",
    "talent management",
]


# ── Scraping Sources ──────────────────────────────────────────────────────────
# RSS feeds and public data sources (no ToS violations).
# Covers US, Canadian, and Mexican business news plus cross-border expansion signals.
RSS_SOURCES = [
    # ── US Business News ──────────────────────────────────────────────────────
    {"name": "Dallas Morning News Business",
     "url": "https://www.dallasnews.com/business/rss.xml",
     "type": "rss"},
    {"name": "Houston Chronicle Business",
     "url": "https://www.houstonchronicle.com/business/?rss=true",
     "type": "rss"},
    {"name": "San Antonio Express Business",
     "url": "https://www.expressnews.com/business/rss.xml",
     "type": "rss"},
    {"name": "Austin American-Statesman Business",
     "url": "https://www.statesman.com/business/?rss=true",
     "type": "rss"},
    {"name": "Chicago Tribune Business",
     "url": "https://www.chicagotribune.com/business/rss2.0.xml",
     "type": "rss"},
    {"name": "Arizona Republic Business",
     "url": "https://www.azcentral.com/business/rss.xml",
     "type": "rss"},

    # ── Canadian Business News ────────────────────────────────────────────────
    {"name": "Globe and Mail Business",
     "url": "https://www.theglobeandmail.com/business/rss",
     "type": "rss"},
    {"name": "Financial Post",
     "url": "https://financialpost.com/feed",
     "type": "rss"},
    {"name": "CBC Business",
     "url": "https://www.cbc.ca/cmlink/rss-business",
     "type": "rss"},

    # ── Mexican Business News ─────────────────────────────────────────────────
    {"name": "El Financiero",
     "url": "https://www.elfinanciero.com.mx/rss",
     "type": "rss"},
    {"name": "Mexico Business News",
     "url": "https://mexicobusiness.news/feed",
     "type": "rss"},
    {"name": "El Economista",
     "url": "https://www.eleconomista.com.mx/rss",
     "type": "rss"},

    # ── Google News: US/Canada → Mexico expansion signals ─────────────────────
    {"name": "Google News: US company expanding to Mexico",
     "url": "https://news.google.com/rss/search?q=US+company+expanding+Mexico+operations&hl=en-US&gl=US&ceid=US:en",
     "type": "rss"},
    {"name": "Google News: Canada company expanding to Mexico",
     "url": "https://news.google.com/rss/search?q=Canadian+company+expanding+Mexico+office+facility&hl=en-US&gl=US&ceid=US:en",
     "type": "rss"},
    {"name": "Google News: nearshoring Mexico",
     "url": "https://news.google.com/rss/search?q=nearshoring+Mexico+manufacturing+expansion&hl=en-US&gl=US&ceid=US:en",
     "type": "rss"},
    {"name": "Google News: empresa registra Mexico",
     "url": "https://news.google.com/rss/search?q=empresa+estadounidense+expansion+Mexico+oficina&hl=es-419&gl=MX&ceid=MX:es-419",
     "type": "rss"},

    # ── Google News: Mexico → US/Canada expansion signals ─────────────────────
    {"name": "Google News: Mexican company expanding USA",
     "url": "https://news.google.com/rss/search?q=Mexican+company+expanding+United+States+office&hl=en-US&gl=US&ceid=US:en",
     "type": "rss"},
    {"name": "Google News: Mexican company Canada",
     "url": "https://news.google.com/rss/search?q=Mexican+company+Canada+expansion+operations&hl=en-US&gl=US&ceid=US:en",
     "type": "rss"},
    {"name": "Google News: empresa mexicana expansion EEUU",
     "url": "https://news.google.com/rss/search?q=empresa+mexicana+expansion+Estados+Unidos+oficina&hl=es-419&gl=MX&ceid=MX:es-419",
     "type": "rss"},
    {"name": "Google News: empresa mexicana Canada",
     "url": "https://news.google.com/rss/search?q=empresa+mexicana+Canada+expansion+operaciones&hl=es-419&gl=MX&ceid=MX:es-419",
     "type": "rss"},

    # ── Google News: Search-intent keywords (relocation / mobility focus) ──────
    # These mirror what HR/Global Mobility managers search when planning moves.
    {"name": "Google News: relocating employees to Mexico",
     "url": "https://news.google.com/rss/search?q=%22relocating+employees%22+Mexico&hl=en-US&gl=US&ceid=US:en",
     "type": "rss"},
    {"name": "Google News: moving operations to Mexico",
     "url": "https://news.google.com/rss/search?q=%22moving+operations%22+OR+%22moving+its+operations%22+Mexico&hl=en-US&gl=US&ceid=US:en",
     "type": "rss"},
    {"name": "Google News: setting up office Mexico",
     "url": "https://news.google.com/rss/search?q=%22setting+up%22+OR+%22opening+office%22+Mexico+company&hl=en-US&gl=US&ceid=US:en",
     "type": "rss"},
    {"name": "Google News: maquiladora expansion",
     "url": "https://news.google.com/rss/search?q=maquiladora+expansion+OR+%22new+maquiladora%22+2026&hl=en-US&gl=US&ceid=US:en",
     "type": "rss"},
    {"name": "Google News: nearshoring relocation employees",
     "url": "https://news.google.com/rss/search?q=nearshoring+relocation+employees+cross-border&hl=en-US&gl=US&ceid=US:en",
     "type": "rss"},
    {"name": "Google News: moving to Mexico business",
     "url": "https://news.google.com/rss/search?q=%22moving+to+Mexico%22+business+OR+company+OR+office&hl=en-US&gl=US&ceid=US:en",
     "type": "rss"},
    {"name": "Google News: traslado empleados Mexico",
     "url": "https://news.google.com/rss/search?q=traslado+empleados+Mexico+expansion+empresa&hl=es-419&gl=MX&ceid=MX:es-419",
     "type": "rss"},
    {"name": "Google News: relocating to US from Mexico",
     "url": "https://news.google.com/rss/search?q=%22relocating+to%22+%22United+States%22+OR+%22US+office%22+Mexican+company&hl=en-US&gl=US&ceid=US:en",
     "type": "rss"},

    # ── Reddit: Cross-border business & expat communities ─────────────────────
    # These communities openly discuss cross-border moves — companies and
    # employees alike. Feedparser handles Reddit's RSS natively.
    {"name": "Reddit r/Mexico (new posts)",
     "url": "https://www.reddit.com/r/mexico/new/.rss?limit=25",
     "type": "rss"},
    {"name": "Reddit r/Expats (new posts)",
     "url": "https://www.reddit.com/r/expats/new/.rss?limit=25",
     "type": "rss"},
    {"name": "Reddit r/Entrepreneur Mexico expansion",
     "url": "https://www.reddit.com/r/Entrepreneur/search.rss?q=mexico+expansion+office&sort=new&limit=25",
     "type": "rss"},
    {"name": "Reddit r/smallbusiness Mexico",
     "url": "https://www.reddit.com/r/smallbusiness/search.rss?q=mexico+expanding+office&sort=new&limit=25",
     "type": "rss"},
    {"name": "Reddit r/mexica (nearshoring discussion)",
     "url": "https://www.reddit.com/r/Nearshoring/new/.rss?limit=25",
     "type": "rss"},
    {"name": "Reddit r/canadaexpats (new posts)",
     "url": "https://www.reddit.com/r/canadaexpats/new/.rss?limit=25",
     "type": "rss"},

    # ── Google Alerts RSS (user must create these in Google Alerts) ───────────
    # To activate: go to google.com/alerts, create an alert for each keyword,
    # choose "RSS feed" as delivery method, copy the feed URL here.
    # Placeholder slots — uncomment and paste your alert URLs when ready.
    {"name": "Google Alert: relocating to Mexico",
     "url": "https://www.google.com/alerts/feeds/06533219242564965389/14346051017784325920",
     "type": "rss"},
    {"name": "Google Alert: moving to Mexico",
     "url": "https://www.google.com/alerts/feeds/06533219242564965389/1808774824278821131",
     "type": "rss"},
    {"name": "Google Alert: expanding to Mexico",
     "url": "https://www.google.com/alerts/feeds/06533219242564965389/557636075779399903",
     "type": "rss"},
    {"name": "Google Alert: empresa mexicana expansion USA/Canada",
     "url": "https://www.google.com/alerts/feeds/06533219242564965389/9739272403724153836",
     "type": "rss"},
]


# ── Target Industries (for qualification scoring) ─────────────────────────────
# Companies in these industries are most likely to relocate employees cross-border.
TARGET_INDUSTRIES = [
    "manufacturing", "aerospace", "automotive", "technology", "software",
    "logistics", "transportation", "warehousing", "distribution",
    "construction", "engineering", "food processing", "food and beverage",
    "retail", "staffing", "financial services", "professional services",
    "healthcare", "energy", "oil and gas", "agriculture",
    "import export", "wholesale", "real estate",
]


# ── Email Templates Config ────────────────────────────────────────────────────
UNSUBSCRIBE_URL    = os.getenv("UNSUBSCRIBE_URL", "")   # set after SendGrid setup
EMAIL_LANGUAGE     = "both"   # options: "english", "spanish", "both"


# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL          = "INFO"   # DEBUG, INFO, WARNING, ERROR


# ── Validation helper ─────────────────────────────────────────────────────────
def validate_config():
    """Returns a list of missing required settings. Empty list = config is valid."""
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY — get one at console.anthropic.com")
    if not SENDGRID_API_KEY:
        missing.append("SENDGRID_API_KEY — get one at sendgrid.com")
    if not AGENT_EMAIL:
        missing.append("AGENT_EMAIL — your verified SendGrid sender email")
    return missing


if __name__ == "__main__":
    issues = validate_config()
    if issues:
        print("WARNING  Missing configuration:")
        for i in issues:
            print(f"   * {i}")
    else:
        print("OK Configuration looks complete.")
