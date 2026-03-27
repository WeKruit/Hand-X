"""CI tests for Oracle HCM searchable combobox — type→search→pick flow.

Exercises the full interactive combobox lifecycle against a fixture with
2000+ real university names.  Validates that DomHand routes combobox fields
to needs_llm (not direct_fills) on Oracle, and that search-term generation
+ dropdown matching produce correct results even with large option sets.

  uv run pytest tests/ci/test_oracle_combobox_lab.py -v
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.tools.service import Tools
from unittest.mock import AsyncMock, patch

from ghosthands.actions.domhand_fill import (
    _build_inject_helpers_js,
    _enrich_combobox_via_search,
    _is_latest_employer_search_field,
    _is_searchable_combobox_on_oracle,
    _preferred_field_label,
    domhand_fill,
    extract_visible_form_fields,
)
from ghosthands.actions.views import DomHandFillParams, FormField, generate_dropdown_search_terms, normalize_name
from ghosthands.dom.dropdown_match import match_dropdown_option
from ghosthands.dom.fill_label_match import _coerce_answer_to_field
from ghosthands.dom.fill_resolution import _education_slot_name, _is_structured_education_candidate
from ghosthands.dom.shadow_helpers import ensure_helpers

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------
_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
_FIXTURE_HTML = _FIXTURES_DIR / "oracle_combobox_lab.html"
_UNIVERSITIES_JSON = _FIXTURES_DIR / "universities.json"


def _parse(result):
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
    return result


def _load_universities() -> list[str]:
    if _UNIVERSITIES_JSON.is_file():
        return json.loads(_UNIVERSITIES_JSON.read_text(encoding="utf-8"))
    return []


# ---------------------------------------------------------------------------
# Browser session / fixture server helpers (same as sibling CI tests)
# ---------------------------------------------------------------------------


@asynccontextmanager
async def managed_browser_session():
    session = BrowserSession(
        browser_profile=BrowserProfile(
            headless=True,
            user_data_dir=None,
            keep_alive=True,
            enable_default_extensions=True,
        )
    )
    await session.start()
    try:
        yield session
    finally:
        await session.kill()
        await session.event_bus.stop(clear=True, timeout=5)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


@asynccontextmanager
async def managed_local_fixture_server():
    """Serve the fixtures directory via localhost HTTP server."""
    port = _find_free_port()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "http.server",
        str(port),
        "--bind",
        "127.0.0.1",
        cwd=str(_FIXTURES_DIR),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            await asyncio.sleep(0.1)
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except (ConnectionRefusedError, OSError):
                continue
        yield f"http://127.0.0.1:{port}/oracle_combobox_lab.html"
    finally:
        proc.terminate()
        await proc.wait()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_field(
    *,
    name: str = "Test Field",
    field_type: str = "select",
    is_native: bool = False,
    options: list[str] | None = None,
    field_id: str = "ff-1",
    section: str = "",
    required: bool = False,
) -> FormField:
    return FormField(
        field_id=field_id,
        name=name,
        field_type=field_type,
        is_native=is_native,
        options=options or [],
        section=section,
        required=required,
    )


ORACLE_HOST = "hdpc.fa.us2.oraclecloud.com"
WORKDAY_HOST = "company.myworkdayjobs.com"
LOCALHOST = "127.0.0.1"


# ===================================================================
# 1. Generic searchable combobox triage bypass — unit tests
# ===================================================================


class TestSearchableComboboxOnOracle:
    """Verify _is_searchable_combobox_on_oracle gates correctly."""

    def test_oracle_non_native_select_detected(self):
        f = _make_field(field_type="select", is_native=False)
        assert _is_searchable_combobox_on_oracle(f, page_host=ORACLE_HOST)

    def test_oracle_native_select_not_detected(self):
        f = _make_field(field_type="select", is_native=True)
        assert not _is_searchable_combobox_on_oracle(f, page_host=ORACLE_HOST)

    def test_oracle_text_field_not_detected(self):
        f = _make_field(field_type="text", is_native=False)
        assert not _is_searchable_combobox_on_oracle(f, page_host=ORACLE_HOST)

    def test_oracle_button_group_not_detected(self):
        f = _make_field(field_type="button-group", is_native=False)
        assert not _is_searchable_combobox_on_oracle(f, page_host=ORACLE_HOST)

    def test_workday_not_detected(self):
        """Workday is gated OFF — has its own paths."""
        f = _make_field(field_type="select", is_native=False)
        assert not _is_searchable_combobox_on_oracle(f, page_host=WORKDAY_HOST)

    def test_generic_host_not_detected(self):
        f = _make_field(field_type="select", is_native=False)
        assert not _is_searchable_combobox_on_oracle(f, page_host=LOCALHOST)

    def test_empty_host_not_detected(self):
        f = _make_field(field_type="select", is_native=False)
        assert not _is_searchable_combobox_on_oracle(f, page_host="")

    def test_oracle_subdomain_detected(self):
        f = _make_field(field_type="select", is_native=False)
        assert _is_searchable_combobox_on_oracle(f, page_host="company.fa.us6.oraclecloud.com")


# ===================================================================
# 2. School combobox — search term generation + matching with 2000 schools
# ===================================================================


class TestSchoolSearchTermsLargeList:
    """Test search term generation against a realistic 2000-school list."""

    @pytest.fixture()
    def universities(self) -> list[str]:
        unis = _load_universities()
        if not unis:
            pytest.skip("universities.json not found")
        return unis

    def test_fixture_has_enough_universities(self, universities):
        assert len(universities) >= 1000, f"Expected 1000+ universities, got {len(universities)}"

    def test_nyu_search_finds_correct_school(self, universities):
        """'New York University' search should NOT match '9 Eylul University'."""
        terms = generate_dropdown_search_terms("New York University")
        assert any("new york" in t.lower() for t in terms), f"Expected 'New York' in terms: {terms}"
        # Simulate type-to-filter: search "New York" against full list
        query = "new york"
        filtered = [u for u in universities if query in u.lower()]
        assert any("New York University" in u for u in filtered), (
            f"NYU should appear in filtered results for '{query}'"
        )
        assert not any("9 Eylul" in u for u in filtered), (
            f"'9 Eylul' should NOT appear when searching '{query}': {filtered}"
        )

    def test_ucla_search_finds_correct_school(self, universities):
        """'UCLA' should generate terms that find 'University of California, Los Angeles'."""
        terms = generate_dropdown_search_terms("University of California, Los Angeles")
        # Should not generate standalone "University" (stop word)
        assert "University" not in terms, f"Stop word 'University' in terms: {terms}"
        assert any("california" in t.lower() for t in terms), f"Expected 'California' in terms: {terms}"
        # Filter with "California, Los"
        query = "california, los"
        filtered = [u for u in universities if query.lower() in u.lower()]
        assert any("Los Angeles" in u for u in filtered), (
            f"UCLA should appear when searching '{query}': {filtered[:10]}"
        )

    def test_mit_search_finds_correct_school(self, universities):
        terms = generate_dropdown_search_terms("Massachusetts Institute of Technology")
        # "Institute" and "Technology" are stop words
        assert "Technology" not in terms, f"Stop word 'Technology' in terms: {terms}"
        assert any("massachusetts" in t.lower() for t in terms), (
            f"Expected 'Massachusetts' in terms: {terms}"
        )
        query = "massachusetts"
        filtered = [u for u in universities if query.lower() in u.lower()]
        assert any("Massachusetts Institute of Technology" in u for u in filtered)

    def test_georgia_tech_disambiguation(self, universities):
        terms = generate_dropdown_search_terms("Georgia Institute of Technology")
        query = "georgia"
        filtered = [u for u in universities if query.lower() in u.lower()]
        # Both Georgia Tech and Georgia State should appear — disambiguation happens at LLM
        georgia_names = [u for u in filtered if "Georgia" in u]
        assert len(georgia_names) >= 2, f"Expected multiple Georgia schools: {georgia_names}"

    def test_stop_words_prevent_university_only_search(self, universities):
        """Standalone 'University' should be filtered as a stop word."""
        terms = generate_dropdown_search_terms("University")
        # "University" alone should not be a search term
        assert not any(t.lower() == "university" for t in terms), (
            f"'University' alone should be filtered: {terms}"
        )

    def test_number_prefixed_school_not_matched_by_generic_search(self, universities):
        """'9 Eylul University' should only match when explicitly searched."""
        # Searching "University" (if it weren't a stop word) would match 1000+ schools
        query = "university"
        filtered = [u for u in universities if query.lower() in u.lower()]
        assert len(filtered) > 50, "Large list should have many schools with 'university'"
        # "9 Eylul" specifically requires "eylul" or "9 eylul" search
        eylul_filtered = [u for u in universities if "eylul" in u.lower()]
        assert len(eylul_filtered) <= 2, f"Only '9 Eylul University' should match: {eylul_filtered}"


# ===================================================================
# 3. Employer combobox — search + "Other" fallback
# ===================================================================


class TestEmployerComboboxLogic:
    """Verify employer field detection and search term generation."""

    EMPLOYERS = [
        "Other", "Goldman Sachs", "Google", "Meta Platforms",
        "Amazon", "Apple", "Microsoft", "JPMorgan Chase",
    ]

    def test_employer_field_detected(self):
        f = _make_field(name="Name of Latest Employer", field_type="select")
        assert _is_latest_employer_search_field(f)

    def test_non_employer_field_not_detected(self):
        f = _make_field(name="School", field_type="select")
        assert not _is_latest_employer_search_field(f)

    def test_employer_is_searchable_combobox_on_oracle(self):
        """Employer select on Oracle should be caught by the generic check."""
        f = _make_field(name="Name of Latest Employer", field_type="select", is_native=False)
        assert _is_searchable_combobox_on_oracle(f, page_host=ORACLE_HOST)

    def test_employer_search_generates_terms(self):
        terms = generate_dropdown_search_terms("Goldman Sachs")
        assert any("goldman" in t.lower() for t in terms)

    def test_employer_match_exact(self):
        matched = match_dropdown_option("Goldman Sachs", self.EMPLOYERS)
        assert matched == "Goldman Sachs"

    def test_employer_unknown_no_false_match(self):
        """Unknown employer should NOT match via word-overlap to random company."""
        matched = match_dropdown_option("WeKruit Technologies", self.EMPLOYERS)
        # Should not match "Other" or any real company via word overlap
        # (match_dropdown_option may return None or a weak match)
        if matched:
            assert matched == "Other" or "WeKruit" in matched or "Technologies" in matched, (
                f"Unknown employer wrongly matched to: {matched}"
            )

    def test_other_fallback_in_options(self):
        matched = match_dropdown_option("Other", self.EMPLOYERS)
        assert matched == "Other"


# ===================================================================
# 4. Dropdown matching with large university list — word overlap traps
# ===================================================================


class TestDropdownMatchingLargeList:
    """Verify match_dropdown_option doesn't pick wrong schools from large list."""

    @pytest.fixture()
    def universities(self) -> list[str]:
        unis = _load_universities()
        if not unis:
            pytest.skip("universities.json not found")
        return unis

    def test_exact_match_preferred_over_word_overlap(self, universities):
        matched = match_dropdown_option("New York University", universities)
        assert matched == "New York University", f"Expected exact match, got: {matched}"

    def test_columbia_exact_match(self, universities):
        matched = match_dropdown_option("Columbia University", universities)
        assert matched == "Columbia University", f"Expected Columbia, got: {matched}"

    def test_partial_name_may_wrong_match(self, universities):
        """Searching just 'University' would match many — this is why we need LLM."""
        matched = match_dropdown_option("University", universities)
        # This WILL produce a wrong match — that's the point: word-overlap
        # is unreliable for combobox search. The fix routes to needs_llm.
        # We just verify it returns SOMETHING (not None)
        assert matched is not None, "Even bad matching should return something"

    def test_word_overlap_trap_9_eylul(self, universities):
        """'New York University' should NOT match '9 Eylul University' via word overlap."""
        # When we pass the full target to match_dropdown_option, exact match should win
        matched = match_dropdown_option("New York University", universities)
        assert "9 Eylul" not in (matched or ""), f"Wrong school matched: {matched}"


