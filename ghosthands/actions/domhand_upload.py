"""DomHand Upload — file upload action for resume and cover letter inputs.

Handles file uploads by:
1. Detecting the file input element at the given index (via browser-use selector map)
2. Searching self, descendants, ancestors, and siblings for an actual <input type="file">
3. Classifying what type of file is expected from the label context
4. Resolving the file path from environment config
5. Dispatching an UploadFileEvent through the browser-use event bus
6. Verifying the upload succeeded via CDP
"""

import asyncio
import logging
import os
import re
from pathlib import Path

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.browser.events import UploadFileEvent
from browser_use.dom.views import EnhancedDOMTreeNode

from ghosthands.actions.views import DomHandUploadParams

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

_RESUME_KEYWORDS = ('resume', 'cv', 'curriculum vitae')
_COVER_LETTER_KEYWORDS = ('cover letter', 'cover_letter', 'coverletter', 'motivation letter')


def _body_text_indicates_upload_confirmation(body_text: str, file_name: str) -> bool:
	"""Best-effort check for upload success indicators rendered outside the raw file input."""
	text = (body_text or '').lower()
	name = (file_name or '').lower().strip()

	if name and name in text:
		return True

	return any(
		signal in text
		for signal in (
			'successfully uploaded',
			'uploaded successfully',
			'upload complete',
			'successfully attached',
			'attached successfully',
			'remove file',
			'delete file',
			'replace file',
			'replace resume',
		)
	)


# ── File input search (mirrors browser_use/tools/service.py pattern) ──

def _find_file_input_near_element(
	node: EnhancedDOMTreeNode,
	browser_session: BrowserSession,
	max_height: int = 3,
	max_descendant_depth: int = 3,
) -> EnhancedDOMTreeNode | None:
	"""Find the closest <input type="file"> to the given element.

	Walks descendants, then ancestors (up to max_height), checking siblings
	at each level. This mirrors the pattern in browser_use/tools/service.py.
	"""

	def _search_descendants(n: EnhancedDOMTreeNode, depth: int) -> EnhancedDOMTreeNode | None:
		if depth < 0:
			return None
		if browser_session.is_file_input(n):
			return n
		for child in n.children_nodes or []:
			result = _search_descendants(child, depth - 1)
			if result:
				return result
		return None

	current = node
	for _ in range(max_height + 1):
		# Check the current node itself
		if browser_session.is_file_input(current):
			return current
		# Check all descendants of the current node
		result = _search_descendants(current, max_descendant_depth)
		if result:
			return result
		# Check siblings and their descendants
		if current.parent_node:
			for sibling in current.parent_node.children_nodes or []:
				if sibling is current:
					continue
				if browser_session.is_file_input(sibling):
					return sibling
				result = _search_descendants(sibling, max_descendant_depth)
				if result:
					return result
		current = current.parent_node
		if not current:
			break
	return None


def _get_label_from_node(node: EnhancedDOMTreeNode) -> str:
	"""Extract a human-readable label from the node and its context.

	Checks aria-label, ax_node name, placeholder, and parent text.
	"""
	# Direct attributes
	label = node.attributes.get('aria-label', '')
	if label:
		return label

	# Accessibility tree name
	if node.ax_node and node.ax_node.name:
		return node.ax_node.name

	# Placeholder / title
	label = node.attributes.get('placeholder', '') or node.attributes.get('title', '')
	if label:
		return label

	# Walk up to parent and grab surrounding text
	parent = node.parent_node
	if parent:
		text = parent.get_all_children_text(max_depth=2)
		if text:
			return text[:200]

	return ''


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

	return None


# ── Core action function ─────────────────────────────────────────────

