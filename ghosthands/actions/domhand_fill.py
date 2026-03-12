"""DomHand Fill — the core action that extracts form fields, generates answers via
a single cheap LLM call, and fills everything via Playwright DOM manipulation.

This is the primary workhorse action for job application form filling. It:
1. Extracts ALL visible form fields from the page
2. Makes a SINGLE cheap LLM call (Haiku) with resume profile + all fields -> answer map
3. Fills each field via CDP DOM manipulation ($0.00 per field)
4. Re-extracts to verify fills and detect newly revealed fields
5. Returns ActionResult with filled/failed/unfilled counts
"""

import json
import logging
import os
import re
from datetime import date
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession

from ghosthands.actions.views import (
	DomHandFillParams,
	FillFieldResult,
	FormField,
	get_stable_field_key,
	is_placeholder_value,
	normalize_name,
)

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

MAX_FILL_ROUNDS = 3

# JavaScript to extract all interactive form fields from the page.
# Runs in browser context via CDP Runtime.evaluate.
_EXTRACT_FIELDS_JS = r"""
() => {
	const INTERACTIVE = [
		'input', 'select', 'textarea',
		'[role="textbox"]', '[role="combobox"]', '[role="listbox"]',
		'[role="checkbox"]', '[role="radio"]', '[role="switch"]',
		'[role="spinbutton"]', '[role="slider"]', '[role="searchbox"]',
		'[data-uxi-widget-type="selectinput"]',
		'[aria-haspopup="listbox"]',
	].join(', ');

	function getLabel(el) {
		// 1. aria-label
		const ariaLabel = el.getAttribute('aria-label');
		if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();

		// 2. <label for="id">
		if (el.id) {
			const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
			if (label) return label.textContent.trim();
		}

		// 3. Wrapping <label>
		const parentLabel = el.closest('label');
		if (parentLabel) {
			const clone = parentLabel.cloneNode(true);
			clone.querySelectorAll('input, select, textarea, button').forEach(c => c.remove());
			const text = clone.textContent.trim();
			if (text) return text;
		}

		// 4. aria-labelledby
		const labelledBy = el.getAttribute('aria-labelledby');
		if (labelledBy) {
			const parts = labelledBy.split(/\s+/).map(id => {
				const ref = document.getElementById(id);
				return ref ? ref.textContent.trim() : '';
			}).filter(Boolean);
			if (parts.length) return parts.join(' ');
		}

		// 5. Previous sibling text
		let prev = el.previousElementSibling;
		if (prev && prev.tagName === 'LABEL') return prev.textContent.trim();
		if (prev && prev.tagName === 'SPAN') {
			const text = prev.textContent.trim();
			if (text && text.length < 100) return text;
		}

		// 6. Parent text (for custom widgets)
		const parent = el.closest('.form-group, .form-field, .field, [class*="field"], [class*="input"]');
		if (parent) {
			const labelEl = parent.querySelector('label, .label, [class*="label"]');
			if (labelEl) return labelEl.textContent.trim();
		}

		// 7. placeholder or title
		return el.getAttribute('placeholder') || el.getAttribute('title') || '';
	}

	function getSection(el) {
		const section = el.closest(
			'[data-section], fieldset, .form-section, .section, [class*="section"], [role="group"]'
		);
		if (!section) return '';
		const legend = section.querySelector('legend, .section-title, .section-header, h2, h3, h4');
		return legend ? legend.textContent.trim() : section.getAttribute('data-section') || '';
	}

	function getFieldType(el) {
		const tag = el.tagName.toLowerCase();
		if (tag === 'select') return 'select';
		if (tag === 'textarea') return 'textarea';
		if (tag === 'input') {
			const type = (el.type || 'text').toLowerCase();
			if (type === 'hidden' || type === 'submit' || type === 'button' || type === 'image' || type === 'reset') return null;
			return type;
		}
		const role = el.getAttribute('role');
		if (role === 'combobox' || el.getAttribute('data-uxi-widget-type') === 'selectinput') return 'select';
		if (role === 'checkbox') return 'checkbox';
		if (role === 'radio') return 'radio';
		if (role === 'textbox' || role === 'searchbox') return 'text';
		if (role === 'listbox') return 'select';
		if (role === 'switch') return 'checkbox';
		return 'text';
	}

	function getOptions(el) {
		if (el.tagName.toLowerCase() === 'select') {
			return Array.from(el.options)
				.map(o => o.text.trim())
				.filter(t => t && !/^(select|choose|--)/i.test(t));
		}
		const listboxId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
		if (listboxId) {
			const listbox = document.getElementById(listboxId);
			if (listbox) {
				return Array.from(listbox.querySelectorAll('[role="option"], li, [class*="option"]'))
					.map(o => o.textContent.trim())
					.filter(Boolean)
					.slice(0, 50);
			}
		}
		return [];
	}

	function isVisible(el) {
		if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') return false;
		const rect = el.getBoundingClientRect();
		return rect.width > 0 && rect.height > 0;
	}

	function getCurrentValue(el) {
		const tag = el.tagName.toLowerCase();
		if (tag === 'select') {
			const sel = el.options[el.selectedIndex];
			return sel ? sel.text.trim() : '';
		}
		if (tag === 'input' && (el.type === 'checkbox' || el.type === 'radio')) {
			return el.checked ? 'checked' : '';
		}
		if (el.getAttribute('role') === 'checkbox' || el.getAttribute('role') === 'switch') {
			return el.getAttribute('aria-checked') === 'true' ? 'checked' : '';
		}
		return el.value || el.textContent?.trim() || '';
	}

	function isRequired(el) {
		if (el.required) return true;
		if (el.getAttribute('aria-required') === 'true') return true;
		const label = getLabel(el);
		if (label && /\*\s*$/.test(label)) return true;
		return false;
	}

	const seen = new Set();
	const fields = [];
	let counter = 0;

	for (const el of document.querySelectorAll(INTERACTIVE)) {
		if (seen.has(el)) continue;
		seen.add(el);
		const fieldType = getFieldType(el);
		if (!fieldType) continue;
		if (!isVisible(el)) continue;

		counter++;
		const ffId = el.getAttribute('data-ff-id') || ('dh-' + counter);
		el.setAttribute('data-ff-id', ffId);

		const label = getLabel(el);
		const options = getOptions(el);
		const choices = [];
		if (fieldType === 'radio') {
			const name = el.getAttribute('name');
			if (name) {
				const radios = document.querySelectorAll('input[type="radio"][name="' + CSS.escape(name) + '"]');
				for (const r of radios) {
					const rLabel = getLabel(r);
					if (rLabel) choices.push(rLabel);
				}
			}
		}

		fields.push({
			field_id: ffId,
			name: label.replace(/\*\s*$/, '').trim(),
			field_type: fieldType,
			section: getSection(el),
			required: isRequired(el),
			options: options,
			choices: choices,
			accept: el.getAttribute('accept') || null,
			is_native: el.tagName.toLowerCase() === 'input' || el.tagName.toLowerCase() === 'select' || el.tagName.toLowerCase() === 'textarea',
			is_multi_select: el.multiple || el.getAttribute('aria-multiselectable') === 'true',
			visible: true,
			raw_label: label,
			synthetic_label: false,
			field_fingerprint: null,
			current_value: getCurrentValue(el),
		});
	}

	return JSON.stringify(fields);
}
"""

