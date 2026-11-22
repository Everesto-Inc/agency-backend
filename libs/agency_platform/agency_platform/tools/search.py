"""Shared web search tool (Tavily), available to every topic.

No mock fallback: if search isn't configured, callers get a clear error rather
than fabricated "results" that an agent might ground an answer on.
"""
from dataclasses import dataclass

import httpx

from agency_platform.errors import ServiceUnavailableError, UpstreamError

_TAVILY_URL = "https://api.tavily.com/search"


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    score: float


class WebSearch:
    def __init__(self, api_key: str | None, http: httpx.AsyncClient) -> None:
        self._key = api_key
        self._http = http

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        country: str | None = None,
    ) -> list[SearchResult]:
        if not self._key:
            raise ServiceUnavailableError("Web search is not configured (TAVILY_API_KEY missing)")
        body: dict[str, object] = {"query": query[:400], "max_results": max_results, "search_depth": "basic"}
        if include_domains:
            body["include_domains"] = include_domains
        if exclude_domains:
            body["exclude_domains"] = exclude_domains
        if country:
            body["country"] = country
        try:
            resp = await self._http.post(
                _TAVILY_URL, json=body, headers={"Authorization": f"Bearer {self._key}"}, timeout=20
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Web search failed: {type(exc).__name__}") from exc
        return [
            SearchResult(
                title=r.get("title", ""), url=r["url"], snippet=r.get("content", ""), score=float(r.get("score", 0))
            )
            for r in resp.json().get("results", [])
            if r.get("url")
        ]
