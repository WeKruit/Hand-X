"""DomHand Select — dropdown selection using platform-aware discovery.

Handles complex dropdowns that domhand_fill can't handle: custom widgets,
Workday portals with hierarchical dropdowns, combobox/listbox patterns, etc.

Flow:
1. Click the dropdown trigger element
2. Wait for listbox/options to appear
3. Discover available options (native <select>, ARIA listbox, or custom widgets)
4. Fuzzy-match the target value against available options
5. Click the matching option
6. Verify the selection stuck

All DOM element lookups use browser-use's selector map (get_element_by_index)
and CDP backend_node_id resolution — never data-highlight-index attributes.
"""

import asyncio
import json
import logging
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.browser.events import (
    ClickElementEvent,
    GetDropdownOptionsEvent,
    SelectDropdownOptionEvent,
)
from browser_use.dom.views import EnhancedDOMTreeNode
from ghosthands.actions.views import (
    DomHandSelectParams,
    generate_dropdown_search_terms,
    normalize_name,
    split_dropdown_value_hierarchy,
)

logger = logging.getLogger(__name__)

FAIL_OVER_NATIVE_SELECT = "[FAIL-OVER:NATIVE_SELECT]"
FAIL_OVER_CUSTOM_WIDGET = "[FAIL-OVER:CUSTOM_WIDGET]"


# ── CDP-based JavaScript helpers ─────────────────────────────────────
# These JS functions operate on a node passed directly via Runtime.callFunctionOn
# (i.e., `this` is the DOM element). They never use data-highlight-index.

_DISCOVER_OPTIONS_ON_NODE_JS = r"""function() {
	const el = this;
	const tag = el.tagName.toLowerCase();

	// Helper: use __ff.queryAll for cross-shadow-root traversal, fallback to document
	const qAll = (sel) => (window.__ff && window.__ff.queryAll)
		? window.__ff.queryAll(sel)
		: Array.from(document.querySelectorAll(sel));
	const qById = (id) => (window.__ff && window.__ff.getByDomId)
		? window.__ff.getByDomId(id)
		: document.getElementById(id);

	// Case 1: Native <select>
	if (tag === 'select') {
		const options = Array.from(el.options).map((o, i) => ({
			text: o.text.trim(),
			value: o.value,
			index: i,
			selected: o.selected,
		}));
		return JSON.stringify({
			type: 'native_select',
			options: options,
			currentValue: el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : '',
		});
	}

	// Case 2: ARIA combobox/listbox
	const listboxId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
	if (listboxId) {
		const listbox = qById(listboxId);
		if (listbox) {
			const options = Array.from(
				listbox.querySelectorAll('[role="option"], li, [class*="option"], [data-value]')
			).map((o, i) => ({
				text: o.textContent.trim(),
				value: o.getAttribute('data-value') || o.getAttribute('value') || o.textContent.trim(),
				index: i,
				selected: o.getAttribute('aria-selected') === 'true' || o.classList.contains('selected'),
			}));
			return JSON.stringify({
				type: 'aria_listbox',
				listboxId: listboxId,
				options: options,
				currentValue: el.value || (el.textContent ? el.textContent.trim() : ''),
			});
		}
	}

	// Case 3: Workday-style custom dropdowns (data-uxi-widget-type)
	const widgetType = el.getAttribute('data-uxi-widget-type');
	if (widgetType === 'selectinput' || el.getAttribute('role') === 'combobox') {
		const popups = qAll(
			'[role="listbox"], [role="menu"], [class*="popup"], [class*="dropdown-menu"], [class*="options-list"]'
		);
		for (const popup of popups) {
			const rect = popup.getBoundingClientRect();
			if (rect.width > 0 && rect.height > 0) {
				const options = Array.from(
					popup.querySelectorAll('[role="option"], li, [class*="option"], [data-value]')
				).map((o, i) => ({
					text: o.textContent.trim(),
					value: o.getAttribute('data-value') || o.textContent.trim(),
					index: i,
					selected: o.getAttribute('aria-selected') === 'true',
				}));
				if (options.length > 0) {
					return JSON.stringify({
						type: 'custom_popup',
						options: options,
						currentValue: el.value || (el.textContent ? el.textContent.trim() : ''),
					});
				}
			}
		}
	}

	// Case 4: Generic — look for any visible listbox/menu across all roots
	const allListboxes = qAll(
		'[role="listbox"], [role="menu"], ul[class*="dropdown"], ul[class*="options"], div[class*="dropdown-menu"]'
	);
	for (const lb of allListboxes) {
		const rect = lb.getBoundingClientRect();
		if (rect.width > 0 && rect.height > 0) {
			const options = Array.from(
				lb.querySelectorAll('[role="option"], [role="menuitem"], li')
			).filter(o => {
				const r = o.getBoundingClientRect();
				return r.width > 0 && r.height > 0;
			}).map((o, i) => ({
				text: o.textContent.trim(),
				value: o.getAttribute('data-value') || o.textContent.trim(),
				index: i,
				selected: o.getAttribute('aria-selected') === 'true' || o.classList.contains('selected'),
			}));
			if (options.length > 0) {
				return JSON.stringify({
					type: 'generic_listbox',
					options: options,
					currentValue: '',
				});
			}
		}
	}

	return JSON.stringify({
		type: 'unknown',
		options: [],
		currentValue: el.value || (el.textContent ? el.textContent.trim() : ''),
		error: 'Could not discover dropdown options. The dropdown may need to be clicked first.',
	});
}"""

