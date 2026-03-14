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
        "Workday uses multi-step sections.  Treat any visible 'Submit' or "
        "'Submit Application' button as the FINAL submission — do NOT click it.\n"
        "\n"
        "ACCOUNT CREATION / SIGN-IN:\n"
        "Do NOT use domhand_fill on auth pages — it uses the applicant email\n"
        "instead of login credentials.\n"
        "\n"
        "EXACT sequence for the Create Account page:\n"
        "  1. input_text: credential email → Email Address field\n"
        "  2. input_text: credential password → Password field\n"
        "  3. input_text: credential password → Verify/Confirm Password field\n"
        "  4. domhand_check_agreement → checks the 'I agree' checkbox.\n"
        "     *** THIS IS REQUIRED.  The Create Account button SILENTLY FAILS\n"
        "     if the checkbox is unchecked.  Do NOT skip this step. ***\n"
        "  5. VERIFY: Look at the checkbox.  If it still appears unchecked,\n"
        "     click it manually before proceeding.\n"
        "  6. domhand_click_button(button_label='Create Account').\n"
        "     Use domhand_click_button, NOT the regular click action.\n"
        "\n"
        "Auth page rules:\n"
        "- Pick ONE path (Create Account OR Sign In) and commit.  Do NOT\n"
        "  toggle between them.\n"
        "- If a confirm-password field is visible, you are on Create Account.\n"
        "- NEVER use SSO/social login (Google, LinkedIn, Facebook, Apple).\n"
        "- If account creation fails, report as blocker — do NOT switch to Sign In.\n"
        "- If a verification code is required, report as blocker.\n"
        "\n"
        "FORM FILLING:\n"
        "- Click the main 'Apply' button first.\n"
        "- If a Workday start dialog offers a SAME-SITE option such as "
        "  'Autofill with Resume' or 'Apply with Resume', prefer that path.\n"
        "- Use 'Apply Manually' only when no same-site resume-autofill option "
        "  exists.\n"
        "- NEVER choose external apply paths such as LinkedIn, Indeed, Google, "
        "  or other third-party apply/import options.\n"
        "- Use domhand_expand or click 'Add' buttons to expand work history\n"
        "  and education sections before filling.\n"
        "- Workday uses shadow DOM with data-automation-id selectors.\n"
        "- Date fields: click MM segment, type continuous digits (e.g. '06152024').\n"
        "- After filling all fields, click 'Save and Continue' / 'Next' to advance.\n"
        "  NEVER click the final 'Submit' button."
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
        "SmartRecruiters may also stay on a single editable application page "
        "until the final submit button.\n"
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


COMPLETION_STATE_ADVANCEABLE = "advanceable"
COMPLETION_STATE_REVIEW = "review"
COMPLETION_STATE_CONFIRMATION = "confirmation"
COMPLETION_STATE_PRESUBMIT_SINGLE_PAGE = "presubmit_single_page"

FAIL_OVER_NATIVE_SELECT = "[FAIL-OVER:NATIVE_SELECT]"
FAIL_OVER_CUSTOM_WIDGET = "[FAIL-OVER:CUSTOM_WIDGET]"


def _platform_allows_single_page_presubmit(platform: str) -> bool:
    """Return whether this platform may stop at a final pre-submit state."""
    from ghosthands.platforms import get_config_by_name

    return bool(get_config_by_name(platform).single_page_presubmit_allowed)