# JavaScript to fill a single field by its data-ff-id.
# Args: ffId (string), value (string), fieldType (string)
_FILL_FIELD_JS = r"""
(ffId, value, fieldType) => {
	const el = document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!el) return JSON.stringify({success: false, error: 'Element not found'});

	try {
		if (fieldType === 'select') {
			const select = el;
			// Try matching by option text first, then by value
			let matched = false;
			const lowerValue = value.toLowerCase();
			for (const opt of select.options) {
				if (opt.text.trim().toLowerCase() === lowerValue || opt.value.toLowerCase() === lowerValue) {
					select.value = opt.value;
					matched = true;
					break;
				}
			}
			// Fuzzy: partial match
			if (!matched) {
				for (const opt of select.options) {
					if (opt.text.trim().toLowerCase().includes(lowerValue) || lowerValue.includes(opt.text.trim().toLowerCase())) {
						select.value = opt.value;
						matched = true;
						break;
					}
				}
			}
			if (!matched) return JSON.stringify({success: false, error: 'No matching option found for: ' + value});
			select.dispatchEvent(new Event('change', {bubbles: true}));
			select.dispatchEvent(new Event('input', {bubbles: true}));
			return JSON.stringify({success: true});
		}

		if (fieldType === 'checkbox' || fieldType === 'radio') {
			const shouldCheck = /^(checked|true|yes|on|1)$/i.test(value);
			if (el.tagName === 'INPUT') {
				if (el.checked !== shouldCheck) {
					el.click();
				}
			} else {
				// ARIA checkbox/radio
				const current = el.getAttribute('aria-checked') === 'true';
				if (current !== shouldCheck) {
					el.click();
				}
			}
			return JSON.stringify({success: true});
		}

		// Text-like fields: text, email, tel, url, number, textarea, search, password, date
		const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
			el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
			'value'
		);
		if (nativeInputValueSetter && nativeInputValueSetter.set) {
			nativeInputValueSetter.set.call(el, value);
		} else {
			el.value = value;
		}
		el.dispatchEvent(new Event('input', {bubbles: true}));
		el.dispatchEvent(new Event('change', {bubbles: true}));
		// Some React frameworks need this
		el.dispatchEvent(new Event('blur', {bubbles: true}));
		return JSON.stringify({success: true});
	} catch (e) {
		return JSON.stringify({success: false, error: e.message});
	}
}
"""


