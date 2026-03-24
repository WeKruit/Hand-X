"""Workday platform configuration.

Ports ALL Workday-specific knowledge from GHOST-HANDS:
- URL patterns (myworkdayjobs.com, wd1-5.myworkday.com, etc.)
- Page type detection: login, create_account, personal_info, experience, review, etc.
- Auth state machine (5 states)
- Guardrails: never use SSO, always use native auth, avoid "Apply Manually" trap
- Shadow DOM specifics: data-automation-id selectors, UXI components
- Navigation patterns: how multi-page Workday applications flow
- Allowed domains list
"""

from ghosthands.platforms.views import PlatformConfig

# ---------------------------------------------------------------------------
# Auth state machine — 5 states
# ---------------------------------------------------------------------------
# Workday's auth flow has these observable states:
#
#   still_create_account     → Page shows a "Create Account" form
#   native_login             → Page shows email/password sign-in (NOT Google SSO)
#   verification_required    → 2FA or email verification code page
#   authenticated_or_application_resumed → Landed on an application form page
#   explicit_auth_error      → "Invalid credentials" or similar error banner
#   unknown_pending          → Transitional / unrecognized state
#
# Transitions:
#   still_create_account  → (fill email + password + confirm) → native_login | verification_required
#   native_login          → (fill email + password)           → authenticated_or_application_resumed | verification_required | explicit_auth_error
#   verification_required → (HITL pause)                      → authenticated_or_application_resumed
#   explicit_auth_error   → (retry with corrected creds)      → native_login
#   unknown_pending       → (wait + re-detect)                → any
AUTH_STATES: list[str] = [
    "still_create_account",
    "native_login",
    "verification_required",
    "authenticated_or_application_resumed",
    "explicit_auth_error",
    "unknown_pending",
]


# ---------------------------------------------------------------------------
# Page types and their detection patterns
# ---------------------------------------------------------------------------
WORKDAY_PAGE_TYPES: dict[str, str] = {
    "job_listing": (
        "URL contains /job/ or /details/. Has an 'Apply' button. "
        "Shows job description, responsibilities, qualifications."
    ),
    "login": (
        "URL contains /login or /signin. Has email and password fields. "
        "May have 'Sign In with Google' button. Heading says 'Sign In'. "
        "DOM: input[type='password'] present AND formFieldCount < 5."
    ),
    "google_signin": (
        "URL contains accounts.google.com. Redirected from Workday SSO. Has Google-branded email/password flow."
    ),
    "verification_code": (
        "Page asks for verification code, security code, or OTP. "
        "Has short numeric input fields. Text mentions 'enter the code sent to'."
    ),
    "phone_2fa": (
        "Google challenge page (URL contains /challenge/). Requires phone verification, device prompt, or CAPTCHA."
    ),
    "account_creation": (
        "Heading says 'Create Account' or 'Register'. "
        "Has TWO password fields (password + confirm password). "
        "Even if a 'Sign In' tab is present, classify as account_creation "
        "when confirm-password field is visible."
    ),
    "personal_info": (
        "Heading says 'My Information' or 'Personal Info'. "
        "Fields: First Name, Last Name, Email, Phone, Address, City, State, ZIP. "
        "Uses data-automation-id selectors for form labels."
    ),
    "experience": (
        "Heading says 'My Experience' or 'Work Experience'. "
        "May have resume upload section. Fields behind 'Add' buttons. "
        "Uses data-automation-id='Add' for expanding sections."
    ),
    "resume_upload": (
        "Section header mentions Resume or CV. "
        "Has file upload input or 'Upload' button. "
        "data-automation-id='file-upload-drop-zone' or similar."
    ),
    "questions": (
        "Heading says 'Application Questions' or 'Additional Questions'. "
        "Pattern: 'Application Questions (1 of N)'. "
        "Has screening questions: radio buttons, dropdowns, text inputs about "
        "eligibility, availability, referral source, etc."
    ),
    "voluntary_disclosure": (
        "Heading says 'Voluntary Disclosures'. "
        "Asks about gender, race/ethnicity, veteran status. "
        "Dropdowns with self-identification options."
    ),
    "self_identify": (
        "Heading says 'Self Identify' or 'Self-Identification'. "
        "Asks specifically about disability status. "
        "Text: 'Please indicate if you have a disability'."
    ),
    "review": (
        "Heading says 'Review'. READ-ONLY summary of the application. "
        "No editable form fields. Has a 'Submit' or 'Submit Application' button. "
        "No 'Save and Continue' button."
    ),
    "confirmation": (
        "Text says 'Thank you for applying' or 'Application received' or 'Successfully submitted'. No form fields."
    ),
    "error": "Error banner visible. Text contains error message.",
    "unknown": "Cannot determine page type from URL or DOM signals.",
}


