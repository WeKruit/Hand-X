"""Regression tests for upload confirmation heuristics."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ghosthands.actions.views import DomHandUploadParams
from ghosthands.actions.domhand_upload import domhand_upload
from ghosthands.actions.domhand_upload import _body_text_indicates_upload_confirmation


def test_body_text_confirmation_matches_uploaded_filename():
	assert _body_text_indicates_upload_confirmation(
		'Safely uploaded file: Ringo Chen_Resume.pdf',
		'Ringo Chen_Resume.pdf',
	)


def test_body_text_confirmation_matches_success_message_without_filename():
	assert _body_text_indicates_upload_confirmation(
		'Successfully Uploaded! You can continue to the next step.',
		'resume.pdf',
	)


def test_body_text_confirmation_rejects_unrelated_text():
	assert not _body_text_indicates_upload_confirmation(
		'Accepted file types: PDF, DOC, DOCX',
		'resume.pdf',
	)


@pytest.mark.asyncio
async def test_domhand_upload_logs_missing_file_path_details():
	node = SimpleNamespace(
		tag_name='button',
		attributes={},
		ax_node=SimpleNamespace(name='Attach Resume'),
		parent_node=None,
		children_nodes=[],
		absolute_position=None,
	)
	file_input = SimpleNamespace(
		tag_name='input',
		attributes={'type': 'file'},
		ax_node=SimpleNamespace(name='Attach Resume'),
		parent_node=None,
		children_nodes=[],
		absolute_position=None,
	)
	node.parent_node = SimpleNamespace(children_nodes=[node, file_input])

	browser_session = AsyncMock()
	browser_session.get_current_page = AsyncMock(return_value=AsyncMock())
	browser_session.get_element_by_index = AsyncMock(return_value=node)
	browser_session.is_file_input = lambda candidate: candidate is file_input

	with (
		patch.dict('os.environ', {}, clear=True),
		patch('ghosthands.actions.domhand_upload.logger.warning') as warn_log,
	):
		result = await domhand_upload(DomHandUploadParams(index=7, file_type='resume'), browser_session)

	assert 'No resume file path configured' in result.error
	warn_log.assert_called_once()
	assert warn_log.call_args.args[0] == 'domhand.upload_missing_file_path'
	extra = warn_log.call_args.kwargs['extra']
	assert extra['effective_type'] == 'resume'
	assert extra['resume_path_present'] is False
