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

from ghosthands.actions.views import (
	DomHandExpandParams,
	DomHandFillParams,
	DomHandSelectParams,
	DomHandUploadParams,
)
from ghosthands.actions.domhand_check_agreement import DomHandCheckAgreementParams
from ghosthands.actions.domhand_click_button import DomHandClickButtonParams

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
	from ghosthands.actions.domhand_check_agreement import domhand_check_agreement
	from ghosthands.actions.domhand_click_button import domhand_click_button
	from ghosthands.actions.domhand_expand import domhand_expand
	from ghosthands.actions.domhand_fill import domhand_fill
	from ghosthands.actions.domhand_select import domhand_select
	from ghosthands.actions.domhand_upload import domhand_upload

	# ── domhand_fill: The core workhorse ──────────────────────
	# Extracts ALL visible form fields, generates answers via a single cheap
	# Haiku LLM call, and fills everything via Playwright DOM manipulation.
	# Handles ~80% of form filling at near-zero cost.
	tools.action(
		description=(
			'Fill all visible form fields at once using fast DOM manipulation. '
			'Extracts fields, generates answers from user profile via a single LLM call, '
			'and fills each field via DOM. Handles text inputs, selects, checkboxes, '
			'textareas, and radio buttons. Use this as the FIRST approach for any form page. '
			'Only fall back to individual input/click actions for fields this cannot handle.'
		),
		param_model=DomHandFillParams,
	)(domhand_fill)

	# ── domhand_select: Complex dropdown handler ──────────────
	# For dropdowns that domhand_fill cannot handle: custom widgets,
	# Workday portals with hierarchical dropdowns, combobox patterns.
	tools.action(
		description=(
			'Select a dropdown option using platform-aware discovery. '
			'Use this for complex custom dropdowns (Workday, combobox widgets) '
			'that the domhand_fill action could not fill. Clicks the trigger, '
			'discovers options, fuzzy-matches the target value, and verifies selection.'
		),
		param_model=DomHandSelectParams,
	)(domhand_select)

	# ── domhand_upload: File upload ───────────────────────────
	# Handles resume and cover letter uploads via file input elements.
	tools.action(
		description=(
			'Upload a file (resume or cover letter) to a file input element. '
			'Automatically detects the file type from the input label, resolves '
			'the file path from environment config, and verifies the upload.'
		),
		param_model=DomHandUploadParams,
	)(domhand_upload)

	# ── domhand_check_agreement: Auth page checkbox handler ───
	# Robust JS-based agreement checkbox checker that works on auth pages
	# where domhand_fill is intentionally skipped.
	tools.action(
		description=(
			'Check agreement/consent checkboxes on the current page. '
			'Uses robust JavaScript to handle native inputs, ARIA role=checkbox, '
			'and custom Workday-style checkbox widgets. Use this on Create Account '
			'or Sign In pages where domhand_fill is not used, to check the '
			'"I agree" / privacy policy / terms checkbox.'
		),
		param_model=DomHandCheckAgreementParams,
	)(domhand_check_agreement)

	# ── domhand_click_button: Trusted-event button click ──────
	# Uses Playwright's native click() instead of CDP mouse events.
	# Critical for React-based sites like Workday where buttons check
	# event.isTrusted and ignore CDP-dispatched untrusted events.
	tools.action(
		description=(
			'Click a button using Playwright trusted events. Use this for buttons '
			'that do not respond to regular click actions (e.g., Create Account, '
			'Sign In on Workday). Regular clicks use CDP mouse events which are '
			'untrusted — this action uses Playwright native click which produces '
			'trusted events that React/Workday form handlers require.'
		),
		param_model=DomHandClickButtonParams,
	)(domhand_click_button)

	# ── domhand_expand: Repeater expansion ────────────────────
	# Clicks "Add More" buttons to expand repeater sections like
	# Work Experience, Education, References.
	tools.action(
		description=(
			'Click "Add More" / "Add Another" buttons to expand repeater sections '
			'(e.g., Work Experience, Education). Finds the section, clicks the add '
			'button, waits for new fields, and reports how many appeared.'
		),
		param_model=DomHandExpandParams,
	)(domhand_expand)

	# Log what was registered
	registered = [
		name for name in tools.registry.registry.actions
		if name.startswith("domhand_")
	]
	logger.info(
		"domhand.actions_registered",
		extra={"count": len(registered), "actions": registered},
	)


__all__ = [
	"register_domhand_actions",
]