# ── Profile evidence extraction ──────────────────────────────────────

def _parse_profile_evidence(profile_text: str) -> dict[str, str | None]:
	"""Extract structured fields from profile text for direct field matching."""
	def read_line(label: str) -> str | None:
		m = re.search(rf'^\s*{re.escape(label)}:\s*(.+)$', profile_text, re.MULTILINE | re.IGNORECASE)
		val = m.group(1).strip() if m else None
		return val if val else None

	name = read_line('Name')
	first_name = name.split()[0] if name else None
	last_name = ' '.join(name.split()[1:]) if name and len(name.split()) > 1 else None

	location = read_line('Location')
	city: str | None = None
	state: str | None = None
	zip_code: str | None = None
	if location:
		parts = [p.strip() for p in location.split(',') if p.strip()]
		if len(parts) >= 2:
			city = parts[0]
			state_zip = parts[1].split()
			state = state_zip[0] if state_zip else None
			zip_code = state_zip[1] if len(state_zip) > 1 else None

	linkedin = read_line('LinkedIn')
	portfolio = read_line('Portfolio') or read_line('Website')

	# Find GitHub/Twitter URLs in profile text
	github_match = re.search(r'https?://(?:www\.)?github\.com/[^\s)]+', profile_text, re.IGNORECASE)
	twitter_match = re.search(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/[^\s)]+', profile_text, re.IGNORECASE)

	return {
		'first_name': first_name,
		'last_name': last_name,
		'email': read_line('Email'),
		'phone': read_line('Phone'),
		'city': city,
		'state': state,
		'zip': zip_code,
		'linkedin': linkedin,
		'portfolio': portfolio,
		'github': github_match.group(0) if github_match else None,
		'twitter': twitter_match.group(0) if twitter_match else None,
	}


