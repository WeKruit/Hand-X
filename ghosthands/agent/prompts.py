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

# ---------------------------------------------------------------------------
# Generic form-filling strategies (platform-agnostic)
# ---------------------------------------------------------------------------
# These cover ALL ATS patterns. A platform hint is injected separately
# so the agent knows which patterns are most likely, without the system
# prompt being bloated with platform-specific text.

GENERIC_FORM_STRATEGIES = (
    "GENERAL APPROACH:\n"
    "- Stay conservative. Prefer filling editable fields over navigation.\n"
    "- Never press a button whose text implies final submission.\n"
    "- Call done(success=True) on read-only review pages and confirmation pages.\n"
    "\n"
    "APPLY BUTTON PREFERENCE:\n"
    "- If the page shows BOTH an 'Easy Apply' and a longer apply button\n"
    "  ('I'm interested', 'Apply', etc.), ALWAYS prefer 'Easy Apply'.\n"
    "- NEVER choose external apply paths (LinkedIn, Indeed, Google, etc.).\n"
    "\n"
    "SHADOW DOM / CUSTOM WIDGETS:\n"
    "- Some platforms use shadow DOM with custom elements. If domhand_fill\n"
    "  or domhand_select fails on a custom widget (e.g. custom dropdowns,\n"
    "  custom checkboxes), fall back to browser-use click actions.\n"
    "- 'Add' buttons for repeater sections (experience, education) may be\n"
    "  inside shadow roots — try clicking them directly.\n"
    "\n"
    "ACCOUNT CREATION / SIGN-IN:\n"
    "- Do NOT use domhand_fill on auth pages — it uses the applicant profile\n"
    "  instead of login credentials. Use standard browser-use input actions.\n"
    "- Credentials are in GH_EMAIL / GH_PASSWORD environment variables.\n"
    "  Type the email into the Email field, password into Password field.\n"
    "- Pick ONE path (Create Account OR Sign In) and commit.\n"
    "- If a confirm-password field is visible, you are on Create Account.\n"
    "- A standalone 'Sign In' button in a header, nav, or start dialog is NOT\n"
    "  permission to switch auth paths. Treat it as navigation unless the page\n"
    "  is clearly a Sign In form.\n"
    "- NEVER use SSO/social login (Google, LinkedIn, Facebook, Apple).\n"
    "- Always check for agreement checkboxes before clicking Create Account.\n"
    "- If account creation fails, report as blocker.\n"
    "- If a verification code is required, report as blocker.\n"
    "- Sign In: attempt EXACTLY ONCE. If it fails, report as blocker immediately.\n"
    "- If the page shows 'verify your account', 'verification email', 'confirm\n"
    "  your email', 'check your inbox', report as blocker immediately.\n"
    "- NEVER loop between Sign In and Create Account.\n"
    "\n"
    "MULTI-STEP FLOWS:\n"
    "- Some platforms split applications across multiple pages/sections.\n"
    "- After filling all fields on a page, click Next/Continue/Save to advance.\n"
    "- On each new page, call domhand_fill AGAIN as the first action.\n"
    "\n"
    "DATE FIELDS:\n"
    "- Some platforms use segmented date fields (click MM, type digits).\n"
    "- Try typing the full date string first, then Tab to commit.\n"
    "- If a calendar picker opens, press Escape to dismiss it, then Tab.\n"
    "\n"
    "CAPTCHA / BLOCKERS:\n"
    "- Any CAPTCHA, turnstile, or verification wall must be reported as a\n"
    "  blocker via done(success=False, text='blocker: CAPTCHA detected')."
)

# Platform hints — short context-setting lines injected when the platform
# is detected from the URL. These are NOT instructions — just hints about
# what to expect so the agent can apply the generic strategies above.
PLATFORM_HINTS: dict[str, str] = {
    "workday": (
        "Detected platform: Workday. Expect multi-step sections, shadow DOM "
        "with data-automation-id selectors, segmented date fields (MM/DD/YYYY "
        "typed as continuous digits), and 'Select One' dropdown buttons. "
        "If a same-site start dialog offers 'Autofill with Resume' or "
        "'Apply with Resume', prefer that path. "
        "After that step, Workday often shows Create Account as the primary button "
        "and 'Sign In' as a secondary link — for NEW credentials do NOT click Sign In "
        "first to 'reach' auth; scroll or wait if the page is still loading, then "
        "choose Create Account when creating a new applicant login."
    ),
    "greenhouse": (
        "Detected platform: Greenhouse. Expect a same-site start state first on some boards "
        "('Apply for this job' and/or resume upload near the top), followed by a single-page "
        "application form once the apply flow is revealed."
    ),
    "lever": ("Detected platform: Lever. Expect a single long page with all fields visible. Scrolling may be needed."),
    "smartrecruiters": (
        "Detected platform: SmartRecruiters. Expect shadow DOM custom elements, "
        "possible split across apply/login/review steps, and custom dropdown "
        "widgets that require click-to-open + search + click-to-select."
    ),
    "generic": (
        "Platform not recognized. Apply generic strategies. Be conservative and watch for custom widget patterns."
    ),
}

