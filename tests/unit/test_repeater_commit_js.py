"""Playwright-based tests for _CLICK_SAVE_BUTTON_JS.

Spins up a real browser page with mock Oracle-like buttons and runs the
actual JavaScript to verify it clicks the RIGHT button for each section.
"""

import json

import pytest
from playwright.sync_api import sync_playwright

from ghosthands.actions.domhand_fill_repeaters import _CLICK_SAVE_BUTTON_JS

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    p = browser.new_page()
    yield p
    p.close()


def _make_oracle_page(page, buttons: list[str], *, hidden_buttons: list[str] | None = None):
    """Create a page with visible Oracle-like buttons."""
    btn_html = []
    for text in buttons:
        btn_html.append(f'<button class="oracle-btn">{text}</button>')
    for text in (hidden_buttons or []):
        btn_html.append(f'<button class="oracle-btn" style="display:none">{text}</button>')
    html = f"""
    <html><body>
        <div id="page3">
            {''.join(btn_html)}
            <div id="clicked-log"></div>
        </div>
        <script>
            // Intercept clicks so we can verify which button was clicked
            document.querySelectorAll('button').forEach(btn => {{
                btn.addEventListener('click', () => {{
                    document.getElementById('clicked-log').textContent = btn.textContent.trim();
                }});
            }});
        </script>
    </body></html>
    """
    page.set_content(html)


def _run_save_js(page, section_hint: str) -> dict:
    """Run _CLICK_SAVE_BUTTON_JS and return the parsed result."""
    raw = page.evaluate(_CLICK_SAVE_BUTTON_JS, section_hint)
    return json.loads(raw) if isinstance(raw, str) else raw


def _get_clicked_text(page) -> str:
    """Get the text of the button that was actually clicked."""
    return page.evaluate('document.getElementById("clicked-log").textContent')


# ═══════════════════════════════════════════════════════════════════════
# Skills section: must click "ADD SKILL", NOT "SAVE" or "Add Education"
# ═══════════════════════════════════════════════════════════════════════


class TestSkillsCommit:
    def test_clicks_add_skill(self, page):
        _make_oracle_page(page, ["CANCEL", "SAVE", "Add Skill", "Add Education", "Add Language"])
        result = _run_save_js(page, "skills")
        assert result["clicked"] is True
        assert "skill" in result["text"].lower()
        assert _get_clicked_text(page) == "Add Skill"

    def test_clicks_ADD_SKILL_uppercase(self, page):
        _make_oracle_page(page, ["CANCEL", "ADD SKILL", "ADD EDUCATION", "ADD LANGUAGE"])
        result = _run_save_js(page, "skills")
        assert result["clicked"] is True
        assert _get_clicked_text(page) == "ADD SKILL"

    def test_does_not_click_save_for_skills(self, page):
        _make_oracle_page(page, ["CANCEL", "SAVE", "Add Skill"])
        result = _run_save_js(page, "skills")
        assert result["clicked"] is True
        assert _get_clicked_text(page) == "Add Skill"  # NOT "SAVE"

    def test_does_not_click_add_education_for_skills(self, page):
        _make_oracle_page(page, ["Add Education", "Add Skill", "Add Language"])
        result = _run_save_js(page, "skills")
        assert result["clicked"] is True
        assert _get_clicked_text(page) == "Add Skill"  # NOT "Add Education"

    def test_skips_hidden_add_skill(self, page):
        _make_oracle_page(page, ["Add Education"], hidden_buttons=["Add Skill"])
        result = _run_save_js(page, "skills")
        assert result["clicked"] is False  # hidden button should be skipped

    def test_no_buttons_returns_not_clicked(self, page):
        _make_oracle_page(page, ["CANCEL", "Next", "Previous"])
        result = _run_save_js(page, "skills")
        assert result["clicked"] is False

    def test_diag_shows_visible_buttons(self, page):
        _make_oracle_page(page, ["CANCEL", "SAVE", "Add Skill"])
        result = _run_save_js(page, "skills")
        diag = result.get("diag", {})
        assert diag["hint"] == "skills"
        assert "Add Skill" in diag["visible_buttons"]
        assert diag["phase"] == "oracle_section_commit"


# ═══════════════════════════════════════════════════════════════════════
# Education section: must click "SAVE", NOT "Add Education"
# ═══════════════════════════════════════════════════════════════════════