async def domhand_upload(params: DomHandUploadParams, browser_session: BrowserSession) -> ActionResult:
	"""Upload a file (resume, cover letter) to a file input element.

	1. Get the element node from browser-use selector map
	2. Search nearby DOM for an actual <input type="file">
	3. Classify what type of file is expected
	4. Resolve the file path from environment config
	5. Dispatch UploadFileEvent through the event bus
	6. Verify the upload succeeded via CDP
	"""
	page = await browser_session.get_current_page()
	if not page:
		return ActionResult(error='No active page found in browser session')

	# ── Step 1: Get the element node ──────────────────────────
	try:
		node = await browser_session.get_element_by_index(params.index)
		if node is None:
			return ActionResult(error=f'Element at index {params.index} not available. Page may have changed.')
	except Exception as e:
		return ActionResult(error=f'Failed to find element at index {params.index}: {e}')

	# ── Step 2: Find the actual file input ────────────────────
	file_input_node = _find_file_input_near_element(node, browser_session)

	if file_input_node is None:
		# Fallback: search the entire selector map for the closest file input
		selector_map = await browser_session.get_selector_map()
		closest_file_input = None
		min_distance = float('inf')

		node_y = node.absolute_position.y if node.absolute_position else 0
		for _idx, element in selector_map.items():
			if browser_session.is_file_input(element):
				if element.absolute_position:
					distance = abs(element.absolute_position.y - node_y)
					if distance < min_distance:
						min_distance = distance
						closest_file_input = element

		if closest_file_input:
			file_input_node = closest_file_input
			logger.info(f'Found file input closest to element {params.index} (distance: {min_distance}px)')
		else:
			return ActionResult(
				error=f'No <input type="file"> found at or near element index {params.index}. '
				f'The element (tag={node.tag_name}) may not be a file upload trigger.',
			)

	# ── Step 3: Classify the file type ────────────────────────
	label = _get_label_from_node(file_input_node)
	detected_type = _classify_file_input(label)

	# Use the param file_type hint if detection is ambiguous
	effective_type = params.file_type
	if detected_type == 'cover_letter':
		effective_type = 'cover_letter'
	elif detected_type == 'resume':
		effective_type = 'resume'
	# For 'generic' or 'other', trust the caller's hint

	# ── Step 4: Resolve the file path ─────────────────────────
	file_path = _resolve_file_path(effective_type)
	if not file_path:
		return ActionResult(
			error=f'No {effective_type} file path configured. '
			f'Set GH_RESUME_PATH or GH_COVER_LETTER_PATH environment variable.',
		)

	file_name = Path(file_path).name

	# Check if the file input already has a file (via attributes or ax_node)
	if file_input_node.ax_node and file_input_node.ax_node.name:
		ax_name = file_input_node.ax_node.name.lower()
		# Many browsers show "No file chosen" or the filename in the ax_name
		if ax_name and ax_name not in ('', 'no file chosen', 'no file selected', 'choose file', 'browse'):
			# Might already have a file — but proceed anyway since user explicitly asked
			pass

	# ── Step 5: Upload via event bus ──────────────────────────
	try:
		event = browser_session.event_bus.dispatch(
			UploadFileEvent(node=file_input_node, file_path=file_path)
		)
		await event
		await event.event_result(raise_if_any=True, raise_if_none=False)
		await asyncio.sleep(0.75)  # Brief wait for UI to update
	except Exception as e:
		return ActionResult(error=f'Failed to upload file "{file_name}": {e}')

	# ── Step 6: Verify the upload ─────────────────────────────
	uploaded = False
	uploaded_name = file_name

	for _ in range(8):
		try:
			# Resolve the file input node via CDP and check its files property
			session_id = file_input_node.session_id
			if not session_id:
				cdp_session = await browser_session.get_or_create_cdp_session()
				session_id = cdp_session.session_id

			backend_node_id = file_input_node.backend_node_id

			resolve_result = await browser_session.cdp_client.send.DOM.resolveNode(
				params={'backendNodeId': backend_node_id},
				session_id=session_id,
			)
			object_id = resolve_result.get('object', {}).get('objectId')

			if object_id:
				call_result = await browser_session.cdp_client.send.Runtime.callFunctionOn(
					params={
						'objectId': object_id,
						'functionDeclaration': """function() {
							if (this.files && this.files.length > 0) {
								return JSON.stringify({uploaded: true, fileName: this.files[0].name});
							}
							if (this.value && this.value.trim().length > 0) {
								return JSON.stringify({uploaded: true, fileName: this.value.split('\\\\').pop()});
							}
							return JSON.stringify({uploaded: false});
						}""",
						'returnByValue': True,
					},
					session_id=session_id,
				)
				raw_value = call_result.get('result', {}).get('value', '{}')
				import json
				verify = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
				uploaded = verify.get('uploaded', False)
				if uploaded and verify.get('fileName'):
					uploaded_name = verify['fileName']
		except Exception as verify_err:
			logger.debug(f'Upload verification via CDP failed (non-critical): {verify_err}')

		if not uploaded:
			try:
				body_text = await page.evaluate("() => (document.body && document.body.innerText) || ''")
				if _body_text_indicates_upload_confirmation(body_text, file_name):
					uploaded = True
			except Exception as verify_err:
				logger.debug(f'Upload verification via page text failed (non-critical): {verify_err}')

		if uploaded:
			break
		await asyncio.sleep(0.75)

	if uploaded:
		await asyncio.sleep(1.25)  # Allow Workday/other ATS UIs to enable the next button
		memory = (
			f'Uploaded {effective_type} "{uploaded_name}" to file input at index {params.index}. '
			'Wait briefly for the page to finish processing before clicking Continue.'
		)
		logger.info(f'DomHand upload: {memory}')
		return ActionResult(
			extracted_content=memory,
			include_extracted_content_only_once=False,
		)
	else:
		# Upload may have succeeded but verification couldn't confirm
		memory = (
			f'Set file "{file_name}" on input at index {params.index}, but could not confirm upload yet. '
			'Wait and verify the filename or upload success message before clicking Continue.'
		)
		logger.warning(f'DomHand upload: {memory}')
		return ActionResult(
			extracted_content=memory,
			include_extracted_content_only_once=False,
		)
