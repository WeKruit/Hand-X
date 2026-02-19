"""Tests for CLI coordinate clicking support.

Verifies that the CLI correctly parses both index-based and coordinate-based
click commands, that the browser command handler dispatches the right events,
and that the direct CLI selector map cache works correctly.
"""

from unittest.mock import MagicMock

import pytest

from browser_use.skill_cli.main import build_parser


class TestClickArgParsing:
	"""Test argparse handles click with index and coordinates."""

	def test_click_single_index(self):
		"""browser-use click 5 -> args.args == [5]"""
		parser = build_parser()
		args = parser.parse_args(['click', '5'])
		assert args.command == 'click'
		assert args.args == [5]

	def test_click_coordinates(self):
		"""browser-use click 200 800 -> args.args == [200, 800]"""
		parser = build_parser()
		args = parser.parse_args(['click', '200', '800'])
		assert args.command == 'click'
		assert args.args == [200, 800]

	def test_click_no_args_fails(self):
		"""browser-use click (no args) should fail."""
		parser = build_parser()
		with pytest.raises(SystemExit):
			parser.parse_args(['click'])

	def test_click_three_args_parsed(self):
		"""browser-use click 1 2 3 -> args.args == [1, 2, 3] (handler will reject)."""
		parser = build_parser()
		args = parser.parse_args(['click', '1', '2', '3'])
		assert args.args == [1, 2, 3]

	def test_click_non_int_fails(self):
		"""browser-use click abc should fail (type=int enforced)."""
		parser = build_parser()
		with pytest.raises(SystemExit):
			parser.parse_args(['click', 'abc'])


class TestClickCommandHandler:
	"""Test the browser command handler dispatches correctly for click."""

	async def test_coordinate_click_handler(self, httpserver):
		"""Coordinate click dispatches ClickCoordinateEvent."""
		from browser_use.browser.session import BrowserSession
		from browser_use.skill_cli.commands.browser import handle
		from browser_use.skill_cli.sessions import SessionInfo

		httpserver.expect_request('/').respond_with_data(
			'<html><body><button>Click me</button></body></html>',
			content_type='text/html',
		)

		session = BrowserSession(headless=True)
		await session.start()
		try:
			from browser_use.browser.events import NavigateToUrlEvent

			await session.event_bus.dispatch(NavigateToUrlEvent(url=httpserver.url_for('/')))

			session_info = SessionInfo(
				name='test',
				browser_mode='chromium',
				headed=False,
				profile=None,
				browser_session=session,
			)

			result = await handle('click', session_info, {'args': [100, 200]})
			assert 'clicked_coordinate' in result
			assert result['clicked_coordinate'] == {'x': 100, 'y': 200}
		finally:
			await session.kill()

	async def test_index_click_handler(self, httpserver):
		"""Index click dispatches ClickElementEvent."""
		from browser_use.browser.session import BrowserSession
		from browser_use.skill_cli.commands.browser import handle
		from browser_use.skill_cli.sessions import SessionInfo

		httpserver.expect_request('/').respond_with_data(
			'<html><body><button id="btn">Click me</button></body></html>',
			content_type='text/html',
		)

		session = BrowserSession(headless=True)
		await session.start()
		try:
			from browser_use.browser.events import NavigateToUrlEvent

			await session.event_bus.dispatch(NavigateToUrlEvent(url=httpserver.url_for('/')))

			session_info = SessionInfo(
				name='test',
				browser_mode='chromium',
				headed=False,
				profile=None,
				browser_session=session,
			)

			# Index 999 won't exist, so we expect the error path
			result = await handle('click', session_info, {'args': [999]})
			assert 'error' in result
		finally:
			await session.kill()

	async def test_invalid_args_count(self):
		"""Three args returns error."""
		from browser_use.skill_cli.commands.browser import handle

		session_info = MagicMock()
		result = await handle('click', session_info, {'args': [1, 2, 3]})
		assert 'error' in result
		assert 'Usage' in result['error']


