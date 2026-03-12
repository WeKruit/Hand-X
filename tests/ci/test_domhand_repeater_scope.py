"""Regression tests for scoped repeater fills in DomHand."""

import asyncio
from contextlib import asynccontextmanager
from datetime import date

from pytest_httpserver import HTTPServer

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.tools.service import Tools
from ghosthands.actions.domhand_fill import (
	_build_inject_helpers_js,
	_click_dropdown_option,
	_default_value,
	_field_value_matches_expected,
	_filter_fields_for_scope,
	_fill_button_group,
	_fill_checkbox,
	_fill_date_field,
	_fill_text_field,
	_find_best_profile_answer,
	_format_entry_profile_text,
	_infer_entry_data_from_scope,
	_known_entry_value,
	_known_entry_value_for_field,
	_known_profile_value,
	_known_profile_value_for_field,
	_match_answer,
	_parse_profile_evidence,
	_replace_placeholder_answers,
)
from ghosthands.actions.domhand_select import _selection_matches_value
from ghosthands.actions.views import (
	FormField,
	generate_dropdown_search_terms,
	split_dropdown_value_hierarchy,
)
from ghosthands.agent.prompts import build_system_prompt
from ghosthands.dom.shadow_helpers import ensure_helpers

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
	return FormField(field_id=field_id, name=name, field_type='text', section=section)


def test_filter_fields_for_heading_boundary_scopes_to_single_entry():
	fields = [
		_field('exp-1', 'Job Title', 'Work Experience 1'),
		_field('exp-2', 'Job Title', 'Work Experience 2'),
		_field('edu-1', 'School', 'Education 1'),
	]

	filtered = _filter_fields_for_scope(fields, heading_boundary='Work Experience 2')

	assert [field.field_id for field in filtered] == ['exp-2']


def test_filter_fields_for_target_section_falls_back_when_sections_do_not_match():
	fields = [
		_field('first-name', 'First Name', ''),
		_field('source', 'How Did You Hear About Us?', 'Contact'),
	]

	filtered = _filter_fields_for_scope(fields, target_section='My Information')

	assert [field.field_id for field in filtered] == ['first-name', 'source']


def test_known_entry_value_matches_work_experience_fields():
	entry = {
		'title': 'Staff Software Engineer',
		'company': 'Acme',
		'location': 'Chicago, IL',
		'start_date': '2022-01-01',
		'end_date': '2024-03-01',
		'description': 'Led platform migrations.',
		'currently_work_here': True,
	}

	assert _known_entry_value('Job Title', entry) == 'Staff Software Engineer'
	assert _known_entry_value('Company', entry) == 'Acme'
	assert _known_entry_value('Location', entry) == 'Chicago, IL'
	assert _known_entry_value('Start Date', entry) == '2022-01-01'
	assert _known_entry_value('End Date', entry) == '2024-03-01'
	assert _known_entry_value('Role Description', entry) == 'Led platform migrations.'
	assert _known_entry_value('I currently work here', entry) == 'checked'


def test_known_entry_value_supports_currently_working_and_graduation_date():
	entry = {
		'degree': 'Bachelor of Science',
		'graduation_date': '2024-05',
		'currently_working': False,
	}
	field = FormField(
		field_id='degree-1',
		name='Degree',
		field_type='select',
		section='Education 1',
		options=['Associates', 'Bachelors', 'Masters'],
	)

	assert _known_entry_value('Graduation Date', entry) == '2024-05'
	assert _known_entry_value('I currently work here', entry) == 'unchecked'
	assert _known_entry_value_for_field(field, entry) == 'Bachelors'


def test_known_entry_value_maps_start_and_end_years_to_correct_side():
	entry = {
		'start_date': '2024-09',
		'graduation_date': '2027-05',
	}
	from_field = FormField(field_id='edu-from', name='From', field_type='text', section='Education 1')
	to_field = FormField(field_id='edu-to', name='To (Actual or Expected)', field_type='text', section='Education 1')

	assert _known_entry_value_for_field(from_field, entry) == '2024'
	assert _known_entry_value_for_field(to_field, entry) == '2027'


def test_known_entry_value_leaves_missing_end_side_blank():
	entry = {'start_date': '2024-09'}
	to_field = FormField(field_id='edu-to-missing', name='To (Actual or Expected)', field_type='text', section='Education 1')

	assert _known_entry_value_for_field(to_field, entry) is None


