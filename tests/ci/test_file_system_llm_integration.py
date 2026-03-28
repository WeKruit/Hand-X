"""Integration tests for DOCX and image file support in LLM messages."""

import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from browser_use.agent.message_manager.service import MessageManager
from browser_use.agent.prompts import AgentMessagePrompt
from browser_use.agent.views import ActionResult, AgentStepInfo
from browser_use.browser.views import BrowserStateSummary, TabInfo
from browser_use.dom.views import SerializedDOMState
from browser_use.filesystem.file_system import FileSystem
from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, SystemMessage


class TestImageInLLMMessages:
	"""Test that images flow correctly through to LLM messages."""

	def create_test_image(self, width: int = 100, height: int = 100) -> bytes:
		"""Create a test image and return bytes."""
		img = Image.new('RGB', (width, height), color='red')
		buffer = io.BytesIO()
		img.save(buffer, format='PNG')
		buffer.seek(0)
		return buffer.read()

	@pytest.mark.asyncio
	async def test_image_stored_in_message_manager(self, tmp_path: Path):
		"""Test that images are stored in MessageManager state."""
		fs = FileSystem(tmp_path)
		system_message = SystemMessage(content='Test system message')
		mm = MessageManager(task='test', system_message=system_message, file_system=fs)

		# Create ActionResult with images
		images = [{'name': 'test.png', 'data': 'base64_test_data'}]
		action_results = [
			ActionResult(
				extracted_content='Read image file test.png',
				long_term_memory='Read image file test.png',
				images=images,
				include_extracted_content_only_once=True,
			)
		]

		# Update message manager with results
		step_info = AgentStepInfo(step_number=1, max_steps=10)
		mm._update_agent_history_description(model_output=None, result=action_results, step_info=step_info)

		# Verify images are stored
		assert mm.state.read_state_images is not None
		assert len(mm.state.read_state_images) == 1
		assert mm.state.read_state_images[0]['name'] == 'test.png'
		assert mm.state.read_state_images[0]['data'] == 'base64_test_data'

	@pytest.mark.asyncio
	async def test_images_cleared_after_step(self, tmp_path: Path):
		"""Test that images are cleared after each step."""
		fs = FileSystem(tmp_path)
		system_message = SystemMessage(content='Test system message')
		mm = MessageManager(task='test', system_message=system_message, file_system=fs)

		# First step with images
		images = [{'name': 'test.png', 'data': 'base64_data'}]
		action_results = [ActionResult(images=images, include_extracted_content_only_once=True)]
		step_info = AgentStepInfo(step_number=1, max_steps=10)
		mm._update_agent_history_description(model_output=None, result=action_results, step_info=step_info)

		assert len(mm.state.read_state_images) == 1

		# Second step without images - should clear
		action_results_2 = [ActionResult(extracted_content='No images')]
		step_info_2 = AgentStepInfo(step_number=2, max_steps=10)
		mm._update_agent_history_description(model_output=None, result=action_results_2, step_info=step_info_2)

		assert len(mm.state.read_state_images) == 0

	@pytest.mark.asyncio
	async def test_multiple_images_accumulated(self, tmp_path: Path):
		"""Test that multiple images in one step are accumulated."""
		fs = FileSystem(tmp_path)
		system_message = SystemMessage(content='Test system message')
		mm = MessageManager(task='test', system_message=system_message, file_system=fs)

		# Multiple action results with images
		action_results = [
			ActionResult(images=[{'name': 'img1.png', 'data': 'data1'}], include_extracted_content_only_once=True),
			ActionResult(images=[{'name': 'img2.jpg', 'data': 'data2'}], include_extracted_content_only_once=True),
		]
		step_info = AgentStepInfo(step_number=1, max_steps=10)
		mm._update_agent_history_description(model_output=None, result=action_results, step_info=step_info)

		assert len(mm.state.read_state_images) == 2
		assert mm.state.read_state_images[0]['name'] == 'img1.png'
		assert mm.state.read_state_images[1]['name'] == 'img2.jpg'

	@pytest.mark.asyncio
	async def test_domhand_fill_and_assess_do_not_pollute_read_state(self, tmp_path: Path):
		"""Test that large DomHand page-pass summaries are suppressed from read_state."""
		fs = FileSystem(tmp_path)
		system_message = SystemMessage(content='Test system message')
		mm = MessageManager(task='test', system_message=system_message, file_system=fs)

		action_results = [
			ActionResult(
				extracted_content='DomHand fill summary that should not enter browser-use read_state',
				include_extracted_content_only_once=True,
				metadata={'tool': 'domhand_fill'},
			),
			ActionResult(
				extracted_content='DomHand assess summary that should not enter browser-use read_state',
				include_extracted_content_only_once=True,
				metadata={'tool': 'domhand_assess_state'},
			),
		]

		step_info = AgentStepInfo(step_number=1, max_steps=10)
		mm._update_agent_history_description(model_output=None, result=action_results, step_info=step_info)

		assert mm.state.read_state_description == ''

	def test_agent_message_prompt_includes_images(self, tmp_path: Path):
		"""Test that AgentMessagePrompt includes images in message content."""
		fs = FileSystem(tmp_path)

		# Create browser state
		browser_state = BrowserStateSummary(
			url='https://example.com',
			title='Test',
			tabs=[TabInfo(target_id='test-0', url='https://example.com', title='Test')],
			screenshot=None,
			dom_state=SerializedDOMState(_root=None, selector_map={}),
		)

		# Create images
		read_state_images = [{'name': 'test.png', 'data': 'base64_image_data_here'}]

		# Create message prompt
		prompt = AgentMessagePrompt(
			browser_state_summary=browser_state,
			file_system=fs,
			read_state_images=read_state_images,
		)

		# Get user message with vision enabled
		user_message = prompt.get_user_message(use_vision=True)

		# Verify message has content parts (not just string)
		assert isinstance(user_message.content, list)

		# Find image content parts
		image_parts = [part for part in user_message.content if isinstance(part, ContentPartImageParam)]
		text_parts = [part for part in user_message.content if isinstance(part, ContentPartTextParam)]

		# Should have at least one image
		assert len(image_parts) >= 1

		# Should have text label
		image_labels = [part.text for part in text_parts if 'test.png' in part.text]
		assert len(image_labels) >= 1

		# Verify image data URL format
		img_part = image_parts[0]
		assert 'data:image/' in img_part.image_url.url
		assert 'base64,base64_image_data_here' in img_part.image_url.url

	def test_agent_message_prompt_png_vs_jpg_media_type(self, tmp_path: Path):
		"""Test that AgentMessagePrompt correctly detects PNG vs JPG media types."""
		fs = FileSystem(tmp_path)

		browser_state = BrowserStateSummary(
			url='https://example.com',
			title='Test',
			tabs=[TabInfo(target_id='test-0', url='https://example.com', title='Test')],
			screenshot=None,
			dom_state=SerializedDOMState(_root=None, selector_map={}),
		)

		# Test PNG
		read_state_images_png = [{'name': 'test.png', 'data': 'data'}]
		prompt_png = AgentMessagePrompt(
			browser_state_summary=browser_state,
			file_system=fs,
			read_state_images=read_state_images_png,
		)
		message_png = prompt_png.get_user_message(use_vision=True)
		image_parts_png = [part for part in message_png.content if isinstance(part, ContentPartImageParam)]
		assert 'data:image/png;base64' in image_parts_png[0].image_url.url

		# Test JPG
		read_state_images_jpg = [{'name': 'photo.jpg', 'data': 'data'}]
		prompt_jpg = AgentMessagePrompt(
			browser_state_summary=browser_state,
			file_system=fs,
			read_state_images=read_state_images_jpg,
		)
		message_jpg = prompt_jpg.get_user_message(use_vision=True)
		image_parts_jpg = [part for part in message_jpg.content if isinstance(part, ContentPartImageParam)]
		assert 'data:image/jpeg;base64' in image_parts_jpg[0].image_url.url

	def test_agent_message_prompt_no_images(self, tmp_path: Path):
		"""Test that message works correctly when no images are present."""
		fs = FileSystem(tmp_path)

		browser_state = BrowserStateSummary(
			url='https://example.com',
			title='Test',
			tabs=[TabInfo(target_id='test-0', url='https://example.com', title='Test')],
			screenshot=None,
			dom_state=SerializedDOMState(_root=None, selector_map={}),
		)

		# No images
		prompt = AgentMessagePrompt(
			browser_state_summary=browser_state,
			file_system=fs,
			read_state_images=[],
		)

		# Get user message without vision
		user_message = prompt.get_user_message(use_vision=False)

		# Should be plain text, not content parts
		assert isinstance(user_message.content, str)

	def test_agent_message_prompt_empty_base64_skipped(self, tmp_path: Path):
		"""Test that images with empty base64 data are skipped."""
		fs = FileSystem(tmp_path)

		browser_state = BrowserStateSummary(
			url='https://example.com',
			title='Test',
			tabs=[TabInfo(target_id='test-0', url='https://example.com', title='Test')],
			screenshot=None,
			dom_state=SerializedDOMState(_root=None, selector_map={}),
		)

		# Image with empty data field
		read_state_images = [
			{'name': 'empty.png', 'data': ''},  # Empty - should be skipped
			{'name': 'valid.png', 'data': 'valid_data'},  # Valid
		]

		prompt = AgentMessagePrompt(
			browser_state_summary=browser_state,
			file_system=fs,
			read_state_images=read_state_images,
		)

		user_message = prompt.get_user_message(use_vision=True)
		image_parts = [part for part in user_message.content if isinstance(part, ContentPartImageParam)]

		# Should only have 1 image (the valid one)
		assert len(image_parts) == 1
		assert 'valid_data' in image_parts[0].image_url.url


