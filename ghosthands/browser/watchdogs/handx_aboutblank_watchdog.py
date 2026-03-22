"""About:blank watchdog for managing about:blank tabs with a loading overlay."""

import json
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import quote

from bubus import BaseEvent
from cdp_use.cdp.target import TargetID
from pydantic import PrivateAttr

from browser_use.browser.events import (
    AboutBlankDVDScreensaverShownEvent,
    BrowserStopEvent,
    BrowserStoppedEvent,
    CloseTabEvent,
    NavigateToUrlEvent,
    TabClosedEvent,
    TabCreatedEvent,
)
from browser_use.browser.watchdog_base import BaseWatchdog

if TYPE_CHECKING:
    pass


_HANDX_LOADING_LOGO_SVG = """
<svg version="1.1" id="Layer_1" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" x="0px" y="0px"
	 width="100%" viewBox="0 0 512 512" enable-background="new 0 0 512 512" xml:space="preserve">
<path fill="#000000" opacity="1.000000" stroke="none" 
	d="
M342.000000,513.000000 
	C228.027695,513.000000 114.555382,513.000000 1.041535,513.000000 
	C1.041535,342.402679 1.041535,171.805328 1.041535,1.103989 
	C171.555649,1.103989 342.111389,1.103989 512.833557,1.103989 
	C512.833557,171.666534 512.833557,342.333252 512.833557,513.000000 
	C456.138641,513.000000 399.319305,513.000000 342.000000,513.000000 
M187.381668,304.684509 
	C186.952957,305.182068 186.573349,305.734283 186.088562,306.169342 
	C157.396500,331.917786 157.280685,368.894623 188.101456,398.904022 
	C187.430771,386.360107 191.308929,375.281647 200.240189,367.256348 
	C207.024231,361.160492 215.374268,356.475220 223.646255,352.426422 
	C255.263748,336.951080 287.133667,321.991516 318.904633,306.829590 
	C329.556030,301.746429 336.721100,293.306427 342.333923,283.134308 
	C352.410767,264.872040 347.237671,245.186432 348.981201,225.082321 
	C339.865173,229.489410 331.834076,233.351120 323.820770,237.249496 
	C281.392944,257.890137 238.971298,278.543488 196.540939,299.178925 
	C193.704239,300.558502 190.813339,301.826630 188.064178,302.258667 
	C187.972763,290.112762 191.981888,279.147095 201.277878,271.679413 
	C209.433594,265.127747 219.018448,260.116821 228.491043,255.487244 
	C257.273834,241.420181 286.348358,227.951492 315.230896,214.086502 
	C336.696106,203.782150 348.009247,186.644180 348.591034,162.752289 
	C348.847534,152.219772 348.634247,141.675812 348.634247,130.666687 
	C347.294128,131.228561 346.694855,131.446472 346.124420,131.723724 
	C297.265625,155.470200 248.328415,179.058334 199.641068,203.151337 
	C192.503998,206.683121 185.629211,211.761917 180.234619,217.603836 
	C165.145096,233.944626 160.823898,253.347229 168.102997,274.523010 
	C171.975220,285.787750 179.266205,295.235687 187.381668,304.684509 
M187.927429,173.365021 
	C188.725067,174.627197 189.458145,176.927765 190.330902,176.982040 
	C195.747330,177.318893 201.195114,177.151566 207.006561,177.151566 
	C207.006561,167.500916 207.006561,158.421356 207.006561,148.682770 
	C214.477783,152.685608 221.466751,156.342957 229.715912,156.174194 
	C229.715912,149.807098 229.715912,143.890533 229.715912,137.580154 
	C215.532562,135.915573 210.391022,125.398277 206.575806,113.720055 
	C201.068405,113.720055 195.904419,113.560883 190.763626,113.835693 
	C189.529770,113.901657 187.728012,115.156258 187.281174,116.293831 
	C183.103271,126.929810 177.450562,135.956497 165.338058,138.084579 
	C165.338058,144.309921 165.338058,150.195404 165.338058,157.544250 
	C173.382141,154.782730 180.607315,152.302338 187.927063,149.789474 
	C187.927063,157.103241 187.927063,164.747421 187.927429,173.365021 
M324.018280,366.380707 
	C327.406586,381.089966 335.460236,392.269440 348.857971,400.124084 
	C348.857971,370.104004 348.857971,340.873535 348.857971,311.542816 
	C332.565460,319.155273 318.844116,341.786011 324.018280,366.380707 
z"/>
<path fill="#FDFDFD" opacity="1.000000" stroke="none" 
	d="
M187.947662,303.146606 
	C190.813339,301.826630 193.704239,300.558502 196.540939,299.178925 
	C238.971298,278.543488 281.392944,257.890137 323.820770,237.249496 
	C331.834076,233.351120 339.865173,229.489410 348.981201,225.082321 
	C347.237671,245.186432 352.410767,264.872040 342.333923,283.134308 
	C336.721100,293.306427 329.556030,301.746429 318.904633,306.829590 
	C287.133667,321.991516 255.263748,336.951080 223.646255,352.426422 
	C215.374268,356.475220 207.024231,361.160492 200.240189,367.256348 
	C191.308929,375.281647 187.430771,386.360107 188.101456,398.904022 
	C157.280685,368.894623 157.396500,331.917786 186.088562,306.169342 
	C186.573349,305.734283 186.952957,305.182068 187.509888,304.216492 
	C187.801407,303.578766 187.904587,303.378174 187.947662,303.146606 
z"/>
<path fill="#FEFEFE" opacity="1.000000" stroke="none" 
	d="
M188.005920,302.702637 
	C187.904587,303.378174 187.801407,303.578766 187.500809,303.850616 
	C179.266205,295.235687 171.975220,285.787750 168.102997,274.523010 
	C160.823898,253.347229 165.145096,233.944626 180.234619,217.603836 
	C185.629211,211.761917 192.503998,206.683121 199.641068,203.151337 
	C248.328415,179.058334 297.265625,155.470200 346.124420,131.723724 
	C346.694855,131.446472 347.294128,131.228561 348.634247,130.666687 
	C348.634247,141.675812 348.847534,152.219772 348.591034,162.752289 
	C348.009247,186.644180 336.696106,203.782150 315.230896,214.086502 
	C286.348358,227.951492 257.273834,241.420181 228.491043,255.487244 
	C219.018448,260.116821 209.433594,265.127747 201.277878,271.679413 
	C191.981888,279.147095 187.972763,290.112762 188.005920,302.702637 
z"/>
<path fill="#0069FD" opacity="1.000000" stroke="none" 
	d="
M187.927246,172.878311 
	C187.927063,164.747421 187.927063,157.103241 187.927063,149.789474 
	C180.607315,152.302338 173.382141,154.782730 165.338058,157.544250 
	C165.338058,150.195404 165.338058,144.309921 165.338058,138.084579 
	C177.450562,135.956497 183.103271,126.929810 187.281174,116.293831 
	C187.728012,115.156258 189.529770,113.901657 190.763626,113.835693 
	C195.904419,113.560883 201.068405,113.720055 206.575806,113.720055 
	C210.391022,125.398277 215.532562,135.915573 229.715912,137.580154 
	C229.715912,143.890533 229.715912,149.807098 229.715912,156.174194 
	C221.466751,156.342957 214.477783,152.685608 207.006561,148.682770 
	C207.006561,158.421356 207.006561,167.500916 207.006561,177.151566 
	C201.195114,177.151566 195.747330,177.318893 190.330902,176.982040 
	C189.458145,176.927765 188.725067,174.627197 187.927246,172.878311 
z"/>
<path fill="#FDFDFD" opacity="1.000000" stroke="none" 
	d="
M323.968872,365.955383 
	C318.844116,341.786011 332.565460,319.155273 348.857971,311.542816 
	C348.857971,340.873535 348.857971,370.104004 348.857971,400.124084 
	C335.460236,392.269440 327.406586,381.089966 323.968872,365.955383 
z"/>
</svg>
""".strip()
_HANDX_LOADING_LOGO_DATA_URI = "data:image/svg+xml;charset=utf-8," + quote(_HANDX_LOADING_LOGO_SVG)


