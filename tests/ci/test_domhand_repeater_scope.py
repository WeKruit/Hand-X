"""Regression tests for scoped repeater fills in DomHand."""

import asyncio
import json
from contextlib import asynccontextmanager

from pytest_httpserver import HTTPServer

from browser_use.agent.variable_detector import _detect_variable_type
from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.tools.service import Tools
from ghosthands.actions.domhand_assess_state import domhand_assess_state
from ghosthands.actions.domhand_close_popup import domhand_close_popup
from ghosthands.actions.domhand_fill import (
    _build_inject_helpers_js,
    _click_dropdown_option,
    _default_screening_answer,
    _default_value,
    _field_value_matches_expected,
    _fill_button_group,
    _fill_checkbox,
    _fill_date_field,
    _fill_text_field,
    _filter_fields_for_scope,
    _find_best_profile_answer,
    _format_entry_profile_text,
    _infer_entry_data_from_scope,
    _known_entry_value,
    _known_entry_value_for_field,
    _known_profile_value,
    _known_profile_value_for_field,
    _match_answer,
    _parse_profile_evidence,
    extract_visible_form_fields,
)
from ghosthands.actions.domhand_interact_control import domhand_interact_control
from ghosthands.actions.domhand_select import domhand_select
from ghosthands.actions.domhand_select import (
    FAIL_OVER_CUSTOM_WIDGET,
    FAIL_OVER_NATIVE_SELECT,
    _build_failover_message,
    _selection_matches_value,
)
from ghosthands.actions.views import (
    DomHandAssessStateParams,
    DomHandClosePopupParams,
    DomHandInteractControlParams,
    DomHandSelectParams,
    FormField,
    generate_dropdown_search_terms,
    split_dropdown_value_hierarchy,
)
from ghosthands.agent.prompts import (
    COMPLETION_STATE_PRESUBMIT_SINGLE_PAGE,
    _format_profile_summary,
    build_completion_detection_lines,
    build_system_prompt,
)
from ghosthands.dom.shadow_helpers import ensure_helpers
from ghosthands.integrations.resume_loader import _map_to_profile
from ghosthands.platforms import detect_platform, detect_platform_from_signals, get_config_by_name
from ghosthands.profile.canonical import build_canonical_profile
from ghosthands.runtime_learning import export_runtime_learning_payload, reset_runtime_learning_state
from ghosthands.worker.executor import detect_platform as detect_executor_platform

SHADOW_DROPDOWN_HTML = """
<!DOCTYPE html>
<html>
<body>
	<div id="dropdown-host"></div>
	<script>
		window.__selected = '';
		const root = document.getElementById('dropdown-host').attachShadow({ mode: 'open' });
		root.innerHTML = `
			<div role="listbox" aria-label="Referral source">
				<div role="option" id="job-board">Job Board</div>
				<div role="option" id="linkedin">LinkedIn</div>
			</div>
		`;
		root.getElementById('job-board').addEventListener('click', () => { window.__selected = 'Job Board'; });
		root.getElementById('linkedin').addEventListener('click', () => { window.__selected = 'LinkedIn'; });
	</script>
</body>
</html>
"""

TRUSTED_BUTTON_GROUP_HTML = """
<!DOCTYPE html>
<html>
<body>
	<div id="disability-group" data-ff-id="disability-group" role="group" aria-label="Disability status">
		<button type="button" id="btn-yes" aria-pressed="true">Yes</button>
		<button type="button" id="btn-no" aria-pressed="false">No, I do not have a disability and have not had one in the past</button>
	</div>
	<script>
		window.__selected = 'Yes';
		const buttons = Array.from(document.querySelectorAll('#disability-group button'));
		buttons.forEach((button) => {
			button.addEventListener('click', (event) => {
				if (!event.isTrusted) return;
				buttons.forEach((candidate) => candidate.setAttribute('aria-pressed', 'false'));
				button.setAttribute('aria-pressed', 'true');
				window.__selected = button.textContent.trim();
			});
		});
	</script>
</body>
</html>
"""

RESET_REQUIRED_BUTTON_GROUP_HTML = """
<!DOCTYPE html>
<html>
<body>
	<div id="disability-group" data-ff-id="disability-group" role="group" aria-label="Disability status">
		<div id="btn-yes" role="cell" aria-pressed="true">Yes</div>
		<div id="btn-no" role="cell" aria-pressed="false">No, I do not have a disability and have not had one in the past</div>
	</div>
	<script>
		window.__selected = 'Yes';
		window.__armed = false;
		const yes = document.getElementById('btn-yes');
		const no = document.getElementById('btn-no');
		function setSelected(node) {
			yes.setAttribute('aria-pressed', node === yes ? 'true' : 'false');
			no.setAttribute('aria-pressed', node === no ? 'true' : 'false');
			window.__selected = node ? node.textContent.trim() : '';
		}
		yes.addEventListener('click', (event) => {
			if (!event.isTrusted) return;
			window.__armed = true;
			setSelected(null);
		});
		no.addEventListener('click', (event) => {
			if (!event.isTrusted) return;
			if (!window.__armed) return;
			setSelected(no);
			window.__armed = false;
		});
	</script>
</body>
</html>
"""

TRUSTED_CHECKBOX_HTML = """
<!DOCTYPE html>
<html>
<body>
	<div id="disability-row" role="row" style="display:flex;gap:12px;align-items:center;padding:16px;border:1px solid #ccc;width:640px;">
		<div
			id="disability-no"
			data-ff-id="disability-no"
			role="checkbox"
			aria-checked="false"
			aria-label="No, I do not have a disability and have not had one in the past"
			style="width:28px;height:28px;border:1px solid #666;"
		></div>
		<div role="cell">No, I do not have a disability and have not had one in the past</div>
	</div>
	<script>
		window.__selected = '';
		const row = document.getElementById('disability-row');
		const checkbox = document.getElementById('disability-no');
		row.addEventListener('click', (event) => {
			if (!event.isTrusted) return;
			checkbox.setAttribute('aria-checked', 'true');
			window.__selected = 'No, I do not have a disability and have not had one in the past';
		});
	</script>
</body>
</html>
"""

