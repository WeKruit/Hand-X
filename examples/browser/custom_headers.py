"""
Custom HTTP Headers via CDP Events.

Registers a CDP Target.attachedToTarget listener that injects custom
headers on every newly created target (tab / iframe).  The listener only
fires for targets created after registration, so we also apply the headers
to the already-existing focused target with browser.set_extra_headers().

Note: Network.setExtraHTTPHeaders is a full replacement (not additive).

Verified by navigating to https://httpbin.org/headers.
"""

import asyncio
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv

load_dotenv()

from browser_use import Agent, Browser, ChatBrowserUse

CUSTOM_HEADERS = {
	'X-Custom-Auth': 'Bearer my-secret-token',
	'X-Request-Source': 'browser-use-agent',
	'X-Trace-Id': 'example-trace-12345',
}


async def main():
	browser = Browser(headless=False)
	await browser.start()

	# 1. Register a CDP listener so every NEW target gets custom headers.
	#    Same pattern as _setup_proxy_auth() which uses Target.attachedToTarget
	#    to call Fetch.enable on freshly-attached sessions.
	def on_target_attached(event, session_id=None):
		sid = event.get('sessionId') or event.get('session_id') or session_id
		if not sid:
			return

		async def _apply():
			try:
				assert browser._cdp_client_root is not None
				await browser._cdp_client_root.send.Network.enable(session_id=sid)
				await browser._cdp_client_root.send.Network.setExtraHTTPHeaders(
					params={'headers': CUSTOM_HEADERS},  # type: ignore[arg-type]
					session_id=sid,
				)
			except Exception:
				pass  # short-lived targets (workers, temp iframes) may detach

		asyncio.create_task(_apply())

	browser.cdp_client.register.Target.attachedToTarget(on_target_attached)

	# 2. The listener above only fires for future targets, so apply headers
	#    to the already-existing focused target too.
	await browser.set_extra_headers(CUSTOM_HEADERS)

	# You can also call set_extra_headers() at any point to change the
	# headers on a specific target without a listener:
	#
	#   await browser.set_extra_headers({'Authorization': 'Bearer xyz'})
	#   await browser.set_extra_headers({'Authorization': 'Bearer xyz'}, target_id=some_target_id)
	#
	# Keep in mind that setExtraHTTPHeaders is a full replacement – each
	# call overwrites all previously set extra headers on that target.

	# 3. Run the agent – httpbin.org/headers echoes all received HTTP headers
	agent = Agent(
		task=(
			'Go to https://httpbin.org/headers and extract the full JSON response shown on the page. '
			'Look for the custom headers X-Custom-Auth, X-Request-Source, and X-Trace-Id in the output.'
		),
		llm=ChatBrowserUse(),
		browser=browser,
	)

	result = await agent.run()
	print(result.final_result())

	await browser.kill()


if __name__ == '__main__':
	asyncio.run(main())
