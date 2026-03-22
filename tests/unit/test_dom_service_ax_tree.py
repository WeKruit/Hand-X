"""Regression tests for AX-tree collection under early frame churn."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from browser_use.dom.service import DomService


def _make_dom_service(frame_tree_result, ax_tree_side_effect):
	get_frame_tree = AsyncMock(
		side_effect=frame_tree_result if isinstance(frame_tree_result, Exception) else None,
		return_value=None if isinstance(frame_tree_result, Exception) else frame_tree_result,
	)
	browser_session = SimpleNamespace()
	browser_session.get_or_create_cdp_session = AsyncMock(
		return_value=SimpleNamespace(
			session_id='session-1',
			cdp_client=SimpleNamespace(
				send=SimpleNamespace(
					Page=SimpleNamespace(getFrameTree=get_frame_tree),
					Accessibility=SimpleNamespace(getFullAXTree=AsyncMock(side_effect=ax_tree_side_effect)),
				),
			),
		),
	)
	logger = logging.getLogger('browser_use.dom.service.test')
	return DomService(browser_session, logger=logger)


@pytest.mark.asyncio
async def test_get_ax_tree_skips_missing_frame_and_keeps_partial_data(caplog):
	frame_tree_result = {
		'frameTree': {
			'frame': {'id': 'root-frame'},
			'childFrames': [
				{'frame': {'id': 'stable-child'}},
				{'frame': {'id': 'missing-child'}},
			],
		}
	}

	async def get_full_ax_tree(*, params, session_id):
		frame_id = params['frameId']
		if frame_id == 'missing-child':
			raise RuntimeError("{'code': -32602, 'message': 'Frame with the given frameId is not found.'}")
		return {'nodes': [{'backendDOMNodeId': 1 if frame_id == 'root-frame' else 2}]}

	service = _make_dom_service(frame_tree_result, get_full_ax_tree)
	caplog.set_level(logging.WARNING, logger='browser_use.dom.service.test')

	result = await service._get_ax_tree_for_all_frames('target-1')

	assert result == {'nodes': [{'backendDOMNodeId': 1}, {'backendDOMNodeId': 2}]}
	assert 'Skipping frame missing-child' in caplog.text
	assert 'Partial accessibility tree collected for target target-1' in caplog.text


@pytest.mark.asyncio
async def test_get_ax_tree_returns_empty_when_frame_tree_disappears(caplog):
	async def get_full_ax_tree(*, params, session_id):
		return {'nodes': [{'backendDOMNodeId': 1}]}

	service = _make_dom_service(
		RuntimeError("{'code': -32602, 'message': 'Frame with the given frameId is not found.'}"),
		get_full_ax_tree,
	)
	caplog.set_level(logging.WARNING, logger='browser_use.dom.service.test')

	result = await service._get_ax_tree_for_all_frames('target-2')

	assert result == {'nodes': []}
	assert 'Failed to read frame tree for target target-2' in caplog.text