def test_format_entry_profile_text_includes_scoped_values():
	entry = {
		'title': 'Software Engineer',
		'company': 'Example Corp',
		'currently_work_here': False,
		'field_of_study': 'Computer Science',
	}

	text = _format_entry_profile_text(entry)

	assert 'Job Title: Software Engineer' in text
	assert 'Company: Example Corp' in text
	assert 'I currently work here: No' in text
	assert 'Field of Study: Computer Science' in text


def test_workday_prompt_mentions_scoped_domhand_fill_for_repeaters():
	prompt = build_system_prompt(
		{
			'experience': [{'title': 'Engineer', 'company': 'Acme'}],
			'education': [{'school': 'State University', 'degree': 'BS'}],
		},
		platform='workday',
	)

	assert 'heading_boundary' in prompt
	assert 'entry_data' in prompt
	assert 'Work Experience 2' in prompt
	assert 'Only the final leaf clears the validation error.' in prompt


def test_workday_prompt_prefers_same_site_resume_apply_flow():
	prompt = build_system_prompt({}, platform='workday')

	assert 'Autofill with Resume' in prompt
	assert 'Apply with Resume' in prompt
	assert 'LinkedIn, Indeed, Google' in prompt


def test_generic_prompt_does_not_include_workday_resume_apply_rule():
	prompt = build_system_prompt({}, platform='generic')

	assert 'Autofill with Resume' not in prompt
	assert 'Apply with Resume' not in prompt


def test_dropdown_search_terms_cover_hierarchy_and_fallback_words():
	terms = generate_dropdown_search_terms('Website > workday.com')

	assert terms[0] == 'Website > workday.com'
	assert 'Website' in terms
	assert 'workday.com' in terms


def test_dropdown_hierarchy_split_preserves_order():
	assert split_dropdown_value_hierarchy('Website > workday.com') == ['Website', 'workday.com']


async def test_click_dropdown_option_finds_open_shadow_root_options(
	httpserver: HTTPServer,
):
	"""Dropdown option lookup should pierce open shadow roots via __ff.queryAll."""
	async with managed_browser_session() as browser_session:
		tools = Tools()
		httpserver.expect_request('/shadow-dropdown').respond_with_data(SHADOW_DROPDOWN_HTML, content_type='text/html')

		await tools.navigate(
			url=httpserver.url_for('/shadow-dropdown'),
			new_tab=False,
			browser_session=browser_session,
		)
		await asyncio.sleep(0.3)

		page = await browser_session.get_current_page()
		assert page is not None
		await ensure_helpers(page)

		clicked = await _click_dropdown_option(page, 'LinkedIn')

		assert clicked == {'clicked': True, 'text': 'LinkedIn'}
		assert await page.evaluate("() => window.__selected") == 'LinkedIn'


async def test_fill_button_group_uses_gui_fallback_for_untrusted_dom_clicks(
	httpserver: HTTPServer,
):
	async with managed_browser_session() as browser_session:
		tools = Tools()
		httpserver.expect_request('/trusted-button-group').respond_with_data(
			TRUSTED_BUTTON_GROUP_HTML,
			content_type='text/html',
		)

		await tools.navigate(
			url=httpserver.url_for('/trusted-button-group'),
			new_tab=False,
			browser_session=browser_session,
		)
		await asyncio.sleep(0.3)

		page = await browser_session.get_current_page()
		assert page is not None
		await page.evaluate(_build_inject_helpers_js())

		field = FormField(
			field_id='disability-group',
			name='Disability Status',
			field_type='button-group',
			section='Self Identify',
			choices=[
				'Yes',
				'No, I do not have a disability and have not had one in the past',
			],
		)

		success = await _fill_button_group(
			page,
			field,
			'No, I do not have a disability and have not had one in the past',
			'[Disability Status]',
		)

		assert success is True
		assert await page.evaluate("() => window.__selected") == 'No, I do not have a disability and have not had one in the past'


async def test_fill_checkbox_uses_gui_fallback_for_untrusted_dom_clicks(
	httpserver: HTTPServer,
):
	async with managed_browser_session() as browser_session:
		tools = Tools()
		httpserver.expect_request('/trusted-checkbox').respond_with_data(
			TRUSTED_CHECKBOX_HTML,
			content_type='text/html',
		)

		await tools.navigate(
			url=httpserver.url_for('/trusted-checkbox'),
			new_tab=False,
			browser_session=browser_session,
		)
		await asyncio.sleep(0.3)

		page = await browser_session.get_current_page()
		assert page is not None
		await page.evaluate(_build_inject_helpers_js())

		field = FormField(
			field_id='disability-no',
			name='No, I do not have a disability and have not had one in the past',
			field_type='checkbox',
			section='Self Identify',
		)

		success = await _fill_checkbox(
			page,
			field,
			'checked',
			'[Disability Status]',
		)

		assert success is True
		assert await page.evaluate("() => window.__selected") == 'No, I do not have a disability and have not had one in the past'
		assert await page.evaluate("() => document.getElementById('disability-no').getAttribute('aria-checked')") == 'true'