def _known_profile_value(field_name: str, evidence: dict[str, str | None]) -> str | None:
	"""Return a profile value if the field name matches a known personal field.
	This is the fast path: no LLM call needed for basic contact info.
	"""
	name = normalize_name(field_name)
	if not name:
		return None

	if 'first name' in name and evidence.get('first_name'):
		return evidence['first_name']
	if 'last name' in name and evidence.get('last_name'):
		return evidence['last_name']
	if 'email' in name and evidence.get('email'):
		return evidence['email']
	if 'phone extension' in name:
		return None
	if any(kw in name for kw in ('phone', 'mobile', 'telephone')) and evidence.get('phone'):
		return evidence['phone']
	if name == 'city' or ' city' in name:
		return evidence.get('city')
	if name == 'state' or 'state/province' in name or 'province' in name:
		return evidence.get('state')
	if 'postal' in name or 'zip' in name:
		return evidence.get('zip')
	if 'linkedin' in name:
		return evidence.get('linkedin')
	if 'github' in name:
		return evidence.get('github')
	if any(kw in name for kw in ('portfolio', 'website', 'personal site', 'blog')):
		return evidence.get('portfolio')
	if 'twitter' in name or 'x handle' in name:
		return evidence.get('twitter')

	return None


# ── LLM answer generation ───────────────────────────────────────────

_SOCIAL_OR_ID_NO_GUESS_RE = re.compile(
	r'\b(twitter|x(\.com)?\s*(handle|username|profile)?|github|gitlab|linkedin'
	r'|instagram|tiktok|facebook|social\s*(media|profile)?|handle|username|user\s*name'
	r'|passport|driver\'?s?\s*license|license\s*number|national\s*id|id\s*number'
	r'|tax\s*id|itin|ein|ssn|social security)\b',
	re.IGNORECASE,
)


def _sanitize_no_guess_answer(
	field_name: str,
	required: bool,
	answer: str | None,
	evidence: dict[str, str | None],
) -> str:
	"""Prevent fabrication of sensitive identity fields not in profile."""
	proposed = (answer or '').strip()
	known = _known_profile_value(field_name, evidence)
	if known:
		return known

	if not _SOCIAL_OR_ID_NO_GUESS_RE.search(field_name or ''):
		return proposed

	if not proposed:
		return 'N/A' if required else ''
	if is_placeholder_value(proposed) or re.match(r'^(n/a|na|none|unknown|not applicable|prefer not|decline)', proposed, re.IGNORECASE):
		return proposed if required else ''

	# Sensitive field without profile evidence: never fabricate
	return 'N/A' if required else ''