# Click an option by its text content within any visible listbox/popup on the page
_CLICK_OPTION_JS = r"""function(targetText) {
	const lowerTarget = targetText.toLowerCase().trim();

	// Helper: use __ff.queryAll for cross-shadow-root traversal, fallback to document
	const qAll = (sel) => (window.__ff && window.__ff.queryAll)
		? window.__ff.queryAll(sel)
		: Array.from(document.querySelectorAll(sel));

	// Collect all visible option-like elements (across shadow roots)
	const selectors = [
		'[role="option"]', '[role="menuitem"]',
		'li', '[class*="option"]', '[data-value]',
	];
	const candidates = [];
	for (const selector of selectors) {
		for (const el of qAll(selector)) {
			const rect = el.getBoundingClientRect();
			if (rect.width > 0 && rect.height > 0) {
				candidates.push(el);
			}
		}
	}

	// Deduplicate
	const seen = new Set();
	const unique = [];
	for (const el of candidates) {
		if (seen.has(el)) continue;
		seen.add(el);
		unique.push(el);
	}

	// Exact match first
	for (const el of unique) {
		const text = el.textContent.trim();
		if (text.toLowerCase() === lowerTarget) {
			el.click();
			return JSON.stringify({success: true, clicked: text});
		}
	}

	// Partial match (target contained in option or option contained in target)
	for (const el of unique) {
		const text = el.textContent.trim().toLowerCase();
		if (text.includes(lowerTarget) || lowerTarget.includes(text)) {
			el.click();
			return JSON.stringify({success: true, clicked: el.textContent.trim()});
		}
	}

	const availableTexts = unique.slice(0, 20).map(el => el.textContent.trim());
	return JSON.stringify({
		success: false,
		error: 'No matching option found for: ' + targetText,
		available: availableTexts,
	});
}"""

