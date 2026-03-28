"""CI tests for education repeater fields on the toy Oracle HCM fixture.

Validates DomHand interactions with the inline education form on page 3:
school combobox search, date field extraction, field count, and search-term
quality.  Uses the existing ``examples/toy-oracle-hcm/index.html`` fixture.

  uv run pytest tests/ci/test_toy_experience_repeaters.py -v
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.tools.service import Tools
from ghosthands.actions.domhand_fill import (
    _build_inject_helpers_js,
    _is_latest_employer_search_field,
    _preferred_field_label,
    extract_visible_form_fields,
)
from ghosthands.actions.views import FormField, generate_dropdown_search_terms, normalize_name
from ghosthands.dom.dropdown_match import match_dropdown_option
from ghosthands.dom.fill_profile_resolver import _current_or_latest_employer_name
from ghosthands.dom.shadow_helpers import ensure_helpers

# Re-use the managed_browser_session / managed_local_fixture_server from the
# sibling test module so CI infrastructure stays DRY.
from tests.ci.test_toy_oracle_hcm_fixture import (
    _parse,
    managed_browser_session,
    managed_local_fixture_server,
)

_FIXTURE = (
    Path(__file__).resolve().parent.parent.parent
    / "examples"
    / "toy-oracle-hcm"
    / "index.html"
)
_PROFILE = Path(__file__).resolve().parent.parent.parent / "scripts" / "test_resume.json"


def _load_profile() -> dict:
    assert _PROFILE.is_file(), f"missing sample profile: {_PROFILE}"
    return json.loads(_PROFILE.read_text(encoding="utf-8"))


# ── Helpers ──────────────────────────────────────────────────────────

_SHOW_PAGE_3_JS = """() => {
    document.querySelectorAll('.apply-flow-page').forEach(p => p.style.display = 'none');
    const p3 = document.getElementById('page-3');
    if (p3) p3.style.display = 'block';
}"""

_CLICK_ADD_EDUCATION_JS = """() => {
    const btn = document.querySelector(
        '#page-3 .apply-flow-profile-item-tile__new-tile[data-profile-type="education"]'
    );
    if (btn) btn.click();
    return !!btn;
}"""


GPA_OPTIONS = [
    "Below 2.0",
    "2.0", "2.1", "2.2", "2.3", "2.4", "2.5", "2.6", "2.7", "2.8", "2.9",
    "3.0", "3.1", "3.2", "3.3", "3.4", "3.5", "3.6", "3.7", "3.8", "3.9",
    "4.0", "4.1", "4.2", "4.3",
]


def _make_field(
    *,
    name: str = "Test Field",
    field_type: str = "text",
    options: list[str] | None = None,
    field_id: str = "ff-1",
) -> FormField:
    return FormField(
        field_id=field_id,
        name=name,
        field_type=field_type,
        options=options or [],
    )


# ---------------------------------------------------------------------------
# 1. School combobox search, dropdown filter, and committed value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_education_school_combobox_searches_correctly() -> None:
    """Type 'New York' in the school combobox, verify dropdown filters to NYU,
    then select the option and verify the committed value sticks."""
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None

            # Navigate to page 3 and open the education inline form
            await page.evaluate(_SHOW_PAGE_3_JS)
            clicked = await page.evaluate(_CLICK_ADD_EDUCATION_JS)
            assert clicked, "Add Education button not found"
            await asyncio.sleep(0.4)

            # Inject __ff helpers for field extraction
            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            # Extract fields and verify the school combobox is detected
            fields = await extract_visible_form_fields(page)
            school_field = None
            for f in fields:
                if "school" in _preferred_field_label(f).lower():
                    school_field = f
                    break
            assert school_field is not None, (
                f"School field not detected. Labels: {[_preferred_field_label(f).lower() for f in fields]}"
            )

            # Type "New York" into the school combobox via JS and verify dropdown filters
            search_result = await page.evaluate("""() => {
                const input = document.querySelector(
                    '.profile-inline-form[data-profile-type="education"] input[id^="school-"]'
                );
                if (!input) return JSON.stringify({found: false});
                input.focus();
                input.value = 'New York';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                const controlsId = input.getAttribute('aria-controls');
                const dropdown = controlsId ? document.getElementById(controlsId) : null;
                if (!dropdown) return JSON.stringify({found: true, dropdown: false});
                const rows = dropdown.querySelectorAll('[role="row"]:not([data-empty-row="true"])');
                const visible = [];
                for (let i = 0; i < rows.length; i++) {
                    const s = getComputedStyle(rows[i]);
                    if (s.display !== 'none')
                        visible.push(rows[i].getAttribute('data-value') || rows[i].textContent.trim());
                }
                return JSON.stringify({found: true, dropdown: true, options: visible});
            }""")
            parsed = _parse(search_result)
            assert parsed["found"], "School input not found"
            assert parsed.get("dropdown"), "School dropdown did not open"
            option_texts = [o.lower() for o in parsed.get("options", [])]
            assert any("new york university" in o for o in option_texts), (
                f"Expected 'New York University' in dropdown, got: {parsed['options']}"
            )

            # Select "New York University" from the dropdown and verify committed value
            commit_result = await page.evaluate("""() => {
                const input = document.querySelector(
                    '.profile-inline-form[data-profile-type="education"] input[id^="school-"]'
                );
                if (!input) return JSON.stringify({selected: false, reason: 'no_input'});
                const controlsId = input.getAttribute('aria-controls');
                const dropdown = controlsId ? document.getElementById(controlsId) : null;
                if (!dropdown) return JSON.stringify({selected: false, reason: 'no_dropdown'});
                const row = Array.from(dropdown.querySelectorAll('[role="row"]'))
                    .find(r => (r.getAttribute('data-value') || '').trim() === 'New York University');
                if (!row) return JSON.stringify({selected: false, reason: 'no_matching_row'});
                row.click();
                return JSON.stringify({
                    selected: true,
                    value: input.value,
                    committed: input.dataset.committedValue || ''
                });
            }""")
            commit_parsed = _parse(commit_result)
            assert commit_parsed["selected"], (
                f"Could not select 'New York University': {commit_parsed}"
            )
            assert commit_parsed["value"] == "New York University", (
                f"Expected input value 'New York University', got: {commit_parsed['value']}"
            )
            assert commit_parsed["committed"] == "New York University", (
                f"Expected committed value 'New York University', got: {commit_parsed['committed']}"
            )


# ---------------------------------------------------------------------------
# 2. GPA dropdown nearest match (unit test, no browser)
# ---------------------------------------------------------------------------


class TestEducationGPADropdownNearestMatch:
    """Verify match_dropdown_option floors GPA to the nearest discrete option."""

    def test_391_floors_to_39(self):
        assert match_dropdown_option("3.91", GPA_OPTIONS) == "3.9"

    def test_385_floors_to_38(self):
        assert match_dropdown_option("3.85", GPA_OPTIONS) == "3.8"

    def test_exact_40(self):
        assert match_dropdown_option("4.0", GPA_OPTIONS) == "4.0"


# ---------------------------------------------------------------------------
# 3. Education date comboboxes are detected as select with month options
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_education_dates_extracted_as_combobox() -> None:
    """Start Date Month/Year and End Date Month/Year should be extracted
    as select fields, and month options should include standard month names."""
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None

            await page.evaluate(_SHOW_PAGE_3_JS)
            clicked = await page.evaluate(_CLICK_ADD_EDUCATION_JS)
            assert clicked, "Add Education button not found"
            await asyncio.sleep(0.4)

            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            fields = await extract_visible_form_fields(page)

            date_labels = {}
            for f in fields:
                label = _preferred_field_label(f).lower()
                if "start date month" in label or "end date month" in label:
                    date_labels[label] = f

            assert len(date_labels) >= 2, (
                f"Expected at least 2 date month fields, found: {list(date_labels.keys())}"
            )

            for label, field in date_labels.items():
                assert field.field_type == "select", (
                    f"{label} should be field_type='select', got '{field.field_type}'"
                )
                options_lower = [o.lower() for o in field.options]
                assert any("january" in o for o in options_lower), (
                    f"{label} missing January in options: {field.options[:5]}"
                )
                assert any("december" in o for o in options_lower), (
                    f"{label} missing December in options: {field.options[:5]}"
                )


# ---------------------------------------------------------------------------
# 4. Education inline form field count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_education_inline_form_field_count() -> None:
    """After clicking Add Education, the inline form should expose the
    expected education fields (Degree, School, Field of Study, dates)."""
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None

            await page.evaluate(_SHOW_PAGE_3_JS)
            clicked = await page.evaluate(_CLICK_ADD_EDUCATION_JS)
            assert clicked, "Add Education button not found"
            await asyncio.sleep(0.4)

            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            fields = await extract_visible_form_fields(page)
            labels_lower = [_preferred_field_label(f).lower() for f in fields]

            expected_keywords = ["degree", "school", "field of study", "start date", "end date"]
            for kw in expected_keywords:
                assert any(kw in l for l in labels_lower), (
                    f"Expected '{kw}' in education fields, got: {labels_lower}"
                )

            # The education form should expose at least 5 fields (degree, school,
            # field of study, start month, start year -- at minimum)
            edu_fields = [
                l for l in labels_lower
                if any(k in l for k in ("degree", "school", "field of study", "start date", "end date"))
            ]
            assert len(edu_fields) >= 5, (
                f"Expected >=5 education-related fields, got {len(edu_fields)}: {edu_fields}"
            )


# ---------------------------------------------------------------------------
# 5. School search does not use generic terms (unit test, no browser)
# ---------------------------------------------------------------------------


class TestSchoolSearchDoesNotUseGenericTerms:
    """generate_dropdown_search_terms should filter out generic school words
    that would match the wrong first-alphabetical result in Oracle comboboxes."""

    def test_nyu_no_university(self):
        terms = generate_dropdown_search_terms("New York University")
        terms_lower = [t.lower() for t in terms]
        assert "university" not in terms_lower, (
            f"'University' should be a stop word, got: {terms}"
        )

    def test_mit_no_technology_or_institute(self):
        terms = generate_dropdown_search_terms("Massachusetts Institute of Technology")
        terms_lower = [t.lower() for t in terms]
        assert "technology" not in terms_lower, (
            f"'Technology' should be a stop word, got: {terms}"
        )
        assert "institute" not in terms_lower, (
            f"'Institute' should be a stop word, got: {terms}"
        )

    def test_full_name_is_included(self):
        """The full school name should always be the first search term."""
        terms = generate_dropdown_search_terms("New York University")
        assert terms[0] == "New York University"


# ---------------------------------------------------------------------------
# 6. Employer name search fallback (unit tests, no browser)
# ---------------------------------------------------------------------------


class TestEmployerNameSearchFallback:
    """Verify _is_latest_employer_search_field and _current_or_latest_employer_name."""

    def test_latest_employer_field_detected(self):
        field = _make_field(
            name="Name of Latest Employer",
            field_type="select",
        )
        assert _is_latest_employer_search_field(field) is True

    def test_current_employer_field_detected(self):
        field = _make_field(
            name="Current Employer",
            field_type="select",
        )
        assert _is_latest_employer_search_field(field) is True

    def test_non_employer_field_rejected(self):
        field = _make_field(
            name="School / University",
            field_type="select",
        )
        assert _is_latest_employer_search_field(field) is False

    def test_text_type_rejected(self):
        """_is_latest_employer_search_field only matches select fields."""
        field = _make_field(
            name="Latest Employer",
            field_type="text",
        )
        assert _is_latest_employer_search_field(field) is False

    def test_profile_current_company_resolved(self):
        profile = _load_profile()
        result = _current_or_latest_employer_name(profile)
        assert result is not None, "Expected a current employer from profile"
        assert result == "NYU AI Mechatronics Systems Lab"

    def test_profile_fallback_to_experience(self):
        """When current_company is absent, resolve from experience entries."""
        profile = {"experience": [
            {"company": "Acme Corp", "currently_work_here": True},
            {"company": "OldCo", "currently_work_here": False},
        ]}
        result = _current_or_latest_employer_name(profile)
        assert result == "Acme Corp"

    def test_empty_profile_returns_none(self):
        assert _current_or_latest_employer_name({}) is None
        assert _current_or_latest_employer_name(None) is None