class TestDocxInLLMMessages:
	"""Test that DOCX content flows correctly through to LLM messages."""

	@pytest.mark.asyncio
	async def test_docx_in_extracted_content(self, tmp_path: Path):
		"""Test that DOCX text appears in extracted_content."""
		fs = FileSystem(tmp_path)

		# Create DOCX file
		content = """# Title
Some important content here."""
		await fs.write_file('test.docx', content)

		# Read it
		result = await fs.read_file('test.docx')

		# Verify content is in the result
		assert 'Title' in result
		assert 'important content' in result

	@pytest.mark.asyncio
	async def test_docx_in_message_manager(self, tmp_path: Path):
		"""Test that DOCX content appears in message manager state."""
		fs = FileSystem(tmp_path)
		system_message = SystemMessage(content='Test system message')
		mm = MessageManager(task='test', system_message=system_message, file_system=fs)

		# Simulate read_file action result
		docx_content = """Read from file test.docx.
<content>
Title
Some content here.
</content>"""

		action_results = [
			ActionResult(
				extracted_content=docx_content,
				long_term_memory='Read file test.docx',
				include_extracted_content_only_once=True,
			)
		]

		step_info = AgentStepInfo(step_number=1, max_steps=10)
		mm._update_agent_history_description(model_output=None, result=action_results, step_info=step_info)

		# Verify it's in read_state_description
		assert 'Title' in mm.state.read_state_description
		assert 'Some content' in mm.state.read_state_description