async def test_fill_text_field_commits_exact_name_with_enter(
	httpserver: HTTPServer,
):
	async with managed_browser_session() as browser_session:
		tools = Tools()
		httpserver.expect_request('/enter-commit-name').respond_with_data(
			ENTER_COMMIT_NAME_HTML,
			content_type='text/html',
		)

		await tools.navigate(
			url=httpserver.url_for('/enter-commit-name'),
			new_tab=False,
			browser_session=browser_session,
		)
		await asyncio.sleep(0.3)

		page = await browser_session.get_current_page()
		assert page is not None
		await page.evaluate(_build_inject_helpers_js())

		field = FormField(
			field_id='candidate-name',
			name='Name',
			field_type='text',
			section='Self Identify',
		)

		success = await _fill_text_field(page, field, 'Rioyang Chen', '[Name]')

		assert success is True
		assert await page.evaluate("() => window.__committedName") == 'Rioyang Chen'
		assert await page.evaluate("() => document.getElementById('candidate-name').getAttribute('data-committed')") == 'true'


async def test_fill_date_field_commits_with_enter(
	httpserver: HTTPServer,
):
	async with managed_browser_session() as browser_session:
		tools = Tools()
		httpserver.expect_request('/enter-commit-date').respond_with_data(
			ENTER_COMMIT_DATE_HTML,
			content_type='text/html',
		)

		await tools.navigate(
			url=httpserver.url_for('/enter-commit-date'),
			new_tab=False,
			browser_session=browser_session,
		)
		await asyncio.sleep(0.3)

		page = await browser_session.get_current_page()
		assert page is not None
		await page.evaluate(_build_inject_helpers_js())

		field = FormField(
			field_id='start-date',
			name='Date',
			field_type='date',
			section='Self Identify',
		)

		success = await _fill_date_field(page, field, '03/12/2026', '[Date]')

		assert success is True
		assert await page.evaluate("() => window.__committedDate") == '03/12/2026'
		assert await page.evaluate("() => document.getElementById('start-date').getAttribute('data-committed')") == 'true'


def test_parse_profile_evidence_includes_address_and_referral_fields():
	evidence = _parse_profile_evidence(
		'{"address":"100 Main St","address_line_2":"Apt 4B","country":"United States",'
		'"how_did_you_hear":"LinkedIn"}'
	)

	assert evidence['address'] == '100 Main St'
	assert evidence['address_line_2'] == 'Apt 4B'
	assert evidence['country'] == 'United States'
	assert evidence['how_did_you_hear'] == 'LinkedIn'
	assert evidence['phone_device_type'] is None
	assert evidence['phone_country_code'] is None


def test_infer_entry_data_from_scope_uses_profile_lists():
	profile = {
		'experience': [{'title': 'Engineer 1'}, {'title': 'Engineer 2'}],
		'education': [{'school': 'RIT'}, {'school': 'UCLA'}],
	}

	assert _infer_entry_data_from_scope(profile, 'Work Experience 2', None) == {'title': 'Engineer 2'}
	assert _infer_entry_data_from_scope(profile, 'Education 1', 'Education') == {'school': 'RIT'}


def test_known_profile_value_matches_optional_address_fields():
	evidence = {
		'address': '100 Main St',
		'address_line_2': 'Apt 4B',
		'country': 'United States',
	}

	assert _known_profile_value('Address Line 1', evidence) == '100 Main St'
	assert _known_profile_value('Apartment / Unit', evidence) == 'Apt 4B'
	assert _known_profile_value('Country / Region', evidence) == 'United States'


def test_known_profile_value_maps_exact_name_to_full_name():
	evidence = {
		'first_name': 'Rioyang',
		'last_name': 'Chen',
	}

	assert _known_profile_value('Name*', evidence) == 'Rioyang Chen'


def test_known_profile_value_matches_optional_address_and_link_variants():
	evidence = {
		'address': '100 Main St',
		'address_line_2': 'Apt 4B',
		'portfolio': 'https://janesmith.dev',
	}

	assert _known_profile_value('Mailing Address', evidence) == '100 Main St'
	assert _known_profile_value('Mailing Address Line 2', evidence) == 'Apt 4B'
	assert _known_profile_value('Website URL', evidence) == 'https://janesmith.dev'