WORKDAY_INTERACTIVE_BLOCKERS_HTML = """
<!DOCTYPE html>
<html>
<body>
	<h2>My Information</h2>
	<div
		id="error-summary"
		role="alert"
		style="border:1px solid #d33;padding:12px;margin-bottom:16px;color:#b00020;"
	>
		Error - Have you previously worked at Exact Sciences?
		The field Have you previously worked at Exact Sciences? is required and must have a value.
	</div>

	<div
		id="source-field"
		data-automation-id="formField"
		style="display:flex;flex-direction:column;gap:8px;width:520px;margin-bottom:20px;"
	>
		<label for="source--source">How Did You Hear About Us?*</label>
		<div style="display:flex;align-items:center;gap:8px;border:1px solid #bbb;padding:8px;">
			<div data-automation-id="selectedItem" class="selected-pill">LinkedIn</div>
			<input
				id="source--source"
				data-ff-id="source--source"
				role="combobox"
				data-uxi-widget-type="selectinput"
				aria-haspopup="listbox"
				aria-label="How Did You Hear About Us?"
				value=""
			/>
		</div>
	</div>

	<fieldset
		id="prior-employment-field"
		data-automation-id="formField"
		aria-invalid="true"
		style="display:flex;flex-direction:column;gap:8px;width:520px;"
	>
		<legend>Have you previously worked at Exact Sciences?*</legend>
		<div id="prior-employment-error" class="error" style="color:#b00020;">
			Error: The field Have you previously worked at Exact Sciences? is required and must have a value.
		</div>
		<div
			id="prompt-yes"
			data-ff-id="prompt-yes"
			data-automation-id="promptOption-yes"
			name="prior-employment"
			role="radio"
			aria-required="true"
			aria-checked="false"
			aria-label="Yes"
			style="display:flex;gap:8px;align-items:center;border:1px solid #aaa;padding:8px;cursor:pointer;"
		>
			<span>Yes</span>
		</div>
		<div
			id="prompt-no"
			data-ff-id="prompt-no"
			data-automation-id="promptOption-no"
			name="prior-employment"
			role="radio"
			aria-required="true"
			aria-checked="false"
			aria-label="No"
			style="display:flex;gap:8px;align-items:center;border:1px solid #aaa;padding:8px;cursor:pointer;"
		>
			<span>No</span>
		</div>
	</fieldset>
	<script>
		window.__radioSelected = '';
		const summary = document.getElementById('error-summary');
		const fieldset = document.getElementById('prior-employment-field');
		const error = document.getElementById('prior-employment-error');
		const yes = document.getElementById('prompt-yes');
		const no = document.getElementById('prompt-no');

		function select(value) {
			yes.setAttribute('aria-checked', value === 'Yes' ? 'true' : 'false');
			no.setAttribute('aria-checked', value === 'No' ? 'true' : 'false');
			fieldset.setAttribute('aria-invalid', 'false');
			error.textContent = '';
			summary.textContent = '';
			window.__radioSelected = value;
		}

		yes.addEventListener('click', (event) => {
			if (!event.isTrusted) return;
			select('Yes');
		});
		no.addEventListener('click', (event) => {
			if (!event.isTrusted) return;
			select('No');
		});
	</script>
</body>
</html>
"""

ALREADY_SELECTED_SOURCE_WITH_UNRELATED_POPUP_HTML = """
<!DOCTYPE html>
<html>
<body>
	<div
		id="source-field"
		data-automation-id="formField"
		style="display:flex;flex-direction:column;gap:8px;width:520px;margin-bottom:20px;"
	>
		<label for="source--source">How Did You Hear About Us?*</label>
		<div style="display:flex;align-items:center;gap:8px;border:1px solid #bbb;padding:8px;">
			<div data-automation-id="selectedItem" class="selected-pill">LinkedIn</div>
			<input
				id="source--source"
				role="combobox"
				data-uxi-widget-type="selectinput"
				aria-haspopup="listbox"
				aria-label="How Did You Hear About Us?"
				value=""
			/>
		</div>
	</div>

	<div
		id="phone-popup"
		role="listbox"
		style="display:block;border:1px solid #aaa;padding:8px;width:320px;"
	>
		<div role="option">United States of America (+1)</div>
		<div role="option">United States of America (+1)</div>
	</div>
</body>
</html>
"""

ALREADY_SELECTED_INVALID_SOURCE_WITH_OPTIONS_HTML = """
<!DOCTYPE html>
<html>
<body>
	<div
		id="source-field"
		data-automation-id="formField"
		aria-invalid="true"
		style="display:flex;flex-direction:column;gap:8px;width:520px;margin-bottom:20px;"
	>
		<label for="source--source">How Did You Hear About Us?*</label>
		<div style="display:flex;align-items:center;gap:8px;border:1px solid #bbb;padding:8px;">
			<div data-automation-id="selectedItem" class="selected-pill">LinkedIn</div>
			<input
				id="source--source"
				role="combobox"
				data-uxi-widget-type="selectinput"
				aria-haspopup="listbox"
				aria-label="How Did You Hear About Us?"
				value=""
			/>
		</div>
	</div>

	<div
		id="source-popup"
		role="listbox"
		style="display:block;border:1px solid #aaa;padding:8px;width:320px;"
	>
		<div role="option" id="linkedin-option">LinkedIn</div>
		<div role="option" id="social-option">Social Media</div>
	</div>
	<script>
		window.__selectedSource = 'LinkedIn';
		const field = document.getElementById('source-field');
		const pill = document.querySelector('.selected-pill');
		document.getElementById('linkedin-option').addEventListener('click', () => {
			field.setAttribute('aria-invalid', 'false');
			pill.textContent = 'LinkedIn';
			window.__selectedSource = 'LinkedIn';
		});
	</script>
</body>
</html>
"""

APPLICATION_QUESTIONS_TRANSITION_HTML = """
<!DOCTYPE html>
<html>
<body>
	<h2>Application Questions</h2>
	<section>
		<label for="school-year">Please tell us your current year in school (e.g., Freshman, Sophomore, Junior, Senior, etc.)*</label>
		<textarea
			id="school-year"
			data-ff-id="school-year"
			aria-required="true"
			style="display:block;width:520px;height:120px;"
		></textarea>
	</section>
	<button type="button">Save and Continue</button>
</body>
</html>
"""

ENTER_COMMIT_NAME_HTML = """
<!DOCTYPE html>
<html>
<body>
	<label for="candidate-name">Name</label>
	<input id="candidate-name" data-ff-id="candidate-name" type="text" aria-label="Name" />
	<script>
		window.__committedName = '';
		const input = document.getElementById('candidate-name');
		input.addEventListener('keydown', (event) => {
			if (event.key === 'Enter' && event.isTrusted) {
				window.__committedName = input.value;
				input.setAttribute('data-committed', 'true');
			}
		});
	</script>
</body>
</html>
"""