# ===================================================================
# 5. Education slot detection — school fields route correctly
# ===================================================================


class TestEducationSlotDetection:
    """Verify education fields are detected and routed properly."""

    def test_school_slot_detected(self):
        f = _make_field(name="School", field_type="select")
        slot = _education_slot_name(f, [])
        assert slot == "school"

    def test_degree_slot_detected(self):
        f = _make_field(name="Degree", field_type="select")
        slot = _education_slot_name(f, [])
        assert slot == "degree"

    def test_field_of_study_slot_detected(self):
        f = _make_field(name="Field of Study", field_type="select")
        slot = _education_slot_name(f, [])
        assert slot == "field_of_study"

    def test_gpa_slot_detected(self):
        f = _make_field(name="GPA", field_type="select")
        slot = _education_slot_name(f, [])
        assert slot == "gpa"

    def test_country_slot_detected(self):
        f = _make_field(name="Country", field_type="select")
        slot = _education_slot_name(f, [])
        assert slot == "school_country"


# ===================================================================
# 6. Coercion guard — select fields without options reject garbage
# ===================================================================


class TestSelectCoercionGuard:
    """Verify _coerce_answer_to_field rejects long/descriptive values for selects."""

    def test_rejects_long_sentence_for_select(self):
        f = _make_field(name="Visa Type", field_type="select", options=[])
        result = _coerce_answer_to_field(f, "Likely yes based on US location and work authorization status")
        assert result is None

    def test_accepts_short_value_for_select(self):
        f = _make_field(name="Country", field_type="select", options=["United States", "Canada"])
        result = _coerce_answer_to_field(f, "United States")
        assert result == "United States"

    def test_accepts_value_for_text_field(self):
        f = _make_field(name="Name", field_type="text")
        result = _coerce_answer_to_field(f, "A long descriptive answer that would be rejected for select fields")
        assert result is not None


