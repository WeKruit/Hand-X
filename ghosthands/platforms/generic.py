"""Generic platform configuration.

Fallback for unknown ATS platforms. Uses conservative heuristics and
broad allowed domains. Suitable for any job application site that
doesn't match a specific platform.
"""

from ghosthands.platforms.views import PlatformConfig


# ---------------------------------------------------------------------------
# Page types (universal detection)
# ---------------------------------------------------------------------------
GENERIC_PAGE_TYPES: dict[str, str] = {
	"job_listing": (
		"Has an 'Apply', 'Apply Now', or 'Apply for this job' button. "
		"Shows job description, responsibilities, qualifications. "
		"No form fields (or minimal — just the apply button)."
	),
	"login": (
		"Has email/password fields or SSO buttons (Google, LinkedIn). "
		"Heading says 'Sign In', 'Log In', or 'Create Account'. "
		"Requires: password field OR SSO button OR (email field + sign-in button). "
		"A standalone 'Sign In' link in a nav bar does NOT qualify."
	),
	"google_signin": (
		"URL contains accounts.google.com. Google-branded login flow. "
		"Has email or password input on Google's domain."
	),
	"verification_code": (
		"Asks for verification code, security code, or OTP. "
		"Has short input fields. Text mentions 'enter the code' or "
		"'verify your email'. No password field."
	),
	"phone_2fa": (
		"Google challenge page or 2FA prompt. "
		"Requires phone verification or device prompt."
	),
	"account_creation": (
		"2+ password fields (password + confirm). Heading says "
		"'Create Account', 'Register', or 'Sign Up'."
	),
	"personal_info": (
		"Fields for name, email, phone, address. "
		"Text includes 'personal info', 'contact info', or 'your information'."
	),
	"experience": (
		"Fields for work experience, education, resume upload. "
		"Text includes 'work experience', 'resume', 'upload cv'."
	),
	"resume_upload": (
		"File upload input or drop zone for resume/CV. "
		"Text includes 'upload resume', 'attach CV'."
	),
	"questions": (
		"Screening questions with radio buttons, dropdowns, text inputs. "
		"Text includes 'application questions', 'screening questions'. "
		"Default classification when form fields exist but no more "
		"specific page type matches."
	),
	"review": (
		"READ-ONLY summary of the application. Has 'Submit' button. "
		"NO editable form fields. NO 'Save and Continue'. "
		"IMPORTANT: A page with a Submit button but also form fields "
		"is NOT a review page."
	),
	"confirmation": (
		"'Thank you for applying', 'Application received', "
		"or 'Successfully submitted'. No form fields."
	),
	"error": "Error message visible. Form validation failed.",
	"unknown": "Cannot determine page type from URL or DOM signals.",
}


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------
GENERIC_GUARDRAILS: list[str] = [
	(
		"Work TOP TO BOTTOM. Start with the topmost unanswered field and "
		"fill it, then move to the next one below."
	),
	(
		"FILL every empty field that is fully visible on screen. "
		"SKIP fields that already have text, a selection, or a checked checkbox."
	),
	(
		"For OPTIONAL fields with no matching data, skip them. "
		"For REQUIRED fields with no matching data, use best judgment "
		"to provide a reasonable answer."
	),
	(
		"CUT-OFF DETECTION: Before answering any question near the bottom of "
		"the screen, verify you can see the COMPLETE question text AND every "
		"answer option. If anything is cut off, do not touch it — report done."
	),
	(
		"You may click dropdowns, radio buttons, checkboxes, 'Add Another', "
		"'Upload', and other form controls — but ONLY for fully visible questions."
	),
	(
		"NEVER click Next, Continue, Submit, Save, Send, or any navigation "
		"button. You are ONLY here to fill fields."
	),
	(
		"Text fields: Click, type the value, click away to deselect. "
		"Dropdowns: Click to open, type to filter, click the match. "
		"Radio buttons: Click the matching option. "
		"Required checkboxes (terms, agreements): Check them."
	),
	(
		"If stuck on a field after two tries, skip it and move on."
	),
]


# ---------------------------------------------------------------------------
# Allowed domains — broad set for generic/unknown platforms
# ---------------------------------------------------------------------------
GENERIC_ALLOWED_DOMAINS: list[str] = [
	# Major ATS platforms
	"myworkdayjobs.com",
	"myworkday.com",
	"workday.com",
	"greenhouse.io",
	"boards.greenhouse.io",
	"job-boards.greenhouse.io",
	"lever.co",
	"jobs.lever.co",
	"smartrecruiters.com",
	"jobs.smartrecruiters.com",
	"icims.com",
	"careers-page.icims.com",
	"ashbyhq.com",
	"jobs.ashbyhq.com",
	"taleo.net",
	"successfactors.com",
	"brassring.com",
	"ultipro.com",
	"paylocity.com",
	"bamboohr.com",
	"rippling.com",
	# LinkedIn
	"linkedin.com",
	"www.linkedin.com",
	"licdn.com",
	# Amazon
	"amazon.jobs",
	"www.amazon.jobs",
	"amazon.com",
	"www.amazon.com",
	# Google auth (for SSO flows)
	"accounts.google.com",
	# Common company career page patterns
	"careers.google.com",
]


# ---------------------------------------------------------------------------
# Auth flow
# ---------------------------------------------------------------------------
GENERIC_AUTH_FLOW = (
	"Unknown platform — auth flow varies. May require login, account "
	"creation, or no auth at all. Detect the page type dynamically. "
	"Prefer email/password over SSO when both are available."
)


# ---------------------------------------------------------------------------
# Exported config
# ---------------------------------------------------------------------------
GENERIC_CONFIG = PlatformConfig(
	name="generic",
	display_name="Generic (any site)",
	url_patterns=[],  # Empty — matches nothing (used as fallback)
	allowed_domains=GENERIC_ALLOWED_DOMAINS,
	guardrails=GENERIC_GUARDRAILS,
	auth_flow=GENERIC_AUTH_FLOW,
	page_types=GENERIC_PAGE_TYPES,
	shadow_dom_selectors={},
	navigation_hints=[
		"Detect multi-page vs single-page forms dynamically.",
		"Look for 'Next', 'Continue', 'Save and Continue' for multi-page.",
		"Look for 'Submit', 'Submit Application' for single-page or final step.",
	],
	form_strategy="dom_first",
)
