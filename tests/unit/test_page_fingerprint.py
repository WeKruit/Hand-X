"""Unit tests for SPA page fingerprint transition detection.

Validates that _page_identity() includes the page_fingerprint field
so SPA transitions (same URL, different content) produce different
identity strings and trigger PAGE UPDATE.

  uv run pytest tests/unit/test_page_fingerprint.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from browser_use.browser.views import BrowserStateSummary


def _make_state(
    url: str = "https://workday.example.com/apply",
    title: str = "Workday",
    elem_count: int = 20,
    fingerprint: str = "",
) -> BrowserStateSummary:
    dom = MagicMock()
    dom.selector_map = {str(i): None for i in range(elem_count)}
    return BrowserStateSummary(
        dom_state=dom,
        url=url,
        title=title,
        tabs=[],
        page_fingerprint=fingerprint,
    )


def _page_identity(state: BrowserStateSummary) -> str:
    """Replicate _page_identity logic for isolated testing."""
    url = (state.url or "").strip()
    title = (state.title or "").strip()
    elem_count = 0
    try:
        if state.dom_state and state.dom_state.selector_map:
            elem_count = len(state.dom_state.selector_map)
    except Exception:
        pass
    elem_bucket = (elem_count // 5) * 5
    fp = (state.page_fingerprint or "").strip()
    if fp:
        # Fingerprint is authoritative — drop elem_bucket (changes on scroll)
        return f"{title}\n{url}\n{fp}"
    return f"{title}\n{url}\n{elem_bucket}"


class TestPageFingerprintIdentity:
    """_page_identity includes fingerprint to detect SPA transitions."""

    def test_different_fingerprints_produce_different_identity(self) -> None:
        """SPA transition: same URL/title/elem_count, different fingerprint."""
        s1 = _make_state(fingerprint="abc123def456")
        s2 = _make_state(fingerprint="789012345678")
        assert _page_identity(s1) != _page_identity(s2)

    def test_same_fingerprint_produces_same_identity(self) -> None:
        """Same page reload: everything identical."""
        s1 = _make_state(fingerprint="abc123def456")
        s2 = _make_state(fingerprint="abc123def456")
        assert _page_identity(s1) == _page_identity(s2)

    def test_empty_fingerprint_degrades_to_legacy_format(self) -> None:
        """When fingerprint is empty, identity matches legacy format."""
        s_empty = _make_state(fingerprint="")
        s_none = _make_state(fingerprint="")
        legacy = f"Workday\nhttps://workday.example.com/apply\n20"
        assert _page_identity(s_empty) == legacy
        assert _page_identity(s_none) == legacy

    def test_fingerprint_replaces_elem_bucket(self) -> None:
        """Fingerprint replaces elem_bucket (which changes on scroll)."""
        s = _make_state(fingerprint="deadbeef1234")
        parts = _page_identity(s).split("\n")
        assert len(parts) == 3
        assert parts[0] == "Workday"
        assert parts[1] == "https://workday.example.com/apply"
        assert parts[2] == "deadbeef1234"

    def test_scroll_does_not_change_identity_with_fingerprint(self) -> None:
        """Different elem_count (from scroll) but same fingerprint → same identity."""
        s1 = _make_state(elem_count=20, fingerprint="same_fp")
        s2 = _make_state(elem_count=120, fingerprint="same_fp")
        assert _page_identity(s1) == _page_identity(s2)

    def test_url_change_still_changes_identity(self) -> None:
        """URL-based transitions still work even with same fingerprint."""
        s1 = _make_state(url="https://example.com/page1", fingerprint="same")
        s2 = _make_state(url="https://example.com/page2", fingerprint="same")
        assert _page_identity(s1) != _page_identity(s2)

    def test_whitespace_fingerprint_treated_as_empty(self) -> None:
        """Whitespace-only fingerprint degrades to legacy format."""
        s = _make_state(fingerprint="   ")
        legacy = f"Workday\nhttps://workday.example.com/apply\n20"
        assert _page_identity(s) == legacy


class TestBrowserStateSummaryField:
    """BrowserStateSummary has page_fingerprint field with correct default."""

    def test_default_is_empty_string(self) -> None:
        s = _make_state()
        assert s.page_fingerprint == ""

    def test_accepts_fingerprint_value(self) -> None:
        s = _make_state(fingerprint="abc123def456")
        assert s.page_fingerprint == "abc123def456"