class TestEndToEndIntegration:
	"""End-to-end tests for file reading and LLM message creation."""

	def create_test_image(self) -> bytes:
		"""Create a test image."""
		img = Image.new('RGB', (50, 50), color='blue')
		buffer = io.BytesIO()
		img.save(buffer, format='PNG')
		buffer.seek(0)
		return buffer.read()

	@pytest.mark.asyncio
	async def test_image_end_to_end(self, tmp_path: Path):
		"""Test complete flow: external image → FileSystem → ActionResult → MessageManager → Prompt."""
		# Step 1: Create external image
		external_file = tmp_path / 'photo.png'
		img_bytes = self.create_test_image()
		external_file.write_bytes(img_bytes)

		# Step 2: Read via FileSystem
		fs = FileSystem(tmp_path / 'workspace')
		structured_result = await fs.read_file_structured(str(external_file), external_file=True)

		assert structured_result['images'] is not None

		# Step 3: Create ActionResult (simulating tools/service.py)
		action_result = ActionResult(
			extracted_content=structured_result['message'],
			long_term_memory='Read image file photo.png',
			images=structured_result['images'],
			include_extracted_content_only_once=True,
		)

		# Step 4: Process in MessageManager
		system_message = SystemMessage(content='Test system message')
		mm = MessageManager(task='test', system_message=system_message, file_system=fs)
		step_info = AgentStepInfo(step_number=1, max_steps=10)
		mm._update_agent_history_description(model_output=None, result=[action_result], step_info=step_info)

		# Verify images stored
		assert len(mm.state.read_state_images) == 1
		assert mm.state.read_state_images[0]['name'] == 'photo.png'

		# Step 5: Create message with AgentMessagePrompt
		browser_state = BrowserStateSummary(
			url='https://example.com',
			title='Test',
			tabs=[TabInfo(target_id='test-0', url='https://example.com', title='Test')],
			screenshot=None,
			dom_state=SerializedDOMState(_root=None, selector_map={}),
		)

		prompt = AgentMessagePrompt(
			browser_state_summary=browser_state,
			file_system=fs,
			read_state_images=mm.state.read_state_images,
		)

		user_message = prompt.get_user_message(use_vision=True)

		# Verify image is in message
		assert isinstance(user_message.content, list)
		image_parts = [part for part in user_message.content if isinstance(part, ContentPartImageParam)]
		assert len(image_parts) >= 1

		# Verify image data is correct
		base64_str = base64.b64encode(img_bytes).decode('utf-8')
		assert base64_str in image_parts[0].image_url.url