def build_completion_detection_lines(platform: str) -> list[str]:
    """Return reusable completion-state guidance for prompts and task text."""
    lines = [
        "Classify the current page into exactly one completion state before acting:",
        f"- `{COMPLETION_STATE_ADVANCEABLE}` — a real non-final 'Next', 'Continue', or 'Save & Continue' step still remains. Fill/fix fields and advance. Do NOT call done().",
        f"- `{COMPLETION_STATE_REVIEW}` — read-only review or summary page before final submit. Call done(success=True).",
        f"- `{COMPLETION_STATE_CONFIRMATION}` — thank-you, submitted, or success page. Call done(success=True).",
    ]
    if _platform_allows_single_page_presubmit(platform):
        lines.append(
            f"- `{COMPLETION_STATE_PRESUBMIT_SINGLE_PAGE}` — final submit-like CTA is visible, there is NO real 'Next' / 'Continue' / 'Save & Continue' step left, no visible required/error/invalid markers remain, the submit control is not disabled for missing inputs, and no DomHand-required unresolved fields remain. Call done(success=True) without clicking final submit."
        )
        lines.append(
            "- On this platform, editable fields may still be visible in `presubmit_single_page`. Do NOT start a top-to-bottom re-verification loop once this state is reached."
        )
    else:
        lines.append(
            f"- `{COMPLETION_STATE_PRESUBMIT_SINGLE_PAGE}` — ignore this state on this platform. Keep filling or advancing until you reach `{COMPLETION_STATE_REVIEW}` or `{COMPLETION_STATE_CONFIRMATION}`."
        )
    lines.append(
        "- Once the page is in a terminal-state candidate (`review`, `confirmation`, or allowed `presubmit_single_page`), only do one of two things next: fix one concrete unresolved invalid/required field, or call done(success=True)."
    )
    lines.append(
        "- Use domhand_assess_state before any large scroll, before clicking Next/Continue/Save, and before calling done(). Follow its unresolved field list and scroll_bias instead of doing a full-page reverification loop."
    )
    return lines


def build_domhand_select_failover_lines() -> list[str]:
    """Return reusable failover guidance for domhand_select."""
    return [
        f"If domhand_select returns `{FAIL_OVER_NATIVE_SELECT}`:",
        "- STOP. Do NOT click the native <select> element.",
        "- Call dropdown_options(index=...) to inspect the exact option text/value, then use select_dropdown(index=..., text=...).",
        "- Use the exact text/value string that appears in the dropdown options.",
        f"If domhand_select returns `{FAIL_OVER_CUSTOM_WIDGET}`:",
        "- STOP retrying domhand_select for that field.",
        "- Open the widget manually, type/search if supported, click the option directly, and keep going until the final leaf option visibly sticks.",
    ]


def build_compact_reasoning_lines() -> list[str]:
    """Return concise reasoning rules to reduce late-stage loops and token use."""
    return [
        "Keep memory and next_goal short.",
        "Do NOT restate all completed fields after each step.",
        "When close to completion, mention only the unresolved blocker or the terminal-state decision.",
    ]


def build_completion_detection_text(platform: str) -> str:
    """Return completion guidance as a newline-joined text block."""
    return "\n".join(build_completion_detection_lines(platform))


def build_domhand_select_failover_text() -> str:
    """Return domhand_select failover guidance as a newline-joined text block."""
    return "\n".join(build_domhand_select_failover_lines())


