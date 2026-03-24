"""Shared Pydantic models for platform configuration."""

from pydantic import BaseModel, Field


class PlatformConfig(BaseModel):
    """Configuration for a single ATS platform.

    Encapsulates everything the agent needs to know about a platform:
    URL matching, allowed domains, guardrails for the system prompt,
    auth flow descriptions, and page-type detection patterns.
    """

    name: str = Field(description="Platform identifier: 'workday', 'greenhouse', 'lever', etc.")
    display_name: str = Field(description="Human-readable platform name.")
    url_patterns: list[str] = Field(
        default_factory=list,
        description="Domain substrings that identify this platform (e.g. 'myworkdayjobs.com').",
    )
    allowed_domains: list[str] = Field(
        default_factory=list,
        description="Domains the agent is allowed to navigate to on this platform.",
    )
    guardrails: list[str] = Field(
        default_factory=list,
        description="Rules injected into the system prompt for this platform.",
    )
    auth_flow: str | None = Field(
        default=None,
        description="Description of the platform's authentication flow.",
    )
    page_types: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of page_type name → detection pattern/description.",
    )
    shadow_dom_selectors: dict[str, str] = Field(
        default_factory=dict,
        description="Platform-specific shadow DOM / custom element selectors.",
    )
    content_markers: list[str] = Field(
        default_factory=list,
        description=(
            "DOM/text markers that help identify hosted or white-label versions "
            "of this platform when the URL alone is ambiguous."
        ),
    )
    strong_content_markers: list[str] = Field(
        default_factory=list,
        description=(
            "Higher-confidence markers that can identify a hosted or white-label "
            "platform on their own when the URL is ambiguous."
        ),
    )
    content_marker_min_hits: int = Field(
        default=2,
        description=(
            "Minimum number of non-strong content markers that must match before "
            "inferring a hosted platform from generic page text."
        ),
    )
    navigation_hints: list[str] = Field(
        default_factory=list,
        description="Hints about how multi-page forms work on this platform.",
    )
    form_strategy: str = Field(
        default="dom_first",
        description="Preferred form-filling strategy: 'dom_first', 'fill_first', 'repeater_expand'.",
    )
    automation_id_map: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Mapping of logical field role (e.g. 'form_label', 'date_month') to "
            "a CSS selector or data-automation-id pattern the executor can use."
        ),
    )
    fill_overrides: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-control-type fill strategy overrides keyed by field_type. "
            "Values are fill strategy names: 'native_select', 'combobox_toggle', "
            "'searchable_dropdown', 'playwright_fill', etc."
        ),
    )
    single_page_presubmit_allowed: bool = Field(
        default=False,
        description=(
            "Whether a single-page form may stop at the final pre-submit state "
            "(final submit visible, no remaining required/invalid fields) "
            "instead of requiring a separate read-only review page."
        ),
    )

    def matches_url(self, url: str) -> bool:
        """Check if a URL matches this platform's URL patterns."""
        normalized = url.lower()
        return any(pattern in normalized for pattern in self.url_patterns)

    def get_system_prompt_rules(self) -> str:
        """Return guardrails formatted for injection into a system prompt."""
        if not self.guardrails:
            return ""
        lines = [f"## {self.display_name} Platform Rules", ""]
        for i, rule in enumerate(self.guardrails, 1):
            lines.append(f"{i}. {rule}")
        return "\n".join(lines)
