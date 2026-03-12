"""System prompt builder for the GhostHands job-application agent.

Ports the decision-engine prompts from GHOST-HANDS v1 (TypeScript) into a
browser-use ``extend_system_message`` string.  The prompt is appended after
browser-use's own system prompt so the agent keeps all native capabilities
while gaining job-application-specific guardrails and the DomHand action
preference hierarchy.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Platform guardrails — one block per ATS, injected into the system prompt
# ---------------------------------------------------------------------------

PLATFORM_GUARDRAILS: dict[str, str] = {
	"workday": (
		"Workday often uses multi-step sections with repeated 'Next' buttons.\n"
		"Treat any visible 'Submit', 'Submit Application', or final review CTA "
		"as a stop condition — do NOT click it.\n"
		"\n"
		"ACCOUNT CREATION / SIGN-IN:\n"
		"CRITICAL: Do NOT use domhand_fill on account creation or sign-in pages.\n"
		"The email/password must come from your credentials (sensitive_data),\n"
		"NOT from the applicant profile.  domhand_fill would use the wrong email.\n"
		"\n"
		"On the Create Account page, follow this EXACT sequence:\n"
		"  1. Type the credential email into the Email Address field using input_text\n"
		"  2. Type the credential password into the Password field using input_text\n"
		"  3. Type the credential password into the Verify/Confirm Password field\n"
		"  4. Call domhand_check_agreement to check the 'I agree' checkbox — "
		"this is REQUIRED, the Create Account button will NOT work without it.\n"
		"     Do NOT try to click the checkbox manually — Workday uses custom "
		"widgets that need JavaScript-based checking.  domhand_check_agreement "
		"handles this reliably.\n"
		"  5. Use domhand_click_button with button_label='Create Account' to click the button.\n"
		"     CRITICAL: Do NOT use the regular click action for Create Account or Sign In.\n"
		"     Workday buttons require trusted events — regular click uses CDP mouse events\n"
		"     which Workday silently ignores. domhand_click_button uses Playwright native\n"
		"     click which produces trusted events.\n"
		"\n"
		"If domhand_check_agreement reports 'no agreement checkboxes found' or "
		"the checkbox still appears unchecked, use the evaluate action with:\n"
		"  evaluate(\"document.querySelectorAll('input[type=checkbox]').forEach(c => {c.checked=true; c.dispatchEvent(new Event('change',{bubbles:true}))})\")\n"
		"\n"
		"IMPORTANT: For ALL Workday auth buttons (Create Account, Sign In), use\n"
		"domhand_click_button instead of the regular click action.\n"
		"\n"
		"NEVER click the 'Sign In' button when you are on the Create Account page.\n"
		"NEVER toggle between Create Account and Sign In.  Pick ONE path and stick to it.\n"
		"If the first page shown is Create Account, stay on Create Account.\n"
		"If Create Account fails with 'Invalid Username/Password' or similar error,\n"
		"report it as: done(success=False, text='blocker: account creation failed — "
		"invalid credentials or account already exists').\n"
		"Do NOT switch to Sign In after a Create Account failure.\n"
		"\n"
		"NEVER click any SSO/social login icons (Google, LinkedIn, Facebook, Apple,\n"
		"or any icon-based login buttons).  Stay on the native email/password path ONLY.\n"
		"\n"
		"After sign-in or account creation, you may see a verification code "
		"page. Report it as: done(success=False, text='blocker: verification code required').\n"
		"\n"
		"FORM FILLING:\n"
		"- If 'Apply Manually' is visible after clicking Apply, choose that first "
		"before any field-filling.\n"
		"- Use expand_repeaters or click 'Add' buttons when work history or "
		"education sections expose visible 'Add' controls.\n"
		"- Workday uses shadow DOM with data-automation-id selectors. DomHand "
		"handles these, but if it fails, use generic input/click.\n"
		"- Date fields use MM/DD/YYYY format. For segmented date inputs, type "
		"continuous digits (e.g. '06152024') — Workday auto-advances segments."
	),
	"greenhouse": (
		"Greenhouse usually has a single-page application flow with resume "
		"upload near the top.\n"
		"The initial 'Apply' button can be valid, but never click a final "
		"'Submit Application' button.\n"
		"If the page shows a review/confirmation summary, call done with "
		"success=True and provide extracted data."
	),
	"lever": (
		"Lever often keeps the application on one long page with a final "
		"submit button at the bottom.\n"
		"Prefer filling while editable fields remain visible; never convert "
		"a visible submit button into a click.\n"
		"Scrolling is acceptable when the page is long and no higher-priority "
		"action is clear."
	),
	"smartrecruiters": (
		"SmartRecruiters may split flows across apply, login, and review steps.\n"
		"If authentication prompts appear, prefer login or create_account "
		"rather than generic click actions.\n"
		"SmartRecruiters uses shadow DOM custom elements — 'Add' buttons for "
		"work experience, education, and other repeatable sections may appear "
		"inside shadow roots.\n"
		"CRITICAL: Before filling a repeater section, expand ALL visible 'Add' "
		"or '+' buttons to create enough entries for the applicant profile.\n"
		"After expanding, re-observe before filling to see the newly created "
		"fields.\n"
		"Any CAPTCHA, turnstile, or verification wall must be reported as a "
		"blocker via done(success=False, text='blocker: CAPTCHA detected')."
	),
	"generic": (
		"Stay conservative on unfamiliar platforms.\n"
		"Prefer filling editable fields over navigation when fields remain.\n"
		"Never press a button whose text implies final submission.\n"
		"Call done(success=True) on read-only review pages and true "
		"confirmation/success pages."
	),
}


def _format_profile_summary(resume_profile: dict) -> str:
	"""Build a concise applicant profile summary from the resume dict.

	The summary is included in the system prompt so the agent knows what
	data is available for form filling without needing an extra LLM call.

	Handles both structured resume format (name, experience, education)
	and flat JSON format (first_name, last_name, email, etc.).
	"""
	lines: list[str] = []

	# ── Name (handle both formats) ───────────────────────────────
	name = resume_profile.get("name")
	if not name:
		first = resume_profile.get("first_name", "")
		last = resume_profile.get("last_name", "")
		if first or last:
			name = f"{first} {last}".strip()
	if name:
		lines.append(f"Name: {name}")

	if email := resume_profile.get("email"):
		lines.append(f"Email: {email}")
	if phone := resume_profile.get("phone"):
		lines.append(f"Phone: {phone}")

	# ── Location (handle both formats) ───────────────────────────
	location = resume_profile.get("location")
	if not location:
		city = resume_profile.get("city", "")
		state = resume_profile.get("state", "")
		country = resume_profile.get("country", "")
		postal = resume_profile.get("postal_code", "")
		address = resume_profile.get("address", "")
		loc_parts = []
		for p in [address, city, state, postal, country]:
			if p is None or p == "":
				continue
			if isinstance(p, str):
				loc_parts.append(p.strip())
			elif isinstance(p, dict):
				loc_parts.append(", ".join(str(v).strip() for v in p.values() if v))
			elif isinstance(p, (list, tuple)):
				loc_parts.append(", ".join(str(x).strip() for x in p if x))
			else:
				loc_parts.append(str(p).strip())
		if loc_parts:
			location = ", ".join(loc_parts)
	if location:
		if isinstance(location, dict):
			location = ", ".join(str(v).strip() for v in location.values() if v)
		elif not isinstance(location, str):
			location = str(location)
		lines.append(f"Location: {location}")

	# ── Flat fields common in sample data ────────────────────────
	flat_fields = {
		"age": "Age",
		"gender": "Gender",
		"race_ethnicity": "Race/Ethnicity",
		"years_experience": "Years of experience",
		"veteran_status": "Veteran status",
		"disability_status": "Disability status",
		"hispanic_latino": "Hispanic/Latino",
		"visa_sponsorship": "Visa sponsorship",
	}
	for key, label in flat_fields.items():
		val = resume_profile.get(key)
		if val is not None and val != "":
			lines.append(f"{label}: {val}")

	# ── Boolean fields ───────────────────────────────────────────
	if resume_profile.get("US_citizen") is not None:
		lines.append(f"US Citizen: {'Yes' if resume_profile['US_citizen'] else 'No'}")
	if resume_profile.get("sponsorship_needed") is not None:
		lines.append(f"Sponsorship needed: {'Yes' if resume_profile['sponsorship_needed'] else 'No'}")

	# ── Work experience (structured format) ──────────────────────
	experiences = resume_profile.get("experience", [])
	if experiences:
		exp_lines: list[str] = []
		for exp in experiences[:5]:
			title = exp.get("title", "")
			company = exp.get("company", "")
			location = exp.get("location", "")
			start = exp.get("start_date", "")
			end = exp.get("end_date", "")
			current = exp.get("currently_working", False)
			desc = exp.get("description", "")
			if title or company:
				date_range = f"{start} — {'Present' if current else end}" if start else ""
				loc_str = f" ({location})" if location else ""
				line = f"  - {title} at {company}{loc_str}"
				if date_range:
					line += f" [{date_range}]"
				exp_lines.append(line.strip())
				if desc:
					exp_lines.append(f"    {desc[:200]}")
		if exp_lines:
			lines.append("Work experience:")
			lines.extend(exp_lines)

	# ── Education (structured format) ────────────────────────────
	education = resume_profile.get("education", [])
	if education:
		edu_lines: list[str] = []
		for edu in education[:3]:
			degree = edu.get("degree", "")
			school = edu.get("school", "")
			field = edu.get("field_of_study", "")
			grad_date = edu.get("graduation_date", "")
			gpa = edu.get("gpa", "")
			if degree or school:
				degree_str = f"{degree} in {field}" if field else degree
				line = f"  - {degree_str} — {school}"
				if grad_date:
					line += f" (Graduated {grad_date})"
				if gpa:
					line += f" GPA: {gpa}"
				edu_lines.append(line.strip())
		if edu_lines:
			lines.append("Education:")
			lines.extend(edu_lines)

	# ── Skills ───────────────────────────────────────────────────
	skills = resume_profile.get("skills", [])
	if skills:
		lines.append(f"Skills: {', '.join(skills[:20])}")

	# ── Languages ────────────────────────────────────────────────
	languages = resume_profile.get("languages", [])
	if languages:
		lang_strs = [f"{l.get('language', '')} ({l.get('proficiency', '')})" for l in languages if l.get("language")]
		if lang_strs:
			lines.append(f"Languages: {', '.join(lang_strs)}")

	# ── Certifications ───────────────────────────────────────────
	certs = resume_profile.get("certifications", [])
	if certs:
		cert_strs = [f"{c.get('name', '')} ({c.get('issuer', '')})" for c in certs if c.get("name")]
		if cert_strs:
			lines.append(f"Certifications: {', '.join(cert_strs)}")

	# ── Work authorization / availability ────────────────────────
	if wa := resume_profile.get("work_authorization"):
		lines.append(f"Work authorization: {wa}")
	if start := resume_profile.get("available_start_date"):
		lines.append(f"Available start date: {start}")
	if salary := resume_profile.get("salary_expectation"):
		currency = resume_profile.get("salary_currency", "USD")
		lines.append(f"Salary expectation: {salary} {currency}")
	if resume_profile.get("willing_to_relocate") is not None:
		lines.append(f"Willing to relocate: {'Yes' if resume_profile['willing_to_relocate'] else 'No'}")
	if source := resume_profile.get("how_did_you_hear"):
		lines.append(f"How did you hear about us: {source}")

	# ── Open-ended answers ───────────────────────────────────────
	for key, val in resume_profile.items():
		if key.startswith("what_") or key.startswith("why_") or key.startswith("how_"):
			if isinstance(val, str) and val.strip():
				label = key.replace("_", " ").capitalize()
				lines.append(f"{label}: {val}")

	if not lines:
		return "No applicant profile provided."

	return "\n".join(lines)


def build_system_prompt(
	resume_profile: dict,
	platform: str = "generic",
) -> str:
	"""Build the ``extend_system_message`` string for the browser-use Agent.

	This prompt is **appended** to browser-use's native system prompt.  It
	defines the agent's job-application role, the DomHand action preference
	hierarchy, platform-specific guardrails, and the applicant profile.

	Parameters
	----------
	resume_profile:
		Dict containing the applicant's parsed resume data (name, email,
		experience, education, skills, etc.).
	platform:
		ATS identifier used to select platform-specific guardrails.
		One of ``"workday"``, ``"greenhouse"``, ``"lever"``,
		``"smartrecruiters"``, or ``"generic"`` (default).

	Returns
	-------
	str
		The prompt extension string.
	"""
	guardrails = PLATFORM_GUARDRAILS.get(platform, PLATFORM_GUARDRAILS["generic"])
	profile_summary = _format_profile_summary(resume_profile)

	prompt_parts: list[str] = [
		# ── Role ────────────────────────────────────────────────────
		"<ghosthands_role>",
		"You are a job application automation agent.  Your job is to navigate",
		"an ATS (Applicant Tracking System), fill out every form field, upload",
		"the resume when prompted, and advance through each step of the",
		"application flow — WITHOUT ever clicking the final submit button.",
		"</ghosthands_role>",
		"",
		# ── DomHand action hierarchy ───────────────────────────────
		"<domhand_actions>",
		"You have access to DomHand actions that fill forms efficiently by",
		"operating directly on the DOM.  ALWAYS prefer them over generic",
		"click/input actions when filling forms:",
		"",
		"Action preference hierarchy (highest to lowest priority):",
		"1. domhand_fill — Fills ALL visible form fields in one call for",
		"   near-zero cost.  Try this FIRST whenever you see a form.",
		"2. domhand_select — Selects dropdown/radio/checkbox options by",
		"   label matching.  Use when domhand_fill reports unresolved",
		"   select/radio/checkbox fields.",
		"3. domhand_upload — Uploads the applicant's resume file.  Use",
		"   when you see a file-upload input.",
		"4. Generic browser-use actions (click, input_text, etc.) — Use",
		"   ONLY as a fallback when DomHand actions fail, e.g. due to",
		"   shadow DOM, custom widgets, or iframes that DomHand cannot",
		"   reach.",
		"",
		"After calling domhand_fill, inspect its result to see which fields",
		"were filled and which remain unresolved.  Handle unresolved fields",
		"individually with domhand_select, generic input, or by skipping",
		"optional fields the applicant profile does not cover.",
		"</domhand_actions>",
		"",
		# ── Hard rules ─────────────────────────────────────────────
		"<hard_rules>",
		"- NEVER click any final 'Submit', 'Submit Application', 'Finish',",
		"  or equivalent CTA.  When you reach a review page or the next",
		"  action would be final submission, call done(success=True) and",
		"  include any extracted confirmation data in the text.",
		"- Leave optional fields empty when the applicant profile does not",
		"  provide an explicit value.  Do NOT invent information.",
		"- After filling a step, look for 'Next', 'Continue', or",
		"  'Save & Continue' buttons and click them to advance.",
		"- Prefer the smallest reversible action that advances the flow.",
		"- If the page is ambiguous or unstable, wait one step before",
		"  forcing a risky action.",
		"</hard_rules>",
		"",
		# ── Blocker handling ───────────────────────────────────────
		"<blocker_handling>",
		"If you encounter any of the following blockers, call done immediately",
		"with success=False and a descriptive text:",
		"- CAPTCHA / turnstile / bot detection → done(success=False,",
		"  text='blocker: CAPTCHA detected')",
		"- Login wall requiring credentials you do not have →",
		"  done(success=False, text='blocker: login required')",
		"- 403 / access-denied / geo-blocked page →",
		"  done(success=False, text='blocker: access denied')",
		"- Application already submitted or position closed →",
		"  done(success=False, text='blocker: position closed')",
		"Do NOT retry blockers.  Report them and stop.",
		"</blocker_handling>",
		"",
		# ── Multi-page flow guidance ──────────────────────────────
		"<multi_page_flow>",
		"Many ATS platforms split applications across multiple pages or",
		"sections.  Follow this EXACT sequence on EVERY page transition:",
		"",
		"MANDATORY PAGE ENTRY SEQUENCE:",
		"1. When you land on ANY new page or section, your VERY FIRST",
		"   action MUST be domhand_fill.  No exceptions.  Do NOT use",
		"   click or input_text before trying domhand_fill.",
		"2. After domhand_fill completes, review its output for unresolved",
		"   fields.  Handle them with domhand_select or generic actions.",
		"3. Check for agreement checkboxes ('I agree', 'I accept', 'I",
		"   understand', privacy policy consent, terms of service).  These",
		"   are often missed by domhand_fill.  Click any unchecked agreement",
		"   checkbox before proceeding.",
		"4. Look for a 'Next' / 'Continue' / 'Save & Continue' button.",
		"5. Click it to advance to the next section.",
		"6. On the new page, go back to step 1 — domhand_fill FIRST.",
		"",
		"Repeat until you reach a review/confirmation page, then call",
		"done(success=True).",
		"</multi_page_flow>",
		"",
		# ── Review / completion detection ─────────────────────────
		"<completion_detection>",
		"- Review pages are usually read-only summaries with a visible final",
		"  submit button and few or no editable fields → call done(success=True).",
		"- Confirmation pages usually contain thank-you, submitted, received,",
		"  or success language → call done(success=True).",
		"- If you detect a review page, DO NOT click submit.  Just call done.",
		"</completion_detection>",
		"",
		# ── Platform guardrails ───────────────────────────────────
		"<platform_guardrails>",
		f"Platform: {platform}",
		guardrails,
		"</platform_guardrails>",
		"",
		# ── Applicant profile ─────────────────────────────────────
		"<applicant_profile>",
		profile_summary,
		"</applicant_profile>",
	]

	return "\n".join(prompt_parts)


def build_task_prompt(
	job_url: str,
	resume_path: str,
	sensitive_data: dict | None,
) -> str:
	"""Build the task prompt for the agent."""
	task = (
		f"Go to {job_url} and fill out the job application form completely.\n"
		"\n"
		"CRITICAL -- Action Order:\n"
		"1. After navigating to the page, your FIRST action MUST be domhand_fill.\n"
		"2. After domhand_fill completes, review its output to see which fields were filled and which failed.\n"
		"3. For failed dropdowns/selects, use domhand_select.\n"
		f"4. For file uploads (resume), use domhand_upload or upload_file action with path: {resume_path}\n"
		"5. Only use generic browser-use actions (click, input_text) as a LAST RESORT.\n"
		"6. After all fields on the current page are filled, click Next/Continue/Save to advance.\n"
		"7. On each new page, call domhand_fill AGAIN as the first action.\n"
		"\n"
		"Other rules:\n"
	)
	if sensitive_data:
		task += (
			"- Use the provided credentials to log in or create an account if needed. "
			"For Workday, fill email + password + confirm password on the Create Account page.\n"
		)
	else:
		task += "- If a login wall appears, report it as a blocker.\n"
	task += (
		"- Do NOT click the final Submit button. Stop at the review page and use the done action.\n"
		"- If anything pops up blocking the form, close it and continue.\n"
	)
	return task