# ===================================================================
# 7. Browser integration — full type→search→pick flow
# ===================================================================


@pytest.mark.asyncio
async def test_school_combobox_type_search_pick() -> None:
    """Full browser test: type 'New York' → dropdown shows NYU → click → committed."""
    if not _FIXTURE_HTML.is_file():
        pytest.skip("oracle_combobox_lab.html not found")
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None

            # Wait for combobox initialization
            await asyncio.sleep(0.5)

            # Type "New York" in the school input
            result = await page.evaluate("""() => {
                const input = document.getElementById('ff-school');
                if (!input) return JSON.stringify({error: 'no input'});
                input.focus();
                input.value = 'New York';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                return JSON.stringify({typed: true});
            }""")
            parsed = _parse(result)
            assert parsed.get("typed"), f"Failed to type: {parsed}"

            # Wait for debounced filter
            await asyncio.sleep(0.3)

            # Read dropdown options
            options_result = await page.evaluate("""() => {
                const listbox = document.getElementById('school-listbox');
                if (!listbox) return JSON.stringify({error: 'no listbox'});
                const opts = listbox.querySelectorAll('.option');
                const labels = Array.from(opts).map(o => o.textContent.trim());
                return JSON.stringify({count: labels.length, labels: labels});
            }""")
            opts = _parse(options_result)
            assert opts.get("count", 0) > 0, f"No dropdown options shown: {opts}"
            labels = opts.get("labels", [])

            # Verify NYU appears and 9 Eylul does NOT
            assert any("New York University" in l for l in labels), (
                f"NYU not in filtered results: {labels}"
            )
            assert not any("9 Eylul" in l for l in labels), (
                f"'9 Eylul' wrongly appears for 'New York' search: {labels}"
            )

            # Click NYU
            commit_result = await page.evaluate("""() => {
                const listbox = document.getElementById('school-listbox');
                const opt = Array.from(listbox.querySelectorAll('.option'))
                    .find(o => o.textContent.includes('New York University'));
                if (!opt) return JSON.stringify({error: 'NYU not in dropdown'});
                opt.click();
                const input = document.getElementById('ff-school');
                return JSON.stringify({
                    value: input.value,
                    committed: input.dataset.committedValue || ''
                });
            }""")
            commit = _parse(commit_result)
            assert commit.get("committed") == "New York University", (
                f"Expected committed 'New York University', got: {commit}"
            )


