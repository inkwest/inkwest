"""
Web search service for documentation lookup.

Provides search capabilities with source validation and trust levels.
"""

import logging
from enum import Enum
from typing import NamedTuple
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class SourceTrust(str, Enum):
    """Trust level for a web source."""

    OFFICIAL = "official"  # Official documentation
    TRUSTED = "trusted"  # Well-known community sources
    COMMUNITY = "community"  # General community content
    UNKNOWN = "unknown"  # Unknown sources
    BLOCKED = "blocked"  # Blocked sources


class SearchResult(NamedTuple):
    """A single search result."""

    title: str
    url: str
    snippet: str
    source_domain: str
    trust_level: SourceTrust


class WebSearchService:
    """
    Web search service with source validation.

    Supports multiple search providers and classifies sources by trust level.
    """

    # Official Kubernetes ecosystem documentation
    OFFICIAL_DOMAINS = {
        "kubernetes.io",
        "k8s.io",
        "docs.docker.com",
        "helm.sh",
        "prometheus.io",
        "grafana.com",
        "istio.io",
        "linkerd.io",
        "argoproj.github.io",
        "fluxcd.io",
        "cert-manager.io",
        "external-secrets.io",
        "crossplane.io",
        "kustomize.io",
        "tekton.dev",
        "knative.dev",
        "opentelemetry.io",
        "etcd.io",
        "containerd.io",
        "cncf.io",
    }

    # Trusted community sources
    TRUSTED_DOMAINS = {
        "github.com",
        "stackoverflow.com",
        "serverfault.com",
        "digitalocean.com",
        "aws.amazon.com",
        "cloud.google.com",
        "docs.microsoft.com",
        "learn.microsoft.com",
        "redhat.com",
        "suse.com",
        "canonical.com",
        "linuxfoundation.org",
    }

    # Known community sources
    COMMUNITY_DOMAINS = {
        "medium.com",
        "dev.to",
        "hashnode.dev",
        "learnk8s.io",
        "itnext.io",
        "blog.kubecost.com",
    }

    def __init__(self):
        self.settings = get_settings()
        self._additional_trusted = {
            d.strip() for d in self.settings.web_search_trusted_domains.split(",") if d.strip()
        }
        self._blocked = {
            d.strip() for d in self.settings.web_search_blocked_domains.split(",") if d.strip()
        }

    @property
    def is_enabled(self) -> bool:
        """Check if web search is enabled and configured."""
        return self.settings.web_search_enabled and self.settings.web_search_api_key is not None

    def get_domain(self, url: str) -> str:
        """Extract domain from URL."""
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        # Remove www. prefix
        if domain.startswith("www."):
            domain = domain[4:]
        return domain

    def classify_source(self, url: str) -> SourceTrust:
        """
        Classify the trust level of a source URL.

        Args:
            url: URL to classify

        Returns:
            SourceTrust level
        """
        domain = self.get_domain(url)

        # Check blocked first
        if domain in self._blocked:
            return SourceTrust.BLOCKED
        for blocked in self._blocked:
            if domain.endswith(f".{blocked}") or blocked.endswith(f".{domain}"):
                return SourceTrust.BLOCKED

        # Check official
        if domain in self.OFFICIAL_DOMAINS:
            return SourceTrust.OFFICIAL
        for official in self.OFFICIAL_DOMAINS:
            if domain.endswith(f".{official}"):
                return SourceTrust.OFFICIAL

        # Check trusted (including user-configured)
        all_trusted = self.TRUSTED_DOMAINS | self._additional_trusted
        if domain in all_trusted:
            return SourceTrust.TRUSTED
        for trusted in all_trusted:
            if domain.endswith(f".{trusted}"):
                return SourceTrust.TRUSTED

        # Check community
        if domain in self.COMMUNITY_DOMAINS:
            return SourceTrust.COMMUNITY
        for community in self.COMMUNITY_DOMAINS:
            if domain.endswith(f".{community}"):
                return SourceTrust.COMMUNITY

        return SourceTrust.UNKNOWN

    async def search_brave(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """
        Search using Brave Search API.

        Args:
            query: Search query
            max_results: Maximum number of results

        Returns:
            List of SearchResult
        """
        url = "https://api.search.brave.com/res/v1/web/search"
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.settings.web_search_api_key,
        }
        params = {
            "q": f"{query} kubernetes",  # Always include kubernetes context
            "count": max_results,
            "safesearch": "moderate",
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=10.0,
                )
                response.raise_for_status()
                data = response.json()

                results = []
                for item in data.get("web", {}).get("results", []):
                    result_url = item.get("url", "")
                    trust = self.classify_source(result_url)

                    # Skip blocked sources
                    if trust == SourceTrust.BLOCKED:
                        continue

                    results.append(
                        SearchResult(
                            title=item.get("title", ""),
                            url=result_url,
                            snippet=item.get("description", ""),
                            source_domain=self.get_domain(result_url),
                            trust_level=trust,
                        )
                    )

                return results

            except httpx.HTTPError as e:
                logger.error(f"Brave search failed: {e}")
                return []

    async def search_serpapi(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """
        Search using SerpAPI (Google search).

        Args:
            query: Search query
            max_results: Maximum number of results

        Returns:
            List of SearchResult
        """
        url = "https://serpapi.com/search"
        params = {
            "q": f"{query} kubernetes",
            "api_key": self.settings.web_search_api_key,
            "num": max_results,
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, params=params, timeout=10.0)
                response.raise_for_status()
                data = response.json()

                results = []
                for item in data.get("organic_results", []):
                    result_url = item.get("link", "")
                    trust = self.classify_source(result_url)

                    if trust == SourceTrust.BLOCKED:
                        continue

                    results.append(
                        SearchResult(
                            title=item.get("title", ""),
                            url=result_url,
                            snippet=item.get("snippet", ""),
                            source_domain=self.get_domain(result_url),
                            trust_level=trust,
                        )
                    )

                return results

            except httpx.HTTPError as e:
                logger.error(f"SerpAPI search failed: {e}")
                return []

    async def search(
        self,
        query: str,
        max_results: int | None = None,
        filter_trust: list[SourceTrust] | None = None,
    ) -> list[SearchResult]:
        """
        Perform a web search.

        Args:
            query: Search query
            max_results: Maximum number of results
            filter_trust: Only return results with these trust levels

        Returns:
            List of SearchResult
        """
        if not self.is_enabled:
            logger.warning("Web search is not enabled or configured")
            return []

        if max_results is None:
            max_results = self.settings.web_search_max_results

        # Use configured provider
        provider = self.settings.web_search_provider.lower()

        if provider == "brave":
            results = await self.search_brave(query, max_results * 2)  # Get extra for filtering
        elif provider == "serpapi" or provider == "google":
            results = await self.search_serpapi(query, max_results * 2)
        else:
            logger.error(f"Unknown search provider: {provider}")
            return []

        # Filter by trust level if specified
        if filter_trust:
            results = [r for r in results if r.trust_level in filter_trust]

        # Sort by trust level (official first)
        trust_order = {
            SourceTrust.OFFICIAL: 0,
            SourceTrust.TRUSTED: 1,
            SourceTrust.COMMUNITY: 2,
            SourceTrust.UNKNOWN: 3,
        }
        results.sort(key=lambda r: trust_order.get(r.trust_level, 99))

        return results[:max_results]

    def format_results_for_ai(self, results: list[SearchResult]) -> str:
        """
        Format search results for AI consumption.

        Args:
            results: List of search results

        Returns:
            Formatted string for AI context
        """
        if not results:
            return "No search results found."

        lines = ["Web search results:\n"]

        for i, result in enumerate(results, 1):
            trust_badge = {
                SourceTrust.OFFICIAL: "[OFFICIAL]",
                SourceTrust.TRUSTED: "[TRUSTED]",
                SourceTrust.COMMUNITY: "[COMMUNITY]",
                SourceTrust.UNKNOWN: "[UNVERIFIED]",
            }.get(result.trust_level, "")

            lines.append(f"{i}. {trust_badge} {result.title}")
            lines.append(f"   Source: {result.source_domain}")
            lines.append(f"   URL: {result.url}")
            lines.append(f"   {result.snippet}")
            lines.append("")

        return "\n".join(lines)


# Singleton instance
_search_service: WebSearchService | None = None


def get_web_search_service() -> WebSearchService:
    """Get the web search service singleton."""
    global _search_service
    if _search_service is None:
        _search_service = WebSearchService()
    return _search_service
