"""Actions module — browser-use @tools.action() definitions for DomHand form filling.

Usage:
        from browser_use.tools.service import Tools
        from ghosthands.actions import register_domhand_actions

        tools = Tools()
        register_domhand_actions(tools)
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from browser_use.tools.service import Tools

from ghosthands.actions.domhand_check_agreement import DomHandCheckAgreementParams
from ghosthands.actions.domhand_click_button import DomHandClickButtonParams
from ghosthands.actions.views import (
    DomHandAssessStateParams,
    DomHandClosePopupParams,
    DomHandExpandParams,
    DomHandFillParams,
    DomHandRequestUserInputParams,
    DomHandSelectParams,
    DomHandUploadParams,
)

logger = logging.getLogger(__name__)


def register_domhand_actions(tools: "Tools") -> None:
    """Register all DomHand actions with the browser-use Tools controller.

    These actions provide DOM-first form filling that bypasses expensive LLM
    vision calls.  The agent can invoke them as regular tool calls.

    Parameters
    ----------
    tools:
            The ``Tools`` instance from browser-use.  Actions are registered
            via ``tools.action(description, param_model=...)(func)`` which
            delegates to the underlying ``Registry``.
    """
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.domhand_check_agreement import domhand_check_agreement
    from ghosthands.actions.domhand_click_button import domhand_click_button
    from ghosthands.actions.domhand_close_popup import domhand_close_popup
    from ghosthands.actions.domhand_expand import domhand_expand
    from ghosthands.actions.domhand_fill import domhand_fill
    from ghosthands.actions.domhand_request_user_input import domhand_request_user_input
    from ghosthands.actions.domhand_select import domhand_select
    from ghosthands.actions.domhand_upload import domhand_upload

    def _register_action(*, description: str, param_model, func) -> None:
        try:
            tools.action(description=description, param_model=param_model)(func)
        except Exception as exc:
            logger.warning(
                "domhand.action_registration_failed",
                extra={"action": func.__name__, "error": str(exc)},
            )

    # ── Enable global visual cursor (patches Mouse + Element) ───
    # Keep this independent from action registration so a single broken tool
    # does not silently remove the visible cursor from the whole run.
    try:
        from ghosthands.visuals.patch import enable_visual_cursor

        enable_visual_cursor()
    except Exception as exc:
        logger.warning("visual_cursor.enable_failed", extra={"error": str(exc)})

    # ── domhand_fill: The core workhorse ──────────────────────
    # Extracts ALL visible form fields, generates answers via a single cheap
    # Haiku LLM call, and fills everything via Playwright DOM manipulation.
    # Handles ~80% of form filling at near-zero cost.
    _register_action(
        description=(
            "Fill all visible form fields at once using fast DOM manipulation. "
            "Extracts fields, generates answers from user profile via a single LLM call, "
            "and fills each field via DOM. Handles text inputs, selects, checkboxes, "
            "textareas, and radio buttons. Supports scoped repeater fills via "
            "target_section, heading_boundary, focus_fields, and entry_data. Use this as the FIRST "
            "approach for any APPLICATION FORM page. Do NOT use on auth/login pages — "
            "use standard browser-use input actions for email/password fields instead. "
            "Only fall back to individual input/click actions "
            "for fields this cannot handle."
        ),
        param_model=DomHandFillParams,
        func=domhand_fill,
    )

    _register_action(
        description=(
            "Assess the current application state before scrolling, advancing, or stopping. "
            "Classifies the page into advanceable/review/confirmation/presubmit_single_page, "
            "finds unresolved required fields, reports visible errors, suggests scroll bias, "
            "and emits a machine-readable state summary for browser-use planning."
        ),
        param_model=DomHandAssessStateParams,
        func=domhand_assess_state,
    )

    _register_action(
        description=(
            "Close a blocking popup, modal, or interstitial before continuing with the form. "
            'Use this for newsletter prompts, cookie-like overlays, "Not ready to apply" '
            "dialogs, promo modals, and other blockers. Prefers a visible close button, "
            "then backdrop click, then Escape, and verifies the popup is gone."
        ),
        param_model=DomHandClosePopupParams,
        func=domhand_close_popup,
    )

    # ── domhand_select: Complex dropdown handler ──────────────
    # For dropdowns that domhand_fill cannot handle: custom widgets,
    # Workday portals with hierarchical dropdowns, combobox patterns.
    _register_action(
        description=(
            "Select a dropdown option using platform-aware discovery. "
            "Use this for complex custom dropdowns (Workday, combobox widgets) "
            "that the domhand_fill action could not fill. Clicks the trigger, "
            "discovers options, fuzzy-matches the target value, and verifies selection."
        ),
        param_model=DomHandSelectParams,
        func=domhand_select,
    )

    # ── domhand_upload: File upload ───────────────────────────
    # Handles resume and cover letter uploads via file input elements.
    _register_action(
        description=(
            "Upload a file (resume or cover letter) to a file input element. "
            "Automatically detects the file type from the input label, resolves "
            "the file path from environment config, and verifies the upload."
        ),
        param_model=DomHandUploadParams,
        func=domhand_upload,
    )

    # ── domhand_check_agreement: Auth page checkbox handler ───
    # Robust JS-based agreement checkbox checker that works on auth pages
    # where domhand_fill is intentionally skipped.
    _register_action(
        description=(
            "Check agreement/consent checkboxes on the current page. "
            "Uses robust JavaScript to handle native inputs, ARIA role=checkbox, "
            "and custom Workday-style checkbox widgets. Use this on Create Account "
            "or Sign In pages where domhand_fill is not used, to check the "
            '"I agree" / privacy policy / terms checkbox.'
        ),
        param_model=DomHandCheckAgreementParams,
        func=domhand_check_agreement,
    )

    # ── domhand_click_button: Multi-strategy button fallback ───
    # Diagnostic/fallback helper for button-like controls that need
    # extra candidate selection or submission heuristics.
    _register_action(
        description=(
            "Try multiple strategies to activate a button-like control and report "
            "what changed. Use this as a fallback when the normal click action "
            "cannot find or activate the intended button, or when you need richer "
            "diagnostics about why a submit control did not advance."
        ),
        param_model=DomHandClickButtonParams,
        func=domhand_click_button,
    )

    # ── domhand_expand: Repeater expansion ────────────────────
    # Clicks "Add More" buttons to expand repeater sections like
    # Work Experience, Education, References.
    _register_action(
        description=(
            'Click "Add More" / "Add Another" buttons to expand repeater sections '
            "(e.g., Work Experience, Education). Finds the section, clicks the add "
            "button, waits for new fields, and reports how many appeared."
        ),
        param_model=DomHandExpandParams,
        func=domhand_expand,
    )

    _register_action(
        description=(
            "Pause the current run and ask the user for one missing required answer. "
            "Use this when you discover a required field that cannot be answered from "
            "the profile or QA bank, but it is not a true blocker like CAPTCHA or "
            "access denied. After the user answers and resumes, this action returns "
            "the answer so you can continue filling the field."
        ),
        param_model=DomHandRequestUserInputParams,
        func=domhand_request_user_input,
    )

    # Log what was registered
    registered = [name for name in tools.registry.registry.actions if name.startswith("domhand_")]
    logger.info(
        "domhand.actions_registered",
        extra={"count": len(registered), "actions": registered},
    )


__all__ = [
    "register_domhand_actions",
]
