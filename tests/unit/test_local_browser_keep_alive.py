import asyncio
import logging
import os
import subprocess as stdlib_subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession
from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog


def _build_watchdog(*, keep_alive: bool) -> LocalBrowserWatchdog:
    browser_session = BrowserSession(
        browser_profile=BrowserProfile(
            keep_alive=keep_alive,
            user_data_dir="/tmp/browser-profile",
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ),
    )
    browser_session._logger = logging.getLogger("test-local-browser-watchdog")
    return LocalBrowserWatchdog(event_bus=browser_session.event_bus, browser_session=browser_session)


@patch(
    "browser_use.browser.watchdogs.local_browser_watchdog.asyncio.create_subprocess_exec",
    new_callable=AsyncMock,
)
@patch("browser_use.browser.watchdogs.local_browser_watchdog.psutil.Process")
async def test_local_browser_watchdog_detaches_keep_alive_launch(
    process_mock,
    create_subprocess_exec_mock: AsyncMock,
) -> None:
    watchdog = _build_watchdog(keep_alive=True)
    create_subprocess_exec_mock.return_value = SimpleNamespace(pid=1234)
    process_mock.return_value = SimpleNamespace(pid=1234)
    watchdog._find_free_port = lambda: 9222
    watchdog._wait_for_cdp_url = AsyncMock(return_value="http://127.0.0.1:9222")

    process, cdp_url = await watchdog._launch_browser(max_retries=1)

    assert process.pid == 1234
    assert cdp_url == "http://127.0.0.1:9222"
    _, kwargs = create_subprocess_exec_mock.await_args
    assert kwargs["stdin"] == stdlib_subprocess.DEVNULL
    assert kwargs["stdout"] == stdlib_subprocess.DEVNULL
    assert kwargs["stderr"] == stdlib_subprocess.DEVNULL
    if os.name == "nt":
        assert "creationflags" in kwargs
    else:
        assert kwargs["start_new_session"] is True


@patch(
    "browser_use.browser.watchdogs.local_browser_watchdog.asyncio.create_subprocess_exec",
    new_callable=AsyncMock,
)
@patch("browser_use.browser.watchdogs.local_browser_watchdog.psutil.Process")
async def test_local_browser_watchdog_keeps_piped_launch_when_not_keep_alive(
    process_mock,
    create_subprocess_exec_mock: AsyncMock,
) -> None:
    watchdog = _build_watchdog(keep_alive=False)
    create_subprocess_exec_mock.return_value = SimpleNamespace(pid=5678)
    process_mock.return_value = SimpleNamespace(pid=5678)
    watchdog._find_free_port = lambda: 9333
    watchdog._wait_for_cdp_url = AsyncMock(return_value="http://127.0.0.1:9333")

    process, cdp_url = await watchdog._launch_browser(max_retries=1)

    assert process.pid == 5678
    assert cdp_url == "http://127.0.0.1:9333"
    _, kwargs = create_subprocess_exec_mock.await_args
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.PIPE
    assert "stdin" not in kwargs
    assert "start_new_session" not in kwargs
    assert "creationflags" not in kwargs
