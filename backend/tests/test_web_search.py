"""
Tests for the web search service.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.web_search import (
    SearchResult,
    SourceTrust,
    WebSearchService,
)


class TestSourceClassification:
    """Tests for source trust classification."""

    @pytest.fixture
    def service(self):
        """Create a web search service instance."""
        with patch("app.services.web_search.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                web_search_enabled=True,
                web_search_api_key="test-key",
                web_search_provider="brave",
                web_search_max_results=5,
                web_search_trusted_domains="custom-trusted.com",
                web_search_blocked_domains="blocked-site.com",
            )
            return WebSearchService()

    def test_official_kubernetes_io(self, service):
        """kubernetes.io should be classified as official."""
        trust = service.classify_source("https://kubernetes.io/docs/concepts/")
        assert trust == SourceTrust.OFFICIAL

    def test_official_subdomain(self, service):
        """Subdomains of official sources should be official."""
        trust = service.classify_source("https://docs.kubernetes.io/tutorials/")
        assert trust == SourceTrust.OFFICIAL

    def test_official_helm(self, service):
        """helm.sh should be classified as official."""
        trust = service.classify_source("https://helm.sh/docs/")
        assert trust == SourceTrust.OFFICIAL

    def test_trusted_github(self, service):
        """github.com should be classified as trusted."""
        trust = service.classify_source("https://github.com/kubernetes/kubernetes")
        assert trust == SourceTrust.TRUSTED

    def test_trusted_stackoverflow(self, service):
        """stackoverflow.com should be classified as trusted."""
        trust = service.classify_source("https://stackoverflow.com/questions/12345")
        assert trust == SourceTrust.TRUSTED

    def test_community_medium(self, service):
        """medium.com should be classified as community."""
        trust = service.classify_source("https://medium.com/@someone/article")
        assert trust == SourceTrust.COMMUNITY

    def test_community_devto(self, service):
        """dev.to should be classified as community."""
        trust = service.classify_source("https://dev.to/user/kubernetes-tips")
        assert trust == SourceTrust.COMMUNITY

    def test_unknown_random_site(self, service):
        """Unknown sites should be classified as unknown."""
        trust = service.classify_source("https://random-blog.xyz/post")
        assert trust == SourceTrust.UNKNOWN

    def test_blocked_site(self, service):
        """Blocked sites should be classified as blocked."""
        trust = service.classify_source("https://blocked-site.com/article")
        assert trust == SourceTrust.BLOCKED

    def test_custom_trusted_from_config(self, service):
        """Custom trusted domains from config should be trusted."""
        trust = service.classify_source("https://custom-trusted.com/docs")
        assert trust == SourceTrust.TRUSTED

    def test_www_prefix_stripped(self, service):
        """www. prefix should be stripped for classification."""
        trust = service.classify_source("https://www.kubernetes.io/docs/")
        assert trust == SourceTrust.OFFICIAL


class TestDomainExtraction:
    """Tests for domain extraction."""

    @pytest.fixture
    def service(self):
        """Create a web search service instance."""
        with patch("app.services.web_search.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                web_search_enabled=True,
                web_search_api_key="test-key",
                web_search_provider="brave",
                web_search_max_results=5,
                web_search_trusted_domains="",
                web_search_blocked_domains="",
            )
            return WebSearchService()

    def test_basic_domain(self, service):
        """Should extract basic domain."""
        domain = service.get_domain("https://example.com/path")
        assert domain == "example.com"

    def test_subdomain(self, service):
        """Should include subdomain."""
        domain = service.get_domain("https://docs.example.com/path")
        assert domain == "docs.example.com"

    def test_www_stripped(self, service):
        """Should strip www. prefix."""
        domain = service.get_domain("https://www.example.com/path")
        assert domain == "example.com"

    def test_port_included(self, service):
        """Should include port if present."""
        domain = service.get_domain("https://example.com:8080/path")
        assert domain == "example.com:8080"


class TestSearchResultFormatting:
    """Tests for formatting search results for AI."""

    @pytest.fixture
    def service(self):
        """Create a web search service instance."""
        with patch("app.services.web_search.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                web_search_enabled=True,
                web_search_api_key="test-key",
                web_search_provider="brave",
                web_search_max_results=5,
                web_search_trusted_domains="",
                web_search_blocked_domains="",
            )
            return WebSearchService()

    def test_format_empty_results(self, service):
        """Should handle empty results."""
        formatted = service.format_results_for_ai([])
        assert formatted == "No search results found."

    def test_format_single_result(self, service):
        """Should format a single result."""
        results = [
            SearchResult(
                title="Kubernetes Docs",
                url="https://kubernetes.io/docs/",
                snippet="Official documentation",
                source_domain="kubernetes.io",
                trust_level=SourceTrust.OFFICIAL,
            )
        ]

        formatted = service.format_results_for_ai(results)

        assert "Web search results:" in formatted
        assert "[OFFICIAL]" in formatted
        assert "Kubernetes Docs" in formatted
        assert "kubernetes.io" in formatted

    def test_format_multiple_results(self, service):
        """Should format multiple results with numbering."""
        results = [
            SearchResult(
                title="Result 1",
                url="https://kubernetes.io/",
                snippet="Snippet 1",
                source_domain="kubernetes.io",
                trust_level=SourceTrust.OFFICIAL,
            ),
            SearchResult(
                title="Result 2",
                url="https://stackoverflow.com/",
                snippet="Snippet 2",
                source_domain="stackoverflow.com",
                trust_level=SourceTrust.TRUSTED,
            ),
        ]

        formatted = service.format_results_for_ai(results)

        assert "1." in formatted
        assert "2." in formatted
        assert "[OFFICIAL]" in formatted
        assert "[TRUSTED]" in formatted


class TestServiceEnabled:
    """Tests for service enablement checks."""

    def test_enabled_when_configured(self):
        """Should be enabled when API key is configured."""
        with patch("app.services.web_search.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                web_search_enabled=True,
                web_search_api_key="test-key",
                web_search_provider="brave",
                web_search_max_results=5,
                web_search_trusted_domains="",
                web_search_blocked_domains="",
            )
            service = WebSearchService()
            assert service.is_enabled is True

    def test_disabled_when_no_api_key(self):
        """Should be disabled when API key is missing."""
        with patch("app.services.web_search.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                web_search_enabled=True,
                web_search_api_key=None,
                web_search_provider="brave",
                web_search_max_results=5,
                web_search_trusted_domains="",
                web_search_blocked_domains="",
            )
            service = WebSearchService()
            assert service.is_enabled is False

    def test_disabled_when_explicitly_disabled(self):
        """Should be disabled when web_search_enabled is False."""
        with patch("app.services.web_search.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                web_search_enabled=False,
                web_search_api_key="test-key",
                web_search_provider="brave",
                web_search_max_results=5,
                web_search_trusted_domains="",
                web_search_blocked_domains="",
            )
            service = WebSearchService()
            assert service.is_enabled is False