ENTER_COMMIT_DATE_HTML = """
<!DOCTYPE html>
<html>
<body>
	<label for="start-date">Date</label>
	<input id="start-date" data-ff-id="start-date" type="text" aria-label="Date" />
	<script>
		window.__committedDate = '';
		const input = document.getElementById('start-date');
		input.addEventListener('keydown', (event) => {
			if (event.key === 'Enter' && event.isTrusted) {
				window.__committedDate = input.value;
				input.setAttribute('data-committed', 'true');
			}
		});
	</script>
</body>
</html>
"""

GREENHOUSE_PRESUBMIT_HTML = """
<!DOCTYPE html>
<html>
<body>
	<form id="application_form">
		<section>
			<h2>Personal Information</h2>
			<label>First Name <input data-ff-id="first-name" type="text" required value="Ruiyang" /></label>
			<label>Last Name <input data-ff-id="last-name" type="text" required value="Chen" /></label>
			<label>Email <input data-ff-id="email" type="email" required value="rc5663@nyu.edu" /></label>
		</section>
		<button type="submit">Submit Application</button>
	</form>
</body>
</html>
"""

GREENHOUSE_PRESUBMIT_WITH_SOCIAL_CONTINUE_HTML = """
<!DOCTYPE html>
<html>
<body>
	<form id="application_form">
		<section>
			<h2>Personal Information</h2>
			<label>First Name <input data-ff-id="first-name" type="text" required value="Ruiyang" /></label>
			<label>Last Name <input data-ff-id="last-name" type="text" required value="Chen" /></label>
			<label>Email <input data-ff-id="email" type="email" required value="rc5663@nyu.edu" /></label>
		</section>
		<button type="button">Continue with Google</button>
		<button type="button">Save and Continue Later</button>
		<button type="submit">Submit Application</button>
	</form>
</body>
</html>
"""

GREENHOUSE_BLOCKED_HTML = """
<!DOCTYPE html>
<html>
<body>
	<form id="application_form">
		<section>
			<h2>Personal Information</h2>
			<label>First Name <input data-ff-id="first-name" type="text" required value="" aria-invalid="true" /></label>
			<div class="error">First Name is required.</div>
		</section>
		<button type="submit" disabled>Submit Application</button>
	</form>
</body>
</html>
"""

ADVANCEABLE_MULTI_STEP_HTML = """
<!DOCTYPE html>
<html>
<body>
	<form class="application-form">
		<section>
			<h2>Questions</h2>
			<label>Email <input data-ff-id="email" type="email" required value="rc5663@nyu.edu" /></label>
		</section>
		<button type="button">Next</button>
	</form>
</body>
</html>
"""

BLOCKING_POPUP_HTML = """
<!DOCTYPE html>
<html>
<body style="margin:0;background:#0f172a;">
	<div id="overlay" style="position:fixed;inset:0;background:rgba(2,6,23,0.55);display:flex;align-items:center;justify-content:center;">
		<div
			id="newsletter-modal"
			role="dialog"
			aria-modal="true"
			aria-label="Not ready to apply today?"
			style="position:relative;width:420px;padding:28px;border-radius:20px;background:#ffffff;box-shadow:0 25px 60px rgba(15,23,42,0.35);"
		>
			<button
				id="close-modal"
				aria-label="Close"
				style="position:absolute;top:12px;right:14px;width:32px;height:32px;border:none;background:transparent;font-size:24px;cursor:pointer;"
			>×</button>
			<h2 style="margin:0 0 12px;">Not ready to apply today?</h2>
			<p style="margin:0;">Join our career network to hear about future roles.</p>
		</div>
	</div>
	<script>
		window.__popupClosed = false;
		document.getElementById('close-modal').addEventListener('click', () => {
			document.getElementById('overlay').remove();
			window.__popupClosed = true;
		});
	</script>
</body>
</html>
"""

SCROLL_BIAS_DOWN_HTML = """
<!DOCTYPE html>
<html>
<head>
	<style>
		body { margin: 0; }
		.spacer { height: 1800px; }
	</style>
</head>
<body>
	<div class="spacer"></div>
	<section>
		<h2>Education</h2>
		<label>School <input data-ff-id="school" type="text" required value="" /></label>
	</section>
</body>
</html>
"""


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


def _field(field_id: str, name: str, section: str) -> FormField:
    return FormField(field_id=field_id, name=name, field_type="text", section=section)


def test_filter_fields_for_heading_boundary_scopes_to_single_entry():
    fields = [
        _field("exp-1", "Job Title", "Work Experience 1"),
        _field("exp-2", "Job Title", "Work Experience 2"),
        _field("edu-1", "School", "Education 1"),
    ]

    filtered = _filter_fields_for_scope(fields, heading_boundary="Work Experience 2")

    assert [field.field_id for field in filtered] == ["exp-2"]


def test_filter_fields_for_target_section_falls_back_when_sections_do_not_match():
    fields = [
        _field("first-name", "First Name", ""),
        _field("source", "How Did You Hear About Us?", "Contact"),
    ]

    filtered = _filter_fields_for_scope(fields, target_section="My Information")

    assert {field.field_id for field in filtered} == {"first-name", "source"}


def test_filter_fields_for_target_section_keeps_blank_sections_when_some_fields_match():
    fields = [
        _field("source", "How Did You Hear About Us?", "My Information"),
        _field("address-line-1", "Address Line 1", ""),
        _field("postal-code", "Postal Code", ""),
        _field("school", "School", "Education"),
    ]

    filtered = _filter_fields_for_scope(fields, target_section="My Information")

    assert [field.field_id for field in filtered] == ["source", "address-line-1", "postal-code"]


def test_filter_fields_for_target_section_includes_information_child_sections():
    fields = [
        _field("source", "How Did You Hear About Us?", "My Information"),
        _field("address-line-1", "Address Line 1", "Address"),
        _field("postal-code", "Postal Code", "Address"),
        _field("phone-code", "Country Phone Code", "Phone"),
        _field("school", "School", "Education"),
    ]

    filtered = _filter_fields_for_scope(fields, target_section="My Information")

    assert [field.field_id for field in filtered] == ["source", "address-line-1", "postal-code", "phone-code"]