# Select a native <select> option by value or text — operates on `this` = the <select> element
_SELECT_NATIVE_ON_NODE_JS = r"""function(targetValue) {
	const el = this;
	if (el.tagName.toLowerCase() !== 'select') {
		return JSON.stringify({success: false, error: 'Not a native select element'});
	}

	const lowerTarget = targetValue.toLowerCase().trim();
	let matched = false;

	for (const opt of el.options) {
		const optText = opt.text.trim().toLowerCase();
		const optValue = opt.value.toLowerCase();
		if (optText === lowerTarget || optValue === lowerTarget) {
			el.value = opt.value;
			matched = true;
			break;
		}
	}

	// Fuzzy fallback
	if (!matched) {
		for (const opt of el.options) {
			const optText = opt.text.trim().toLowerCase();
			if (optText.includes(lowerTarget) || lowerTarget.includes(optText)) {
				el.value = opt.value;
				matched = true;
				break;
			}
		}
	}

	if (!matched) {
		const available = Array.from(el.options).map(o => o.text.trim()).slice(0, 20);
		return JSON.stringify({success: false, error: 'No match', available: available});
	}

	el.dispatchEvent(new Event('change', {bubbles: true}));
	el.dispatchEvent(new Event('input', {bubbles: true}));
	const selected = el.options[el.selectedIndex];
	return JSON.stringify({success: true, clicked: selected ? selected.text.trim() : targetValue});
}"""

# Verify current selection — operates on `this` = the trigger element
_VERIFY_SELECTION_ON_NODE_JS = r"""function() {
	const el = this;
	if (el.tagName.toLowerCase() === 'select') {
		const sel = el.options[el.selectedIndex];
		return JSON.stringify({value: sel ? sel.text.trim() : ''});
	}

	// For ARIA/custom: check aria-label, value, or text content
	const value = el.value || el.getAttribute('aria-label') || (el.textContent ? el.textContent.trim() : '');
	return JSON.stringify({value: value});
}"""


# ── CDP helper to call JS on a resolved node ────────────────────────


async def _call_function_on_node(
    browser_session: BrowserSession,
    node: EnhancedDOMTreeNode,
    function_declaration: str,
    arguments: list[dict[str, Any]] | None = None,
) -> Any:
    """Resolve a node via CDP and call a JS function on it.

    Uses DOM.resolveNode with backend_node_id, then Runtime.callFunctionOn.
    Returns the parsed JSON result or raw value.
    """
    session_id = node.session_id
    if not session_id:
        cdp_session = await browser_session.get_or_create_cdp_session()
        session_id = cdp_session.session_id

    # Resolve the backend node to a JS remote object
    resolve_result = await browser_session.cdp_client.send.DOM.resolveNode(
        params={"backendNodeId": node.backend_node_id},
        session_id=session_id,
    )
    object_id = resolve_result.get("object", {}).get("objectId")
    if not object_id:
        raise RuntimeError(f"Could not resolve node (backend_node_id={node.backend_node_id}) to JS object")

    call_params: dict[str, Any] = {
        "objectId": object_id,
        "functionDeclaration": function_declaration,
        "returnByValue": True,
    }
    if arguments:
        call_params["arguments"] = arguments

    call_result = await browser_session.cdp_client.send.Runtime.callFunctionOn(
        params=call_params,
        session_id=session_id,
    )
    raw_value = call_result.get("result", {}).get("value")
    if isinstance(raw_value, str):
        try:
            return json.loads(raw_value)
        except (json.JSONDecodeError, ValueError):
            return raw_value
    return raw_value


# ── Fuzzy matching helper ────────────────────────────────────────────