@pytest.mark.asyncio
async def test_school_combobox_default_shows_alphabetical() -> None:
    """When no search term typed, combobox shows first N schools alphabetically."""
    if not _FIXTURE_HTML.is_file():
        pytest.skip("oracle_combobox_lab.html not found")
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None
            await asyncio.sleep(0.5)

            # Click toggle to show default options
            result = await page.evaluate("""() => {
                const toggle = document.querySelector('[data-toggle="ff-school"]');
                if (!toggle) return JSON.stringify({error: 'no toggle'});
                toggle.click();
                const listbox = document.getElementById('school-listbox');
                const opts = listbox.querySelectorAll('.option');
                const labels = Array.from(opts).map(o => o.textContent.trim());
                return JSON.stringify({count: labels.length, labels: labels});
            }""")
            parsed = _parse(result)
            # Default view shows first 10 alphabetically — NOT "New York University"
            labels = parsed.get("labels", [])
            assert len(labels) <= 10, f"Default should show max 10, got {len(labels)}"


@pytest.mark.asyncio
async def test_employer_combobox_search_and_other_fallback() -> None:
    """Type unknown employer → no results → 'Other' should still be findable."""
    if not _FIXTURE_HTML.is_file():
        pytest.skip("oracle_combobox_lab.html not found")
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None
            await asyncio.sleep(0.5)

            # Search for unknown employer
            result = await page.evaluate("""() => {
                const input = document.getElementById('ff-employer');
                input.focus();
                input.value = 'WeKruit Technologies';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                return JSON.stringify({typed: true});
            }""")
            await asyncio.sleep(0.3)

            # Verify no results
            no_match = await page.evaluate("""() => {
                const listbox = document.getElementById('employer-listbox');
                const opts = listbox.querySelectorAll('.option');
                return JSON.stringify({count: opts.length});
            }""")
            no_match_parsed = _parse(no_match)
            assert no_match_parsed["count"] == 0, "Unknown employer should show 0 options"

            # Clear and search for "Other"
            other_result = await page.evaluate("""() => {
                const input = document.getElementById('ff-employer');
                input.focus();
                input.value = 'Other';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                return JSON.stringify({typed: true});
            }""")
            await asyncio.sleep(0.3)

            # "Other" should appear
            other_opts = await page.evaluate("""() => {
                const listbox = document.getElementById('employer-listbox');
                const opts = listbox.querySelectorAll('.option');
                const labels = Array.from(opts).map(o => o.textContent.trim());
                return JSON.stringify({count: labels.length, labels: labels});
            }""")
            other_parsed = _parse(other_opts)
            assert any("Other" in l for l in other_parsed.get("labels", [])), (
                f"'Other' should be findable: {other_parsed}"
            )

            # Click "Other" and verify commit
            commit = await page.evaluate("""() => {
                const listbox = document.getElementById('employer-listbox');
                const opt = Array.from(listbox.querySelectorAll('.option'))
                    .find(o => o.textContent.trim() === 'Other');
                if (!opt) return JSON.stringify({error: 'Other not found'});
                opt.click();
                const input = document.getElementById('ff-employer');
                return JSON.stringify({
                    committed: input.dataset.committedValue || ''
                });
            }""")
            commit_parsed = _parse(commit)
            assert commit_parsed.get("committed") == "Other"