async def _generate_answers(
	fields: list[FormField],
	profile_text: str,
) -> tuple[dict[str, str], int, int]:
	"""Call Haiku to generate answers for all fields in a single batch.

	Returns:
		(answer_map, input_tokens, output_tokens)
	"""
	try:
		import anthropic
	except ImportError:
		logger.error('anthropic package not installed — cannot generate answers')
		return {}, 0, 0

	api_key = os.environ.get('GH_ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_API_KEY', '')
	if not api_key:
		logger.error('No Anthropic API key found (GH_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY)')
		return {}, 0, 0

	client = anthropic.Anthropic(api_key=api_key)
	evidence = _parse_profile_evidence(profile_text)

	# Disambiguate duplicate field names by appending "#2", "#3", etc.
	name_counts: dict[str, int] = {}
	disambiguated_names: list[str] = []
	for i, field in enumerate(fields):
		base_name = (field.name or '').strip() or f'Field {i + 1}'
		norm = normalize_name(base_name) or f'field-{i + 1}'
		count = name_counts.get(norm, 0) + 1
		name_counts[norm] = count
		disambiguated_names.append(f'{base_name} #{count}' if count > 1 else base_name)

	field_descriptions = '\n'.join(
		_build_field_description(field, disambiguated_names[i])
		for i, field in enumerate(fields)
	)

	today = date.today().isoformat()
	prompt = f"""You are filling out a job application form on behalf of an applicant. Today's date is {today}.

Here is their profile:

{profile_text}

Here are the form fields to fill:

{field_descriptions}

Rules:
- For each field, decide what value to put based on the profile.
- Fields marked with * are REQUIRED. NEVER return "" for required fields. Use profile-backed values first; for non-identity fields you may use careful contextual inference.
- For optional fields, still fill them in if the profile has any relevant info. Only return "" for optional fields where there is truly nothing relevant to put.
- NEVER fabricate personal identifiers or social handles/URLs not explicitly in the profile. If missing: return "" for optional, "N/A" for required.
- For dropdowns/radio groups with listed options, pick the EXACT text of one of the available options.
- For multi-select fields, return a JSON array of ALL matching options (e.g., ["Python", "Java"]).
- For checkboxes/toggles, respond with "checked" or "unchecked".
- For file upload fields, skip them (don't include in output).
- For textarea fields, write 2-4 thoughtful sentences using the applicant's real background.
- For demographic/EEO fields, use the applicant's actual info from their profile. If no info, choose the most neutral "decline" option.
- NEVER select a default placeholder value like "Select One", "Please select", etc.
- For salary fields, provide a realistic number based on role and experience level.
- Use the EXACT field names shown above (including any "#N" suffix) as JSON keys.
- Respond with ONLY a valid JSON object. No explanation, no markdown fences.

Example: {{"First Name": "Alex", "Cover Letter": "I am excited to apply because..."}}"""

	try:
		response = client.messages.create(
			model='claude-haiku-4-5-20251001',
			max_tokens=4096,
			messages=[{'role': 'user', 'content': prompt}],
		)

		text = response.content[0].text if response.content and response.content[0].type == 'text' else ''
		input_tokens = response.usage.input_tokens if response.usage else 0
		output_tokens = response.usage.output_tokens if response.usage else 0

		if response.stop_reason == 'max_tokens':
			logger.warning('LLM response was truncated (hit max_tokens). Some fields may be missing answers.')
		logger.info(f'LLM answer response: {text[:200]}{"..." if len(text) > 200 else ""}')

		# Parse JSON
		cleaned = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.MULTILINE)
		cleaned = re.sub(r'\n?```\s*$', '', cleaned, flags=re.MULTILINE).strip()
		parsed: dict[str, Any] = json.loads(cleaned)

		# Normalize array values to comma-separated strings, numbers to strings
		for k, v in list(parsed.items()):
			if isinstance(v, list):
				parsed[k] = ','.join(str(item) for item in v)
			elif isinstance(v, (int, float)):
				parsed[k] = str(v)

		# Post-process: replace placeholder answers with neutral "decline" options
		_replace_placeholder_answers(parsed, fields, disambiguated_names)

		# Enforce no-fabrication policy for sensitive fields
		for i, field in enumerate(fields):
			key = disambiguated_names[i]
			if key in parsed and isinstance(parsed[key], str):
				parsed[key] = _sanitize_no_guess_answer(field.name, field.required, parsed[key], evidence)

		return parsed, input_tokens, output_tokens

	except json.JSONDecodeError:
		logger.warning('Failed to parse LLM response as JSON, using empty answers')
		return {}, 0, 0
	except Exception as e:
		logger.error(f'LLM answer generation failed: {e}')
		return {}, 0, 0


def _build_field_description(field: FormField, display_name: str) -> str:
	"""Build a single-line field description for the LLM prompt."""
	type_label = 'multi-select' if field.is_multi_select else field.field_type
	req_marker = ' *' if field.required else ''
	desc = f'- "{display_name}"{req_marker} (type: {type_label})'
	if field.options:
		desc += f' options: [{", ".join(field.options[:50])}]'
	if field.choices:
		desc += f' choices: [{", ".join(field.choices[:30])}]'
	if field.section:
		desc += f' [section: {field.section}]'
	return desc


