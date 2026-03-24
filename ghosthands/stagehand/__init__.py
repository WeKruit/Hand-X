"""Stagehand Layer — middle-tier semantic browser automation between DOM-first fill and browser-use agent.

Provides act() / observe() / extract() via the Stagehand Python SDK, sharing
the same Chrome process as browser-use through CDP WebSocket.

Architecture:
    Layer 0  DOM fill ($0)          — fill_executor / fill_verify
    Layer 1  Stagehand (~$0.002)    — this module
    Layer 2  browser-use agent      — full vision + reasoning fallback

Stagehand is initialized lazily on first escalation so happy-path DOM fills
pay zero overhead.  All calls are wrapped in try/except — failures degrade
gracefully to Layer 2.
"""

from ghosthands.stagehand.layer import StagehandLayer, get_stagehand_layer

__all__ = [
    "StagehandLayer",
    "get_stagehand_layer",
]
