from __future__ import annotations

from pydantic import Field

from browser_use import BrowserProfile


class HandXBrowserProfile(BrowserProfile):
    aboutblank_loading_logo_enabled: bool = Field(
        default=True,
        description="Show the Hand-X logo in the about:blank loading overlay.",
    )
    aboutblank_loading_min_display_seconds: float = Field(
        default=2.0,
        description="Minimum time to keep the Hand-X about:blank loading overlay visible before navigating away.",
    )
