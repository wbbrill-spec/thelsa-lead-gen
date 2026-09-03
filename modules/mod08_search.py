"""MOD-08: Web Search

Centralized web search module. Single point for all search calls —
swap provider here without touching other modules.

Supports SerpAPI and Perplexity. Set SEARCH_PROVIDER env var to choose.
"""

from __future__ import annotations
import time
import requests
import config


class SearchError(RuntimeError):
    """Raised for search failures that will affect every query in the run
    (bad key, exhausted quota, no key in production). Transient errors are
    retried and then logged; they do NOT raise."""


# Process-wide counters so a run can report what the search layer actually did.
STATS = {"calls": 0, "failures": 0, "last_error": ""}


class SearchResult:
    def __init__(self, title: str, url: str, snippet: str, date: str = ""):
        self.title = title
        self.url = url
        self.snippet = snippet
        self.date = date

    def __repr__(self):
        return f"<SearchResult {self.title[:50]}>"


def account_status() -> dict:
    """SerpAPI account info: {'plan_searches_left', 'searches_per_month', 'this_month_usage', 'plan_name'}.
    Returns {} if not SerpAPI / not configured / request failed."""
    if config.SEARCH_PROVIDER.lower() != "serpapi" or not config.SEARCH_API_KEY:
        return {}
    try:
        resp = requests.get("https://serpapi.com/account", params={"api_key": config.SEARCH_API_KEY}, timeout=10)
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        d = resp.json()
        return {k: d.get(k) for k in ("plan_searches_left", "searches_per_month", "this_month_usage",
                                       "plan_name", "extra_credits", "total_searches_left") if k in d}
    except Exception as e:
        return {"error": str(e)}


def search(query: str, num_results: int = 10, recency_days: int = 30) -> list[SearchResult]:
    """Execute a web search and return results.

    Args:
        query: Search query string
        num_results: Max results to return
        recency_days: Filter to results within this many days (best effort)

    Returns:
        List of SearchResult objects
    """
    provider = config.SEARCH_PROVIDER.lower()

    if provider == "perplexity":
        return _search_perplexity(query, num_results)
    else:
        return _search_serpapi(query, num_results, recency_days)


def _search_serpapi(query: str, num_results: int, recency_days: int) -> list[SearchResult]:
    """Search via SerpAPI."""
    if not config.SEARCH_API_KEY:
        if config.FLASK_ENV == "production":
            raise SearchError("SEARCH_API_KEY is not set — refusing to use mock search results in production")
        return _mock_search_results(query)

    # Map recency_days to Google's tbs parameter (0 / negative = no date filter)
    if recency_days <= 0:
        tbs = ""
    elif recency_days <= 7:
        tbs = "qdr:w"      # past week
    elif recency_days <= 30:
        tbs = "qdr:m"      # past month
    else:
        tbs = "qdr:y"      # past year (covers 90, 180, 365 day requests)

    params = {
        "q": query,
        "api_key": config.SEARCH_API_KEY,
        "num": min(num_results, 10),
        "engine": "google",
    }
    if tbs:
        params["tbs"] = tbs

    STATS["calls"] += 1
    last_err = ""
    for attempt in range(3):
        try:
            resp = requests.get(
                "https://serpapi.com/search",
                params=params,
                timeout=15,
            )
            if resp.status_code in (401, 403, 429):
                # Bad key or quota exhausted — every remaining query would fail too.
                detail = resp.text[:300]
                STATS["failures"] += 1
                STATS["last_error"] = f"SerpAPI HTTP {resp.status_code}: {detail}"
                raise SearchError(
                    f"SerpAPI rejected the request (HTTP {resp.status_code}) — "
                    f"check the key / monthly quota at serpapi.com: {detail}"
                )
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                # SerpAPI returns 200 with an "error" key for some failures
                # ("Google hasn't returned any results for this query" is benign).
                err = str(data["error"])
                if "hasn't returned any results" not in err:
                    print(f"[MOD-08] SerpAPI error for {query!r}: {err}")
                    STATS["last_error"] = err
                return []
            results = []
            for item in data.get("organic_results", []):
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    date=item.get("date", ""),
                ))
            return results
        except requests.exceptions.Timeout:
            last_err = "timeout"
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue
        except requests.exceptions.RequestException as e:
            last_err = str(e)
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue

    STATS["failures"] += 1
    STATS["last_error"] = f"SerpAPI failed after 3 attempts for {query!r}: {last_err}"
    print(f"[MOD-08] {STATS['last_error']}")
    return []


def _search_perplexity(query: str, num_results: int) -> list[SearchResult]:
    """Search via Perplexity API."""
    if not config.SEARCH_API_KEY:
        if config.FLASK_ENV == "production":
            raise SearchError("SEARCH_API_KEY is not set — refusing to use mock search results in production")
        return _mock_search_results(query)

    headers = {
        "Authorization": f"Bearer {config.SEARCH_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "sonar",
        "messages": [
            {
                "role": "user",
                "content": f"Search for recent news about: {query}. Return key facts, company names, locations, and source URLs.",
            }
        ],
        "max_tokens": 1000,
        "search_recency_filter": "month",
    }

    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.perplexity.ai/chat/completions",
                headers=headers,
                json=payload,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            citations = data.get("citations", [])

            results = []
            if citations:
                for i, url in enumerate(citations[:num_results]):
                    results.append(SearchResult(
                        title=f"Source {i+1}",
                        url=url,
                        snippet=content[:500] if i == 0 else "",
                        date="",
                    ))
            else:
                results.append(SearchResult(
                    title=query,
                    url="",
                    snippet=content,
                    date="",
                ))
            return results
        except Exception:
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue

    return []


def _mock_search_results(query: str) -> list[SearchResult]:
    """Return mock results when no API key is configured (dev/staging only)."""
    return [
        SearchResult(
            title=f"[MOCK] Result for: {query}",
            url="https://example.com/mock-result",
            snippet=f"This is a mock search result for query: {query}. Configure SEARCH_API_KEY to get real results.",
            date="2026-06-01",
        )
    ]
