"""SmartRecruiters platform configuration.

SmartRecruiters uses a multi-step form hosted on jobs.smartrecruiters.com.
Key distinction: repeater sections (work experience, education) must be
expanded BEFORE filling — the form hides additional entries behind
"Add Another" buttons.
"""

from ghosthands.platforms.views import PlatformConfig

# ---------------------------------------------------------------------------
# Page types
# ---------------------------------------------------------------------------
SMARTRECRUITERS_PAGE_TYPES: dict[str, str] = {
    "job_listing": (
        "URL pattern: jobs.smartrecruiters.com/{company}/{job_id}. "
        "Shows job title, description, location, department. "
        "Has an 'Apply Now' or 'Apply' button."
    ),
    "login": (
        "Account login page. May show 'Sign in with email' or social login "
        "options (Google, Facebook, LinkedIn). Has email/password fields."
    ),
    "account_creation": (
        "Registration page with email, password, and confirm password fields. "
        "May appear if applying for the first time."
    ),
    "personal_info": ("Basic information step: name, email, phone, location/address. Standard text inputs."),
    "experience": (
        "Work experience and education step. Has repeater sections — "
        "multiple entries for job history and education. "
        "'Add Another' buttons to expand sections."
    ),
    "resume_upload": (
        "Resume/CV upload step. File upload input or drag-and-drop zone. May also have LinkedIn profile import option."
    ),
    "questions": (
        "Screening questions step. Radio buttons, dropdowns, text inputs for eligibility, work authorization, etc."
    ),
    "voluntary_disclosure": (
        "EEO / self-identification step. Gender, race/ethnicity, veteran status, disability status dropdowns."
    ),
    "review": (
        "Application summary / review step. Read-only display of all entered information. Submit button present."
    ),
    "confirmation": ("'Application submitted' or 'Thank you for applying' message."),
    "error": "Error message or validation failure.",
}


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------
SMARTRECRUITERS_GUARDRAILS: list[str] = [
    (
        "REPEATER EXPANSION BEFORE FILL: SmartRecruiters uses repeater sections "
        "for work experience and education. Before filling fields, check if "
        "'Add Another' buttons exist and click them to expand all required "
        "entry blocks. Each expanded block has its own set of fields."
    ),
    (
        "SmartRecruiters forms may be multi-step or single-page depending on "
        "the company configuration. Detect 'Next' / 'Continue' buttons for "
        "multi-step, or look for section anchors in single-page mode."
    ),
    (
        "On single-page SmartRecruiters flows, stop with done(success=True) "
        "when the only remaining CTA is the final submit button, there is no "
        "'Next' / 'Continue' / 'Save & Continue' action left, and no visible "
        "required/error/invalid fields remain."
    ),
    (
        "Login: SmartRecruiters supports social login (Google, LinkedIn) and "
        "email/password. Prefer email/password authentication."
    ),
    (
        "Resume parsing: After uploading a resume, SmartRecruiters may "
        "auto-fill some fields. Wait for parsing to complete before "
        "overwriting auto-filled values."
    ),
    (
        "Dropdowns in SmartRecruiters are typically native <select> elements "
        "or standard ARIA comboboxes. Use DOM-first filling."
    ),
    (
        "Required fields are marked with red asterisks (*) or highlighted "
        "borders. Ensure all required fields are filled before proceeding."
    ),
    ("NEVER click 'Submit Application' — the orchestrator handles final submission after verification."),
]


# ---------------------------------------------------------------------------
# Allowed domains
# ---------------------------------------------------------------------------
SMARTRECRUITERS_ALLOWED_DOMAINS: list[str] = [
    "smartrecruiters.com",
    "jobs.smartrecruiters.com",
    "api.smartrecruiters.com",
    "www.smartrecruiters.com",
]


# ---------------------------------------------------------------------------
# Auth flow
# ---------------------------------------------------------------------------
SMARTRECRUITERS_AUTH_FLOW = (
    "SmartRecruiters may require account creation on first application. "
    "Supports email/password registration and social login (Google, LinkedIn, "
    "Facebook). Some company instances allow guest applications without login. "
    "After login/registration, the application form loads."
)


# ---------------------------------------------------------------------------
# Exported config
# ---------------------------------------------------------------------------
SMARTRECRUITERS_CONFIG = PlatformConfig(
    name="smartrecruiters",
    display_name="SmartRecruiters",
    url_patterns=[
        "jobs.smartrecruiters.com",
        "smartrecruiters.com",
    ],
    allowed_domains=SMARTRECRUITERS_ALLOWED_DOMAINS,
    guardrails=SMARTRECRUITERS_GUARDRAILS,
    auth_flow=SMARTRECRUITERS_AUTH_FLOW,
    page_types=SMARTRECRUITERS_PAGE_TYPES,
    shadow_dom_selectors={
        "application_form": ".application-form, #application",
        "submit_button": 'button[type="submit"], .btn-submit',
        "resume_input": 'input[type="file"]',
        "add_another": 'button:has-text("Add Another"), .add-another-btn',
        "repeater_section": ".repeater-section, .experience-section",
        "next_button": 'button:has-text("Next"), button:has-text("Continue")',
    },
    content_markers=[
        "smartrecruiters",
        "add another",
        "c-spl-select-field",
        "ready to apply",
        "review your application",
    ],
    strong_content_markers=[
        "c-spl-select-field",
    ],
    content_marker_min_hits=2,
    navigation_hints=[
        ("May be multi-step or single-page depending on company config. Detect step indicators / progress bar."),
        (
            "If a single-page form only has a final submit CTA left and no "
            "visible validation blockers remain, stop with done(success=True)."
        ),
        (
            "Experience sections use repeater pattern — click 'Add Another' "
            "to create additional entry blocks before filling."
        ),
        "Wait for resume parsing to complete after upload before filling fields.",
    ],
    form_strategy="repeater_expand",
    automation_id_map={
        "application_form": ".application-form, #application",
        "submit_button": 'button[type="submit"], .btn-submit',
        "add_another": 'button:has-text("Add Another"), .add-another-btn',
        "repeater_section": ".repeater-section, .experience-section",
    },
    fill_overrides={
        "select": "searchable_dropdown",
    },
    single_page_presubmit_allowed=True,
)
