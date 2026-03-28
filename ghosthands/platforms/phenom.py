"""Phenom People platform configuration.

Phenom powers career sites for many large employers (RBC, etc.) under
white-label domains.  The pages are identifiable by CDN references to
phenompeople.com, the ``phApp`` JavaScript object, and ``ph-*`` CSS
classes / data attributes.
"""

from ghosthands.platforms.views import PlatformConfig

# ---------------------------------------------------------------------------
# Page types
# ---------------------------------------------------------------------------
PHENOM_PAGE_TYPES: dict[str, str] = {
    "job_listing": (
        "Shows job title, description, qualifications, and an 'Apply Now' "
        "button.  URL typically contains /job/ or /jobs/."
    ),
    "application_form": (
        "Multi-step form.  Sections include 'My Information' (personal "
        "details, address, phone), 'My Experience' (resume upload, work "
        "history, education), and 'Application Questions'.  Each section "
        "has a 'Continue' or 'Save & Continue' button."
    ),
    "login": (
        "Sign-in page with email and password fields.  May offer social "
        "login options."
    ),
    "confirmation": (
        "Text says 'Application submitted' or 'Thank you for applying'."
    ),
    "error": "Form validation error.  Inline error messages next to fields.",
}


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------
PHENOM_GUARDRAILS: list[str] = [
    (
        "Phenom uses a MULTI-STEP form.  Expect sections like "
        "'My Information', 'My Experience', 'Application Questions', etc. "
        "with Continue / Save & Continue buttons between them."
    ),
    (
        "Dropdowns may use native <select> elements that resist programmatic "
        "value setting.  If a dropdown selection is reverted by the page "
        "framework, try clicking the dropdown to open it, then clicking the "
        "desired option directly."
    ),
    (
        "Resume upload: Use the file input element.  Some Phenom sites "
        "reject filenames that are too long — ensure the filename is concise."
    ),
    (
        "NEVER click the final 'Submit' or 'Submit Application' button — "
        "the orchestrator handles submission."
    ),
]


# ---------------------------------------------------------------------------
# Allowed domains
# ---------------------------------------------------------------------------
PHENOM_ALLOWED_DOMAINS: list[str] = [
    "phenom.com",
    "phenompeople.com",
    "cdn.phenompeople.com",
    "pp-cdn.phenompeople.com",
    "assets.phenompeople.com",
    "content-us.phenompeople.com",
]


# ---------------------------------------------------------------------------
# Auth flow
# ---------------------------------------------------------------------------
PHENOM_AUTH_FLOW = (
    "Phenom career sites may require account creation or sign-in before "
    "applying.  Use native email/password login — avoid social SSO."
)


# ---------------------------------------------------------------------------
# Exported config
# ---------------------------------------------------------------------------
PHENOM_CONFIG = PlatformConfig(
    name="phenom",
    display_name="Phenom",
    url_patterns=[
        "phenom.com",
        "phenompeople.com",
    ],
    allowed_domains=PHENOM_ALLOWED_DOMAINS,
    guardrails=PHENOM_GUARDRAILS,
    auth_flow=PHENOM_AUTH_FLOW,
    page_types=PHENOM_PAGE_TYPES,
    shadow_dom_selectors={},
    content_markers=[
        "phenompeople",
        "ph-widget",
        "data-ph-id",
        "phenomtrack",
        "phapp",
        "careerconnectresources",
    ],
    strong_content_markers=[
        "cdn.phenompeople.com",
        "phenomtrack.min.js",
    ],
    content_marker_min_hits=2,
    navigation_hints=[
        "Multi-step form — use 'Continue' or 'Save & Continue' to advance.",
        "Cookie consent banner may appear on first load — dismiss it first.",
    ],
    form_strategy="dom_first",
)