def _replace_placeholder_answers(
	parsed: dict[str, Any],
	fields: list[FormField],
	disambiguated_names: list[str],
) -> None:
	"""Replace LLM-generated placeholder answers with real "decline" options."""
	placeholder_re = re.compile(
		r'^(select one|choose one|please select|-- ?select ?--|— ?select ?—|\(select\)|select\.{0,3})$',
		re.IGNORECASE,
	)
	decline_patterns = [
		re.compile(p, re.IGNORECASE)
		for p in [
			r'not declared', r'prefer not', r'decline', r'do not wish',
			r'choose not', r'rather not', r'not specified', r'not applicable', r'n/?a',
		]
	]

	for key, val in list(parsed.items()):
		if not isinstance(val, str) or not placeholder_re.match(val.strip()):
			continue

		# Find the matching field
		idx = disambiguated_names.index(key) if key in disambiguated_names else -1
		field = fields[idx] if idx >= 0 else None

		# Optional fields should remain empty rather than guessed
		if field and not field.required:
			parsed[key] = ''
			continue

		options = (field.options or field.choices or []) if field else []
		# Find a neutral "decline" option
		neutral = next((o for o in options if any(p.search(o) for p in decline_patterns)), None)
		if neutral:
			logger.info(f'Replaced placeholder "{val}" -> "{neutral}" for field "{key}"')
			parsed[key] = neutral
		elif options:
			non_placeholder = [o for o in options if not placeholder_re.match(o.strip())]
			if non_placeholder:
				fallback = non_placeholder[-1]
				logger.info(f'Replaced placeholder "{val}" -> "{fallback}" (last non-placeholder) for field "{key}"')
				parsed[key] = fallback


# ── Field-answer matching ────────────────────────────────────────────

def _match_answer(
	field: FormField,
	answers: dict[str, str],
	evidence: dict[str, str | None],
) -> str | None:
	"""5-pass field-answer matching: exact -> contains -> reverse contains -> word overlap -> profile."""

	# Pass 0: Direct profile evidence (free, no LLM needed)
	profile_val = _known_profile_value(field.name, evidence)
	if profile_val:
		return profile_val

	field_norm = normalize_name(field.name)
	if not field_norm:
		return None

	# Pass 1: Exact label match
	for key, val in answers.items():
		if normalize_name(key) == field_norm:
			return val

	# Pass 2: Answer key contains field name
	for key, val in answers.items():
		if field_norm in normalize_name(key):
			return val

	# Pass 3: Field name contains answer key
	for key, val in answers.items():
		key_norm = normalize_name(key)
		if key_norm and key_norm in field_norm:
			return val

	# Pass 4: Word overlap (at least 2 shared meaningful words)
	field_words = set(field_norm.split()) - {'the', 'a', 'an', 'of', 'for', 'in', 'to', 'and', 'or', 'your', 'my'}
	if len(field_words) >= 2:
		best_overlap = 0
		best_val: str | None = None
		for key, val in answers.items():
			key_words = set(normalize_name(key).split()) - {'the', 'a', 'an', 'of', 'for', 'in', 'to', 'and', 'or', 'your', 'my'}
			overlap = len(field_words & key_words)
			if overlap >= 2 and overlap > best_overlap:
				best_overlap = overlap
				best_val = val
		if best_val is not None:
			return best_val

	# Pass 5: Stem matching — strip common suffixes and retry
	def stem(word: str) -> str:
		for suffix in ('tion', 'sion', 'ment', 'ness', 'ing', 'ity', 'ous', 'ive', 'ful', 'ly', 'ed', 'er', 'es', 's'):
			if word.endswith(suffix) and len(word) > len(suffix) + 2:
				return word[:-len(suffix)]
		return word

	field_stems = {stem(w) for w in field_words}
	for key, val in answers.items():
		key_words = set(normalize_name(key).split()) - {'the', 'a', 'an', 'of', 'for', 'in', 'to', 'and', 'or'}
		key_stems = {stem(w) for w in key_words}
		overlap = len(field_stems & key_stems)
		if overlap >= 2:
			return val

	return None


