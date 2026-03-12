"""DomHand Upload — file upload action for resume and cover letter inputs.

Handles file uploads by:
1. Detecting the file input element at the given index
2. Classifying what type of file is expected from the label context
3. Resolving the file path from environment config
4. Using CDP to set the file on the input element
5. Verifying the upload succeeded
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession

from ghosthands.actions.views import DomHandUploadParams

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

_RESUME_KEYWORDS = ('resume', 'cv', 'curriculum vitae')
_COVER_LETTER_KEYWORDS = ('cover letter', 'cover_letter', 'coverletter', 'motivation letter')

# JavaScript: get file input info at an index
_GET_FILE_INPUT_INFO_JS = r"""
(triggerIndex) => {
	const el = document.querySelector('[data-highlight-index="' + triggerIndex + '"]');
	if (!el) return JSON.stringify({error: 'Element not found at index ' + triggerIndex});

	const tag = el.tagName.toLowerCase();
	const isFileInput = tag === 'input' && el.type === 'file';

	// Also check for file input within the element (e.g., a wrapper div)
	let fileInput = isFileInput ? el : el.querySelector('input[type="file"]');

	if (!fileInput) {
		// Look in parent context for hidden file inputs
		const parent = el.closest('.form-group, .form-field, .field, [class*="upload"], [class*="file"], [class*="drop"]');
		if (parent) {
			fileInput = parent.querySelector('input[type="file"]');
		}
	}

	if (!fileInput) {
		return JSON.stringify({
			error: 'No file input found at or near element ' + triggerIndex,
			elementTag: tag,
			elementType: el.type || '',
			elementRole: el.getAttribute('role') || '',
		});
	}

	// Gather label context
	let label = fileInput.getAttribute('aria-label') || '';
	if (!label && fileInput.id) {
		const labelEl = document.querySelector('label[for="' + CSS.escape(fileInput.id) + '"]');
		if (labelEl) label = labelEl.textContent.trim();
	}
	if (!label) {
		const parent = fileInput.closest('.form-group, .form-field, .field, [class*="upload"]');
		if (parent) {
			const clone = parent.cloneNode(true);
			clone.querySelectorAll('input, button').forEach(c => c.remove());
			label = clone.textContent.trim();
		}
	}

	const hasFile = (fileInput.files && fileInput.files.length > 0) || (fileInput.value || '').trim().length > 0;
	const accept = fileInput.getAttribute('accept') || '';

	return JSON.stringify({
		found: true,
		label: label,
		hasFile: hasFile,
		accept: accept,
		fileInputId: fileInput.id || null,
		multiple: fileInput.multiple || false,
	});
}
"""

# JavaScript: check if upload succeeded
_VERIFY_UPLOAD_JS = r"""
(triggerIndex) => {
	const el = document.querySelector('[data-highlight-index="' + triggerIndex + '"]');
	if (!el) return JSON.stringify({uploaded: false, error: 'Element not found'});

	let fileInput = (el.tagName.toLowerCase() === 'input' && el.type === 'file')
		? el
		: el.querySelector('input[type="file"]');

	if (!fileInput) {
		const parent = el.closest('.form-group, .form-field, .field, [class*="upload"], [class*="file"]');
		if (parent) fileInput = parent.querySelector('input[type="file"]');
	}

	if (!fileInput) return JSON.stringify({uploaded: false, error: 'File input not found'});

	const hasFile = (fileInput.files && fileInput.files.length > 0) || (fileInput.value || '').trim().length > 0;
	let fileName = '';
	if (fileInput.files && fileInput.files.length > 0) {
		fileName = fileInput.files[0].name;
	} else if (fileInput.value) {
		fileName = fileInput.value.split('\\').pop() || fileInput.value;
	}

	// Also check for success indicators in the parent container
	const parent = fileInput.closest('.form-group, .form-field, .field, [class*="upload"], [class*="file"]');
	let successIndicator = false;
	if (parent) {
		const text = parent.textContent.toLowerCase();
		successIndicator = text.includes('uploaded') || text.includes('attached') || text.includes('complete');
	}

	return JSON.stringify({
		uploaded: hasFile || successIndicator,
		fileName: fileName,
		successIndicator: successIndicator,
	});
}
"""


# ── File classification ──────────────────────────────────────────────

def _classify_file_input(label: str) -> str:
	"""Classify a file input field by its label.

	Returns: 'resume', 'cover_letter', 'generic', or 'other'
	"""
	trimmed = label.strip()
	if not trimmed:
		return 'generic'

	lower = trimmed.lower()

	# Explicit resume match
	if any(kw in lower for kw in _RESUME_KEYWORDS):
		return 'resume'

	# Explicit cover letter match
	if any(kw in lower for kw in _COVER_LETTER_KEYWORDS):
		return 'cover_letter'

	# Generic labels (ambiguous — default to resume)
	generic_re = re.compile(r'^(attach|upload|choose|browse|select|add)(\s+(a\s+)?file)?s?\.?$', re.IGNORECASE)
	if generic_re.match(trimmed):
		return 'generic'

	# Anything else (portfolio, writing sample, etc.)
	return 'other'


def _resolve_file_path(file_type: str) -> str | None:
	"""Resolve the file path for a given file type from environment variables.

	Looks for:
	- GH_RESUME_PATH for resume uploads
	- GH_COVER_LETTER_PATH for cover letter uploads
	- GH_FILE_PATH as a generic fallback
	"""
	if file_type in ('resume', 'generic'):
		path_str = os.environ.get('GH_RESUME_PATH', '')
		if path_str:
			p = Path(path_str)
			if p.is_file():
				return str(p.resolve())

	if file_type == 'cover_letter':
		path_str = os.environ.get('GH_COVER_LETTER_PATH', '')
		if path_str:
			p = Path(path_str)
			if p.is_file():
				return str(p.resolve())

	# Generic fallback
	path_str = os.environ.get('GH_FILE_PATH', '')
	if path_str:
		p = Path(path_str)
		if p.is_file():
			return str(p.resolve())

	# If file_type is resume or generic, also try GH_RESUME_PATH
	if file_type in ('resume', 'generic'):
		path_str = os.environ.get('GH_RESUME_PATH', '')
		if path_str:
			p = Path(path_str)
			if p.is_file():
				return str(p.resolve())

	return None


# ── Core action function ─────────────────────────────────────────────

async def domhand_upload(params: DomHandUploadParams, browser_session: BrowserSession) -> ActionResult:
	"""Upload a file (resume, cover letter) to a file input element.

	1. Detect the file input at the given element index
	2. Classify what type of file is expected
	3. Resolve the file path from environment config
	4. Set the file via CDP
	5. Verify the upload succeeded
	"""
	page = await browser_session.get_current_page()
	if not page:
		return ActionResult(error='No active page found in browser session')

	# ── Step 1: Detect the file input ─────────────────────────
	try:
		raw_info = await page.evaluate(_GET_FILE_INPUT_INFO_JS, params.index)
		info: dict[str, Any] = json.loads(raw_info) if isinstance(raw_info, str) else raw_info
	except Exception as e:
		return ActionResult(error=f'Failed to inspect file input at index {params.index}: {e}')

	if info.get('error'):
		return ActionResult(error=info['error'])

	if not info.get('found'):
		return ActionResult(error=f'No file input found at index {params.index}')

	# Already has a file?
	if info.get('hasFile'):
		return ActionResult(
			extracted_content=f'File input at index {params.index} already has a file uploaded.',
			include_extracted_content_only_once=True,
		)

	# ── Step 2: Classify the file type ────────────────────────
	label = info.get('label', '')
	detected_type = _classify_file_input(label)

	# Use the param file_type hint if it overrides detection
	effective_type = params.file_type
	if detected_type == 'cover_letter':
		effective_type = 'cover_letter'
	elif detected_type == 'other':
		# Label describes something specific that isn't resume/cover letter
		effective_type = params.file_type  # Trust the caller's hint
	elif detected_type == 'generic':
		effective_type = params.file_type  # Trust the caller's hint

	# ── Step 3: Resolve the file path ─────────────────────────
	file_path = _resolve_file_path(effective_type)
	if not file_path:
		return ActionResult(
			error=f'No {effective_type} file path configured. '
			f'Set GH_RESUME_PATH or GH_COVER_LETTER_PATH environment variable.',
		)

	file_name = Path(file_path).name

	# ── Step 4: Upload the file via CDP ───────────────────────
	try:
		# We need to use CDP to set the file on the input element.
		# Get the node from the browser session for CDP interaction.
		node = await browser_session.get_element_by_index(params.index)
		if node is None:
			return ActionResult(error=f'Element at index {params.index} not available. Page may have changed.')

		# Use the browser-use event bus to dispatch the upload
		from browser_use.browser.events import UploadFileEvent
		event = browser_session.event_bus.dispatch(
			UploadFileEvent(node=node, file_path=file_path)
		)
		await event
		await event.event_result(raise_if_any=True, raise_if_none=False)

		# Brief wait for UI to update
		await asyncio.sleep(0.5)
	except Exception as e:
		# Fallback: try using CDP DOM.setFileInputFiles directly
		try:
			cdp_page = await browser_session.get_current_page()
			if cdp_page:
				session_id = await cdp_page.session_id
				# We need the backend node ID for the file input
				# Use JavaScript to find and tag the actual file input
				tag_result = await page.evaluate(f"""
					(triggerIndex) => {{
						const el = document.querySelector('[data-highlight-index="' + triggerIndex + '"]');
						if (!el) return null;
						let fi = (el.tagName === 'INPUT' && el.type === 'file') ? el : el.querySelector('input[type="file"]');
						if (!fi) {{
							const parent = el.closest('.form-group, .form-field, [class*="upload"]');
							if (parent) fi = parent.querySelector('input[type="file"]');
						}}
						if (!fi) return null;
						fi.setAttribute('data-dh-upload-target', 'true');
						return true;
					}}
				""", params.index)

				if not tag_result:
					return ActionResult(error=f'Could not locate file input for upload: {e}')

				# Resolve the node via CDP
				cdp_client = browser_session.cdp_client
				if cdp_client:
					doc_result = await cdp_client.send.DOM.getDocument(session_id=session_id)
					root_id = doc_result.get('root', {}).get('nodeId', 0)
					search_result = await cdp_client.send.DOM.querySelector(
						params={'nodeId': root_id, 'selector': '[data-dh-upload-target="true"]'},
						session_id=session_id,
					)
					target_node_id = search_result.get('nodeId', 0)
					if target_node_id:
						await cdp_client.send.DOM.setFileInputFiles(
							params={'files': [file_path], 'nodeId': target_node_id},
							session_id=session_id,
						)
						await asyncio.sleep(0.5)
					else:
						return ActionResult(error=f'Failed to upload file: {e}')
				else:
					return ActionResult(error=f'Failed to upload file (no CDP client): {e}')
		except Exception as fallback_e:
			return ActionResult(error=f'Failed to upload file via both event bus and CDP: {e} / {fallback_e}')

	# ── Step 5: Verify the upload ─────────────────────────────
	try:
		verify_json = await page.evaluate(_VERIFY_UPLOAD_JS, params.index)
		verify: dict[str, Any] = json.loads(verify_json) if isinstance(verify_json, str) else verify_json
	except Exception:
		verify = {'uploaded': False}

	if verify.get('uploaded'):
		uploaded_name = verify.get('fileName', file_name)
		memory = f'Uploaded {effective_type} "{uploaded_name}" to file input at index {params.index}'
		logger.info(f'DomHand upload: {memory}')
		return ActionResult(
			extracted_content=memory,
			include_extracted_content_only_once=False,
		)
	else:
		# Upload may have succeeded but verification couldn't confirm
		memory = f'Set file "{file_name}" on input at index {params.index}, but could not confirm upload. Check visually.'
		logger.warning(f'DomHand upload: {memory}')
		return ActionResult(
			extracted_content=memory,
			include_extracted_content_only_once=False,
		)