# Legacy compatibility — keep the old dict structure for any external callers
PLATFORM_GUARDRAILS: dict[str, str] = {
    platform: f"{PLATFORM_HINTS.get(platform, '')}\n\n{GENERIC_FORM_STRATEGIES}" for platform in PLATFORM_HINTS
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
    address_raw = resume_profile.get("address")
    if address_raw:
        if isinstance(address_raw, dict):
            addr_str = ", ".join(str(v).strip() for v in address_raw.values() if v and str(v).strip())
            if addr_str:
                lines.append(f"Address: {addr_str}")
        elif isinstance(address_raw, str) and address_raw.strip():
            lines.append(f"Address: {address_raw}")
    if address2 := resume_profile.get("address_line_2"):
        lines.append(f"Address line 2: {address2}")
    if city := resume_profile.get("city"):
        lines.append(f"City: {city}")
    if state := resume_profile.get("state"):
        lines.append(f"State: {state}")
    if postal := resume_profile.get("postal_code"):
        lines.append(f"Postal code: {postal}")
    if county := resume_profile.get("county"):
        lines.append(f"County: {county}")
    if country := resume_profile.get("country"):
        lines.append(f"Country: {country}")
    if phone_type := resume_profile.get("phone_type") or resume_profile.get("phone_device_type"):
        lines.append(f"Phone type: {phone_type}")

    # ── Links / URLs ─────────────────────────────────────────────
    if linkedin := resume_profile.get("linkedin") or resume_profile.get("linkedin_url"):
        lines.append(f"LinkedIn: {linkedin}")
    if portfolio := resume_profile.get("portfolio") or resume_profile.get("portfolio_url"):
        lines.append(f"Portfolio / Website: {portfolio}")
    if github := resume_profile.get("github") or resume_profile.get("github_url"):
        lines.append(f"GitHub: {github}")
    if website := resume_profile.get("website") or resume_profile.get("personal_website"):
        if website != portfolio:
            lines.append(f"Website: {website}")

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
        lang_strs: list[str] = []
        for lang in languages:
            if isinstance(lang, dict):
                language = str(lang.get("language") or "").strip()
                proficiency = str(lang.get("proficiency") or "").strip()
                if not language:
                    continue
                lang_strs.append(f"{language} ({proficiency})" if proficiency else language)
                continue
            if isinstance(lang, str):
                text = lang.strip()
                if text:
                    lang_strs.append(text)
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
    if spoken_languages := resume_profile.get("spoken_languages"):
        lines.append(f"Preferred spoken languages: {spoken_languages}")
    if english_proficiency := resume_profile.get("english_proficiency"):
        lines.append(f"English proficiency: {english_proficiency}")
    if country_of_residence := resume_profile.get("country_of_residence"):
        lines.append(f"Country of residence: {country_of_residence}")
    relocate = resume_profile.get("willing_to_relocate")
    if relocate not in (None, ""):
        if isinstance(relocate, bool):
            relocate_text = "Yes" if relocate else "No"
        else:
            relocate_text = str(relocate)
        lines.append(f"Willing to relocate: {relocate_text}")
    if source := resume_profile.get("how_did_you_hear"):
        lines.append(f"How did you hear about us: {source}")
    if preferred_mode := resume_profile.get("preferred_work_mode"):
        lines.append(f"Preferred work setup: {preferred_mode}")
    if preferred_locations := resume_profile.get("preferred_locations"):
        lines.append(f"Preferred locations: {preferred_locations}")
    if availability_window := resume_profile.get("availability_window"):
        lines.append(f"Availability to start: {availability_window}")
    if notice_period := resume_profile.get("notice_period"):
        lines.append(f"Notice period: {notice_period}")

    # ── Open-ended answers ───────────────────────────────────────
    _already_emitted = {"how_did_you_hear"}
    for key, val in resume_profile.items():
        if (
            (key.startswith("what_") or key.startswith("why_") or key.startswith("how_"))
            and key not in _already_emitted
            and isinstance(val, str)
            and val.strip()
        ):
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
        "4. domhand_interact_control — Resolves one exact non-text",
        "   control by question label and desired value. Use this first",
        "   for stubborn radios, checkboxes, toggles, button groups, and",
        "   exact known select blockers after domhand_fill.",
        "5. domhand_select — Selects dropdown options using",
        "   platform-aware discovery. Use this for complex custom",
        "   dropdown/combobox widgets that domhand_fill could not settle.",
        "6. domhand_record_expected_value — After a raw manual click/input/",
        "   select fallback changes one exact field, record the intended",
        "   field/value here before reassessing the page.",
        "7. domhand_upload — Uploads the applicant's resume file.  Use",
        "   when you see a file-upload input.",
        "8. Generic browser-use actions (click, input_text, etc.) — Use",
        "   ONLY as a fallback when DomHand actions fail, e.g. due to",
        "   shadow DOM, custom widgets, or iframes that DomHand cannot",
        "   reach.",
        "9. Vision/screenshot-based reasoning — Use only as a bounded last",
        "   fallback for the exact stuck field after DOM/manual actions fail.",
        "",
        "After calling domhand_fill, inspect its result to see which fields",
        "were filled and which remain unresolved.  Handle unresolved fields",
        "individually with domhand_interact_control for exact boolean/select",
        "blockers, domhand_select for dropdown widgets, generic input, or by skipping",
        "ONLY optional fields the applicant profile does not cover.",
        "When more than one blocker remains, resolve EXACTLY ONE field at a",
        "time. Finish that field, verify its error cleared, then reassess",
        "before touching the next blocker.",
        "Prefer simpler radio/checkbox blockers before complex searchable",
        "dropdowns when you have multiple unresolved fields.",
        'If domhand_fill or domhand_select returns "domhand_retry_capped" for a field, do',
        "NOT repeat that SAME DomHand strategy on that exact field/value pair in this run.",
        "For radios, checkboxes, and button groups, switch to domhand_interact_control with the exact field_id/field_type",
        "so it can use live exact-target recovery. After any recovery attempt, immediately reassess.",
        "If the same exact field has already failed twice with DOM/manual",
        "actions, take ONE screenshot/vision retry on that field, then",
        "return to DOM/manual actions.",
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
        "  confidence, EXCEPT for low-risk standardized screening fields",
        "  where saved defaults or closest-option matching are explicitly",
        "  allowed below.",
        "- NEVER invent placeholder personal information such as 'John',",
        "  'Doe', 'John Doe', fake emails, or fake addresses. Use the exact",
        "  applicant identity from the profile. If it is missing, leave the",
        "  field unresolved and continue or report a blocker.",
        "- Every substantive applicant answer must come from the provided",
        "  profile. Do NOT infer missing salary, start date, work history,",
        "  education history, essays, or personal identifiers from the page,",
        "  job description, or model assumptions.",
        "- For low-risk standardized screening fields, make a best-effort",
        "  selection before escalating: use saved profile defaults, EEO",
        "  decline answers, referral source defaults, phone type defaults,",
        "  country defaults, work-setup defaults, relocation defaults, and",
        "  language-rubric defaults, then pick the closest matching option",
        "  visible in the UI.",
        "- If the applicant profile clearly provides the value and the match",
        "  is high confidence, make a best-effort fill attempt for that",
        "  optional field before advancing.",
        "- Avoid HITL for standardized dropdown/radio screening fields unless",
        "  the field is truly substantive and no safe profile/default answer",
        "  exists after closest-option matching.",
        "- After any screenshot/vision fallback, immediately return to DOM",
        "  actions on that same field. Do NOT stay in screenshot-driven",
        "  reasoning loops.",
        "- At most ONE screenshot/vision retry per exact field before moving",
        "  back to DOM/manual actions or reporting a blocker.",
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
        "- Workday compensation/salary EXPECTATIONS textareas: if ONLY that field",
        "  stays red 'required' while text is visible, this is usually a commit/state",
        "  issue on one control — NOT a reason to refresh the whole application.",
        "  Call domhand_assess_state, then domhand_fill AGAIN with target_section",
        "  set to current_section and focus_fields set to that ONE label (exact",
        "  unresolved name). Avoid full-page refresh as the first recovery.",
        "- Safe to batch in one step: multiple text input fills, or",
        "  filling text + clicking a non-dropdown checkbox.",
        "",
        "DATE PICKER STRATEGY:",
        "- Do NOT use domhand_fill for interactive date pickers (calendar",
        "  widgets, month/year selectors, date popovers). domhand_fill",
        "  cannot reliably interact with date picker UIs.",
        "- Prefer clicking a visible date icon, calendar button, or the",
        "  picker affordance FIRST. If the picker opens, click the actual",
        "  month/year/day cell directly.",
        "- Only type the date string when there is no usable picker",
        "  affordance or the picker interaction has already failed.",
        "- After any typed date input, blur or Tab away so Workday",
        "  re-validates the field before moving on.",
        "",
        "READBACK VS ASSESS (custom dropdowns / react-select):",
        "- domhand_assess_state uses DOM readback; custom selects often LOOK filled while",
        "  readback stays empty (especially Greenhouse). Do not treat a long list of",
        "  unresolved custom selects with empty current_value and NO visible_errors as",
        "  proof the prefill failed — after domhand_fill + resume upload, prefer ONE",
        "  reassess or generic input/click/select_dropdown on the specific visible gap.",
        "- After TWO failed DomHand retries on the SAME field label, stop calling",
        "  domhand_fill / domhand_select / domhand_interact_control for it; use standard",
        "  browser-use tools instead.",
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
        "- CRITICAL: Use domhand_assess_state.advance_allowed as the page",
        "  gate. Do NOT advance just because a checkbox looks right or a",
        "  Next/Continue button is visible.",
        "- If a checkbox or toggle does not stick after 2 click attempts,",
        "  call domhand_assess_state IMMEDIATELY. If it reports",
        "  advance_allowed=true, stop retrying and move on.",
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
        "- Missing structured applicant data for GPA, field of study, expected",
        "  vs actual education dates, or language rubric fields is a user-data",
        "  gap. Do NOT guess those answers.",
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
        "fields. Do NOT call domhand_fill on auth pages — it would use the",
        "applicant profile email instead of the login credentials.",
        "Use standard browser-use input actions to type credentials.",
        "If credentials were provided for this run, they are available and",
        "you MUST use them here. Do NOT claim 'blocker: login required' on",
        "any page that visibly shows email/password fields when credentials",
        "were provided.",
        "",
        "IMPORTANT AUTH FLOW RULES:",
        "- If the task says STORED CREDENTIALS: go to Sign In ONLY.",
        "  Do NOT create a new account. If sign-in fails, report blocker.",
        "- If the task says NEW CREDENTIALS: go to Create Account FIRST.",
        "  Do NOT click any Sign In control unless the page explicitly says",
        "  the account already exists after a failed Create Account attempt.",
        "  After creation, sign in. Do NOT loop — max 1 attempt each.",
        "- If the task says ACCOUNT NEEDS VERIFICATION: do NOT attempt auth.",
        "  Report the verification blocker immediately.",
        "- If the task says CREDENTIALS NEED REPAIR: do NOT attempt auth.",
        "  Report the repair blocker immediately.",
        "- NEVER loop between Sign In ↔ Create Account more than once.",
        "  If both fail, report done(success=False) immediately.",
        "",
        "Follow this sequence on auth pages:",
        "  1. Type the credential email into the Email field (input_text).",
        "  2. Type the credential password into the Password field.",
        "  3. If a Confirm Password field is visible, type password again.",
        "  4. Call domhand_check_agreement to check any 'I agree' checkbox.",
        "     This step is REQUIRED — the submit button will silently fail",
        "     if the agreement checkbox is unchecked.",
        "  5. VERIFY the checkbox is checked before proceeding.  If",
        "     domhand_check_agreement reports no checkboxes found, look for",
        "     a checkbox visually and click it manually.",
        "  6. Only AFTER the checkbox is confirmed checked, submit",
        "     'Create Account' or 'Sign In' using domhand_click_button.",
        "  7. Wait 3 seconds before deciding the outcome.",
        "  8. Only report 'blocker: login required' if NO credentials were",
        "     provided and you are stuck on an auth page.",
        "",
        "FORM PAGES (everything else):",
        "  1. Your FIRST action MUST be domhand_fill.  Do NOT use click or",
        "     input_text before trying domhand_fill.",
        "  2. Immediately call domhand_assess_state to understand the active",
        "     section, unresolved required fields, and scroll direction.",
        "  3. Review domhand_fill output for unresolved fields. If",
        "     domhand_assess_state reports unresolved_required_fields, call",
        "     domhand_fill AGAIN with target_section set to the reported",
        "     current_section and focus_fields set to ONE exact unresolved",
        "     label at a time before falling back to manual clicks. If the",
        "     unresolved field belongs to a repeater entry, preserve the",
        "     same heading_boundary instead of broadening to the whole",
        "     section. Reassess after each single-field retry before moving",
        "     to the next blocker.",
        "  4. Only after that targeted domhand_fill attempt should you use",
        "     domhand_interact_control for exact radio/checkbox/toggle/button",
        "     blockers, domhand_select for dropdown widgets,",
        "     dropdown_options/select_dropdown, or generic DOM actions. Do",
        "     this one blocker at a time for required",
        "     fields, and for optional fields only when the applicant",
        "     profile maps to that field with high confidence.",
        "  4c. After EVERY blocker-level domhand_interact_control or",
        "      domhand_select, IMMEDIATELY call domhand_assess_state before",
        "      doing any unrelated action. After EVERY targeted manual",
        "      click/input/select recovery, FIRST call",
        "      domhand_record_expected_value for that exact field/value,",
        "      THEN call domhand_assess_state. Do not assume the page context",
        "      updated correctly until reassessment confirms it.",
        "  4a. Do NOT jump straight to vision/screenshot fallback while",
        "      DOM/manual takeover options are still available.",
        "  4b. If the same exact blocker still fails after two DOM/manual",
        "      attempts, take ONE screenshot/vision retry for that blocker,",
        "      then go back to DOM/manual actions.",
        "  5. Check for agreement checkboxes and click any that are unchecked.",
        "  6. Before scrolling away or clicking 'Next' / 'Continue' /",
        "     'Save & Continue', call domhand_assess_state again and follow",
        "     its scroll_bias, unresolved fields, and advance_allowed result.",
        "  7. Click 'Next' / 'Continue' / 'Save & Continue' ONLY when",
        "     domhand_assess_state says advance_allowed=true.",
        "  8. On the new page, determine if it's an auth page or form page",
        "     and follow the appropriate sequence above.",
        "",
        "CRITICAL: If the page is in `advanceable` but advance_allowed=false,",
        "you MUST NOT click Next / Continue / Save & Continue yet.",
        "Do NOT call done() while a real non-final advance step remains.",
        "Use the completion-state model below to decide when to advance versus when to stop.",
        "</multi_page_flow>",
        "",
        "<transition_waiting>",
        "If the page looks blank, partially rendered, or still loading after",
        "you click a start/continue button, use SHORT waits first (2-3 seconds).",
        "If the page is still blank/loading after two short waits and there are",
        "still no form elements, call refresh() ONCE, then wait 2-3 seconds and",
        "reassess the current page.",
        "While the page is settling, do NOT click header/nav controls such as",
        "'Sign In', 'Careers Home', or other fallback navigation buttons.",
        "A blank/loading transition is not evidence that the flow requires",
        "switching from Create Account to Sign In.",
        "Refresh is allowed only as a blank-page recovery step; it is not",
        "permission to restart the auth flow or click a different auth path.",
        "Never use navigate() to return to the original job URL as recovery",
        "after you have already clicked into the application flow. Waiting",
        "and one refresh are the default recovery, not restarting.",
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
        "- Source/referral fields ('How Did You Hear About Us?') are low-",
        "  priority — any reasonable answer is fine. Do NOT loop on this field.",
        "  These are often multi-layer dropdowns: click a parent category,",
        "  WAIT for sub-options, then click a leaf. Only the leaf clears",
        "  the validation error.",
        "- ACCEPTABLE ANSWERS (in preference order): 'LinkedIn', 'Job Board',",
        "  'Social Media', 'Company Careers Page', 'Website', 'Online',",
        "  'Internet', 'Other'. If the first parent you try (e.g. 'Social",
        "  Media') has no matching leaf, pick ANY leaf under it — or press",
        "  Escape and try the next parent. After TWO parent attempts without",
        "  a match, just select 'Other' or whatever parent accepts as final.",
        "  Do NOT spend more than 3 actions total on this field.",
        "- After clicking an option, verify the field text changed or the",
        "  validation error cleared before clicking Save/Continue.",
        "- Do NOT resolve a source/referral dropdown in the same recovery",
        "  batch as a radio button or any other unrelated field.",
        "- If the same exact dropdown or radio field still fails after two",
        "  DOM/manual attempts, take ONE screenshot/vision retry on that",
        "  field before continuing.",
        "- Do NOT click a dropdown option and then Save/Continue in the same",
        "  action batch. After any option click, WAIT briefly and re-evaluate",
        "  before the next click.",
        "- NEVER click Save/Continue immediately after the first dropdown",
        "  click when the widget still looks open or the field still looks",
        "  empty.",
        "- If domhand_fill plus domhand_interact_control/domhand_select both fail, try the existing DOM",
        "  interaction tools first: dropdown_options, select_dropdown, click,",
        "  input_text, Enter, Tab, and focused retry actions.",
        "- Only use vision/screenshot as a last automated fallback after the",
        "  DOM/manual path has failed for that exact field.",
        *build_domhand_select_failover_lines(),
        "</dropdown_fallback>",
        "",
        # ── Review / completion detection ─────────────────────────
        "<completion_detection>",
        *build_completion_detection_lines(platform),
        "</completion_detection>",
        "",
        # ── Platform-agnostic form strategies ─────────────────────
        "<form_strategies>",
        GENERIC_FORM_STRATEGIES,
        "</form_strategies>",
        "",
        # ── Platform hint (injected from URL detection) ───────────
        "<platform_hint>",
        PLATFORM_HINTS.get(platform, PLATFORM_HINTS["generic"]),
        "</platform_hint>",
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
    credential_source: str = "",
    credential_intent: str = "",
    platform: str = "generic",
) -> str:
    """Build the task prompt for the agent."""
    task = (
        f"Go to {job_url} and fill out the job application form completely.\n"
        "\n"
        "CRITICAL -- Action Order:\n"
        "1. After navigating to the page, LOOK for an 'Easy Apply' section or\n"
        "   a resume upload area at the top of the form. If present, upload the\n"
        f"   resume FIRST using domhand_upload with path: {resume_path}\n"
        "   Easy Apply with resume upload is ALWAYS the preferred path — it\n"
        "   auto-fills many fields and shortens the application.\n"
        "2. Then call domhand_fill to fill remaining visible form fields.\n"
        "3. After domhand_fill completes, review its output to see which fields were filled and which failed.\n"
        "4. Immediately call domhand_assess_state. If unresolved fields remain, resolve them ONE FIELD AT A TIME: "
        "call domhand_fill again with target_section=current_section and focus_fields set to a single exact "
        "unresolved label. Preserve heading_boundary when you are inside a repeater entry, and reassess after "
        "each single-field retry before touching the next blocker. If the latest domhand_assess_state no longer "
        "lists a field as unresolved/mismatched/unverified, do NOT retry that field again on the same page.\n"
        "5. For fields still unresolved after the targeted domhand_fill retry, use DomHand control tools first: "
        "domhand_interact_control for radios/checkboxes/toggles/button groups and domhand_select for dropdowns. "
        "When domhand_assess_state gives you a field_id/field_type for the blocker, pass that exact field_id to "
        "domhand_interact_control or domhand_record_expected_value instead of relying on label-only matching. "
        "After EACH blocker-level DomHand action, immediately call domhand_assess_state to refresh the current context. "
        "After EACH targeted manual recovery action, first make sure the field visibly shows the value and its validation has cleared, then call domhand_record_expected_value for that exact field/value, then immediately call domhand_assess_state. "
        "Only then use dropdown_options/select_dropdown, click, input_text, Enter, Tab, scroll, and focus actions. "
        "Keep these manual recoveries to ONE FIELD AT A TIME. Do NOT combine a referral/source widget with a radio "
        "button or another blocker in the same action batch.\n"
        '5b. If domhand_fill or domhand_select returns "domhand_retry_capped", stop repeating that SAME DomHand '
        'strategy on that exact field/value pair for the rest of the run. For radios/checkboxes/button groups, '
        "switch to domhand_interact_control with the exact field_id/field_type so it can use live exact-target recovery, then reassess.\n"
        "6. If the same exact field still fails after two DOM/manual attempts, take ONE screenshot/vision retry on "
        "that blocker. Do not use screenshot earlier, and do not keep using screenshot repeatedly.\n"
        "7. After that screenshot/vision retry, go back to concrete DOM/manual actions instead of staying in visual "
        "reasoning.\n"
        f"8. For other file uploads, use domhand_upload or upload_file action with path: {resume_path}\n"
        "9. After all fields on the current page are filled, click Next/Continue/Save ONLY when domhand_assess_state reports advance_allowed=true.\n"
        "10. On each new page, repeat from step 1 (check for Easy Apply / resume upload first).\n"
        "11. Prefer short waits: use a short poll loop with wait(seconds=1) up to 3 times for auth transitions, "
        "resume-processing, and SPA loading. Reassess after each short wait instead of doing one long blind wait.\n"
        "12. If the page is still blank/loading after the short poll loop and there are still no form elements, "
        "navigate back to the original job URL ONCE when the current page is blank or blocked; otherwise call "
        "refresh() ONCE on the current allowed page, then wait 2-3 seconds and reassess. Do NOT click Sign In/"
        "Create Account just because the page is blank.\n"
        "13. For salary/compensation fields, ONLY use the exact saved profile answer already provided by DomHand "
        "or recovered from the profile/answer-bank context. NEVER improvise with generic text like 'Competitive', "
        "'Negotiable', or 'Flexible'. If a deterministic answer is not available, use DomHand's final best-effort "
        "answer and leave it for review in the final report instead of stopping for HITL.\n"
        "\n"
        "Other rules:\n"
    )
    if sensitive_data:
        # Shared verification-detection rule injected for all credential modes.
        _verification_rule = (
            "- VERIFICATION DETECTION: If after clicking Sign In or Create Account the page shows "
            "ANY text like 'Verify your account', 'verification email', 'confirm your email', "
            "'check your inbox', 'check your spam', 'verify your email address', or any banner "
            "asking the user to verify/confirm via email, IMMEDIATELY report "
            "done(success=False, text='blocker: email verification required — user must verify email then retry'). "
            "Do NOT attempt to sign in again. Do NOT refresh. Do NOT wait.\n"
        )
        if credential_source == "stored":
            task += (
                "- STORED CREDENTIALS: We have a saved account for this platform from a previous application. "
                "On auth pages, go DIRECTLY to Sign In — do NOT click Create Account. "
                "Fill email + password using browser-use input actions (NOT domhand_fill), "
                "then submit Sign In using domhand_click_button.\n"
                "  After clicking Sign In, wait 3 seconds before deciding what happened.\n"
                "  When sign-in succeeds, include EXACTLY `AUTH_RESULT=STORED_SIGN_IN_SUCCESS` in your memory or evaluation.\n"
                "  CRITICAL AUTH RULES:\n"
                "  - Sign In: attempt EXACTLY ONCE. If it fails for ANY reason (error message, wrong password, "
                "account not found, page reload, etc.), immediately report "
                "done(success=False, text='blocker: sign-in failed — [describe the error]'). Do NOT retry.\n"
                "  - NEVER attempt to create a new account with stored credentials.\n"
            )
            task += _verification_rule
        elif credential_source == "generated":
            task += (
                "- NEW CREDENTIALS: This is a first-time application on this platform — no existing account. "
                "On auth pages, go DIRECTLY to Create Account (not Sign In). "
                "Fill email + password + confirm password using browser-use input actions (NOT domhand_fill), "
                "check agreement using domhand_check_agreement, then submit Create Account using domhand_click_button.\n"
                "  After clicking Create Account, use a short poll loop: wait 1 second, inspect the page, and repeat "
                "up to 3 times before deciding the outcome.\n"
                "  AUTH OUTCOME MARKERS:\n"
                "  - If the account appears created and you move past the auth wall, include EXACTLY "
                "`AUTH_RESULT=ACCOUNT_CREATED_ACTIVE` in your memory or evaluation.\n"
                "  - If Create Account submission lands on the native Sign In page (email + password, no confirm-password field), "
                "treat that as the expected post-create step on Workday. This is NOT a failure. "
                "Use the SAME email/password to sign in ONCE. Do NOT click Create Account again.\n"
                "  - If Create Account leads to email verification / check inbox / confirm your email, include EXACTLY "
                "`AUTH_RESULT=ACCOUNT_CREATED_PENDING_VERIFICATION` in your memory or evaluation BEFORE reporting the blocker.\n"
                "  - If Create Account fails before the account exists, include EXACTLY "
                "`AUTH_RESULT=ACCOUNT_CREATE_FAILED` in your memory or evaluation.\n"
                "  - If the site says the account already exists, include EXACTLY "
                "`AUTH_RESULT=ACCOUNT_ALREADY_EXISTS` in your memory or evaluation.\n"
                "  CRITICAL AUTH RULES:\n"
                "  - Create Account: attempt EXACTLY ONCE. If it fails, report blocker immediately.\n"
                "  - NEVER click Sign In proactively on a blank/loading page or because a header/nav Sign In button is visible.\n"
                "  - Sign In is allowed ONLY after Create Account fails with an explicit 'account already exists' signal.\n"
                "  - If Create Account fails with 'account already exists', switch to Sign In ONCE.\n"
                "  - After clicking Sign In, use the standard wait action for 3 seconds before deciding the outcome.\n"
                "  - Sign In after account creation: attempt EXACTLY ONCE. If it fails, immediately report "
                "done(success=False, text='blocker: account created but sign-in failed — may need email verification').\n"
                "  - NEVER go back to Create Account after attempting Sign In.\n"
                "  - NEVER go back to Sign In after a failed Sign In attempt. One attempt only.\n"
                "  - NEVER loop between Sign In and Create Account. One direction only.\n"
            )
            task += _verification_rule
        elif credential_source == "await_verification":
            task += (
                "- ACCOUNT NEEDS VERIFICATION: An account was previously created on this platform "
                "but email verification has not been completed yet. "
                "Do NOT attempt to sign in or create a new account. "
                "Report immediately: done(success=False, text='blocker: account needs email verification — "
                "user must verify their email before this application can proceed'). "
                "Do NOT retry or attempt any auth actions.\n"
            )
        elif credential_source == "repair_credentials":
            task += (
                "- CREDENTIALS NEED REPAIR: The stored credentials for this platform are known to be "
                "broken or invalid. Do NOT attempt to sign in with them. "
                "Report immediately: done(success=False, text='blocker: stored credentials are invalid — "
                "user must fix or reset their account credentials before this application can proceed'). "
                "Do NOT retry, create a new account, or attempt any auth actions.\n"
            )
        elif credential_source == "user" and credential_intent == "existing_account":
            task += (
                "- USER-PROVIDED EXISTING ACCOUNT: The user supplied credentials for an account that already exists. "
                "On auth pages, go DIRECTLY to Sign In — do NOT click Create Account. "
                "Fill email + password using browser-use input actions (NOT domhand_fill), then submit Sign In using domhand_click_button.\n"
                "  After clicking Sign In, wait 3 seconds before deciding what happened.\n"
                "  CRITICAL AUTH RULES:\n"
                "  - Sign In: attempt EXACTLY ONCE. If it fails for ANY reason, immediately report "
                "done(success=False, text='blocker: sign-in failed — [describe the error]'). Do NOT retry.\n"
                "  - NEVER attempt to create a new account with these credentials.\n"
            )
            task += _verification_rule
        elif credential_source == "user" and credential_intent == "create_account":
            task += (
                "- USER-PROVIDED NEW ACCOUNT: The user supplied credentials that must be used to create the account on this platform. "
                "On auth pages, go DIRECTLY to Create Account first — do NOT click Sign In first. "
                "Fill email + password + confirm password using browser-use input actions (NOT domhand_fill), "
                "check agreement using domhand_check_agreement, then submit Create Account using domhand_click_button.\n"
                "  After clicking Create Account, wait 3 seconds before deciding the outcome.\n"
                "  AUTH OUTCOME MARKERS:\n"
                "  - If the account appears created and you move past the auth wall, include EXACTLY "
                "`AUTH_RESULT=ACCOUNT_CREATED_ACTIVE` in your memory or evaluation.\n"
                "  - If Create Account leads to email verification / check inbox / confirm your email, include EXACTLY "
                "`AUTH_RESULT=ACCOUNT_CREATED_PENDING_VERIFICATION` in your memory or evaluation BEFORE reporting the blocker.\n"
                "  - If Create Account fails before the account exists, include EXACTLY "
                "`AUTH_RESULT=ACCOUNT_CREATE_FAILED` in your memory or evaluation.\n"
                "  - If the site says the account already exists, include EXACTLY "
                "`AUTH_RESULT=ACCOUNT_ALREADY_EXISTS` in your memory or evaluation.\n"
                "  CRITICAL AUTH RULES:\n"
                "  - Create Account: attempt EXACTLY ONCE. If it fails, report blocker immediately.\n"
                "  - NEVER click Sign In proactively from the start dialog, header, or a blank/loading auth transition.\n"
                "  - If the same Create Account form is still on screen after the one allowed submit, inspect inline errors "
                "and report the blocker from that page. Do NOT click Create Account again.\n"
                "  - If Create Account submission lands on the native Sign In page (email + password, no confirm-password field), "
                "treat that as the expected next step. Use the SAME email/password to sign in ONCE. Do NOT click Create Account again.\n"
                "  - Sign In after account creation: attempt EXACTLY ONCE. If it fails, immediately report "
                "done(success=False, text='blocker: account created but sign-in failed — [describe the error]').\n"
                "  - NEVER go back to Create Account after attempting Sign In.\n"
                "  - NEVER loop between Sign In and Create Account.\n"
            )
            task += _verification_rule
        else:
            task += (
                "- Use the provided credentials to log in or create an account if needed. "
                "Fill email + password (+ confirm password if visible) on auth pages "
                "using browser-use input actions (NOT domhand_fill), then submit the auth form using "
                "domhand_click_button.\n"
                "  CRITICAL AUTH RULES:\n"
                "  - Sign In: attempt EXACTLY ONCE. If it fails, immediately report "
                "done(success=False, text='blocker: sign-in failed — [describe the error]'). Do NOT retry.\n"
            )
            task += _verification_rule
    else:
        task += "- If a login wall appears, report it as a blocker.\n"
    task += (
        "- Do NOT click the final Submit button. Stop at the review page and use the done action.\n"
        "- If anything pops up blocking the form, close it and continue.\n"
    )
    return task