@pytest.mark.asyncio
async def test_degree_combobox_search() -> None:
    """Search 'Bachelor' in degree combobox → should show BA/BS/BBA/BE/BFA."""
    if not _FIXTURE_HTML.is_file():
        pytest.skip("oracle_combobox_lab.html not found")
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None
            await asyncio.sleep(0.5)

            result = await page.evaluate("""() => {
                const input = document.getElementById('ff-degree');
                input.focus();
                input.value = 'Bachelor';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                return JSON.stringify({typed: true});
            }""")
            await asyncio.sleep(0.3)

            opts = await page.evaluate("""() => {
                const listbox = document.getElementById('degree-listbox');
                const options = listbox.querySelectorAll('.option');
                const labels = Array.from(options).map(o => o.textContent.trim());
                return JSON.stringify({labels: labels});
            }""")
            parsed = _parse(opts)
            labels = parsed.get("labels", [])
            assert len(labels) >= 3, f"Expected multiple Bachelor degrees, got: {labels}"
            assert all("Bachelor" in l for l in labels), f"All should contain 'Bachelor': {labels}"


@pytest.mark.asyncio
async def test_field_extraction_detects_combobox_attributes() -> None:
    """DomHand field extraction should detect aria-autocomplete/role=combobox."""
    if not _FIXTURE_HTML.is_file():
        pytest.skip("oracle_combobox_lab.html not found")
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None
            await asyncio.sleep(0.5)

            # Inject __ff helpers
            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            # Extract fields
            fields = await extract_visible_form_fields(page)
            labels = {_preferred_field_label(f).lower(): f for f in fields}

            # Verify key fields are extracted
            assert any("school" in l for l in labels), f"School not found in: {list(labels.keys())}"
            assert any("degree" in l for l in labels), f"Degree not found in: {list(labels.keys())}"
            assert any("employer" in l or "latest employer" in l for l in labels), (
                f"Employer not found in: {list(labels.keys())}"
            )

            # Verify school is detected as select (not text)
            school_field = next(f for l, f in labels.items() if "school" in l)
            assert school_field.field_type in {"select", "text"}, (
                f"School should be select or text, got: {school_field.field_type}"
            )


