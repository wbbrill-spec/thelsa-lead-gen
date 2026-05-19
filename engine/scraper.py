"""
scraper.py — Intelligence gathering module.
Scans RSS feeds and public HTML sources for signals of companies expanding
cross-border between the US/Canada and Mexico in either direction.
Respects rate limits, caches results, and deduplicates.
"""

import hashlib
import logging
import time
from datetime import datetime
from typing import Optional, List

import feedparser
import requests
from bs4 import BeautifulSoup

from engine.config import (
    RSS_SOURCES,
    SCRAPE_DELAY_SECONDS,
    CACHE_TTL_HOURS,
    MAX_ARTICLES_PER_SOURCE,
    MAX_SCRAPE_SOURCES_PER_RUN,
)
from engine.database import get_cached_scrape, set_scrape_cache

logger = logging.getLogger(__name__)


# ── Keywords: expansion signals in both directions ────────────────────────────

# Generic expansion terms (English)
EXPANSION_KEYWORDS_EN = [
    "expand", "expansion", "expanding", "opening", "launches", "launch",
    "new office", "new facility", "new plant", "new warehouse", "new location",
    "establishing", "setting up", "registering", "entering", "entering the market",
    "cross-border", "nearshoring", "nearshore", "maquiladora", "relocation",
    "relocating", "moving operations", "entity registration", "foreign entity",
    "subsidiary", "branch office",
]

# Generic expansion terms (Spanish)
EXPANSION_KEYWORDS_ES = [
    "expansion", "expansión", "abre oficina", "nueva oficina", "nueva planta",
    "nueva bodega", "nuevo almacén", "operaciones", "ingresa", "registra",
    "establecer", "inversión", "nearshoring", "nearshore", "maquiladora",
    "traslado", "reubicación", "filial", "sucursal",
]

EXPANSION_KEYWORDS = EXPANSION_KEYWORDS_EN + EXPANSION_KEYWORDS_ES

# Mexico geography keywords
MEXICO_KEYWORDS = [
    "mexico", "mexican", "mexico-based", "monterrey", "guadalajara",
    "ciudad de mexico", "cdmx", "tijuana", "juarez", "ciudad juárez",
    "saltillo", "queretaro", "querétaro", "san luis potosi", "puebla",
    "mexicana", "mexicano", "empresa mexicana", "compañía mexicana",
    "mx", "maquiladora",
]

# US geography keywords
US_KEYWORDS = [
    "united states", "usa", "u.s.", "texas", "houston", "dallas",
    "san antonio", "austin", "el paso", "laredo", "mcallen", "brownsville",
    "california", "los angeles", "san diego", "chicago", "illinois",
    "new york", "florida", "miami", "arizona", "phoenix", "new mexico",
    "georgia", "atlanta", "north carolina", "tennessee", "ohio",
]

# Canada geography keywords
CANADA_KEYWORDS = [
    "canada", "canadian", "ontario", "toronto", "british columbia",
    "vancouver", "alberta", "calgary", "edmonton", "quebec", "montreal",
    "canada-based",
]

US_CANADA_KEYWORDS = US_KEYWORDS + CANADA_KEYWORDS


# ── Cache helper ──────────────────────────────────────────────────────────────

def _url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def _fetch_with_cache(url: str, timeout: int = 10) -> Optional[str]:
    """Fetch URL content, using the scrape cache to avoid redundant requests."""
    h = _url_hash(url)
    cached = get_cached_scrape(h, ttl_hours=CACHE_TTL_HOURS)
    if cached:
        logger.debug(f"Cache hit: {url}")
        return cached
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; TMSLeadBot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        content = resp.text
        set_scrape_cache(h, url, content)
        time.sleep(SCRAPE_DELAY_SECONDS)  # polite delay
        return content
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return None


# ── Relevance filter ──────────────────────────────────────────────────────────

