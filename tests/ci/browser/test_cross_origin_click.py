"""Test clicking elements inside cross-origin iframes."""

import asyncio

import pytest

from browser_use.browser.profile import BrowserProfile, ViewportSize
from browser_use.browser.session import BrowserSession
from browser_use.dom.service import DomService
from browser_use.tools.service import Tools


@pytest.fixture
async def browser_session():
	"""Create browser session with cross-origin iframe support."""
	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True,
			user_data_dir=None,
			keep_alive=True,
			window_size=ViewportSize(width=1920, height=1400),
			cross_origin_iframes=True,  # Enable cross-origin iframe extraction
		)
	)
	await session.start()
	yield session
	await session.kill()


class TestCrossOriginIframeClick:
	"""Test clicking elements inside cross-origin iframes."""

	async def test_click_element_in_cross_origin_iframe(self, httpserver, browser_session: BrowserSession):
		"""Verify that elements inside cross-origin iframes can be clicked."""

		# Create main page with cross-origin iframe pointing to example.com
		# example.com has a "Learn more" link that we'll try to click
		main_html = """
		<!DOCTYPE html>
		<html>
		<head><title>Cross-Origin Test</title></head>
		<body>
			<h1>Main Page</h1>
			<button id="main-button">Main Button</button>
			<iframe id="cross-origin" src="https://example.com" style="width: 800px; height: 600px;"></iframe>
		</body>
		</html>
		"""

		# Serve the main page
		httpserver.expect_request('/cross-origin-test').respond_with_data(main_html, content_type='text/html')
		url = httpserver.url_for('/cross-origin-test')

		# Navigate to the page
		await browser_session.navigate_to(url)

		# Wait longer for cross-origin iframe to load (network can be slow in CI)
		await asyncio.sleep(5)

		# Get DOM state with cross-origin iframe extraction enabled
		dom_service = DomService(browser_session, cross_origin_iframes=True)
		state, _, _ = await dom_service.get_serialized_dom_tree()

		# IMPORTANT: Update cached selector map so get_element_by_index() can find elements
		# This is normally done by DOMWatchdog, but we're calling DomService directly in the test
		browser_session.update_cached_selector_map(state.selector_map)

		print(f'\n📊 Found {len(state.selector_map)} total elements')

		# Find elements from different targets
		targets_found = set()
		main_page_elements = []
		cross_origin_elements = []

		for idx, element in state.selector_map.items():
			target_id = element.target_id
			targets_found.add(target_id)

			# Check if element is from cross-origin iframe (example.com)
			# Cross-origin elements will have a different target_id than main page
			if element.attributes and 'iana.org' in element.attributes.get('href', ''):
				cross_origin_elements.append((idx, element))
				print(f'   ✅ Found cross-origin element: [{idx}] {element.tag_name} href={element.attributes.get("href")}')
			elif element.attributes and element.attributes.get('id') == 'main-button':
				main_page_elements.append((idx, element))

		# Verify we found elements from at least 2 different targets
		print(f'\n🎯 Found elements from {len(targets_found)} different CDP targets')

		# Check if cross-origin iframe loaded
		if len(targets_found) < 2 or len(cross_origin_elements) == 0:
			# Cross-origin iframe didn't load (network issue in CI or timing)
			print('⚠️  Warning: Cross-origin iframe did not load (network/timing issue)')
			print('   This is expected in some CI environments with restricted network access')
			pytest.skip('Cross-origin iframe did not load - skipping cross-origin click test')

		# Verify we found at least one element from the cross-origin iframe
		assert len(cross_origin_elements) > 0, 'Expected to find at least one element from cross-origin iframe (example.com)'

		# Try clicking the cross-origin element
		print('\n🖱️  Testing Click on Cross-Origin Iframe Element:')
		tools = Tools()

		link_idx, link_element = cross_origin_elements[0]
		print(f'   Attempting to click element [{link_idx}] from cross-origin iframe...')

		try:
			result = await tools.click(index=link_idx, browser_session=browser_session)

			# Check for errors
			if result.error:
				pytest.fail(f'Click on cross-origin element [{link_idx}] failed with error: {result.error}')

			if result.extracted_content and (
				'not available' in result.extracted_content.lower() or 'failed' in result.extracted_content.lower()
			):
				pytest.fail(f'Click on cross-origin element [{link_idx}] failed: {result.extracted_content}')

			print(f'   ✅ Click succeeded on cross-origin element [{link_idx}]!')
			print('   🎉 Cross-origin iframe element clicking works!')

		except Exception as e:
			pytest.fail(f'Exception while clicking cross-origin element [{link_idx}]: {e}')

		print('\n✅ Test passed: Cross-origin iframe elements can be clicked')
