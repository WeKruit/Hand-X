"""Regression tests for upload confirmation heuristics."""

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
