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


async def _field_label(session: Any, index: Any) -> str:
    """Resolve a human field label from an element index (aria-label / name / id /
    placeholder) so the loop-guard can name WHICH field, not just the typed value."""
    if index is None:
        return "this field"
    try:
        node = await session.get_element_by_index(int(index))
        attrs = (getattr(node, "attributes", None) or {}) if node else {}
        for k in ("aria-label", "name", "id", "placeholder", "aria-labelledby"):
            v = attrs.get(k)
            if v:
                return str(v)
    except Exception:
        pass
    return f"field #{index}"


def make_loop_verify_hook(verify_at: int = 2, stop_at: int = 3) -> Any:
    """Return an on_step_end(agent) callback that DETERMINISTICALLY breaks the false-empty
    retype loop. Keyed by the TEXT VALUE typed (NOT element index — the index shifts and the
    retypes are interleaved with other actions, so consecutive/index detection misses them):
      - on the `verify_at`-th time a value is typed, run the cheap visual_check; if the field
        is visibly filled, mark it verified and inject a hard 'stop, it is filled' nudge.
      - if a vision-VERIFIED-filled value is STILL re-typed up to `stop_at` times (bu-2-0
        ignoring the nudge), mechanically agent.stop() — the form is filled and this is a
        fill-only run, so stopping IS success. Caps a 21x runaway loop at ~4."""
    from collections import defaultdict

    counts: dict[str, int] = defaultdict(int)
    verified: set[str] = set()

    async def on_step_end(agent: Any) -> None:
        out = getattr(agent.state, "last_model_output", None)
        if not out or not getattr(out, "action", None):
            return
        for act in out.action:
            try:
                dumped = act.model_dump(exclude_none=True)
            except Exception:
                continue
            params = dumped.get("input") or dumped.get("input_text")
            if not isinstance(params, dict) or "text" not in params:
                continue
            key = str(params["text"])[:80].strip()
            if not key:
                continue
            counts[key] += 1
            n = counts[key]
            if n == verify_at:
                label = await _field_label(agent.browser_session, params.get("index"))
                verdict = await visual_check(agent.browser_session, f"the '{label}' field")
                if '"filled": true' in verdict.lower() or "'filled': true" in verdict.lower() or '"filled":true' in verdict.lower():
                    verified.add(key)
                agent.add_new_task(
                    f"LOOP GUARD (automatic visual check): you typed into the '{label}' field {n}x. "
                    f"Vision model reports for '{label}': {verdict}. If filled=true, the '{label}' field "
                    f"IS DONE — issue NO further input into '{label}'; move to another field or call done."
                )
            elif n >= stop_at and key in verified:
                label = await _field_label(agent.browser_session, params.get("index"))
                agent.add_new_task(
                    f"LOOP GUARD: the '{label}' field is vision-verified FILLED yet you keep re-typing it. "
                    f"The form is filled; stopping the fill-only run now."
                )
                try:
                    res = agent.stop()
                    if hasattr(res, "__await__"):
                        await res
                except Exception:
                    pass
                return

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
