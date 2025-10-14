"""Remote execution package for browser-use

This package provides type-safe remote code execution with SSE streaming.

Example:
    from browser_use.remote_execute import remote_execute, SSEEvent, SSEEventType

    @remote_execute(log_level="INFO")
    async def my_task(browser: Browser) -> str:
        page = await browser.get_current_page()
        await page.goto("https://example.com")
        return await page.title()

    result = await my_task()
"""

from browser_use.remote_execute.remote import RemoteExecutionError, remote_execute
from browser_use.remote_execute.views import (
	BrowserCreatedData,
	ErrorData,
	ExecutionResponse,
	LogData,
	ResultData,
	SSEEvent,
	SSEEventType,
)

__all__ = [
	# Main decorator
	'remote_execute',
	'RemoteExecutionError',
	# Event types
	'SSEEvent',
	'SSEEventType',
	# Event data models
	'BrowserCreatedData',
	'LogData',
	'ResultData',
	'ErrorData',
	'ExecutionResponse',
]
