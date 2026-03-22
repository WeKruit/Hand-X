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
from ghosthands.actions.domhand_fill_auth_fields import DomHandFillAuthFieldsParams
from ghosthands.actions.views import (
    DomHandAssessStateParams,
    DomHandClosePopupParams,
    DomHandExpandParams,
    DomHandFillParams,
    DomHandInteractControlParams,
    DomHandRecordExpectedValueParams,
    DomHandSelectParams,
    DomHandUploadParams,
)

logger = logging.getLogger(__name__)


def _enable_visual_cursor_best_effort() -> None:
    """Enable the global visual cursor without failing action registration."""
    try:
        from ghosthands.visuals.patch import enable_visual_cursor

        enable_visual_cursor()
    except Exception as exc:
        logger.warning("visual_cursor.enable_failed", extra={"error": str(exc)})


def _register_action_best_effort(tools: "Tools", *, description: str, param_model, func) -> None:
    try:
        tools.action(description=description, param_model=param_model)(func)
    except Exception as exc:
        logger.warning(
            f"domhand.action_registration_failed action={func.__name__} error={exc}",
            extra={"action": func.__name__, "error": str(exc)},
        )


def register_domhand_auth_actions(tools: "Tools") -> None:
    """Register only the auth-page DomHand helpers.

    This exposes the narrow, tested Workday auth helpers without bringing the
    full DomHand form-filling surface back into generic no-DomHand runs.
    """

    from ghosthands.actions.domhand_check_agreement import domhand_check_agreement
    from ghosthands.actions.domhand_click_button import domhand_click_button
    from ghosthands.actions.domhand_fill_auth_fields import domhand_fill_auth_fields

    _enable_visual_cursor_best_effort()

    _register_action_best_effort(
        tools,
        description=(
            "Fill visible auth fields on the current page using GH_EMAIL and GH_PASSWORD. "
            "Targets only auth-like fields such as email, password, and confirm password. "
            "Use this on Workday Create Account or Sign In pages before agreement checking "
            "and before the final auth submit button."
        ),
        param_model=DomHandFillAuthFieldsParams,
        func=domhand_fill_auth_fields,
    )

    _register_action_best_effort(
        tools,
        description=(
            "Check agreement/consent checkboxes on the current page. "
            "Uses robust JavaScript to handle native inputs, ARIA role=checkbox, "
            "and custom Workday-style checkbox widgets. Use this on Create Account "
            "or Sign In pages to check the 'I agree' / privacy policy / terms checkbox."
        ),
        param_model=DomHandCheckAgreementParams,
        func=domhand_check_agreement,
    )

    _register_action_best_effort(
        tools,
        description=(
            "Try multiple strategies to activate a button-like auth control and report "
            "what changed. Use this on Workday Create Account / Sign In submits where "
            "a normal click may double-submit, hit a stale node, or miss a shadow-DOM "
            "backed auth button."
        ),
        param_model=DomHandClickButtonParams,
        func=domhand_click_button,
    )


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
    from ghosthands.actions.domhand_interact_control import domhand_interact_control
    from ghosthands.actions.domhand_record_expected_value import domhand_record_expected_value
    from ghosthands.actions.domhand_select import domhand_select
    from ghosthands.actions.domhand_upload import domhand_upload

    _enable_visual_cursor_best_effort()

    # ── domhand_fill: The core workhorse ──────────────────────
    # Extracts ALL visible form fields, generates answers via a single cheap
    # Haiku LLM call, and fills everything via Playwright DOM manipulation.
    # Handles ~80% of form filling at near-zero cost.
    _register_action_best_effort(
        tools,
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

    _register_action_best_effort(
        tools,
        description=(
            "Assess the current application state before scrolling, advancing, or stopping. "
            "Classifies the page into advanceable/review/confirmation/presubmit_single_page, "
            "finds unresolved required fields, reports visible errors, suggests scroll bias, "
            "and emits a machine-readable state summary for browser-use planning."
        ),
        param_model=DomHandAssessStateParams,
        func=domhand_assess_state,
    )

    _register_action_best_effort(
        tools,
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
    _register_action_best_effort(
        tools,
        description=(
            "Select a dropdown option using platform-aware discovery. "
            "Use this for complex custom dropdowns (Workday, combobox widgets) "
            "that the domhand_fill action could not fill. Clicks the trigger, "
            "discovers options, fuzzy-matches the target value, and verifies selection."
        ),
        param_model=DomHandSelectParams,
        func=domhand_select,
    )

    _register_action_best_effort(
        tools,
        description=(
            "Interact with one exact non-text control by field label and desired value. "
            "Use this for stubborn radios, checkboxes, toggles, button groups, and selects "
            "when domhand_fill did not clear a required blocker. Resolves the real control "
            "by question label, applies the desired option/value, verifies the committed state, "
            "and captures diagnostics if the control still does not stick."
        ),
        param_model=DomHandInteractControlParams,
        func=domhand_interact_control,
    )

    _register_action_best_effort(
        tools,
        description=(
            "Record the expected visible value for one field after a raw manual recovery action. "
            "Use this immediately after a fallback click/input/select that changed a specific field, "
            "then call domhand_assess_state before any unrelated action."
        ),
        param_model=DomHandRecordExpectedValueParams,
        func=domhand_record_expected_value,
    )

    # ── domhand_upload: File upload ───────────────────────────
    # Handles resume and cover letter uploads via file input elements.
    _register_action_best_effort(
        tools,
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
    _register_action_best_effort(
        tools,
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
    _register_action_best_effort(
        tools,
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
    _register_action_best_effort(
        tools,
        description=(
            'Click "Add More" / "Add Another" buttons to expand repeater sections '
            "(e.g., Work Experience, Education). Finds the section, clicks the add "
            "button, waits for new fields, and reports how many appeared."
        ),
        param_model=DomHandExpandParams,
        func=domhand_expand,
    )

    # Log what was registered
    registered = [name for name in tools.registry.registry.actions if name.startswith("domhand_")]
    logger.info(
        f"domhand.actions_registered count={len(registered)} actions={registered}",
        extra={"count": len(registered), "actions": registered},
    )


__all__ = [
    "register_domhand_auth_actions",
    "register_domhand_actions",
]
