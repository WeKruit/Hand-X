"""Greenhouse platform configuration.

Greenhouse uses a single-page application form hosted on boards.greenhouse.io.
Most fields are standard HTML inputs on one long page.
"""

from ghosthands.platforms.views import PlatformConfig

# ---------------------------------------------------------------------------
# Page types
# ---------------------------------------------------------------------------
GREENHOUSE_PAGE_TYPES: dict[str, str] = {
    "job_listing": (
        "URL contains /jobs/. Shows job title, description, department, location. "
        "Has an 'Apply for this job' button at the top or bottom."
    ),
    "application_form": (
        "Single-page form after clicking Apply. Sections: "
        "Personal Information, Resume/Cover Letter, Custom Questions, "
        "Voluntary Self-Identification (EEO). All on one scrollable page."
    ),
    "login": ("Some Greenhouse instances require login. URL contains /users/sign_in or similar."),
    "confirmation": ("Text says 'Application submitted' or 'Thank you for applying'. No form fields remaining."),
    "error": "Error message visible. Form validation failed.",
}


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------
GREENHOUSE_GUARDRAILS: list[str] = [
    (
        "Greenhouse uses a SINGLE-PAGE form. All sections (personal info, "
        "resume, questions, EEO) are on one scrollable page. Do not look for "
        "'Next' or multi-page navigation."
    ),
    (
        "Do NOT click the final 'Submit Application' button until all sections "
        "are filled and validated. Greenhouse validates on submit and shows "
        "inline errors — there is no multi-page checkpoint."
    ),
    (
        "Resume upload: Greenhouse accepts drag-and-drop or file input. "
        "Use the file input element directly via Playwright setInputFiles."
    ),
    (
        "Custom questions appear below the resume section. They may include "
        "text inputs, dropdowns, checkboxes, and radio buttons. Fill all "
        "required fields (marked with *)."
    ),
    (
        "EEO / Voluntary Self-Identification section is at the bottom. "
        "Fields: Gender, Race/Ethnicity, Veteran Status. These are optional "
        "but should be filled if the user provided answers."
    ),
    (
        "Some Greenhouse forms have EEOC disability questions on a separate "
        "sub-section. Check for 'Voluntary Self-Identification of Disability' "
        "below the main EEO section."
    ),
    (
        "NEVER NAVIGATE: Do not click Submit Application. The orchestrator "
        "handles final submission after verifying all fields."
    ),
]


# ---------------------------------------------------------------------------
# Allowed domains
# ---------------------------------------------------------------------------
GREENHOUSE_ALLOWED_DOMAINS: list[str] = [
    "greenhouse.io",
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "api.greenhouse.io",
]


# ---------------------------------------------------------------------------
# Auth flow
# ---------------------------------------------------------------------------
GREENHOUSE_AUTH_FLOW = (
    "Most Greenhouse job boards do not require authentication — the application "
    "form is publicly accessible after clicking Apply. Some enterprise instances "
    "may require login via /users/sign_in. No Google SSO typically needed."
)


# ---------------------------------------------------------------------------
# Exported config
# ---------------------------------------------------------------------------
GREENHOUSE_CONFIG = PlatformConfig(
    name="greenhouse",
    display_name="Greenhouse",
    url_patterns=[
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "greenhouse.io",
    ],
    allowed_domains=GREENHOUSE_ALLOWED_DOMAINS,
    guardrails=GREENHOUSE_GUARDRAILS,
    auth_flow=GREENHOUSE_AUTH_FLOW,
    page_types=GREENHOUSE_PAGE_TYPES,
    shadow_dom_selectors={
        "application_form": "#application_form",
        "submit_button": '#submit_app, button[type="submit"]',
        "resume_input": 'input[type="file"][name*="resume"]',
        "cover_letter_input": 'input[type="file"][name*="cover_letter"]',
        "custom_question": "[data-question-id]",
        "eeo_section": "#eeoc_fields, .eeoc-fields",
    },
    content_markers=[
        "gh_jid",
        "application_form",
        "eeoc_fields",
        "submit application",
        "greenhouse",
    ],
    strong_content_markers=[
        "eeoc_fields",
    ],
    content_marker_min_hits=2,
    navigation_hints=[
        "Single-page form — scroll down to reveal all sections.",
        "Submit button is at the very bottom of the page.",
        "Validation errors appear inline next to the field.",
    ],
    form_strategy="dom_first",
    single_page_presubmit_allowed=True,
)