async def test_extract_visible_form_fields_groups_radios_even_when_sections_match_choices():
    class DummyPage:
        def __init__(self):
            self.calls = 0

        async def evaluate(self, *_args):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(
                    [
                        {
                            "field_id": "ff-4",
                            "name": "Have you previously worked at Exact Sciences?*",
                            "raw_label": "Have you previously worked at Exact Sciences?*",
                            "questionLabel": "Have you previously worked at Exact Sciences?*",
                            "groupKey": "prior-employment",
                            "field_type": "radio",
                            "section": "Yes",
                            "required": False,
                            "itemLabel": "Yes",
                            "current_value": "",
                            "visible": True,
                            "is_native": False,
                        },
                        {
                            "field_id": "ff-5",
                            "name": "Have you previously worked at Exact Sciences?*",
                            "raw_label": "Have you previously worked at Exact Sciences?*",
                            "questionLabel": "Have you previously worked at Exact Sciences?*",
                            "groupKey": "prior-employment",
                            "field_type": "radio",
                            "section": "No",
                            "required": False,
                            "itemLabel": "No",
                            "current_value": "",
                            "visible": True,
                            "is_native": False,
                        },
                    ]
                )
            return json.dumps([])

    fields = await extract_visible_form_fields(DummyPage())

    assert len(fields) == 1
    assert fields[0].field_type == "radio-group"
    assert fields[0].name == "Have you previously worked at Exact Sciences?*"
    assert fields[0].section == ""
    assert fields[0].required is True
    assert fields[0].choices == ["Yes", "No"]


def test_known_entry_value_matches_work_experience_fields():
    entry = {
        "title": "Staff Software Engineer",
        "company": "Acme",
        "location": "Chicago, IL",
        "start_date": "2022-01-01",
        "end_date": "2024-03-01",
        "description": "Led platform migrations.",
        "currently_work_here": True,
    }

    assert _known_entry_value("Job Title", entry) == "Staff Software Engineer"
    assert _known_entry_value("Company", entry) == "Acme"
    assert _known_entry_value("Location", entry) == "Chicago, IL"
    assert _known_entry_value("Start Date", entry) == "2022-01-01"
    assert _known_entry_value("End Date", entry) == "2024-03-01"
    assert _known_entry_value("Role Description", entry) == "Led platform migrations."
    assert _known_entry_value("I currently work here", entry) == "checked"


def test_known_entry_value_supports_currently_working_and_graduation_date():
    entry = {
        "degree": "Bachelor of Science",
        "graduation_date": "2024-05",
        "end_date_type": "Expected",
        "currently_working": False,
    }
    field = FormField(
        field_id="degree-1",
        name="Degree",
        field_type="select",
        section="Education 1",
        options=["Associates", "Bachelors", "Masters"],
    )

    assert _known_entry_value("Graduation Date", entry) == "2024-05"
    assert _known_entry_value("Actual or Expected", entry) == "Expected"
    assert _known_entry_value("I currently work here", entry) == "unchecked"
    assert _known_entry_value_for_field(field, entry) == "Bachelors"


def test_format_entry_profile_text_includes_scoped_values():
    entry = {
        "title": "Software Engineer",
        "company": "Example Corp",
        "currently_work_here": False,
        "field_of_study": "Computer Science",
        "end_date_type": "Actual",
    }

    text = _format_entry_profile_text(entry)

    assert "Job Title: Software Engineer" in text
    assert "Company: Example Corp" in text
    assert "I currently work here: No" in text
    assert "Field of Study: Computer Science" in text
    assert "End Date Status: Actual" in text


def test_profile_summary_includes_start_dates_and_end_status():
    summary = _format_profile_summary(
        {
            "first_name": "Ruiyang",
            "last_name": "Chen",
            "preferred_name": "Ringo",
            "experience": [
                {
                    "title": "Intern",
                    "company": "Acme",
                    "start_date": "2025-01",
                    "end_date": "2025-05",
                    "end_date_type": "Actual",
                },
            ],
            "education": [
                {
                    "school": "New York University",
                    "degree": "Bachelor of Science",
                    "field_of_study": "Computer Science",
                    "start_date": "2024-09",
                    "end_date": "2027-05",
                    "end_date_type": "Expected",
                },
            ],
        }
    )

    assert "First name: Ruiyang" in summary
    assert "Last name: Chen" in summary
    assert "Preferred name: Ringo" in summary
    assert "[2025-01 — 2025-05 (Actual end)]" in summary
    assert "[2024-09 — 2027-05] (Expected end)" in summary


def test_workday_prompt_mentions_scoped_domhand_fill_for_repeaters():
    prompt = build_system_prompt(
        {
            "experience": [{"title": "Engineer", "company": "Acme"}],
            "education": [{"school": "State University", "degree": "BS"}],
        },
        platform="workday",
    )

    assert "heading_boundary" in prompt
    assert "entry_data" in prompt
    assert "Work Experience 2" in prompt
    assert "Only the final leaf clears the validation error." in prompt


def test_workday_prompt_prefers_same_site_resume_apply_flow():
    prompt = build_system_prompt({}, platform="workday")

    assert "Autofill with Resume" in prompt
    assert "Apply with Resume" in prompt
    assert "LinkedIn, Indeed, Google" in prompt


def test_generic_prompt_does_not_include_workday_resume_apply_rule():
    prompt = build_system_prompt({}, platform="generic")

    assert "Autofill with Resume" not in prompt
    assert "Apply with Resume" not in prompt


def test_smartrecruiters_platform_allows_single_page_presubmit():
    assert get_config_by_name("smartrecruiters").single_page_presubmit_allowed is True
    assert get_config_by_name("generic").single_page_presubmit_allowed is False
    assert get_config_by_name("greenhouse").single_page_presubmit_allowed is True


def test_detect_platform_recognizes_greenhouse_hosted_pages_with_gh_jid():
    url = "https://careers.withwaymo.com/jobs/example-role?gh_jid=7393132"
    assert detect_platform(url) == "greenhouse"


def test_detect_platform_does_not_promote_generic_query_key_without_hosted_greenhouse_shape():
    url = "https://example.com/about-us?gh_jid=7393132"
    assert detect_platform(url) == "generic"


def test_executor_detect_platform_matches_shared_detector_for_hosted_greenhouse():
    url = "https://careers.withwaymo.com/jobs/example-role?gh_jid=7393132"
    assert detect_executor_platform(url) == "greenhouse"
    assert detect_executor_platform(url) == detect_platform(url)


def test_smartrecruiters_prompt_includes_presubmit_single_page_state():
    prompt = build_system_prompt({}, platform="smartrecruiters")
    lines = build_completion_detection_lines("smartrecruiters")

    assert COMPLETION_STATE_PRESUBMIT_SINGLE_PAGE in prompt
    assert any(COMPLETION_STATE_PRESUBMIT_SINGLE_PAGE in line for line in lines)
    assert "editable fields may still be visible" in prompt


def test_generic_completion_guidance_disallows_presubmit_single_page():
    lines = build_completion_detection_lines("generic")
    prompt = build_system_prompt({}, platform="generic")

    assert any("ignore this state on this platform" in line for line in lines)
    assert "ignore this state on this platform" in prompt