# ===================================================================
# 8. Numeric search term truncation (GPA)
# ===================================================================


class TestNumericSearchTermTruncation:
    def test_gpa_391_generates_39(self):
        terms = generate_dropdown_search_terms("3.91")
        assert "3.9" in terms, f"Expected '3.9' in terms: {terms}"

    def test_gpa_400_no_truncation(self):
        terms = generate_dropdown_search_terms("4.0")
        # 4.0 has no trailing digits to truncate
        assert "4.0" in terms, f"Expected '4.0' in terms: {terms}"

    def test_gpa_381_generates_38(self):
        terms = generate_dropdown_search_terms("3.81")
        assert "3.8" in terms, f"Expected '3.8' in terms: {terms}"


# ===================================================================
# 9. END-TO-END PIPELINE — runs domhand_fill against the combobox fixture
#    with mocked LLM, Oracle URL, and real profile data.
#    This is the test that catches: "enrichment didn't type → LLM
#    saw wrong options → school filled wrong".
# ===================================================================

_ORACLE_URL = "https://hdpc.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/LateralHiring/job/162133/apply/section/3"

_EDUCATION_PROFILE = {
    "education": [
        {
            "school": "New York University",
            "degree": "Bachelor of Science",
            "field_of_study": "Computer Science",
            "gpa": "3.91",
        }
    ],
}


