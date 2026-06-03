"""MOD-08: Web Search

Centralized web search module. Single point for all search calls —
swap provider here without touching other modules.

Supports SerpAPI and Perplexity. Set SEARCH_PROVIDER env var to choose.
"""

from __future__ import annotations
import time
import requests
import config


class SearchResult:
    def __init__(self, title: str, url: str, snippet: str, date: str = ""):
        self.title = title
        self.url = url
        self.snippet = snippet
        self.date = date

    def __repr__(self):
        return f"<SearchResult {self.title[:50]}>"


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
        return _mock_search_results(query)

    params = {
        "q": query,
        "api_key": config.SEARCH_API_KEY,
        "num": min(num_results, 10),
        "engine": "google",
        "tbs": f"qdr:m",  # past month
    }

    for attempt in range(3):
        try:
            resp = requests.get(
                "https://serpapi.com/search",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
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
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue
        except requests.exceptions.RequestException as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue

    return []


def _search_perplexity(query: str, num_results: int) -> list[SearchResult]:
    """Search via Perplexity API."""
    if not config.SEARCH_API_KEY:
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
