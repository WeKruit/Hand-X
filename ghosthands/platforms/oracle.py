"""Oracle Cloud HCM (Fusion / Candidate Experience) platform configuration.

Covers Oracle-hosted ATS pages such as Goldman Sachs, Citigroup, and other
companies running Oracle Cloud HCM at ``*.oraclecloud.com``.

Key patterns:
- ``apply-flow-stepper`` pagination with numbered page dots
- ``cx-select-pills`` for single-choice options (gender, work auth, etc.)
- ``cx-select`` combobox with ``role="grid"`` dropdown (searchable selects)
- ``geo-hierarchy-form-element`` for cascading Country → Address → ZIP → City → State
- ``profile-inline-form`` repeater tiles (Add Experience / Education / Skill / Language)
- ``apply-flow-input-radio`` for radio groups (disability)
"""

from ghosthands.platforms.views import PlatformConfig

ORACLE_CONFIG = PlatformConfig(
    name="oracle",
    display_name="Oracle Cloud HCM",
    url_patterns=[
        "oraclecloud.com",
        "fa.ocs.oraclecloud.com",
    ],
    allowed_domains=[
        "*.oraclecloud.com",
    ],
    guardrails=[
        (
            "Oracle combobox selects (Degree, School, Country, etc.) require REAL "
            "keyboard typing followed by selecting a suggestion from the dropdown. "
            "Do NOT try to set values via JavaScript — Oracle's framework will "
            "silently reject them."
        ),
        (
            "After filling a combobox field, wait at least 1 second for the "
            "suggestion dropdown to appear before clicking an option."
        ),
        (
            "Repeater sections (Education, Experience, Skills, Languages) use "
            "'Add' tile buttons. PREFER domhand_fill_repeaters to fill ALL "
            "entries in one call rather than manually orchestrating each entry."
        ),
        (
            "The application form uses a stepper (page dots 1-4). After filling "
            "all fields on a page, click 'Next' to advance. Do NOT click "
            "'Submit' until the final review page."
        ),
    ],
    content_markers=[
        "apply-flow-stepper",
        "cx-select-pills",
        "profile-inline-form",
        "apply-flow-pagination",
        "oraclecloud",
    ],
    strong_content_markers=[
        "apply-flow-stepper",
    ],
    content_marker_min_hits=2,
    navigation_hints=[
        (
            "Oracle HCM applications use a stepper with 3-5 pages. "
            "Page flow is typically: Personal Info → Application Questions → "
            "Experience → More About You (disclosures + e-signature)."
        ),
        (
            "The Experience page has repeater tiles for Education, Work Experience, "
            "Skills, Languages, and Licenses. Each 'Add' button opens an inline form."
        ),
    ],
    form_strategy="dom_first",
    # No blanket fill_overrides for Oracle. DomHand uses per-node detection
    # (_IS_ORACLE_SEARCHABLE_JS): try oracle_combobox first only when the element
    # looks like a searchable combobox, then always fall through to the normal
    # select/text/custom-dropdown path on failure — same rule for address LOVs,
    # skills, degree, etc., without "sometimes override, sometimes not".
    fill_overrides={},
)