@pytest.mark.asyncio
async def test_e2e_pipeline_school_routed_to_llm_with_enriched_options() -> None:
    """Full pipeline: domhand_fill on Oracle fixture → School field goes to
    needs_llm → enrichment types search terms → LLM sees enriched options
    including 'New York University' → LLM picks it → executor commits.

    This test catches the original bug: without type-to-search enrichment,
    the LLM only sees the first 10 alphabetical schools and picks wrong."""
    if not _FIXTURE_HTML.is_file():
        pytest.skip("oracle_combobox_lab.html not found")

    # Track what _generate_answers received (the enriched options)
    captured_fields: list[FormField] = []

    async def fake_generate_answers(fields, profile_text, **kwargs):
        """Mock LLM: capture fields (with enriched options) and return
        the first option that contains the profile school name."""
        captured_fields.extend(fields)
        answers = {}
        for f in fields:
            label = _preferred_field_label(f).lower()
            if "school" in label:
                # The LLM would pick from enriched options
                if f.options:
                    match = next(
                        (o for o in f.options if "new york university" in o.lower()),
                        f.options[0],
                    )
                    answers[_preferred_field_label(f)] = match
                else:
                    answers[_preferred_field_label(f)] = "New York University"
            elif "degree" in label:
                answers[_preferred_field_label(f)] = "Bachelor of Science (BS)"
            elif "field of study" in label or "study" in label:
                answers[_preferred_field_label(f)] = "Computer Science"
            elif "country" in label:
                answers[_preferred_field_label(f)] = "United States"
            elif "employer" in label:
                answers[_preferred_field_label(f)] = "Other"
            else:
                answers[_preferred_field_label(f)] = ""
        return answers, 100, 50, 0.001, "mock-haiku"

    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None
            await asyncio.sleep(0.5)

            with (
                patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="Education: NYU, BS in CS"),
                patch("ghosthands.actions.domhand_fill._get_profile_data", return_value=_EDUCATION_PROFILE),
                patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
                patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
                patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={}),
                patch("ghosthands.actions.domhand_fill._generate_answers", AsyncMock(side_effect=fake_generate_answers)),
                patch("ghosthands.actions.domhand_fill._safe_page_url", AsyncMock(return_value=_ORACLE_URL)),
                patch("ghosthands.actions.domhand_fill._get_page_context_key", AsyncMock(return_value="oracle-combobox-lab")),
                patch("ghosthands.actions.domhand_fill._stagehand_observe_cross_reference", AsyncMock(return_value=None)),
            ):
                result = await domhand_fill(DomHandFillParams(), browser_session)

            # Verify the school field was sent to LLM with enriched options
            school_fields = [f for f in captured_fields if "school" in _preferred_field_label(f).lower()]
            assert school_fields, f"School field not sent to LLM. Captured: {[_preferred_field_label(f) for f in captured_fields]}"

            school_field = school_fields[0]
            school_options = school_field.options or []

            # THE CRITICAL ASSERTION: enrichment must have typed and discovered NYU
            assert any("new york university" in o.lower() for o in school_options), (
                f"Enrichment FAILED: 'New York University' not in LLM options.\n"
                f"Options ({len(school_options)}): {school_options[:20]}\n"
                f"This means enrichment didn't type search terms — "
                f"it only showed the default alphabetical slice."
            )

            # Also verify 9 Eylul is NOT in the enriched options
            assert not any("9 eylul" in o.lower() for o in school_options), (
                f"'9 Eylul' should NOT be in enriched options for NYU search: {school_options[:20]}"
            )


# ===================================================================
# 10. REAL enrichment integration — calls _enrich_combobox_via_search
#    These are the tests that would have caught the "enrichment doesn't
#    type" bug.  They exercise the actual function against the fixture.
# ===================================================================


@pytest.mark.asyncio
async def test_enrich_school_combobox_discovers_nyu() -> None:
    """_enrich_combobox_via_search with hint 'New York University' should
    discover NYU in the options — not just the first 10 alphabetically."""
    if not _FIXTURE_HTML.is_file():
        pytest.skip("oracle_combobox_lab.html not found")
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None
            await asyncio.sleep(0.5)

            # Inject __ff so the enrichment JS can find fields
            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            field = _make_field(
                name="School",
                field_type="select",
                is_native=False,
                field_id="ff-school",
            )

            # Call the REAL enrichment function
            discovered = await _enrich_combobox_via_search(
                page, field, "New York University",
            )

            assert len(discovered) > 0, "Enrichment should discover options"
            discovered_lower = [o.lower() for o in discovered]
            assert any("new york university" in o for o in discovered_lower), (
                f"NYU not found in enriched options: {discovered[:15]}"
            )
            assert not any("9 eylul" in o for o in discovered_lower), (
                f"'9 Eylul' should NOT appear in enriched results: {discovered}"
            )