def _build_loading_overlay_script(browser_session_label: str, show_logo: bool) -> str:
    """Build the about:blank loading overlay script.

    Hand-X can show either the branded loading logo or a neutral loading ring.
    """
    content_block = """
			const ring = document.createElement('div');
			ring.className = 'handx-loading-ring';
			shell.appendChild(ring);

			const title = document.createElement('div');
			title.className = 'handx-loading-title';
			title.textContent = 'VALET';
			shell.appendChild(title);

			const subtitle = document.createElement('div');
			subtitle.className = 'handx-loading-subtitle';
			subtitle.textContent = 'WeKruit - VALET';
			shell.appendChild(subtitle);

			const status = document.createElement('div');
			status.className = 'handx-loading-status';
			status.textContent = 'Loading secure browser session...';
			shell.appendChild(status);
		"""
    if show_logo:
        content_block = f"""
				const img = document.createElement('img');
				img.className = 'handx-loading-logo';
				img.src = {json.dumps(_HANDX_LOADING_LOGO_DATA_URI)};
				img.alt = 'VALET';
				shell.appendChild(img);

				const title = document.createElement('div');
				title.className = 'handx-loading-title';
				title.textContent = 'VALET';
				shell.appendChild(title);

				const subtitle = document.createElement('div');
				subtitle.className = 'handx-loading-subtitle';
				subtitle.textContent = 'WeKruit - VALET';
				shell.appendChild(subtitle);

				const status = document.createElement('div');
				status.className = 'handx-loading-status';
				status.textContent = 'Loading secure browser session...';
				shell.appendChild(status);
			"""
    return f"""
		(function(browser_session_label) {{
			if (window.__dvdAnimationRunning) {{
				return;
			}}
			window.__dvdAnimationRunning = true;

			if (!document.body) {{
				window.__dvdAnimationRunning = false;
				if (document.readyState === 'loading') {{
					document.addEventListener('DOMContentLoaded', () => arguments.callee(browser_session_label));
				}}
				return;
			}}

			const animatedTitle = `Starting VALET ${{browser_session_label}}...`;
			if (document.title === animatedTitle) {{
				return;
			}}
			document.title = animatedTitle;

			const loadingOverlay = document.createElement('div');
			loadingOverlay.id = 'pretty-loading-animation';
			loadingOverlay.style.position = 'fixed';
			loadingOverlay.style.top = '0';
			loadingOverlay.style.left = '0';
			loadingOverlay.style.width = '100vw';
			loadingOverlay.style.height = '100vh';
			loadingOverlay.style.background = 'radial-gradient(circle at top, rgba(37,99,235,0.18), transparent 35%), linear-gradient(180deg, #020617 0%, #0f172a 100%)';
			loadingOverlay.style.zIndex = '99999';
			loadingOverlay.style.overflow = 'hidden';

			const style = document.createElement('style');
			style.textContent = `
				#pretty-loading-animation .handx-loading-shell {{
					position: absolute;
					left: 50%;
					top: 50%;
					transform: translate(-50%, -50%);
					display: flex;
					flex-direction: column;
					align-items: center;
					gap: 14px;
					color: #e2e8f0;
					font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
					text-align: center;
					pointer-events: none;
				}}
				#pretty-loading-animation .handx-loading-logo {{
					width: 92px;
					height: 92px;
					object-fit: contain;
					filter: drop-shadow(0 18px 42px rgba(37, 99, 235, 0.25));
				}}
				#pretty-loading-animation .handx-loading-ring {{
					position: relative;
					width: 72px;
					height: 72px;
					border-radius: 999px;
					border: 2px solid rgba(96, 165, 250, 0.78);
					box-shadow: 0 0 0 10px rgba(96, 165, 250, 0.12), 0 18px 50px rgba(2, 6, 23, 0.45);
					animation: handx-loading-pulse 1.6s ease-in-out infinite;
				}}
				#pretty-loading-animation .handx-loading-ring::after {{
					content: "";
					position: absolute;
					inset: 21px;
					border-radius: 999px;
					background: linear-gradient(135deg, #60a5fa, #2563eb);
					box-shadow: 0 0 20px rgba(59, 130, 246, 0.45);
				}}
				#pretty-loading-animation .handx-loading-title {{
					font-size: 18px;
					font-weight: 700;
					letter-spacing: 0.02em;
				}}
				#pretty-loading-animation .handx-loading-subtitle {{
					font-size: 12px;
					letter-spacing: 0.08em;
					text-transform: uppercase;
					color: rgba(226, 232, 240, 0.72);
				}}
				#pretty-loading-animation .handx-loading-status {{
					font-size: 13px;
					font-weight: 600;
					color: rgba(191, 219, 254, 0.92);
					letter-spacing: 0.04em;
					animation: handx-loading-breathe 2s ease-in-out infinite;
				}}
				#pretty-loading-animation img {{
					user-select: none;
					pointer-events: none;
				}}
				@keyframes handx-loading-pulse {{
					0%, 100% {{
						transform: scale(0.96);
						box-shadow: 0 0 0 10px rgba(96, 165, 250, 0.10), 0 18px 50px rgba(2, 6, 23, 0.38);
					}}
					50% {{
						transform: scale(1);
						box-shadow: 0 0 0 14px rgba(96, 165, 250, 0.16), 0 18px 50px rgba(2, 6, 23, 0.48);
					}}
				}}
				@keyframes handx-loading-breathe {{
					0%, 100% {{
						opacity: 0.55;
					}}
					50% {{
						opacity: 1;
					}}
				}}
			`;
			document.head.appendChild(style);

			const shell = document.createElement('div');
			shell.className = 'handx-loading-shell';

			{content_block}

			loadingOverlay.appendChild(shell);
			document.body.appendChild(loadingOverlay);
		}})({json.dumps(browser_session_label)});
	"""