def _is_relevant(text: str) -> bool:
    """
    Quick keyword check before sending to AI classifier.
    Must mention Mexico AND (US or Canada) AND an expansion signal.
    """
    text_lower = text.lower()
    has_mexico    = any(k in text_lower for k in MEXICO_KEYWORDS)
    has_us_canada = any(k in text_lower for k in US_CANADA_KEYWORDS)
    has_expand    = any(k in text_lower for k in EXPANSION_KEYWORDS)
    return has_mexico and has_us_canada and has_expand


# ── RSS Scraper ───────────────────────────────────────────────────────────────

def scrape_rss(source: dict) -> List[dict]:
    """Parse an RSS feed and return relevant articles."""
    results = []
    logger.info(f"Scraping RSS: {source['name']}")
    try:
        # Fetch with an explicit timeout so slow/blocked sources don't hang the pipeline
        try:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; TMSLeadBot/1.0)"}
            resp = requests.get(source["url"], headers=headers, timeout=10)
            resp.raise_for_status()
            feed = feedparser.parse(resp.text)
        except Exception as fetch_err:
            logger.warning(f"  Skipping {source['name']} — fetch failed: {fetch_err}")
            return results
        count = 0
        for entry in feed.entries:
            if count >= MAX_ARTICLES_PER_SOURCE:
                break
            title     = entry.get("title", "")
            summary   = entry.get("summary", "")
            link      = entry.get("link", "")
            published = entry.get("published", "")
            full_text = f"{title} {summary}"

            if _is_relevant(full_text):
                results.append({
                    "title":       title,
                    "snippet":     summary[:500],
                    "url":         link,
                    "published":   published,
                    "source_name": source["name"],
                    "source_url":  source["url"],
                })
                logger.debug(f"  Relevant: {title[:80]}")
            count += 1
    except Exception as e:
        logger.warning(f"RSS parse error for {source['name']}: {e}")
    return results


# ── HTML Scraper (public filing pages and similar) ────────────────────────────

def scrape_html(source: dict) -> List[dict]:
    """
    Scrape a public HTML page for relevant signals.
    Looks for text blocks mentioning cross-border expansion.
    """
    results = []
    logger.info(f"Scraping HTML: {source['name']}")
    content = _fetch_with_cache(source["url"])
    if not content:
        return results

    soup = BeautifulSoup(content, "html.parser")

    text_blocks = []
    for tag in soup.find_all(["tr", "li", "p", "div"]):
        text = tag.get_text(separator=" ", strip=True)
        if len(text) > 30:
            text_blocks.append(text)

    count = 0
    for block in text_blocks:
        if count >= MAX_ARTICLES_PER_SOURCE:
            break
        if _is_relevant(block):
            results.append({
                "title":       block[:120],
                "snippet":     block[:500],
                "url":         source["url"],
                "published":   datetime.utcnow().isoformat(),
                "source_name": source["name"],
                "source_url":  source["url"],
            })
            count += 1

    return results


# ── Main scrape runner ────────────────────────────────────────────────────────

def run_scraper() -> List[dict]:
    """
    Run one full scraping pass across all configured sources.
    Respects MAX_SCRAPE_SOURCES_PER_RUN cap.
    Returns a deduplicated list of relevant article signals.
    """
    logger.info("=== Scraper starting ===")
    all_results = []
    seen_urls   = set()
    sources_run = 0

    for source in RSS_SOURCES:
        if sources_run >= MAX_SCRAPE_SOURCES_PER_RUN:
            logger.info(f"Source cap reached ({MAX_SCRAPE_SOURCES_PER_RUN}). Stopping.")
            break

        if source["type"] == "rss":
            items = scrape_rss(source)
        elif source["type"] == "html":
            items = scrape_html(source)
        else:
            logger.warning(f"Unknown source type: {source['type']}")
            items = []

        for item in items:
            url = item.get("url", "")
            if url and url in seen_urls:
                continue
            seen_urls.add(url)
            all_results.append(item)

        sources_run += 1

    logger.info(f"=== Scraper done: {len(all_results)} relevant signals from {sources_run} sources ===")
    return all_results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    signals = run_scraper()
    print(f"\nFound {len(signals)} signals:")
    for s in signals:
        print(f"  * [{s['source_name']}] {s['title'][:80]}")