# ---------------------------------------------------------------------------
# Shadow DOM / data-automation-id selectors
# ---------------------------------------------------------------------------
WORKDAY_SELECTORS: dict[str, str] = {
    # Form labels and fields
    "form_label": '[data-automation-id*="formLabel"]',
    "form_field": '[data-automation-id*="formField"]',
    "question_text": '[data-automation-id*="questionText"]',
    "page_header": '[data-automation-id*="pageHeader"]',
    "step_title": '[data-automation-id*="stepTitle"]',
    # Dropdowns — Workday uses "Select One" buttons
    "select_one_button": 'button:has-text("Select One")',
    "dropdown_option": '[role="option"]',
    "listbox": '[role="listbox"]',
    # Date fields — segmented MM/DD/YYYY
    "date_month": 'input[data-automation-id*="dateSectionMonth"]',
    "date_day": 'input[data-automation-id*="dateSectionDay"]',
    "date_year": 'input[data-automation-id*="dateSectionYear"]',
    "date_input_mm": 'input[placeholder*="MM"]',
    "date_input_aria": 'input[aria-label*="Month"], input[aria-label*="date"]',
    # File upload
    "file_upload_zone": '[data-automation-id="file-upload-drop-zone"]',
    "file_input": 'input[type="file"]',
    # Navigation
    "next_button": '[data-automation-id="bottom-navigation-next-button"]',
    "previous_button": '[data-automation-id="bottom-navigation-previous-button"]',
    "save_and_continue": 'button:has-text("Save and Continue")',
    "submit_button": 'button:has-text("Submit")',
    # Experience section
    "add_button": '[data-automation-id="Add"]',
    "add_another": 'button:has-text("Add Another")',
    # Checkboxes
    "checkbox": 'input[type="checkbox"]',
    "acknowledgement": '[data-automation-id*="acknowledge"]',
}


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------
WORKDAY_GUARDRAILS: list[str] = [
    # Auth rules
    ("NEVER use 'Sign In with Google' or any SSO option. Always use native Workday email/password authentication."),
    (
        "If the page shows BOTH 'Create Account' and 'Sign In' options, "
        "pick ONE path and commit to it.  If a confirm-password field is "
        "visible, you are on Create Account — stay on it.  Do NOT toggle "
        "between the two.  Do NOT click 'Sign In' when on the Create Account "
        "page."
    ),
    (
        "Avoid the 'Apply Manually' link on job listing pages — it leads to a "
        "different, often broken flow. Click the main 'Apply' button instead."
    ),
    (
        "If the Workday start dialog offers a same-site option like "
        "'Autofill with Resume' or 'Apply with Resume', prefer that over "
        "'Apply Manually'. Never choose external apply/import paths such as "
        "LinkedIn, Indeed, Google, or other third-party account flows."
    ),
    # Form filling rules
    (
        "ONE ATTEMPT PER TEXT FIELD: Type into a text field at most once. "
        "After typing and clicking away, the field is done — even if it appears "
        "empty. Retyping causes duplicate text (e.g. 'WuWuWu'). "
        "Exception: dropdowns showing 'Select One' CAN be retried."
    ),
    (
        "CLICK BEFORE TYPING: Never type unless you just clicked a focused text "
        "input (blue border or cursor visible). Typing without focus causes the "
        "page to jump to random locations."
    ),
    (
        "NO TAB KEY: Never use Tab to move between fields. Click on whitespace "
        "to deselect, then click directly on the next field."
    ),
    # Dropdown rules
    (
        "DROPDOWNS — fill ONE per turn: (1) Click the dropdown button, "
        "(2) Type your answer to filter, (3) Wait 3 seconds, "
        "(4) Click the matching option in the list below, "
        "(5) Verify the button text changed from 'Select One'. "
        "If still 'Select One', retry up to 2 times. Then stop."
    ),
    (
        "NESTED DROPDOWNS: Some dropdowns have sub-menus. After selecting a "
        "category (e.g. 'Website'), a second list appears with sub-options. "
        "Select the sub-option. Do NOT click back arrows."
    ),
    # Date fields
    (
        "DATE FIELDS (MM/DD/YYYY): Click on the MM segment first, then type "
        "the full date as continuous digits with NO slashes (e.g. '01152026'). "
        "Workday auto-advances through the segments."
    ),
    # Navigation — agent handles it directly
    (
        "After filling all fields on a page, click 'Save and Continue' or "
        "'Next' to advance.  NEVER click 'Submit' or 'Submit Application' — "
        "that is the final submission button."
    ),
]