def test_domhand_select_native_failover_message_mentions_select_dropdown():
    message = _build_failover_message(
        "native_select",
        61,
        reason='No match for "CPT" in element 61.',
        available_texts=["OPT", "CPT", "H-1B"],
    )

    assert message.startswith(FAIL_OVER_NATIVE_SELECT)
    assert "dropdown_options(index=61)" in message
    assert "select_dropdown(index=61, text=...)" in message
    assert "Do NOT use click on this element." in message


def test_domhand_select_custom_failover_message_mentions_manual_click():
    message = _build_failover_message(
        "custom_widget",
        928,
        reason='Selection for "United States" was not confirmed on element 928.',
        current_value="Afghanistan +93",
    )

    assert message.startswith(FAIL_OVER_CUSTOM_WIDGET)
    assert "STOP — do NOT retry domhand_select for this field." in message
    assert "Open the widget manually" in message
    assert 'Current value: "Afghanistan +93".' in message


def test_dropdown_search_terms_cover_hierarchy_and_fallback_words():
    terms = generate_dropdown_search_terms("Website > workday.com")

    assert terms[0] == "Website > workday.com"
    assert "Website" in terms
    assert "workday.com" in terms


def test_dropdown_search_terms_include_phone_country_code_aliases():
    terms = generate_dropdown_search_terms("United States")

    assert "United States +1" in terms
    assert "+1" in terms
    assert "USA" in terms


def test_dropdown_hierarchy_split_preserves_order():
    assert split_dropdown_value_hierarchy("Website > workday.com") == ["Website", "workday.com"]