# ── Core action function ─────────────────────────────────────────────

async def domhand_fill(params: DomHandFillParams, browser_session: BrowserSession) -> ActionResult:
	"""Fill all visible form fields using fast DOM manipulation.

	1. Extract form fields from the page via JavaScript
	2. Generate answers via a single Haiku LLM call
	3. Fill each field via DOM manipulation
	4. Re-extract to verify and catch newly revealed fields
	5. Repeat for up to MAX_FILL_ROUNDS rounds
	"""
	page = await browser_session.get_current_page()
	if not page:
		return ActionResult(error='No active page found in browser session')

	profile_text = _get_profile_text()
	if not profile_text:
		return ActionResult(error='No user profile text found. Set GH_USER_PROFILE_TEXT or GH_USER_PROFILE_PATH env var.')

	evidence = _parse_profile_evidence(profile_text)
	all_results: list[FillFieldResult] = []
	total_input_tokens = 0
	total_output_tokens = 0
	llm_calls = 0
	fields_seen: set[str] = set()

	for round_num in range(1, MAX_FILL_ROUNDS + 1):
		logger.info(f'DomHand fill round {round_num}/{MAX_FILL_ROUNDS}')

		# ── Step 1: Extract fields ────────────────────────────
		try:
			raw_json = await page.evaluate(_EXTRACT_FIELDS_JS)
			raw_fields: list[dict[str, Any]] = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
		except Exception as e:
			logger.error(f'Field extraction failed: {e}')
			return ActionResult(error=f'Failed to extract form fields: {e}')

		fields = [FormField.model_validate(f) for f in raw_fields]

		# Filter by section if requested
		if params.target_section:
			section_norm = normalize_name(params.target_section)
			fields = [
				f for f in fields
				if normalize_name(f.section) == section_norm
				or section_norm in normalize_name(f.section)
				or normalize_name(f.section) in section_norm
			]

		# Skip file inputs and already-filled fields
		fillable_fields: list[FormField] = []
		for f in fields:
			if f.field_type == 'file':
				continue
			key = get_stable_field_key(f)
			if key in fields_seen and f.current_value and not is_placeholder_value(f.current_value):
				continue  # Already filled in a previous round
			fillable_fields.append(f)

		if not fillable_fields:
			if round_num == 1:
				return ActionResult(
					extracted_content='No fillable form fields found on the page.',
					include_extracted_content_only_once=True,
				)
			break  # All fields handled

		logger.info(f'Round {round_num}: {len(fillable_fields)} fillable fields found')

		# ── Step 2: Identify which fields need LLM answers ────
		# Fields where we already know the answer from profile evidence skip the LLM
		needs_llm: list[FormField] = []
		direct_fills: dict[str, str] = {}

		for f in fillable_fields:
			# Skip fields that already have a non-placeholder value
			if f.current_value and not is_placeholder_value(f.current_value):
				fields_seen.add(get_stable_field_key(f))
				continue

			profile_val = _known_profile_value(f.name, evidence)
			if profile_val:
				direct_fills[f.field_id] = profile_val
			else:
				needs_llm.append(f)

		# ── Step 3: Generate LLM answers for remaining fields ─
		answers: dict[str, str] = {}
		if needs_llm:
			llm_answers, in_tok, out_tok = await _generate_answers(needs_llm, profile_text)
			answers = llm_answers
			total_input_tokens += in_tok
			total_output_tokens += out_tok
			llm_calls += 1

		# ── Step 4: Fill fields via DOM ───────────────────────
		round_filled = 0
		round_failed = 0

		# 4a: Fill direct profile matches
		for f in fillable_fields:
			if f.field_id in direct_fills:
				value = direct_fills[f.field_id]
				success = await _fill_single_field(page, f, value)
				all_results.append(FillFieldResult(
					field_id=f.field_id,
					name=f.name,
					success=success,
					actor='dom',
					value_set=value if success else None,
					error=None if success else 'DOM fill failed',
				))
				fields_seen.add(get_stable_field_key(f))
				if success:
					round_filled += 1
				else:
					round_failed += 1

		# 4b: Fill LLM-answered fields
		for f in needs_llm:
			matched_answer = _match_answer(f, answers, evidence)
			if not matched_answer:
				if f.required:
					all_results.append(FillFieldResult(
						field_id=f.field_id,
						name=f.name,
						success=False,
						actor='unfilled',
						error='No answer generated for required field',
					))
					round_failed += 1
				fields_seen.add(get_stable_field_key(f))
				continue

			success = await _fill_single_field(page, f, matched_answer)
			all_results.append(FillFieldResult(
				field_id=f.field_id,
				name=f.name,
				success=success,
				actor='dom',
				value_set=matched_answer if success else None,
				error=None if success else 'DOM fill failed',
			))
			fields_seen.add(get_stable_field_key(f))
			if success:
				round_filled += 1
			else:
				round_failed += 1

		logger.info(f'Round {round_num}: filled={round_filled}, failed={round_failed}')

		# If nothing new was filled this round, stop iterating
		if round_filled == 0:
			break

	# ── Build result summary ──────────────────────────────────
	filled_count = sum(1 for r in all_results if r.success)
	failed_count = sum(1 for r in all_results if not r.success and r.actor == 'dom')
	unfilled_count = sum(1 for r in all_results if r.actor == 'unfilled')

	unfilled_descriptions = [
		f'  - "{r.name}" ({r.error or "no answer"})'
		for r in all_results
		if not r.success
	]

	summary_lines = [
		f'DomHand fill complete: {filled_count} filled, {failed_count} DOM failures, {unfilled_count} unfilled.',
		f'LLM calls: {llm_calls} (input: {total_input_tokens} tokens, output: {total_output_tokens} tokens)',
	]
	if unfilled_descriptions:
		summary_lines.append('Unfilled/failed fields:')
		summary_lines.extend(unfilled_descriptions[:20])  # Cap at 20 to avoid huge messages
		if len(unfilled_descriptions) > 20:
			summary_lines.append(f'  ... and {len(unfilled_descriptions) - 20} more')

	summary = '\n'.join(summary_lines)
	logger.info(summary)

	return ActionResult(
		extracted_content=summary,
		include_extracted_content_only_once=False,
	)