def _fuzzy_match_option(
    target: str,
    options: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find the best matching option for a target value using multi-pass fuzzy matching."""
    target_norm = normalize_name(target)
    if not target_norm:
        return None

    # Pass 1: Exact match
    for opt in options:
        if normalize_name(opt.get("text", "")) == target_norm:
            return opt
        if normalize_name(opt.get("value", "")) == target_norm:
            return opt

    # Pass 2: Contains (either direction)
    for opt in options:
        opt_norm = normalize_name(opt.get("text", ""))
        if opt_norm and (target_norm in opt_norm or opt_norm in target_norm):
            return opt

    # Pass 3: Word overlap (at least 1 shared meaningful word)
    target_words = set(target_norm.split()) - {"the", "a", "an", "of", "for", "in", "to"}
    if len(target_words) >= 1:
        best_overlap = 0
        best_opt: dict[str, Any] | None = None
        for opt in options:
            opt_words = set(normalize_name(opt.get("text", "")).split()) - {"the", "a", "an", "of", "for", "in", "to"}
            overlap = len(target_words & opt_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best_opt = opt
        if best_opt is not None and best_overlap >= 1:
            return best_opt

    return None


async def _clear_dropdown_search(page: Any) -> None:
    """Clear the current typed query for an open searchable dropdown."""
    for shortcut in ("Meta+A", "Control+A"):
        try:
            await page.keyboard.press(shortcut)
        except Exception:
            pass
    try:
        await page.keyboard.press("Backspace")
    except Exception:
        pass
    await asyncio.sleep(0.15)


async def _search_and_click_dropdown_option(page: Any, value: str) -> dict[str, Any]:
    """Type generic fallback search terms into an open dropdown and click a match."""
    for idx, term in enumerate(generate_dropdown_search_terms(value)):
        try:
            if idx > 0:
                await _clear_dropdown_search(page)
            await page.keyboard.type(term, delay=45)
            await asyncio.sleep(0.3)
            result = await _click_option_via_page_js(page, value, "typed_search")
            if result.get("success"):
                return result
            await page.keyboard.press("Enter")
            await asyncio.sleep(1.1)
            result = await _click_option_via_page_js(page, value, "typed_search")
            if result.get("success"):
                return result
            result = await _click_option_via_page_js(page, term, "typed_search")
            if result.get("success"):
                return result
        except Exception as e:
            logger.debug(f'Typed dropdown search failed for "{term}": {e}')
    return {"success": False, "error": f"No matching option found for: {value}"}


async def _search_and_click_dropdown_path(page: Any, value: str) -> dict[str, Any]:
    """Handle hierarchical dropdown labels such as "Website > workday.com"."""
    segments = split_dropdown_value_hierarchy(value)
    if len(segments) <= 1:
        return await _search_and_click_dropdown_option(page, value)

    last_result: dict[str, Any] = {"success": False, "error": f"No matching option found for: {value}"}
    for segment in segments:
        result = await _click_option_via_page_js(page, segment, "hierarchy")
        if not result.get("success"):
            result = await _search_and_click_dropdown_option(page, segment)
        if not result.get("success"):
            return result
        last_result = result
        await asyncio.sleep(0.8)
    return last_result


def _selection_matches_value(current: str, expected: str) -> bool:
    """Return True when the widget visibly reflects the intended selection."""
    current_norm = normalize_name(current or "")
    expected_norm = normalize_name(expected or "")
    if not current_norm or "select one" in current_norm or "choose one" in current_norm:
        return False
    if expected_norm and (expected_norm in current_norm or current_norm in expected_norm):
        return True
    segments = split_dropdown_value_hierarchy(expected)
    if not segments:
        return False
    final_segment = normalize_name(segments[-1])
    return bool(final_segment and final_segment in current_norm)


def _failover_prefix(widget_kind: str) -> str:
    """Return a machine-readable failover token for the widget type."""
    return FAIL_OVER_NATIVE_SELECT if widget_kind == "native_select" else FAIL_OVER_CUSTOM_WIDGET


def _native_select_failover_hint(index: int) -> str:
    """Return the exact native-select fallback instructions for the agent."""
    return (
        f"STOP — do NOT retry domhand_select for this field. This is a native <select>. Use dropdown_options(index={index}) to inspect "
        f"the exact option text/value, then call select_dropdown(index={index}, text=...) "
        "with the exact text/value string. Do NOT use click on this element."
    )


def _custom_widget_failover_hint() -> str:
    """Return the fallback instructions for custom dropdown widgets."""
    return (
        "Open the widget manually, type/search if supported, click the option directly, "
        "follow any secondary menu to the final leaf option, and only continue once the "
        "field visibly changes."
    )


def _build_failover_message(
    widget_kind: str,
    index: int,
    *,
    reason: str,
    available_texts: list[str] | None = None,
    current_value: str | None = None,
) -> str:
    """Build a structured, widget-specific failover message."""
    prefix = _failover_prefix(widget_kind)
    parts = [prefix, reason]
    if available_texts:
        parts.append(f"Options: {available_texts}.")
    if current_value:
        parts.append(f'Current value: "{current_value}".')
    if widget_kind == "native_select":
        parts.append(_native_select_failover_hint(index))
    else:
        parts.append("STOP — do NOT retry domhand_select for this field.")
        parts.append(_custom_widget_failover_hint())
    return " ".join(parts)


async def _read_current_selection(
    browser_session: BrowserSession,
    node: EnhancedDOMTreeNode,
) -> str:
    """Read the widget's currently visible value."""
    try:
        verify = await _call_function_on_node(browser_session, node, _VERIFY_SELECTION_ON_NODE_JS)
    except Exception:
        return ""
    return verify.get("value", "") if isinstance(verify, dict) else ""


async def _confirm_selection(
    page: Any,
    browser_session: BrowserSession,
    node: EnhancedDOMTreeNode,
    dropdown_type: str,
    expected: str,
    clicked_text: str,
) -> tuple[str, str]:
    """Retry searchable/multi-layer dropdowns until the final visible value is confirmed."""
    current = ""
    last_clicked = clicked_text
    for attempt in range(3):
        await asyncio.sleep(0.7 if attempt == 0 else 0.9)
        current = await _read_current_selection(browser_session, node)
        if _selection_matches_value(current, expected):
            return current, last_clicked
        if dropdown_type == "native_select" or attempt == 2:
            break
        try:
            event = browser_session.event_bus.dispatch(ClickElementEvent(node=node))
            await event
            await event.event_result(raise_if_any=True, raise_if_none=False)
            await asyncio.sleep(0.45)
        except Exception:
            pass
        retry = await _search_and_click_dropdown_path(page, expected)
        if retry.get("success"):
            last_clicked = retry.get("clicked", last_clicked)
    current = await _read_current_selection(browser_session, node)
    return current, last_clicked


# ── Core action function ─────────────────────────────────────────────


async def domhand_select(params: DomHandSelectParams, browser_session: BrowserSession) -> ActionResult:
    """Select a dropdown option using platform-aware discovery.

    1. Click the dropdown trigger to open it
    2. Discover available options (native, ARIA, or custom)
    3. Fuzzy-match the target value
    4. Click the matching option
    5. Verify the selection
    """
    page = await browser_session.get_current_page()
    if not page:
        return ActionResult(error="No active page found in browser session")

    # ── Step 0: Inject __ff shadow-DOM helpers if not present ─
    try:
        from ghosthands.dom.shadow_helpers import ensure_helpers

        await ensure_helpers(page)
    except Exception as e:
        logger.debug(f"Could not inject __ff helpers: {e}")

    # ── Step 1: Get the trigger element ───────────────────────
    try:
        node = await browser_session.get_element_by_index(params.index)
        if node is None:
            return ActionResult(error=f"Element index {params.index} not available. Page may have changed.")
    except Exception as e:
        return ActionResult(error=f"Failed to find element at index {params.index}: {e}")

    # ── Step 2: Try browser-use event bus first (handles native + ARIA) ──
    # For native <select> elements, try the built-in SelectDropdownOptionEvent
    is_native_select = node.tag_name == "select"
    widget_kind = "native_select" if is_native_select else "custom_widget"

    if is_native_select:
        try:
            return await _select_via_event_bus(browser_session, node, params)
        except Exception as e:
            logger.debug(f"Event bus select failed for native <select>, falling back to CDP: {e}")

    # ── Step 3: Discover options via CDP (resolve node, run JS on it) ──
    try:
        discovery = await _call_function_on_node(browser_session, node, _DISCOVER_OPTIONS_ON_NODE_JS)
    except Exception as e:
        discovery = {"type": "unknown", "options": [], "error": str(e)}

    dropdown_type = discovery.get("type", "unknown") if isinstance(discovery, dict) else "unknown"
    options = discovery.get("options", []) if isinstance(discovery, dict) else []

    # ── Step 3b: If no options found, click to open and re-discover ──
    # React-select dropdowns often need TWO clicks: first to focus, second to open.
    if not options or dropdown_type == "unknown":
        for click_attempt in range(4):
            try:
                event = browser_session.event_bus.dispatch(ClickElementEvent(node=node))
                await event
                await event.event_result(raise_if_any=True, raise_if_none=False)
                await asyncio.sleep(0.5)  # Wait for dropdown animation

                # Re-discover after clicking
                discovery = await _call_function_on_node(browser_session, node, _DISCOVER_OPTIONS_ON_NODE_JS)
                dropdown_type = discovery.get("type", "unknown") if isinstance(discovery, dict) else "unknown"
                options = discovery.get("options", []) if isinstance(discovery, dict) else []
                if options:
                    break  # Got options, no need for second click
            except Exception as e:
                logger.warning(f"Failed to click dropdown trigger (attempt {click_attempt + 1}): {e}")
                break

    # ── Step 3c: If still no options, try GetDropdownOptionsEvent ──
    if not options:
        try:
            event = browser_session.event_bus.dispatch(GetDropdownOptionsEvent(node=node))
            dropdown_data = await event.event_result(timeout=3.0, raise_if_none=False, raise_if_any=False)
            if dropdown_data and isinstance(dropdown_data, dict):
                raw_options = dropdown_data.get("options", [])
                if raw_options:
                    options = raw_options
                    dropdown_type = dropdown_data.get("type", "event_bus")
        except Exception as e:
            logger.debug(f"GetDropdownOptionsEvent failed: {e}")

    if not options:
        return ActionResult(
            error=_build_failover_message(
                widget_kind,
                params.index,
                reason=f"domhand_select cannot handle element {params.index}.",
            ),
        )

    # ── Step 4: Match the target value ────────────────────────
    matched = _fuzzy_match_option(params.value, options)
    result: dict[str, Any] | None = None
    matched_text = params.value

    if not matched:
        if dropdown_type != "native_select":
            result = await _search_and_click_dropdown_path(page, params.value)
            if result.get("success"):
                matched_text = result.get("clicked", params.value)
        if result is None or not result.get("success"):
            available_texts = [opt.get("text", "") for opt in options[:20]]
            return ActionResult(
                error=_build_failover_message(
                    widget_kind,
                    params.index,
                    reason=f'No match for "{params.value}" in element {params.index}.',
                    available_texts=available_texts,
                ),
            )

    # ── Step 5: Click the matched option ──────────────────────
    if result is None:
        matched_text = matched.get("text", params.value)
        try:
            if dropdown_type == "native_select":
                # For native selects, use JS to set value directly on the resolved node
                result = await _call_function_on_node(
                    browser_session,
                    node,
                    _SELECT_NATIVE_ON_NODE_JS,
                    arguments=[{"value": matched_text}],
                )
            else:
                # For custom dropdowns, try SelectDropdownOptionEvent first
                try:
                    event = browser_session.event_bus.dispatch(SelectDropdownOptionEvent(node=node, text=matched_text))
                    selection_data = await event.event_result(timeout=3.0, raise_if_none=False, raise_if_any=False)
                    if selection_data and isinstance(selection_data, dict) and selection_data.get("success"):
                        result = {"success": True, "clicked": selection_data.get("selected_text", matched_text)}
                    else:
                        # Fallback: click the option via page-level JS
                        result = await _click_option_via_page_js(page, matched_text, dropdown_type)
                except Exception:
                    result = await _click_option_via_page_js(page, matched_text, dropdown_type)
        except Exception as e:
            return ActionResult(error=f'Failed to select option "{matched_text}": {e}')

    if isinstance(result, dict) and not result.get("success"):
        available = result.get("available", [])
        return ActionResult(
            error=f'Failed to select "{matched_text}": {result.get("error", "unknown")}. Available: {available}',
        )

    clicked_text = result.get("clicked", matched_text) if isinstance(result, dict) else matched_text

    # ── Step 6: Verify the selection ──────────────────────────
    current, clicked_text = await _confirm_selection(
        page,
        browser_session,
        node,
        dropdown_type,
        params.value,
        clicked_text,
    )

    if not _selection_matches_value(current, params.value):
        return ActionResult(
            error=_build_failover_message(
                widget_kind,
                params.index,
                reason=f'Selection for "{params.value}" was not confirmed on element {params.index}.',
                current_value=current,
            ),
        )

    memory = f'Selected "{clicked_text}" for dropdown at index {params.index}'
    if current and normalize_name(current) != normalize_name(clicked_text):
        memory += f' (showing: "{current}")'

    logger.info(f"DomHand select: {memory}")

    return ActionResult(
        extracted_content=memory,
        include_extracted_content_only_once=False,
    )


# ── Helper: select via event bus for native selects ──────────────────


async def _select_via_event_bus(
    browser_session: BrowserSession,
    node: EnhancedDOMTreeNode,
    params: DomHandSelectParams,
) -> ActionResult:
    """Use browser-use's GetDropdownOptionsEvent + SelectDropdownOptionEvent.

    This path is preferred for native <select> elements since browser-use
    handles all the change/input event dispatching correctly.
    """
    # Get options
    event = browser_session.event_bus.dispatch(GetDropdownOptionsEvent(node=node))
    dropdown_data = await event.event_result(timeout=3.0, raise_if_none=True, raise_if_any=True)

    if not dropdown_data or not isinstance(dropdown_data, dict):
        raise ValueError("Failed to get dropdown options from event bus")

    raw_options = dropdown_data.get("options", [])
    if not raw_options:
        raise ValueError("No options returned from event bus")

    # Convert to our option format if needed
    options = raw_options if isinstance(raw_options, list) else []
    matched = _fuzzy_match_option(params.value, options)

    if not matched:
        available_texts = [opt.get("text", "") for opt in options[:20]]
        return ActionResult(
            error=_build_failover_message(
                "native_select",
                params.index,
                reason=f'No match for "{params.value}" in element {params.index}.',
                available_texts=available_texts,
            ),
        )

    matched_text = matched.get("text", params.value)

    # Select via event bus
    event = browser_session.event_bus.dispatch(SelectDropdownOptionEvent(node=node, text=matched_text))
    selection_data = await event.event_result(timeout=3.0, raise_if_none=False, raise_if_any=True)

    clicked_text = matched_text
    if selection_data and isinstance(selection_data, dict):
        clicked_text = selection_data.get("selected_text", matched_text)

    memory = f'Selected "{clicked_text}" for dropdown at index {params.index}'
    logger.info(f"DomHand select: {memory}")

    return ActionResult(
        extracted_content=memory,
        include_extracted_content_only_once=False,
    )


# ── Helper: click option via page-level JS ───────────────────────────


async def _click_option_via_page_js(page: Any, matched_text: str, dropdown_type: str) -> dict[str, Any]:
    """Click a dropdown option using page.evaluate with a global JS search.

    This is the fallback when the event bus approach doesn't work. It searches
    all visible option-like elements on the page (not tied to any specific
    element index).
    """
    raw_result = await page.evaluate(_CLICK_OPTION_JS, matched_text)
    if isinstance(raw_result, str):
        return json.loads(raw_result)
    return raw_result if isinstance(raw_result, dict) else {"success": False, "error": "Unexpected result type"}