async def test_click_dropdown_option_finds_open_shadow_root_options(
    httpserver: HTTPServer,
):
    """Dropdown option lookup should pierce open shadow roots via __ff.queryAll."""
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/shadow-dropdown").respond_with_data(SHADOW_DROPDOWN_HTML, content_type="text/html")

        await tools.navigate(
            url=httpserver.url_for("/shadow-dropdown"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        page = await browser_session.get_current_page()
        assert page is not None
        await ensure_helpers(page)

        clicked = await _click_dropdown_option(page, "LinkedIn")

        assert clicked == {"clicked": True, "text": "LinkedIn"}
        assert await page.evaluate("() => window.__selected") == "LinkedIn"


async def test_domhand_close_popup_dismisses_visible_modal(
    httpserver: HTTPServer,
):
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/blocking-popup").respond_with_data(
            BLOCKING_POPUP_HTML,
            content_type="text/html",
        )

        await tools.navigate(
            url=httpserver.url_for("/blocking-popup"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        result = await domhand_close_popup(
            DomHandClosePopupParams(target_text="not ready to apply"),
            browser_session,
        )

        page = await browser_session.get_current_page()
        assert page is not None
        assert result.error is None
        closed = await page.evaluate("() => Boolean(window.__popupClosed)")
        assert str(closed).lower() == "true"


async def test_fill_button_group_uses_gui_fallback_for_untrusted_dom_clicks(
    httpserver: HTTPServer,
):
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/trusted-button-group").respond_with_data(
            TRUSTED_BUTTON_GROUP_HTML,
            content_type="text/html",
        )

        await tools.navigate(
            url=httpserver.url_for("/trusted-button-group"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        page = await browser_session.get_current_page()
        assert page is not None
        await page.evaluate(_build_inject_helpers_js())

        field = FormField(
            field_id="disability-group",
            name="Disability Status",
            field_type="button-group",
            section="Self Identify",
            choices=[
                "Yes",
                "No, I do not have a disability and have not had one in the past",
            ],
        )

        success = await _fill_button_group(
            page,
            field,
            "No, I do not have a disability and have not had one in the past",
            "[Disability Status]",
        )

        assert success is True
        assert (
            await page.evaluate("() => window.__selected")
            == "No, I do not have a disability and have not had one in the past"
        )


async def test_fill_button_group_can_reset_then_reselect_when_stuck(
    httpserver: HTTPServer,
):
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/reset-button-group").respond_with_data(
            RESET_REQUIRED_BUTTON_GROUP_HTML,
            content_type="text/html",
        )

        await tools.navigate(
            url=httpserver.url_for("/reset-button-group"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        page = await browser_session.get_current_page()
        assert page is not None
        await page.evaluate(_build_inject_helpers_js())

        field = FormField(
            field_id="disability-group",
            name="Disability Status",
            field_type="button-group",
            section="Self Identify",
            choices=[
                "Yes",
                "No, I do not have a disability and have not had one in the past",
            ],
        )

        success = await _fill_button_group(
            page,
            field,
            "No, I do not have a disability and have not had one in the past",
            "[Disability Status]",
        )

        assert success is True
        assert (
            await page.evaluate("() => window.__selected")
            == "No, I do not have a disability and have not had one in the past"
        )


async def test_fill_checkbox_uses_gui_fallback_for_untrusted_dom_clicks(
    httpserver: HTTPServer,
):
    reset_runtime_learning_state()
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/trusted-checkbox").respond_with_data(
            TRUSTED_CHECKBOX_HTML,
            content_type="text/html",
        )

        await tools.navigate(
            url=httpserver.url_for("/trusted-checkbox"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        page = await browser_session.get_current_page()
        assert page is not None
        await page.evaluate(_build_inject_helpers_js())

        field = FormField(
            field_id="disability-no",
            name="No, I do not have a disability and have not had one in the past",
            field_type="checkbox",
            section="Self Identify",
        )

        success = await _fill_checkbox(
            page,
            field,
            "checked",
            "[Disability Status]",
        )

        assert success is True
        assert (
            await page.evaluate("() => window.__selected")
            == "No, I do not have a disability and have not had one in the past"
        )
        assert (
            await page.evaluate("() => document.getElementById('disability-no').getAttribute('aria-checked')") == "true"
        )
        payload = export_runtime_learning_payload()
        assert payload["learned_interaction_recipes"] == [
            {
                "platform": "other",
                "host": "localhost",
                "normalized_label": "no i do not have a disability and have not had one in the past",
                "widget_signature": "checkbox",
                "preferred_action_chain": ["binary_gui_click"],
                "source": "visual_fallback",
            }
        ]


async def test_domhand_interact_control_uses_gui_fallback_for_trusted_radio_wrappers(
    httpserver: HTTPServer,
):
    reset_runtime_learning_state()
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/workday-interactive-blockers").respond_with_data(
            WORKDAY_INTERACTIVE_BLOCKERS_HTML,
            content_type="text/html",
        )
        httpserver.expect_request("/favicon.ico").respond_with_data("", status=204)

        await tools.navigate(
            url=httpserver.url_for("/workday-interactive-blockers"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        result = await domhand_interact_control(
            DomHandInteractControlParams(
                field_label="Have you previously worked at Exact Sciences?",
                desired_value="No",
                target_section="My Information",
            ),
            browser_session,
        )

        assert result.error is None
        assert await (await browser_session.get_current_page()).evaluate("() => window.__radioSelected") == "No"
        payload = export_runtime_learning_payload()
        assert payload["learned_interaction_recipes"] == [
            {
                "platform": "other",
                "host": "localhost",
                "normalized_label": "have you previously worked at exact sciences",
                "widget_signature": "radio-group",
                "preferred_action_chain": ["group_option_gui_click"],
                "source": "visual_fallback",
            }
        ]


async def test_domhand_interact_control_recovers_radio_above_viewport(
    httpserver: HTTPServer,
):
    reset_runtime_learning_state()
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/workday-interactive-blockers-offscreen").respond_with_data(
            WORKDAY_INTERACTIVE_BLOCKERS_HTML,
            content_type="text/html",
        )
        httpserver.expect_request("/favicon.ico").respond_with_data("", status=204)

        await tools.navigate(
            url=httpserver.url_for("/workday-interactive-blockers-offscreen"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        page = await browser_session.get_current_page()
        await page.evaluate(
            """() => {
                const spacer = document.createElement('div');
                spacer.style.height = '3200px';
                spacer.id = 'bottom-spacer';
                document.body.appendChild(spacer);
                window.scrollTo(0, document.body.scrollHeight);
            }"""
        )
        await asyncio.sleep(0.2)

        result = await domhand_interact_control(
            DomHandInteractControlParams(
                field_label="Have you previously worked at Exact Sciences?",
                desired_value="No",
                target_section="My Information",
            ),
            browser_session,
        )

        assert result.error is None
        assert await page.evaluate("() => window.__radioSelected") == "No"


async def test_domhand_select_short_circuits_when_value_already_selected(
    httpserver: HTTPServer,
):
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/already-selected-source").respond_with_data(
            ALREADY_SELECTED_SOURCE_WITH_UNRELATED_POPUP_HTML,
            content_type="text/html",
        )

        await tools.navigate(
            url=httpserver.url_for("/already-selected-source"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)
        await browser_session.get_browser_state_summary()
        selector_map = await browser_session.get_selector_map()
        source_index = next(
            idx
            for idx, element in selector_map.items()
            if (element.attributes or {}).get("id") == "source--source"
        )

        result = await domhand_select(
            DomHandSelectParams(index=source_index, value="LinkedIn"),
            browser_session,
        )

        assert result.error is None
        assert "already showed" in (result.extracted_content or "")


async def test_domhand_select_does_not_short_circuit_when_selected_value_is_invalid(
    httpserver: HTTPServer,
):
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/already-selected-invalid-source").respond_with_data(
            ALREADY_SELECTED_INVALID_SOURCE_WITH_OPTIONS_HTML,
            content_type="text/html",
        )

        await tools.navigate(
            url=httpserver.url_for("/already-selected-invalid-source"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)
        await browser_session.get_browser_state_summary()
        selector_map = await browser_session.get_selector_map()
        source_index = next(
            idx
            for idx, element in selector_map.items()
            if (element.attributes or {}).get("id") == "source--source"
        )

        result = await domhand_select(
            DomHandSelectParams(index=source_index, value="LinkedIn"),
            browser_session,
        )

        assert result.error is None
        assert "already showed" not in (result.extracted_content or "")
        page = await browser_session.get_current_page()
        assert await page.evaluate("() => document.getElementById('source-field').getAttribute('aria-invalid')") == "false"


async def test_fill_text_field_commits_exact_name_with_enter(
    httpserver: HTTPServer,
):
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/enter-commit-name").respond_with_data(
            ENTER_COMMIT_NAME_HTML,
            content_type="text/html",
        )

        await tools.navigate(
            url=httpserver.url_for("/enter-commit-name"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        page = await browser_session.get_current_page()
        assert page is not None
        await page.evaluate(_build_inject_helpers_js())

        field = FormField(
            field_id="candidate-name",
            name="Name",
            field_type="text",
            section="Self Identify",
        )

        success = await _fill_text_field(page, field, "Ruiyang Chen", "[Name]")

        assert success is True
        assert await page.evaluate("() => window.__committedName") == "Ruiyang Chen"
        assert (
            await page.evaluate("() => document.getElementById('candidate-name').getAttribute('data-committed')")
            == "true"
        )


async def test_fill_date_field_commits_with_enter(
    httpserver: HTTPServer,
):
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/enter-commit-date").respond_with_data(
            ENTER_COMMIT_DATE_HTML,
            content_type="text/html",
        )

        await tools.navigate(
            url=httpserver.url_for("/enter-commit-date"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        page = await browser_session.get_current_page()
        assert page is not None
        await page.evaluate(_build_inject_helpers_js())

        field = FormField(
            field_id="start-date",
            name="Date",
            field_type="date",
            section="Self Identify",
        )

        success = await _fill_date_field(page, field, "03/12/2026", "[Date]")

        assert success is True
        assert await page.evaluate("() => window.__committedDate") == "03/12/2026"
        assert (
            await page.evaluate("() => document.getElementById('start-date').getAttribute('data-committed')") == "true"
        )


def test_parse_profile_evidence_includes_address_and_referral_fields():
    evidence = _parse_profile_evidence(
        '{"address":"100 Main St","address_line_2":"Apt 4B","country":"United States",'
        '"how_did_you_hear":"LinkedIn","phone_type":"Mobile"}'
    )

    assert evidence["address"] == "100 Main St"
    assert evidence["address_line_2"] == "Apt 4B"
    assert evidence["country"] == "United States"
    assert evidence["how_did_you_hear"] == "LinkedIn"
    assert evidence["phone_device_type"] == "Mobile"


def test_parse_profile_evidence_does_not_inject_phone_defaults_when_missing():
    evidence = _parse_profile_evidence('{"email":"rc5663@nyu.edu"}')

    assert evidence["phone_device_type"] is None
    assert evidence["phone_country_code"] is None


def test_parse_profile_evidence_reads_explicit_name_lines_from_profile_summary():
    evidence = _parse_profile_evidence(
        "First name: Ruiyang\nLast name: Chen\nFull name: Ruiyang Chen\nEmail: rc5663@nyu.edu"
    )

    assert evidence["first_name"] == "Ruiyang"
    assert evidence["last_name"] == "Chen"


def test_infer_entry_data_from_scope_uses_profile_lists():
    profile = {
        "experience": [{"title": "Engineer 1"}, {"title": "Engineer 2"}],
        "education": [{"school": "RIT"}, {"school": "UCLA"}],
    }

    assert _infer_entry_data_from_scope(profile, "Work Experience 2", None) == {"title": "Engineer 2"}
    assert _infer_entry_data_from_scope(profile, "Education 1", "Education") == {"school": "RIT"}


def test_known_profile_value_matches_optional_address_fields():
    evidence = {
        "address": "100 Main St",
        "address_line_2": "Apt 4B",
        "county": "Travis County",
        "country": "United States",
    }

    assert _known_profile_value("Name*", {"first_name": "Ruiyang", "last_name": "Chen"}) == "Ruiyang Chen"
    assert _known_profile_value("Address Line 1", evidence) == "100 Main St"
    assert _known_profile_value("Apartment / Unit", evidence) == "Apt 4B"
    assert _known_profile_value("County", evidence) == "Travis County"
    assert _known_profile_value("Country / Region", evidence) == "United States"


def test_known_profile_value_matches_optional_address_and_link_variants():
    evidence = {
        "address": "100 Main St",
        "address_line_2": "Apt 4B",
        "portfolio": "https://janesmith.dev",
    }

    assert _known_profile_value("Mailing Address", evidence) == "100 Main St"
    assert _known_profile_value("Mailing Address Line 2", evidence) == "Apt 4B"
    assert _known_profile_value("Website URL", evidence) == "https://janesmith.dev"


def test_find_best_profile_answer_matches_long_screening_questions():
    answer_map = {
        "Are you legally authorized to work in the country in which this job is located?": "Yes",
        "Phone Device Type": "Mobile",
    }

    assert _find_best_profile_answer("Legally authorized to work", answer_map) == "Yes"
    assert _find_best_profile_answer("Phone Type", answer_map) == "Mobile"


def test_find_best_profile_answer_avoids_low_confidence_optional_source_match():
    answer_map = {
        "Source": "LinkedIn",
    }

    assert _find_best_profile_answer("Lead Source Type", answer_map, minimum_confidence="strong") is None


def test_known_profile_value_for_field_uses_structured_profile_answers():
    field = FormField(
        field_id="auth-1",
        name="Are you legally authorized to work in the country in which this job is located?",
        field_type="select",
        section="Application Questions",
        options=["Yes", "No"],
    )
    evidence = {"work_authorization": "US Citizen"}
    profile = {"authorized_to_work": True, "sponsorship_needed": False}

    assert _known_profile_value_for_field(field, evidence, profile) == "Yes"


def test_known_profile_value_for_optional_field_requires_high_confidence():
    field = FormField(
        field_id="lead-source-1",
        name="Lead Source Type",
        field_type="text",
        section="My Information",
        required=False,
    )
    evidence = {"how_did_you_hear": "LinkedIn"}

    assert _known_profile_value_for_field(field, evidence, {}, minimum_confidence="strong") is None
    assert _known_profile_value_for_field(field, evidence, {}, minimum_confidence="medium") == "LinkedIn"


def test_match_answer_fills_optional_field_only_for_high_confidence_matches():
    field = FormField(
        field_id="website-1",
        name="Website URL",
        field_type="text",
        section="My Information",
        required=False,
    )
    answers = {"Website": "https://ringo.dev"}

    assert _match_answer(field, answers, {}, {}) == "https://ringo.dev"


def test_match_answer_skips_ambiguous_optional_field_matches():
    field = FormField(
        field_id="lead-source-2",
        name="Lead Source Type",
        field_type="text",
        section="My Information",
        required=False,
    )
    answers = {"Source": "LinkedIn"}

    assert _match_answer(field, answers, {}, {}) is None


def test_dropdown_selection_match_requires_final_visible_value():
    assert _selection_matches_value("Phone Device Type selected Mobile", "Mobile")
    assert _selection_matches_value("LinkedIn", "How did you hear about us > LinkedIn")
    assert not _selection_matches_value("Job Board/Social Media Web Site", "LinkedIn")
    assert not _selection_matches_value("Select One", "LinkedIn")
    assert not _selection_matches_value("05e15101582a10019dbe3ae8c5a80000", "Yes")
    assert not _selection_matches_value("What degree are you seeking? Select One", "Bachelor's Degree")


def test_fill_dropdown_confirmation_requires_final_visible_value():
    assert _field_value_matches_expected("Phone Device Type selected Mobile", "Mobile")
    assert _field_value_matches_expected("LinkedIn", "Job Board/Social Media Web Site > LinkedIn")
    assert not _field_value_matches_expected("Job Board/Social Media Web Site", "LinkedIn")


def test_default_value_is_strict_provenance_only():
    referral_field = FormField(
        field_id="referral",
        name="How did you hear about us?",
        field_type="select",
        section="Questions",
    )
    date_field = FormField(
        field_id="start-date",
        name="Start Date",
        field_type="date",
        section="Questions",
    )

    assert _default_value(referral_field) == ""
    assert _default_value(date_field) == ""


def test_default_screening_answer_does_not_guess_without_profile_data():
    field = FormField(
        field_id="auth",
        name="Are you legally authorized to work in the country in which this job is located?",
        field_type="select",
        section="Questions",
        options=["Yes", "No"],
    )

    assert _default_screening_answer(field, {}) is None
    assert _default_screening_answer(field, {"authorized_to_work": True}) == "Yes"


def test_resume_loader_does_not_inject_non_profile_defaults():
    profile = _map_to_profile(
        {
            "fullName": "Ruiyang Chen",
            "email": "rc5663@nyu.edu",
            "phone": "(646) 678-9391",
            "location": "New York, NY",
            "workHistory": [],
            "education": [],
        }
    )

    assert profile["phone_device_type"] == ""
    assert profile["phone_country_code"] == ""
    assert profile["work_authorization"] == ""
    assert profile["gender"] == ""


def test_canonical_profile_tracks_explicit_and_derived_values():
    canonical = build_canonical_profile(
        {
            "first_name": "Ruiyang",
            "last_name": "Chen",
            "email": "rc5663@nyu.edu",
        }
    )

    assert canonical.get("first_name") == "Ruiyang"
    assert canonical.get("last_name") == "Chen"
    assert canonical.get("full_name") == "Ruiyang Chen"
    assert canonical.values["full_name"].provenance == "derived"


def test_detect_platform_from_signals_recognizes_greenhouse_markers():
    assert (
        detect_platform_from_signals(
            "https://careers.example.com/jobs/role",
            page_text="Apply for this job",
            markers=["application_form", "submit application"],
        )
        == "greenhouse"
    )


def test_detect_platform_from_signals_does_not_promote_on_single_weak_marker():
    assert (
        detect_platform_from_signals(
            "https://careers.example.com/role",
            page_text="Submit Application",
            markers=[],
        )
        == "generic"
    )


def test_detect_platform_from_signals_allows_strong_structural_marker():
    assert (
        detect_platform_from_signals(
            "https://jobs.example.com/apply",
            page_text="",
            markers=["c-spl-select-field"],
        )
        == "smartrecruiters"
    )


def test_variable_detector_disables_value_pattern_guessing_in_apply_mode():
    assert _detect_variable_type("John Doe", allow_value_pattern_fallback=False) is None
    assert _detect_variable_type("john@example.com", allow_value_pattern_fallback=False) is None
    assert _detect_variable_type("John Doe", allow_value_pattern_fallback=True) == ("full_name", None)


async def test_domhand_assess_state_detects_greenhouse_presubmit_single_page(
    httpserver: HTTPServer,
):
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/greenhouse-presubmit").respond_with_data(
            GREENHOUSE_PRESUBMIT_HTML,
            content_type="text/html",
        )
        await tools.navigate(
            url=httpserver.url_for("/greenhouse-presubmit"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        result = await domhand_assess_state(DomHandAssessStateParams(), browser_session)
        assert result.error is None
        assert result.extracted_content is not None
        state_json = result.extracted_content.split("APPLICATION_STATE_JSON:\n", 1)[1]
        state = json.loads(state_json)

        assert state["terminal_state"] == "presubmit_single_page"
        assert state["platform_hint"] == "greenhouse"
        assert state["unresolved_required_fields"] == []


async def test_domhand_assess_state_ignores_social_or_later_continue_buttons_on_presubmit(
    httpserver: HTTPServer,
):
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/jobs/greenhouse-presubmit-social").respond_with_data(
            GREENHOUSE_PRESUBMIT_WITH_SOCIAL_CONTINUE_HTML,
            content_type="text/html",
        )
        await tools.navigate(
            url=httpserver.url_for("/jobs/greenhouse-presubmit-social") + "?gh_jid=7393132",
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        result = await domhand_assess_state(DomHandAssessStateParams(), browser_session)
        assert result.extracted_content is not None
        state = json.loads(result.extracted_content.split("APPLICATION_STATE_JSON:\n", 1)[1])

        assert state["advance_visible"] is False
        assert state["terminal_state"] == "presubmit_single_page"


async def test_domhand_assess_state_requires_fix_when_submit_blocked(
    httpserver: HTTPServer,
):
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/greenhouse-blocked").respond_with_data(
            GREENHOUSE_BLOCKED_HTML,
            content_type="text/html",
        )
        await tools.navigate(
            url=httpserver.url_for("/greenhouse-blocked"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        result = await domhand_assess_state(DomHandAssessStateParams(), browser_session)
        assert result.extracted_content is not None
        state = json.loads(result.extracted_content.split("APPLICATION_STATE_JSON:\n", 1)[1])

        assert state["terminal_state"] == "advanceable"
        assert state["submit_disabled"] is True
        assert len(state["unresolved_required_fields"]) == 1
        assert state["visible_errors"]


async def test_domhand_assess_state_keeps_multi_step_pages_advanceable(
    httpserver: HTTPServer,
):
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/advanceable-step").respond_with_data(
            ADVANCEABLE_MULTI_STEP_HTML,
            content_type="text/html",
        )
        await tools.navigate(
            url=httpserver.url_for("/advanceable-step"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        result = await domhand_assess_state(DomHandAssessStateParams(), browser_session)
        assert result.extracted_content is not None
        state = json.loads(result.extracted_content.split("APPLICATION_STATE_JSON:\n", 1)[1])

        assert state["terminal_state"] == "advanceable"
        assert state["advance_visible"] is True


async def test_domhand_assess_state_reports_scroll_bias_down_for_lower_unresolved_fields(
    httpserver: HTTPServer,
):
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/scroll-bias-down").respond_with_data(
            SCROLL_BIAS_DOWN_HTML,
            content_type="text/html",
        )
        await tools.navigate(
            url=httpserver.url_for("/scroll-bias-down"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        result = await domhand_assess_state(DomHandAssessStateParams(), browser_session)
        assert result.extracted_content is not None
        state = json.loads(result.extracted_content.split("APPLICATION_STATE_JSON:\n", 1)[1])

        assert state["scroll_bias"] == "down"
        assert state["current_section"] == "Education"
        assert state["unresolved_required_fields"][0]["relative_position"] == "below"


async def test_domhand_assess_state_reads_selected_pill_and_reports_radio_blocker(
    httpserver: HTTPServer,
):
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/workday-interactive-blockers-assess").respond_with_data(
            WORKDAY_INTERACTIVE_BLOCKERS_HTML,
            content_type="text/html",
        )
        await tools.navigate(
            url=httpserver.url_for("/workday-interactive-blockers-assess"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        result = await domhand_assess_state(DomHandAssessStateParams(), browser_session)
        assert result.extracted_content is not None
        state = json.loads(result.extracted_content.split("APPLICATION_STATE_JSON:\n", 1)[1])

        assert len(state["unresolved_required_fields"]) == 1
        assert state["unresolved_required_fields"][0]["name"] == "Have you previously worked at Exact Sciences?*"
        assert state["unresolved_required_fields"][0]["current_value"] == ""
        assert state["visible_errors"][0] == (
            "Error - Have you previously worked at Exact Sciences? The field Have you previously worked at Exact Sciences? is required and must have a value."
        )
        assert all("Yes No" not in error for error in state["visible_errors"])


async def test_domhand_assess_state_prefers_visible_transition_section_over_stale_target(
    httpserver: HTTPServer,
):
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/application-questions-transition").respond_with_data(
            APPLICATION_QUESTIONS_TRANSITION_HTML,
            content_type="text/html",
        )
        await tools.navigate(
            url=httpserver.url_for("/application-questions-transition"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.3)

        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="My Experience"),
            browser_session,
        )
        assert result.extracted_content is not None
        state = json.loads(result.extracted_content.split("APPLICATION_STATE_JSON:\n", 1)[1])

        assert state["current_section"] == "Application Questions"
        assert state["unresolved_required_fields"][0]["name"].startswith(
            "Please tell us your current year in school"
        )
