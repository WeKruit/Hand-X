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

VERIFY_MODEL = os.environ.get("GH_VERIFY_MODEL", "gemini-3.1-flash-lite")


def _vlm() -> Any:
    from browser_use import ChatGoogle  # cheap VLM; swap to ChatOpenAI(base_url=…) for local Qwen2.5-VL

    return ChatGoogle(model=VERIFY_MODEL, api_key=os.environ.get("GOOGLE_API_KEY"))


async def visual_check(session: Any, target: str) -> str:
    """Core cheap-VLM check, reused by the action AND the deterministic loop hook.
    `target` is a field label ("Cover Letter") or a value to look for ("646-678-9391").
    Returns a short JSON-ish verdict string {filled, value}."""
    from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

    try:
        png = await session.take_screenshot()  # bytes (PNG)
    except Exception as exc:
        return f'{{"filled": null, "error": "screenshot: {exc}"}}'
    b64 = base64.b64encode(png).decode()
    prompt = (
        f"This is a job-application web form. Is '{target}' currently filled in / visibly present "
        f'in an input (not blank)? Reply STRICT JSON: {{"filled": true|false, "value": "<visible text>"}}.'
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
        return (getattr(resp, "completion", None) or str(resp)).strip()
    except Exception as exc:
        return f'{{"filled": null, "error": "{type(exc).__name__}: {exc}"}}'


def make_loop_verify_hook(repeat_threshold: int = 2) -> Any:
    """Return an on_step_end(agent) callback that DETERMINISTICALLY breaks the false-empty
    retype loop: if the agent types the SAME value into the SAME field on consecutive steps,
    it runs the cheap visual check itself and injects the verdict (agent.add_new_task) so the
    agent stops re-typing — without waiting for loop-detection nudges to pile up."""
    seen = {"key": None, "n": 0}

    async def on_step_end(agent: Any) -> None:
        out = getattr(agent.state, "last_model_output", None)
        if not out or not getattr(out, "action", None):
            return
        typed = None
        for act in out.action:
            try:
                dumped = act.model_dump(exclude_none=True)
            except Exception:
                continue
            for params in dumped.values():
                if isinstance(params, dict) and "text" in params and ("index" in params or "selector" in params):
                    typed = (params.get("index", params.get("selector")), str(params.get("text", ""))[:60])
        if typed is None:
            seen["key"], seen["n"] = None, 0
            return
        seen["n"] = seen["n"] + 1 if typed == seen["key"] else 1
        seen["key"] = typed
        if seen["n"] >= repeat_threshold:
            verdict = await visual_check(agent.browser_session, typed[1])
            agent.add_new_task(
                f"LOOP GUARD (automatic visual check): you re-typed '{typed[1]}' into the same field "
                f"{seen['n']}x. The vision model reports: {verdict}. If filled=true, that field IS done — "
                f"STOP re-typing it and move on to the next field."
            )
            seen["n"] = 0

    return on_step_end


def register_visual_verify(tools: Any) -> Any:
    """Add the verify_field_visually action onto an existing browser-use Tools object."""
    from browser_use import ActionResult, BrowserSession

    @tools.action(
        "Visually verify whether a form field is filled, using a cheap vision model. "
        "Call this ONLY after you have typed a field and the browser state still reads it empty: "
        "it screenshots the page and a small VLM reports whether the field is visibly filled. "
        "Trust its answer over the state read-back."
    )
    async def verify_field_visually(field_label: str, browser_session: BrowserSession) -> Any:
        verdict = await visual_check(browser_session, field_label)
        return ActionResult(
            extracted_content=f"VISUAL VERIFY '{field_label}' -> {verdict}",
            long_term_memory=f"Visual check '{field_label}': {verdict}",
            include_in_memory=True,
        )

    return tools
