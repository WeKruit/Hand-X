#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from browser_use import Agent, Browser, ChatBrowserUse


TASK = "Go to https://example.com and extract the page title."


async def main() -> None:
    if not os.getenv("BROWSER_USE_API_KEY"):
        raise SystemExit(
            "Set BROWSER_USE_API_KEY before running this script. "
            "ChatBrowserUse requires a Browser Use Cloud API key."
        )

    browser = Browser()

    try:
        agent = Agent(
            task=TASK,
            llm=ChatBrowserUse(),
            browser=browser,
        )
        history = await agent.run(max_steps=10)
    finally:
        await browser.kill()

    print(history.final_result() or history)


if __name__ == "__main__":
    asyncio.run(main())