class AboutBlankWatchdog(BaseWatchdog):
    """Ensures there's always exactly one about:blank tab with DVD screensaver."""

    # Event contracts
    LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [
        BrowserStopEvent,
        BrowserStoppedEvent,
        TabCreatedEvent,
        TabClosedEvent,
    ]
    EMITS: ClassVar[list[type[BaseEvent]]] = [
        NavigateToUrlEvent,
        CloseTabEvent,
        AboutBlankDVDScreensaverShownEvent,
    ]

    _stopping: bool = PrivateAttr(default=False)

    async def on_BrowserStopEvent(self, event: BrowserStopEvent) -> None:
        """Handle browser stop request - stop creating new tabs."""
        # logger.info('[AboutBlankWatchdog] Browser stop requested, stopping tab creation')
        self._stopping = True

    async def on_BrowserStoppedEvent(self, event: BrowserStoppedEvent) -> None:
        """Handle browser stopped event."""
        # logger.info('[AboutBlankWatchdog] Browser stopped')
        self._stopping = True

    async def on_TabCreatedEvent(self, event: TabCreatedEvent) -> None:
        """Check tabs when a new tab is created."""
        # logger.debug(f'[AboutBlankWatchdog] ➕ New tab created: {event.url}')

        # If an about:blank tab was created, show DVD screensaver on all about:blank tabs
        if event.url == "about:blank":
            await self._show_dvd_screensaver_on_about_blank_tabs()

    async def on_TabClosedEvent(self, event: TabClosedEvent) -> None:
        """Check tabs when a tab is closed and proactively create about:blank if needed."""
        # Don't create new tabs if browser is shutting down
        if self._stopping:
            return

        # Don't attempt CDP operations if the WebSocket is dead — dispatching
        # NavigateToUrlEvent on a broken connection will hang until timeout
        if not self.browser_session.is_cdp_connected:
            self.logger.debug("[AboutBlankWatchdog] CDP not connected, skipping tab recovery")
            return

        # Check if we're about to close the last tab (event happens BEFORE tab closes)
        # Use _cdp_get_all_pages for quick check without fetching titles
        page_targets = await self.browser_session._cdp_get_all_pages()
        if len(page_targets) < 1:
            self.logger.debug(
                "[AboutBlankWatchdog] Last tab closing, creating new about:blank tab to avoid closing entire browser"
            )
            # Create the animation tab since no tabs should remain
            navigate_event = self.event_bus.dispatch(NavigateToUrlEvent(url="about:blank", new_tab=True))
            await navigate_event
            # Show DVD screensaver on the new tab
            await self._show_dvd_screensaver_on_about_blank_tabs()
        else:
            # Multiple tabs exist, check after close
            await self._check_and_ensure_about_blank_tab()

    async def attach_to_target(self, target_id: TargetID) -> None:
        """AboutBlankWatchdog doesn't monitor individual targets."""
        pass

    async def _check_and_ensure_about_blank_tab(self) -> None:
        """Check current tabs and ensure exactly one about:blank tab with animation exists."""
        try:
            if not self.browser_session.is_cdp_connected:
                return

            # For quick checks, just get page targets without titles to reduce noise
            page_targets = await self.browser_session._cdp_get_all_pages()

            # If no tabs exist at all, create one to keep browser alive
            if len(page_targets) == 0:
                # Only create a new tab if there are no tabs at all
                self.logger.debug("[AboutBlankWatchdog] No tabs exist, creating new about:blank DVD screensaver tab")
                navigate_event = self.event_bus.dispatch(NavigateToUrlEvent(url="about:blank", new_tab=True))
                await navigate_event
                # Show DVD screensaver on the new tab
                await self._show_dvd_screensaver_on_about_blank_tabs()
            # Otherwise there are tabs, don't create new ones to avoid interfering

        except Exception as e:
            self.logger.error(f"[AboutBlankWatchdog] Error ensuring about:blank tab: {e}")

    async def _show_dvd_screensaver_on_about_blank_tabs(self) -> None:
        """Show DVD screensaver on all about:blank pages only."""
        try:
            # Get just the page targets without expensive title fetching
            page_targets = await self.browser_session._cdp_get_all_pages()
            browser_session_label = str(self.browser_session.id)[-4:]

            for page_target in page_targets:
                target_id = page_target["targetId"]
                url = page_target["url"]

                # Only target about:blank pages specifically
                if url == "about:blank":
                    await self._show_dvd_screensaver_loading_animation_cdp(target_id, browser_session_label)

        except Exception as e:
            self.logger.error(f"[AboutBlankWatchdog] Error showing DVD screensaver: {e}")

    async def _show_dvd_screensaver_loading_animation_cdp(
        self, target_id: TargetID, browser_session_label: str
    ) -> None:
        """
        Inject a loading overlay into the target using CDP.

        The legacy logo animation stays available behind a browser-profile flag, but
        Hand-X defaults to a neutral branded loader instead.
        """
        try:
            # Create temporary session for this target without switching focus
            temp_session = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)
            script = _build_loading_overlay_script(
                browser_session_label=browser_session_label,
                show_logo=self.browser_session.browser_profile.aboutblank_loading_logo_enabled,
            )

            await temp_session.cdp_client.send.Runtime.evaluate(
                params={"expression": script}, session_id=temp_session.session_id
            )

            # No need to detach - session is cached

            # Dispatch event
            self.event_bus.dispatch(AboutBlankDVDScreensaverShownEvent(target_id=target_id))

        except Exception as e:
            self.logger.error(f"[AboutBlankWatchdog] Error injecting DVD screensaver: {e}")