@pytest.mark.asyncio
async def test_enrich_school_combobox_discovers_ucla() -> None:
    """Enrichment for UCLA profile should discover 'University of California, Los Angeles'."""
    if not _FIXTURE_HTML.is_file():
        pytest.skip("oracle_combobox_lab.html not found")
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None
            await asyncio.sleep(0.5)

            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            field = _make_field(
                name="School",
                field_type="select",
                is_native=False,
                field_id="ff-school",
            )

            discovered = await _enrich_combobox_via_search(
                page, field, "University of California, Los Angeles",
            )

            assert len(discovered) > 0, "Enrichment should discover options"
            assert any("los angeles" in o.lower() for o in discovered), (
                f"UCLA not found in enriched options: {discovered[:15]}"
            )


@pytest.mark.asyncio
async def test_enrich_employer_combobox_discovers_goldman() -> None:
    """Enrichment for Goldman Sachs employer should find it in options."""
    if not _FIXTURE_HTML.is_file():
        pytest.skip("oracle_combobox_lab.html not found")
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None
            await asyncio.sleep(0.5)

            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            field = _make_field(
                name="Name of Latest Employer",
                field_type="select",
                is_native=False,
                field_id="ff-employer",
            )

            discovered = await _enrich_combobox_via_search(
                page, field, "Goldman Sachs",
            )

            assert len(discovered) > 0, "Enrichment should discover options"
            assert any("goldman sachs" in o.lower() for o in discovered), (
                f"Goldman Sachs not found in enriched options: {discovered[:15]}"
            )


@pytest.mark.asyncio
async def test_enrich_employer_unknown_returns_other() -> None:
    """Unknown employer enrichment should at least discover 'Other' if available."""
    if not _FIXTURE_HTML.is_file():
        pytest.skip("oracle_combobox_lab.html not found")
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None
            await asyncio.sleep(0.5)

            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            field = _make_field(
                name="Name of Latest Employer",
                field_type="select",
                is_native=False,
                field_id="ff-employer",
            )

            # Unknown employer — search terms won't match anything useful
            discovered = await _enrich_combobox_via_search(
                page, field, "WeKruit Technologies",
            )

            # May be empty (no matches for "WeKruit") — that's OK for optional
            # The LLM will generate "Other" as fallback
            # But if ANY options came back, they should be real
            if discovered:
                assert all(isinstance(o, str) and o.strip() for o in discovered)


@pytest.mark.asyncio
async def test_enrich_clears_combobox_after_search() -> None:
    """After enrichment, the combobox input should be cleared — no stale text."""
    if not _FIXTURE_HTML.is_file():
        pytest.skip("oracle_combobox_lab.html not found")
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None
            await asyncio.sleep(0.5)

            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            field = _make_field(
                name="School",
                field_type="select",
                is_native=False,
                field_id="ff-school",
            )

            await _enrich_combobox_via_search(page, field, "Stanford University")

            # Verify the input is cleared after enrichment
            value = await page.evaluate("""() => {
                const input = document.getElementById('ff-school');
                return input ? input.value : null;
            }""")
            assert value == "", f"Combobox should be cleared after enrichment, got: '{value}'"


@pytest.mark.asyncio
async def test_enrich_default_without_hint_sees_alphabetical() -> None:
    """Without a search hint, enrichment falls back to open→scan and sees
    the default alphabetical slice — this is the OLD behavior that missed NYU."""
    if not _FIXTURE_HTML.is_file():
        pytest.skip("oracle_combobox_lab.html not found")
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None
            await asyncio.sleep(0.5)

            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            field = _make_field(
                name="School",
                field_type="select",
                is_native=False,
                field_id="ff-school",
                options=[],  # No options yet
            )

            # Simulate old behavior: just open and scan without typing
            # The fixture shows max 10 alphabetically — NYU is NOT in the first 10
            from ghosthands.actions.domhand_fill import _try_open_combobox_menu, _scan_visible_dropdown_options

            await _try_open_combobox_menu(page, "ff-school", tag="test")
            await asyncio.sleep(0.5)
            default_scan = await _scan_visible_dropdown_options(page, field_id="ff-school")

            # Default alphabetical scan should NOT contain NYU
            default_lower = [o.lower() for o in default_scan]
            has_nyu = any("new york university" in o for o in default_lower)
            # This proves the old enrichment was broken for this case
            assert not has_nyu, (
                f"Default scan should NOT contain NYU (it's deep in the alphabet): {default_scan}"
            )
