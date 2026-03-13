"""Lever platform configuration.

Lever uses a simple, clean single-page application form at jobs.lever.co.
Forms are straightforward with standard HTML inputs. Fill-first strategy
preferred because of the simple layout.
"""

from ghosthands.platforms.views import PlatformConfig

# ---------------------------------------------------------------------------
# Page types
# ---------------------------------------------------------------------------
LEVER_PAGE_TYPES: dict[str, str] = {
    "job_listing": (
        "URL pattern: jobs.lever.co/{company}/{job_id}. "
        "Shows job title, location, team, and description. "
        "Has an 'Apply for this job' link/button."
    ),
    "application_form": (
        "URL pattern: jobs.lever.co/{company}/{job_id}/apply. "
        "Single-page form with sections: Basic Info (name, email, phone, "
        "current company, LinkedIn, etc.), Resume/CV upload, "
        "Cover Letter, Additional Information, and custom questions."
    ),
    "confirmation": (
        "Text says 'Application submitted' or 'Thanks for applying'. "
        "Shows confirmation message after successful submission."
    ),
    "error": "Form validation error. Inline error messages next to fields.",
}


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------
LEVER_GUARDRAILS: list[str] = [
    ("Lever uses a SIMPLE SINGLE-PAGE form. All fields are on one page. No multi-page navigation needed."),
    (
        "FILL-FIRST PREFERENCE: Lever's form layout is simple and predictable. "
        "Fill all visible fields using DOM-first approach before falling back "
        "to LLM-based interaction."
    ),
    (
        "Resume upload: Lever supports file upload and also has a 'Parse from "
        "LinkedIn' option. Use the file input directly."
    ),
    (
        "LinkedIn URL field: Lever often has a dedicated LinkedIn URL field. "
        "Fill it with the user's LinkedIn profile URL."
    ),
    (
        "Additional Information field: This is a free-text textarea. "
        "If the user has cover letter text, put it here. Otherwise leave blank."
    ),
    (
        "Custom questions at the bottom may include Yes/No radio buttons, "
        "dropdowns, and free-text fields. Fill all required fields."
    ),
    ("NEVER click 'Submit application' — the orchestrator handles submission."),
]


# ---------------------------------------------------------------------------
# Allowed domains
# ---------------------------------------------------------------------------
LEVER_ALLOWED_DOMAINS: list[str] = [
    "lever.co",
    "jobs.lever.co",
    "api.lever.co",
]


# ---------------------------------------------------------------------------
# Auth flow
# ---------------------------------------------------------------------------
LEVER_AUTH_FLOW = (
    "Lever job applications are publicly accessible — no login required. "
    "The candidate fills out the form and submits. No account creation needed."
)


# ---------------------------------------------------------------------------
# Exported config
# ---------------------------------------------------------------------------
LEVER_CONFIG = PlatformConfig(
    name="lever",
    display_name="Lever",
    url_patterns=[
        "jobs.lever.co",
        "lever.co",
    ],
    allowed_domains=LEVER_ALLOWED_DOMAINS,
    guardrails=LEVER_GUARDRAILS,
    auth_flow=LEVER_AUTH_FLOW,
    page_types=LEVER_PAGE_TYPES,
    shadow_dom_selectors={
        "application_form": ".application-form, .postings-btn-wrapper + form",
        "submit_button": 'button[type="submit"], .postings-btn',
        "resume_input": 'input[type="file"][name="resume"]',
        "name_field": 'input[name="name"]',
        "email_field": 'input[name="email"]',
        "phone_field": 'input[name="phone"]',
        "company_field": 'input[name="org"]',
        "linkedin_field": 'input[name*="linkedin"], input[name*="LinkedIn"]',
        "additional_info": 'textarea[name="comments"]',
    },
    content_markers=[
        "apply for this job",
        "lever",
        "resume/cv",
        "additional information",
    ],
    navigation_hints=[
        "Single-page form — no 'Next' or 'Continue' buttons.",
        "Apply link on job listing page navigates to /apply URL.",
        "Submit button at the bottom of the form.",
    ],
    form_strategy="fill_first",
)