class TestPageTransitionContext:
	"""Test that MessageManager injects explicit page-update context across steps."""

	def _browser_state(self, *, url: str, title: str) -> BrowserStateSummary:
		return BrowserStateSummary(
			url=url,
			title=title,
			tabs=[TabInfo(target_id='test-0', url=url, title=title)],
			screenshot=None,
			dom_state=SerializedDOMState(_root=None, selector_map={}),
		)

	def test_prepare_step_state_injects_page_update_note_on_transition(self, tmp_path: Path):
		fs = FileSystem(tmp_path)
		mm = MessageManager(task='test', system_message=SystemMessage(content='System'), file_system=fs)

		page_one = self._browser_state(url='https://example.com/apply/1', title='Personal Info')
		page_two = self._browser_state(url='https://example.com/apply/2', title='Experience')

		mm.prepare_step_state(page_one, result=[], step_info=AgentStepInfo(step_number=1, max_steps=10))
		assert 'PAGE UPDATE:' not in mm.state.read_state_description

		mm.prepare_step_state(page_two, result=[], step_info=AgentStepInfo(step_number=2, max_steps=10))

		assert 'PAGE UPDATE:' in mm.state.read_state_description
		assert 'Current page: Experience | https://example.com/apply/2' in mm.state.read_state_description
		assert any(
			item.system_message and 'PAGE UPDATE:' in item.system_message for item in mm.state.agent_history_items
		)

	def test_prepare_step_state_does_not_inject_page_update_note_on_same_page(self, tmp_path: Path):
		fs = FileSystem(tmp_path)
		mm = MessageManager(task='test', system_message=SystemMessage(content='System'), file_system=fs)

		page_one = self._browser_state(url='https://example.com/apply/1', title='Personal Info')

		mm.prepare_step_state(page_one, result=[], step_info=AgentStepInfo(step_number=1, max_steps=10))
		mm.prepare_step_state(page_one, result=[], step_info=AgentStepInfo(step_number=2, max_steps=10))

		assert 'PAGE UPDATE:' not in mm.state.read_state_description
		assert not any(
			item.system_message and 'PAGE UPDATE:' in item.system_message for item in mm.state.agent_history_items
		)

	def test_domhand_assess_state_does_not_inject_directive(self, tmp_path: Path):
		"""DomHand tools are informational only — no directives injected into agent context."""
		fs = FileSystem(tmp_path)
		mm = MessageManager(task='test', system_message=SystemMessage(content='System'), file_system=fs)

		result = [
			ActionResult(
				extracted_content='DomHand assess_state: state=advanceable; advance_allowed=yes.',
				include_extracted_content_only_once=True,
				metadata={
					'tool': 'domhand_assess_state',
					'application_state_json': json.dumps({'advance_allowed': True}),
				},
			)
		]

		mm._update_agent_history_description(
			model_output=None,
			result=result,
			step_info=AgentStepInfo(step_number=1, max_steps=10),
		)

		assert 'ADVANCE NOW:' not in mm.state.read_state_description
		assert 'REVIEW CURRENT PAGE:' not in mm.state.read_state_description

	def test_same_page_advance_guard_does_not_inject_directive(self, tmp_path: Path):
		"""Same-page guards do not inject directive text into agent context."""
		fs = FileSystem(tmp_path)
		mm = MessageManager(task='test', system_message=SystemMessage(content='System'), file_system=fs)

		result = [
			ActionResult(
				error='DomHand: page already assessed as advance_allowed=yes; broad fill already completed.',
				include_extracted_content_only_once=True,
				metadata={
					'tool': 'domhand_fill',
					'same_page_advance_guard': True,
				},
			)
		]

		mm._update_agent_history_description(
			model_output=None,
			result=result,
			step_info=AgentStepInfo(step_number=1, max_steps=10),
		)

		assert 'ADVANCE NOW:' not in mm.state.read_state_description

	def test_assess_state_blocked_page_does_not_inject_directive(self, tmp_path: Path):
		"""Blocked-page assess_state does not inject REVIEW CURRENT PAGE directives."""
		fs = FileSystem(tmp_path)
		mm = MessageManager(task='test', system_message=SystemMessage(content='System'), file_system=fs)

		result = [
			ActionResult(
				extracted_content='DomHand assess_state: state=blocked; advance_allowed=no.',
				include_extracted_content_only_once=True,
				metadata={
					'tool': 'domhand_assess_state',
					'application_state_json': json.dumps(
						{
							'advance_allowed': False,
							'unresolved_required_count': 2,
							'optional_validation_count': 0,
							'visible_error_count': 1,
						}
					),
				},
			)
		]

		mm._update_agent_history_description(
			model_output=None,
			result=result,
			step_info=AgentStepInfo(step_number=1, max_steps=10),
		)

		assert 'REVIEW CURRENT PAGE:' not in mm.state.read_state_description
		assert 'ADVANCE NOW:' not in mm.state.read_state_description

	@pytest.mark.asyncio
	async def test_docx_end_to_end(self, tmp_path: Path):
		"""Test complete flow: DOCX file → FileSystem → ActionResult → MessageManager."""
		# Step 1: Create DOCX
		fs = FileSystem(tmp_path)
		docx_content = """# Important Document
This is critical information."""

		await fs.write_file('important.docx', docx_content)

		# Step 2: Read it
		read_result = await fs.read_file('important.docx')

		# Step 3: Create ActionResult (simulating tools/service.py)
		action_result = ActionResult(
			extracted_content=read_result,
			long_term_memory=read_result[:100] if len(read_result) > 100 else read_result,
			include_extracted_content_only_once=True,
		)

		# Step 4: Process in MessageManager
		system_message = SystemMessage(content='Test system message')
		mm = MessageManager(task='test', system_message=system_message, file_system=fs)
		step_info = AgentStepInfo(step_number=1, max_steps=10)
		mm._update_agent_history_description(model_output=None, result=[action_result], step_info=step_info)

		# Verify content is in read_state
		assert 'Important Document' in mm.state.read_state_description
		assert 'critical information' in mm.state.read_state_description


if __name__ == '__main__':
	pytest.main([__file__, '-v'])