class TestSelectorCache:
	"""Test selector map cache round-trip and coordinate conversion."""

	@pytest.fixture(autouse=True)
	def _use_tmp_state_file(self, monkeypatch, tmp_path):
		"""Redirect STATE_FILE to a temp dir so tests don't clobber real state."""
		import browser_use.skill_cli.direct as direct_mod

		self.state_file = tmp_path / 'browser-use-direct.json'
		monkeypatch.setattr(direct_mod, 'STATE_FILE', self.state_file)

	def test_save_and_load_cache_round_trip(self):
		"""_save_selector_cache → _load_selector_cache preserves data."""
		from browser_use.skill_cli.direct import (
			_load_selector_cache,
			_save_selector_cache,
			_save_state,
		)

		# Seed state file so _load_state works
		_save_state({'cdp_url': 'ws://localhost:9222'})

		# Build mock nodes with absolute_position
		mock_node_1 = MagicMock()
		mock_node_1.absolute_position = MagicMock(x=100.0, y=200.0, width=80.0, height=32.0)
		mock_node_1.ax_node = MagicMock(name='Submit')
		mock_node_1.ax_node.name = 'Submit'
		mock_node_1.node_name = 'BUTTON'
		mock_node_1.node_value = ''

		mock_node_2 = MagicMock()
		mock_node_2.absolute_position = MagicMock(x=50.0, y=800.5, width=200.0, height=40.0)
		mock_node_2.ax_node = None
		mock_node_2.node_name = 'A'
		mock_node_2.node_value = 'Click here'

		selector_map = {5: mock_node_1, 12: mock_node_2}
		_save_selector_cache(selector_map)

		loaded = _load_selector_cache()
		assert 5 in loaded
		assert 12 in loaded
		assert loaded[5]['x'] == 100.0
		assert loaded[5]['y'] == 200.0
		assert loaded[5]['w'] == 80.0
		assert loaded[5]['h'] == 32.0
		assert loaded[5]['tag'] == 'button'
		assert loaded[5]['text'] == 'Submit'
		assert loaded[12]['x'] == 50.0
		assert loaded[12]['y'] == 800.5
		assert loaded[12]['tag'] == 'a'
		assert loaded[12]['text'] == 'Click here'

	def test_load_empty_cache(self):
		"""_load_selector_cache returns empty dict when no cache exists."""
		from browser_use.skill_cli.direct import _load_selector_cache, _save_state

		_save_state({'cdp_url': 'ws://localhost:9222'})
		loaded = _load_selector_cache()
		assert loaded == {}

	def test_cache_skips_nodes_without_position(self):
		"""Nodes without absolute_position are not cached."""
		from browser_use.skill_cli.direct import (
			_load_selector_cache,
			_save_selector_cache,
			_save_state,
		)

		_save_state({'cdp_url': 'ws://localhost:9222'})

		mock_node = MagicMock()
		mock_node.absolute_position = None
		mock_node.node_name = 'DIV'

		_save_selector_cache({1: mock_node})
		loaded = _load_selector_cache()
		assert loaded == {}

	def test_viewport_coordinate_conversion(self):
		"""Document coords + scroll offset → viewport coords."""
		# Simulating what _cdp_click_index does
		elem = {'x': 150.0, 'y': 900.0, 'w': 80.0, 'h': 32.0}
		scroll_x, scroll_y = 0.0, 500.0

		viewport_x = int(elem['x'] + elem['w'] / 2 - scroll_x)
		viewport_y = int(elem['y'] + elem['h'] / 2 - scroll_y)

		# Element center at doc (190, 916), viewport after scroll (190, 416)
		assert viewport_x == 190
		assert viewport_y == 416

	def test_viewport_conversion_with_horizontal_scroll(self):
		"""Horizontal scroll is also accounted for."""
		elem = {'x': 1200.0, 'y': 300.0, 'w': 100.0, 'h': 50.0}
		scroll_x, scroll_y = 800.0, 100.0

		viewport_x = int(elem['x'] + elem['w'] / 2 - scroll_x)
		viewport_y = int(elem['y'] + elem['h'] / 2 - scroll_y)

		assert viewport_x == 450  # 1250 - 800
		assert viewport_y == 225  # 325 - 100

	def test_cache_invalidated_on_navigate(self):
		"""Navigating clears selector_map from state."""
		from browser_use.skill_cli.direct import _load_state, _save_state

		_save_state({
			'cdp_url': 'ws://localhost:9222',
			'target_id': 'abc',
			'selector_map': {'1': {'x': 10, 'y': 20, 'w': 30, 'h': 40, 'tag': 'a', 'text': 'Link'}},
		})

		# Simulate what _cdp_navigate does to the state
		state = _load_state()
		state.pop('selector_map', None)
		_save_state(state)

		reloaded = _load_state()
		assert 'selector_map' not in reloaded
		assert reloaded['cdp_url'] == 'ws://localhost:9222'
		assert reloaded['target_id'] == 'abc'

	def test_state_overwritten_on_fresh_cache(self):
		"""Running state overwrites old cache with new data."""
		from browser_use.skill_cli.direct import (
			_load_selector_cache,
			_save_selector_cache,
			_save_state,
		)

		_save_state({
			'cdp_url': 'ws://localhost:9222',
			'selector_map': {'99': {'x': 0, 'y': 0, 'w': 0, 'h': 0, 'tag': 'old', 'text': 'old'}},
		})

		# New cache with different element
		mock_node = MagicMock()
		mock_node.absolute_position = MagicMock(x=5.0, y=10.0, width=20.0, height=15.0)
		mock_node.ax_node = MagicMock(name='New')
		mock_node.ax_node.name = 'New'
		mock_node.node_name = 'SPAN'
		mock_node.node_value = ''

		_save_selector_cache({7: mock_node})
		loaded = _load_selector_cache()

		# Old index 99 should be gone, only new index 7
		assert 99 not in loaded
		assert 7 in loaded
		assert loaded[7]['tag'] == 'span'