def _format_profile_summary(resume_profile: dict) -> str:
    """Build a concise applicant profile summary from the resume dict.

    The summary is included in the system prompt so the agent knows what
    data is available for form filling without needing an extra LLM call.

    Handles both structured resume format (name, experience, education)
    and flat JSON format (first_name, last_name, email, etc.).
    """
    lines: list[str] = []

    def _entry_current_flag(entry: dict, *keys: str) -> bool:
        for key in keys:
            value = entry.get(key)
            if value is not None:
                return bool(value)
        return False

    def _entry_end_date(entry: dict) -> str:
        return str(entry.get("end_date") or entry.get("graduation_date") or "").strip()

    # ── Name (handle both formats) ───────────────────────────────
    name = resume_profile.get("name")
    if not name:
        first = resume_profile.get("first_name", "")
        last = resume_profile.get("last_name", "")
        if first or last:
            name = f"{first} {last}".strip()
    first_name = str(resume_profile.get("first_name") or "").strip()
    last_name = str(resume_profile.get("last_name") or "").strip()
    if first_name:
        lines.append(f"First name: {first_name}")
    if last_name:
        lines.append(f"Last name: {last_name}")
    if name:
        lines.append(f"Full name: {name}")
    preferred_name = str(resume_profile.get("preferred_name") or "").strip()
    if preferred_name:
        lines.append(f"Preferred name: {preferred_name}")

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
    if address := resume_profile.get("address"):
        lines.append(f"Address: {address}")
    if address2 := resume_profile.get("address_line_2"):
        lines.append(f"Address line 2: {address2}")
    if city := resume_profile.get("city"):
        lines.append(f"City: {city}")
    if state := resume_profile.get("state"):
        lines.append(f"State: {state}")
    if postal := resume_profile.get("postal_code"):
        lines.append(f"Postal code: {postal}")
    if country := resume_profile.get("country"):
        lines.append(f"Country: {country}")
    if phone_type := resume_profile.get("phone_type") or resume_profile.get("phone_device_type"):
        lines.append(f"Phone type: {phone_type}")

    # ── Flat fields common in sample data ────────────────────────
    flat_fields = {
        "age": "Age",
        "gender": "Gender",
        "race": "Race",
        "race_ethnicity": "Race/Ethnicity",
        "years_experience": "Years of experience",
        "veteran_status": "Veteran status",
        "Veteran_status": "Veteran status",
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
            end = _entry_end_date(exp)
            current = _entry_current_flag(exp, "currently_working", "currently_work_here")
            end_type = str(exp.get("end_date_type") or "").strip()
            desc = exp.get("description", "")
            if title or company:
                end_display = "Present" if current else end
                date_range = ""
                if start or end_display:
                    date_range = f"{start or '?'} — {end_display or '?'}"
                    if end_type and not current:
                        date_range += f" ({end_type} end)"
                loc_str = f" ({location})" if location else ""
                line = f"  - {title} at {company}{loc_str}"
                if date_range:
                    line += f" [{date_range}]"
                exp_lines.append(line.strip())
                if desc:
                    # Include the full description (up to 1000 chars) so the
                    # agent has enough context for description/summary fields.
                    exp_lines.append(f"    {desc[:1000]}")
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
            start = str(edu.get("start_date") or "").strip()
            end = _entry_end_date(edu)
            end_type = str(edu.get("end_date_type") or "").strip()
            gpa = edu.get("gpa", "")
            if degree or school:
                degree_str = f"{degree} in {field}" if field else degree
                line = f"  - {degree_str} — {school}"
                if start or end:
                    line += f" [{start or '?'} — {end or '?'}]"
                if end_type:
                    line += f" ({end_type} end)"
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
        "2. domhand_assess_state — Classifies the page into advanceable,",
        "   review, confirmation, or presubmit_single_page and reports",
        "   unresolved required fields plus scroll bias. Use it before",
        "   major scrolling, before clicking Next/Continue/Save, and before",
        "   calling done().",
        "3. domhand_close_popup — Dismisses blocking modals, newsletter",
        "   prompts, promo dialogs, and interstitial overlays before you",
        "   keep filling the form. Prefer this over blind Escape or",
        "   coordinate clicks when the page is blocked by a popup.",
        "4. domhand_select — Selects dropdown/radio/checkbox options by",
        "   label matching.  Use when domhand_fill reports unresolved",
        "   select/radio/checkbox fields.",
        "5. domhand_upload — Uploads the applicant's resume file.  Use",
        "   when you see a file-upload input.",
        "6. Generic browser-use actions (click, input_text, etc.) — Use",
        "   ONLY as a fallback when DomHand actions fail, e.g. due to",
        "   shadow DOM, custom widgets, or iframes that DomHand cannot",
        "   reach.",
        "",
        "After calling domhand_fill, inspect its result to see which fields",
        "were filled and which remain unresolved.  Handle unresolved fields",
        "individually with domhand_select, generic input, or by skipping",
        "ONLY optional fields the applicant profile does not cover.",
        "If an optional field is visible AND the applicant profile provides",
        "a value (address, website, referral source, LinkedIn, etc.), you",
        "SHOULD make a best-effort attempt to fill it only when the",
        "field-to-profile mapping is high confidence.",
        "</domhand_actions>",
        "",
        # ── Hard rules ─────────────────────────────────────────────
        "<hard_rules>",
        "- NEVER click any final 'Submit', 'Submit Application', 'Finish',",
        "  or equivalent CTA.  When you reach a review page or the next",
        "  action would be final submission, call done(success=True) and",
        "  include any extracted confirmation data in the text.",
        "- Leave optional fields empty when the applicant profile does not",
        "  provide a value OR when the field-to-profile mapping is low",
        "  confidence. Do NOT guess on optional fields.",
        "- NEVER invent placeholder personal information such as 'John',",
        "  'Doe', 'John Doe', fake emails, or fake addresses. Use the exact",
        "  applicant identity from the profile. If it is missing, leave the",
        "  field unresolved and continue or report a blocker.",
        "- Every non-consent answer must come from the provided applicant",
        "  profile. Do NOT infer missing applicant data from the page, the",
        "  job description, common defaults, or model assumptions.",
        "- If the applicant profile clearly provides the value and the match",
        "  is high confidence, make a best-effort fill attempt for that",
        "  optional field before advancing.",
        "- If a popup, modal, newsletter prompt, promo interstitial, or",
        "  dimmed overlay is blocking the form, call domhand_close_popup",
        "  FIRST. Do NOT start with blind coordinate clicks while a DOM",
        "  close path is still available.",
        "- After filling a step, look for 'Next', 'Continue', or",
        "  'Save & Continue' buttons and click them to advance.",
        "- Prefer the smallest reversible action that advances the flow.",
        "- On long or single-page forms, keep working near the current",
        "  unresolved section and continue downward. Do NOT jump back to the",
        "  top for reverification unless a specific earlier required field is",
        "  visibly empty or invalid.",
        "- If the page is ambiguous or unstable, wait one step before",
        "  forcing a risky action.",
        "</hard_rules>",
        "",
        "<reasoning_style>",
        *build_compact_reasoning_lines(),
        "</reasoning_style>",
        "",
        # ── Action batching rules ─────────────────────────────────
        "<action_batching>",
        "CRITICAL: Do NOT batch dropdown/select actions with navigation or",
        "other actions.  Dropdown interactions MUST be their own step:",
        "",
        "- After clicking a dropdown option, STOP and observe the result.",
        "  Workday dropdowns often reveal sub-options or sub-categories",
        "  that require a second selection before the field is resolved.",
        "- NEVER combine a dropdown click with 'Save and Continue',",
        "  'Next', or any navigation button in the same step.",
        "- After ANY dropdown interaction (click option, domhand_select),",
        "  wait one step to verify the selection stuck and no sub-options",
        "  appeared before proceeding to the next action.",
        "- For phone country code or phone type dropdowns, if the first",
        "  selection term fails, try close variants before giving up:",
        "  'United States +1', 'United States', '+1', 'USA', 'US',",
        "  'Mobile', and 'Cell' as appropriate.",
        "- For stubborn checkbox/radio/button-style controls: if the same",
        "  intended option still does not stick after 2 tries, STOP blind",
        "  retries. Re-check which option is currently selected, click the",
        "  currently selected option once to clear/reset stale state, then",
        "  click the intended option again and verify the visible state",
        "  changed before moving on.",
        "- For text/date/search fields that visibly contain the value but",
        "  still show validation errors, focus the field and press Enter",
        "  or Tab to commit the value before continuing.",
        "- Safe to batch in one step: multiple text input fills, or",
        "  filling text + clicking a non-dropdown checkbox.",
        "",
        "DATE PICKER STRATEGY:",
        "- Do NOT use domhand_fill for interactive date pickers (calendar",
        "  widgets, month/year selectors, date popovers). domhand_fill",
        "  cannot reliably interact with date picker UIs.",
        "- Instead, use browser-use actions: click the date field, type",
        "  the date string (e.g. '08/2024'), then click the matching",
        "  calendar cell or press Enter/Tab to confirm.",
        "- If a date picker opens a calendar grid, click the correct",
        "  month/year cell directly.",
        "",
        "SEARCH / AUTOCOMPLETE RESILIENCE:",
        "- For ANY searchable field (country, city, location, job title,",
        "  school, company, etc.), if the first search term does not",
        "  produce results, try progressively shorter or alternative",
        "  forms before giving up:",
        "  Example: 'United States of America' → 'United States' → 'US'",
        "  Example: 'University of Southern California' → 'USC' → 'Southern California'",
        "- After typing a search term, ALWAYS wait 2-3 seconds for the",
        "  autocomplete dropdown to appear before concluding it failed.",
        "- If no results appear after waiting, clear the field and try a",
        "  shorter/alternative term.",
        "",
        "STUBBORN CHECKBOX/TOGGLE RECOVERY:",
        "- CRITICAL: If domhand_assess_state reports 0 unresolved required",
        "  fields and 0 visible errors, TRUST THAT RESULT. Do NOT override",
        "  it based on your visual evaluation of checkbox state. The DOM",
        "  state assessment is more reliable than visual inspection.",
        "  If assess_state says advanceable with 0 unresolved — ADVANCE.",
        "- If a checkbox or toggle does not stick after 2 click attempts,",
        "  call domhand_assess_state IMMEDIATELY. If it reports 0 unresolved",
        "  fields, the checkbox DID work — stop retrying and move on.",
        "- If domhand_assess_state still shows unresolved fields after 2",
        "  checkbox click attempts, try these alternatives:",
        "  1. Click the <label> element associated with the checkbox.",
        "  2. Click the <span> text inside the label.",
        "  3. If the checkbox is for 'I currently work here' and it keeps",
        "     reverting, fill the 'To' date field with today's date as a",
        "     workaround and move on.",
        "- NEVER spend more than 4 steps on a single checkbox.",
        "</action_batching>",
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
        "sections.  There are TWO page types with different sequences:",
        "",
        "AUTH PAGES (Create Account / Sign In):",
        "These pages have email, password, and optionally confirm-password",
        "fields.  Do NOT call domhand_fill on auth pages — it would use the",
        "applicant profile email instead of the login credentials.",
        "If credentials were provided for this run, they are available and",
        "you MUST use them here. Do NOT claim 'blocker: login required' on",
        "any page that visibly shows email/password fields when credentials",
        "were provided.",
        "Follow this sequence instead:",
        "  1. Type the credential email into the Email field (input_text).",
        "  2. Type the credential password into the Password field.",
        "  3. If a Confirm Password field is visible, type password again.",
        "  4. Call domhand_check_agreement to check any 'I agree' checkbox.",
        "     This step is REQUIRED — the submit button will silently fail",
        "     if the agreement checkbox is unchecked.",
        "  5. VERIFY the checkbox is checked before proceeding.  If",
        "     domhand_check_agreement reports no checkboxes found, look for",
        "     a checkbox visually and click it manually.",
        "  6. Only AFTER the checkbox is confirmed checked, use",
        "     domhand_click_button to click 'Create Account' or 'Sign In'.",
        "  7. Only report 'blocker: login required' if NO credentials were",
        "     provided and you are stuck on an auth page.",
        "",
        "FORM PAGES (everything else):",
        "  1. Your FIRST action MUST be domhand_fill.  Do NOT use click or",
        "     input_text before trying domhand_fill.",
        "  2. Immediately call domhand_assess_state to understand the active",
        "     section, unresolved required fields, and scroll direction.",
        "  3. Review domhand_fill output for unresolved fields.  Handle them",
        "     with domhand_select or generic actions. Do this for required",
        "     fields, and for optional fields only when the applicant",
        "     profile maps to that field with high confidence.",
        "  4. Check for agreement checkboxes and click any that are unchecked.",
        "  5. Before scrolling away or clicking 'Next' / 'Continue' /",
        "     'Save & Continue', call domhand_assess_state again and follow",
        "     its scroll_bias and completion-state result.",
        "  6. Click 'Next' / 'Continue' / 'Save & Continue' to advance when",
        "     domhand_assess_state still says the page is advanceable.",
        "  7. On the new page, determine if it's an auth page or form page",
        "     and follow the appropriate sequence above.",
        "",
        "CRITICAL: If the page is in `advanceable`, you MUST click",
        "Next / Continue / Save & Continue to advance.",
        "Do NOT call done() while a real non-final advance step remains.",
        "Use the completion-state model below to decide when to advance versus when to stop.",
        "</multi_page_flow>",
        "",
        "<transition_waiting>",
        "If the page looks blank, partially rendered, or still loading after",
        "you click a start/continue button, WAIT 5-10 seconds before doing",
        "anything else.",
        "Never use navigate() to return to the original job URL as recovery",
        "after you have already clicked into the application flow. Waiting",
        "is the default recovery, not restarting.",
        "</transition_waiting>",
        "",
        # ── Repeater sections (Work Experience, Education, etc.) ──
        "<repeater_sections>",
        "Work Experience, Education, and similar sections have 'Add' buttons",
        "to create new entries.  Fill entries ONE AT A TIME with scoped data:",
        "",
        "  1. If the first empty entry is already visible, call domhand_fill",
        "     with heading_boundary set to that entry heading and entry_data set",
        "     to ONLY that single profile entry.",
        "  2. For additional entries, call domhand_expand(section='Work Experience')",
        "     or domhand_expand(section='Education') to reveal the next blank block.",
        "  3. Immediately call domhand_fill with heading_boundary matching the",
        "     new entry heading (for example 'Work Experience 2') and entry_data",
        "     containing ONLY that one experience or education record.",
        "  4. If domhand_expand fails, click the visible 'Add' or '+' button",
        "     yourself, then call scoped domhand_fill for the new heading.",
        "  5. Repeat steps 1-4 for each additional entry in the profile.",
        "",
        "Rules:",
        "- The applicant profile lists how many entries to create.",
        "  If it has 2 work experiences, expand and fill 2 entries.",
        "- Fill each entry BEFORE expanding the next one.",
        "- NEVER call bare domhand_fill for a repeater entry when there are",
        "  already filled entries above it — always scope it with",
        "  heading_boundary and entry_data.",
        "- CRITICAL: Keep entry_data SHORT. Include only structured fields:",
        "  title, company, location, start_date, end_date, currently_work_here,",
        "  school, degree, field_of_study, gpa.",
        "  Do NOT include the full description text in entry_data — domhand_fill",
        "  already has the full profile and will match descriptions automatically.",
        "  Including long descriptions causes the response to exceed token limits.",
        "- Example: domhand_fill(heading_boundary='Work Experience 2',",
        "  entry_data={'title': 'PM', 'company': 'Acme', 'start_date': '2023-01'})",
        "  fills only that second entry instead of the entire page.",
        "- NEVER delete a filled entry.",
        "</repeater_sections>",
        "",
        # ── Dropdown fallback guidance ────────────────────────────
        "<dropdown_fallback>",
        "For searchable or multi-layer dropdowns, selection may take",
        "multiple actions.  Do NOT assume one click is enough.",
        "- After opening the dropdown, type the target value or a shorter",
        "  search term, then WAIT 2-3 seconds for the list to update.",
        "- If a category is selected and a second list appears, keep going",
        "  until you click the final leaf option.  Do NOT navigate away",
        "  after the first click in a multi-layer dropdown.",
        "- Source/referral fields such as 'How Did You Hear About Us?' are",
        "  dropdowns even when they look like text inputs. Typing a value or",
        "  clicking a parent category is NOT enough; you must click the final",
        "  leaf option that matches the applicant profile value.",
        "- Example: if the applicant profile says LinkedIn and the first menu",
        "  shows a parent option like 'Job Board/Social Media Web Site', click",
        "  that parent, WAIT for the next menu, then click the final leaf such",
        "  as 'LinkedIn'. Only the final leaf clears the validation error.",
        "- After clicking an option, verify the field text changed or the",
        "  validation error cleared before clicking Save/Continue.",
        "- Do NOT click a dropdown option and then Save/Continue in the same",
        "  action batch. After any option click, WAIT briefly and re-evaluate",
        "  before the next click.",
        "- NEVER click Save/Continue immediately after the first dropdown",
        "  click when the widget still looks open or the field still looks",
        "  empty.",
        *build_domhand_select_failover_lines(),
        "</dropdown_fallback>",
        "",
        # ── Review / completion detection ─────────────────────────
        "<completion_detection>",
        *build_completion_detection_lines(platform),
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