def test_find_best_profile_answer_matches_long_screening_questions():
	answer_map = {
		'Are you legally authorized to work in the country in which this job is located?': 'Yes',
		'Phone Device Type': 'Mobile',
	}

	assert _find_best_profile_answer('Legally authorized to work', answer_map) == 'Yes'
	assert _find_best_profile_answer('Phone Type', answer_map) == 'Mobile'


def test_find_best_profile_answer_avoids_low_confidence_optional_source_match():
	answer_map = {
		'Source': 'LinkedIn',
	}

	assert _find_best_profile_answer('Lead Source Type', answer_map, minimum_confidence='strong') is None


def test_known_profile_value_for_field_uses_structured_profile_answers():
	field = FormField(
		field_id='auth-1',
		name='Are you legally authorized to work in the country in which this job is located?',
		field_type='select',
		section='Application Questions',
		options=['Yes', 'No'],
	)
	evidence = {'work_authorization': 'US Citizen'}
	profile = {'authorized_to_work': True, 'sponsorship_needed': False}

	assert _known_profile_value_for_field(field, evidence, profile) == 'Yes'


def test_known_profile_value_for_optional_field_requires_high_confidence():
	field = FormField(
		field_id='lead-source-1',
		name='Lead Source Type',
		field_type='text',
		section='My Information',
		required=False,
	)
	evidence = {'how_did_you_hear': 'LinkedIn'}

	assert _known_profile_value_for_field(field, evidence, {}, minimum_confidence='strong') is None
	assert _known_profile_value_for_field(field, evidence, {}, minimum_confidence='medium') == 'LinkedIn'


def test_match_answer_fills_optional_field_only_for_high_confidence_matches():
	field = FormField(
		field_id='website-1',
		name='Website URL',
		field_type='text',
		section='My Information',
		required=False,
	)
	answers = {'Website': 'https://ringo.dev'}

	assert _match_answer(field, answers, {}, {}) == 'https://ringo.dev'


def test_match_answer_skips_ambiguous_optional_field_matches():
	field = FormField(
		field_id='lead-source-2',
		name='Lead Source Type',
		field_type='text',
		section='My Information',
		required=False,
	)
	answers = {'Source': 'LinkedIn'}

	assert _match_answer(field, answers, {}, {}) is None


def test_default_value_only_uses_narrow_policy_dates():
	regular_date = FormField(field_id='start-date', name='Start Date', field_type='date', section='Education 1')
	self_identify_date = FormField(field_id='self-date', name='Date', field_type='date', section='Self Identify')
	source_field = FormField(field_id='source', name='How Did You Hear About Us?', field_type='select', section='My Information')

	assert _default_value(regular_date) == ''
	assert _default_value(source_field) == ''
	assert _default_value(self_identify_date) == date.today().isoformat()


def test_replace_placeholder_answers_does_not_invent_non_demographic_values():
	field = FormField(
		field_id='source-required',
		name='How Did You Hear About Us?',
		field_type='select',
		section='My Information',
		required=True,
		options=['Select One', 'LinkedIn', 'Other'],
	)
	parsed = {'How Did You Hear About Us?': 'Select One'}

	_replace_placeholder_answers(parsed, [field], ['How Did You Hear About Us?'])

	assert parsed['How Did You Hear About Us?'] == ''


def test_replace_placeholder_answers_allows_neutral_decline_for_self_identify():
	field = FormField(
		field_id='disability-disclosure',
		name='Disability Status',
		field_type='select',
		section='Self Identify',
		required=True,
		options=['Select One', 'I do not wish to answer', 'No'],
	)
	parsed = {'Disability Status': 'Select One'}

	_replace_placeholder_answers(parsed, [field], ['Disability Status'])

	assert parsed['Disability Status'] == 'I do not wish to answer'


def test_dropdown_selection_match_requires_final_visible_value():
	assert _selection_matches_value('Phone Device Type selected Mobile', 'Mobile')
	assert _selection_matches_value('LinkedIn', 'How did you hear about us > LinkedIn')
	assert not _selection_matches_value('Job Board/Social Media Web Site', 'LinkedIn')
	assert not _selection_matches_value('Select One', 'LinkedIn')


def test_fill_dropdown_confirmation_requires_final_visible_value():
	assert _field_value_matches_expected('Phone Device Type selected Mobile', 'Mobile')
	assert _field_value_matches_expected('LinkedIn', 'Job Board/Social Media Web Site > LinkedIn')
	assert not _field_value_matches_expected('Job Board/Social Media Web Site', 'LinkedIn')
