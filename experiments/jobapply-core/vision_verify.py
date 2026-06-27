"""Cheap-VLM visual field verification, registered as a browser-use @tools.action.

The expensive main model (bu-2-0) should NOT burn $3.50/1M reasoning tokens just to
answer "is this field actually filled?" — that's a tiny vision question. This registers
a `verify_field_visually` action that screenshots the page (browser-use CDP) and asks a
SMALL, cheap vision model for a yes/no + the visible value. The agent calls it only when
a field has resisted (state reads empty after retries) — so it's the cheap arbiter of
the retry-then-vision balance.

Model: defaults to gemini-2.5-flash-lite (cheap, needs GOOGLE_API_KEY). To use a local
Qwen2.5-VL-7B instead, point GH_VERIFY_MODEL at it and swap ChatGoogle for a ChatOpenAI
against your vLLM/Ollama OpenAI-compatible endpoint (one-line change in _vlm()).
"""

import base64
import os
from typing import Any

VERIFY_MODEL = os.environ.get("GH_VERIFY_MODEL", "gemini-2.5-flash-lite")


def _vlm() -> Any:
    from browser_use import ChatGoogle  # cheap VLM; swap to ChatOpenAI(base_url=…) for local Qwen2.5-VL

    return ChatGoogle(model=VERIFY_MODEL, api_key=os.environ.get("GOOGLE_API_KEY"))


def register_visual_verify(tools: Any) -> Any:
    """Add the verify_field_visually action onto an existing browser-use Tools object."""
    from browser_use import ActionResult, BrowserSession
    from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

    @tools.action(
        "Visually verify whether a form field is filled, using a cheap vision model. "
        "Call this ONLY after you have typed a field and the browser state still reads it empty: "
        "it screenshots the page and a small VLM reports whether the field is visibly filled. "
        "Trust its answer over the state read-back."
    )
    async def verify_field_visually(field_label: str, browser_session: BrowserSession) -> Any:
        try:
            png = await browser_session.take_screenshot()  # bytes (PNG)
        except Exception as exc:
            return ActionResult(extracted_content=f"visual-verify: screenshot failed ({exc})")
        b64 = base64.b64encode(png).decode()
        prompt = (
            f"This is a job-application web form. Is the field labelled '{field_label}' currently "
            f'filled with a value (not blank)? Reply STRICT JSON: {{"filled": true|false, "value": "<visible text>"}}.'
        )
        msg = UserMessage(
            content=[
                ContentPartTextParam(type="text", text=prompt),
                ContentPartImageParam(
                    type="image_url",
                    image_url=ImageURL(url=f"data:image/png;base64,{b64}", detail="low", media_type="image/png"),
                ),
            ]
        )
        try:
            resp = await _vlm().ainvoke([msg])
            verdict = (getattr(resp, "completion", None) or str(resp)).strip()
        except Exception as exc:
            verdict = f'{{"filled": null, "error": "{type(exc).__name__}: {exc}"}}'
        return ActionResult(
            extracted_content=f"VISUAL VERIFY '{field_label}' -> {verdict}",
            long_term_memory=f"Visual check '{field_label}': {verdict}",
            include_in_memory=True,
        )

    return tools