class TestEducationCommit:
    def test_clicks_save_not_add_education(self, page):
        _make_oracle_page(page, ["CANCEL", "SAVE", "Add Education"])
        result = _run_save_js(page, "education")
        assert result["clicked"] is True
        assert _get_clicked_text(page) == "SAVE"  # NOT "Add Education"

    def test_clicks_save_lowercase(self, page):
        _make_oracle_page(page, ["Cancel", "Save", "Add Education"])
        result = _run_save_js(page, "education")
        assert result["clicked"] is True
        assert _get_clicked_text(page) == "Save"

    def test_no_save_button_returns_not_clicked(self, page):
        _make_oracle_page(page, ["CANCEL", "Add Education"])
        result = _run_save_js(page, "education")
        # No SAVE button, and "Add Education" should NOT be clicked for education
        assert result["clicked"] is False

    def test_diag_phase_is_oracle_save(self, page):
        _make_oracle_page(page, ["CANCEL", "SAVE"])
        result = _run_save_js(page, "education")
        assert result["diag"]["phase"] == "oracle_save"


# ═══════════════════════════════════════════════════════════════════════
# Languages section: must click "Add Language"
# ═══════════════════════════════════════════════════════════════════════


class TestLanguagesCommit:
    def test_clicks_add_language(self, page):
        _make_oracle_page(page, ["CANCEL", "SAVE", "Add Skill", "Add Language"])
        result = _run_save_js(page, "languages")
        assert result["clicked"] is True
        assert _get_clicked_text(page) == "Add Language"

    def test_does_not_click_add_skill_for_languages(self, page):
        _make_oracle_page(page, ["Add Skill", "Add Language", "Add Education"])
        result = _run_save_js(page, "languages")
        assert result["clicked"] is True
        assert _get_clicked_text(page) == "Add Language"


# ═══════════════════════════════════════════════════════════════════════
# Experience section: must click "SAVE" like education
# ═══════════════════════════════════════════════════════════════════════


class TestExperienceCommit:
    def test_clicks_save_not_add_experience(self, page):
        _make_oracle_page(page, ["CANCEL", "SAVE", "Add Work Experience"])
        result = _run_save_js(page, "experience")
        assert result["clicked"] is True
        assert _get_clicked_text(page) == "SAVE"

    def test_diag_phase_is_oracle_save(self, page):
        _make_oracle_page(page, ["Save"])
        result = _run_save_js(page, "experience")
        assert result["diag"]["phase"] == "oracle_save"


# ═══════════════════════════════════════════════════════════════════════
# Licenses section: must click "Add Certification" or "Add License"
# ═══════════════════════════════════════════════════════════════════════


class TestLicensesCommit:
    def test_clicks_add_certification(self, page):
        _make_oracle_page(page, ["CANCEL", "SAVE", "Add Certification"])
        result = _run_save_js(page, "licenses")
        assert result["clicked"] is True
        assert _get_clicked_text(page) == "Add Certification"

    def test_clicks_add_license(self, page):
        _make_oracle_page(page, ["CANCEL", "SAVE", "Add License"])
        result = _run_save_js(page, "licenses")
        assert result["clicked"] is True
        assert _get_clicked_text(page) == "Add License"


# ═══════════════════════════════════════════════════════════════════════
# Cross-section isolation: buttons from OTHER sections must NOT be clicked
# ═══════════════════════════════════════════════════════════════════════


class TestCrossSectionIsolation:
    def test_skills_ignores_add_education_and_add_language(self, page):
        """When filling skills, only 'Add Skill' should be clickable, not others."""
        _make_oracle_page(page, ["Add Education", "Add Language", "SAVE", "Add Skill"])
        result = _run_save_js(page, "skills")
        assert _get_clicked_text(page) == "Add Skill"

    def test_languages_ignores_add_skill_and_add_education(self, page):
        _make_oracle_page(page, ["Add Skill", "Add Education", "SAVE", "Add Language"])
        result = _run_save_js(page, "languages")
        assert _get_clicked_text(page) == "Add Language"

    def test_education_ignores_add_skill_and_add_language(self, page):
        """Education should click SAVE, never Add Skill or Add Language."""
        _make_oracle_page(page, ["Add Skill", "Add Language", "SAVE", "Add Education"])
        result = _run_save_js(page, "education")
        assert _get_clicked_text(page) == "SAVE"

    def test_all_add_buttons_present_skills_picks_right_one(self, page):
        """Full Oracle page 3 scenario: all Add buttons visible."""
        _make_oracle_page(page, [
            "CANCEL", "SAVE",
            "Add Education", "Add Skill", "Add Language",
            "Add Certification", "Next", "Previous",
        ])
        result = _run_save_js(page, "skills")
        assert _get_clicked_text(page) == "Add Skill"
        assert result["diag"]["phase"] == "oracle_section_commit"