# ---------------------------------------------------------------------------
# Navigation hints
# ---------------------------------------------------------------------------
WORKDAY_NAVIGATION: list[str] = [
    (
        "Workday applications are multi-page. After filling all visible fields, "
        "the orchestrator clicks 'Save and Continue' or 'Next' to advance."
    ),
    (
        "Page flow is typically: Job Listing → Login/Create Account → "
        "Personal Info → Experience → Questions (1 of N) → "
        "Voluntary Disclosure → Self Identify → Review → Confirmation."
    ),
    (
        "Some Workday tenants skip certain pages or combine them. "
        "The agent must detect the current page type dynamically, not assume order."
    ),
    (
        "After clicking 'Apply', Workday may redirect to a login/create-account "
        "page on a different subdomain (e.g. wd5.myworkday.com). This is normal."
    ),
    (
        "Experience sections use 'Add' buttons to expand form blocks. "
        "Each block (work experience, education) must be expanded before filling."
    ),
]


# ---------------------------------------------------------------------------
# Auth flow description
# ---------------------------------------------------------------------------
WORKDAY_AUTH_FLOW = (
    "Workday uses per-tenant accounts (not a global Workday account). "
    "Each company's Workday instance requires separate registration. "
    "Flow: (1) Click Apply on job listing → (2) Redirected to login page → "
    "(3) If no account exists, click 'Create Account' → (4) Fill email, "
    "password, confirm password → (5) May require email verification code → "
    "(6) After verification, redirected to application form. "
    "NEVER use Google SSO — always use native email/password auth."
)


# ---------------------------------------------------------------------------
# Allowed domains
# ---------------------------------------------------------------------------
WORKDAY_ALLOWED_DOMAINS: list[str] = [
    # Core Workday domains
    "myworkdayjobs.com",
    "myworkday.com",
    "workday.com",
    # Workday tenant subdomains (wd1 through wd5)
    "wd1.myworkdayjobs.com",
    "wd2.myworkdayjobs.com",
    "wd3.myworkdayjobs.com",
    "wd4.myworkdayjobs.com",
    "wd5.myworkdayjobs.com",
    "wd1.myworkday.com",
    "wd2.myworkday.com",
    "wd3.myworkday.com",
    "wd4.myworkday.com",
    "wd5.myworkday.com",
    "wd5.myworkdaysite.com",
    # Google auth (for account-creation email verification — NOT SSO)
    "accounts.google.com",
]


# ---------------------------------------------------------------------------
# Exported config
# ---------------------------------------------------------------------------
WORKDAY_CONFIG = PlatformConfig(
    name="workday",
    display_name="Workday",
    url_patterns=[
        "myworkdayjobs.com",
        "myworkday.com",
        "wd1.myworkdayjobs.com",
        "wd2.myworkdayjobs.com",
        "wd3.myworkdayjobs.com",
        "wd4.myworkdayjobs.com",
        "wd5.myworkdayjobs.com",
        "wd5.myworkdaysite.com",
        "workday.com",
    ],
    allowed_domains=WORKDAY_ALLOWED_DOMAINS,
    guardrails=WORKDAY_GUARDRAILS,
    auth_flow=WORKDAY_AUTH_FLOW,
    page_types=WORKDAY_PAGE_TYPES,
    shadow_dom_selectors=WORKDAY_SELECTORS,
    content_markers=[
        "data-automation-id",
        "myworkdayjobs",
        "workday",
        "save and continue",
        "create account",
    ],
    strong_content_markers=[
        "myworkdayjobs",
    ],
    content_marker_min_hits=2,
    navigation_hints=WORKDAY_NAVIGATION,
    form_strategy="dom_first",
    automation_id_map=WORKDAY_SELECTORS,
    fill_overrides={
        "select": "combobox_toggle",
        "date": "segmented_date",
    },
)
