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


def _action_target(dumped: dict) -> tuple[Any, Any]:
    """(index, value) for any action that fills/selects a field — text input, dropdown
    select, OR a bare option click (value None, resolved from the element). Else (None, None).
    Generalising past text-only lets the guard catch duplicate dropdown / multi-select picks."""
    for name in ("input", "input_text", "select_dropdown"):
        p = dumped.get(name)
        if isinstance(p, dict) and "text" in p:
            return p.get("index"), str(p["text"])
    p = dumped.get("click")
    if isinstance(p, dict) and "index" in p:
        return p.get("index"), None  # a click (e.g. selecting a react-select option / checkbox)
    return None, None


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
    re-do loop for ANY field action — text input, dropdown select, or duplicate option click
    (multi-select). Keyed by the action's value (or click#index), counted across the whole run
    (interleaving-proof). On the `verify_at`-th repeat, run the cheap visual_check, name the
    FIELD + the VALUE it already holds in the nudge (so bu-2-0 cannot hallucinate it empty);
    if a vision-VERIFIED field is still re-done to `stop_at`, agent.stop() — fill-only, so the
    filled form means stopping IS success."""
    from collections import defaultdict

    counts: dict[str, int] = defaultdict(int)
    verified: set[str] = set()

    async def on_step_end(agent: Any) -> None:
        out = getattr(agent.state, "last_model_output", None)
        if not out or not getattr(out, "action", None):
            return
        session = agent.browser_session
        for act in out.action:
            try:
                dumped = act.model_dump(exclude_none=True)
            except Exception:
                continue
            idx, val = _action_target(dumped)
            if idx is None and not val:
                continue
            key = (str(val).strip()[:80] if val else "") or f"click#{idx}"
            counts[key] += 1
            n = counts[key]
            if n == verify_at:
                label = await _field_label(session, idx)
                shown = (str(val)[:60] if val else label)  # what was put / selected
                verdict = await visual_check(session, f"the '{label}' field")
                if '"filled": true' in verdict.lower() or "'filled': true" in verdict.lower() or '"filled":true' in verdict.lower():
                    verified.add(key)
                agent.add_new_task(
                    f"LOOP GUARD (automatic visual check): the '{label}' field ALREADY has \"{shown}\" "
                    f"set (you did this {n}x; its state read-back is a false-empty). Vision model confirms "
                    f"for '{label}': {verdict}. '{label}' IS DONE — do NOT input or re-select \"{shown}\" "
                    f"into '{label}' again (avoid duplicate entries); move to the next field or call done."
                )
            elif n >= stop_at and key in verified:
                label = await _field_label(session, idx)
                agent.add_new_task(
                    f"LOOP GUARD: the '{label}' field is vision-verified FILLED yet you keep re-doing it. "
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