async def _fill_single_field(page: Any, field: FormField, value: str) -> bool:
	"""Fill a single field via CDP JavaScript evaluation. Returns True on success."""
	try:
		result_json = await page.evaluate(_FILL_FIELD_JS, field.field_id, value, field.field_type)
		result = json.loads(result_json) if isinstance(result_json, str) else result_json
		if isinstance(result, dict) and result.get('success'):
			logger.debug(f'Filled "{field.name}" with "{value[:50]}{"..." if len(value) > 50 else ""}"')
			return True
		error = result.get('error', 'unknown') if isinstance(result, dict) else 'unexpected response'
		logger.warning(f'DOM fill failed for "{field.name}": {error}')
		return False
	except Exception as e:
		logger.warning(f'DOM fill exception for "{field.name}": {e}')
		return False


def _get_profile_text() -> str | None:
	"""Load the user profile text from environment variable or file path."""
	# Direct text
	text = os.environ.get('GH_USER_PROFILE_TEXT', '')
	if text.strip():
		return text.strip()

	# File path
	path = os.environ.get('GH_USER_PROFILE_PATH', '')
	if path:
		try:
			import pathlib
			p = pathlib.Path(path)
			if p.is_file():
				return p.read_text(encoding='utf-8').strip()
		except Exception as e:
			logger.warning(f'Failed to read profile from {path}: {e}')

	return None
