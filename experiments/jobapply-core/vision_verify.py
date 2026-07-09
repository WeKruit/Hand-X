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

COST CONTROL (so the deterministic loop hook is affordable to keep wired):
  * A per-page CACHE keyed by (url + field label): the agent action AND the loop hook
    share verdicts, so a field is vision-checked at most ONCE per page — repeat checks
    are served free. URL in the key means a stale "filled" never leaks across pages.
  * A per-page CAP on unique VLM calls (GH_VERIFY_MAX_CALLS). Past it the hook stays
    SILENT rather than spend — it degrades to plain auto-vision, never guesses.
  * The hook only nudges on a vision-CONFIRMED filled=true (a real false-empty loop).
    filled=false -> the agent is legitimately filling, so we say nothing.
"""

import base64
import os
from typing import Any

VERIFY_MODEL = os.environ.get("GH_VERIFY_MODEL", "gemini-3.1-flash-lite")
# Max unique VLM calls per page (reset on navigation). flash-lite low-detail is cheap,
# but this bounds latency + nudge noise hard. Past it the hook stops spending/intervening.
VLM_MAX_CALLS = int(os.environ.get("GH_VERIFY_MAX_CALLS", "6"))

# ---- run-scoped state (one process == one application run) -------------------
_VCACHE: dict[str, str] = {}  # cache key (url|label) -> raw verdict string
_VLM_CALLS = {"n": 0}  # unique VLM calls on the CURRENT page


def reset_visual_cache() -> None:
    """Full reset — call once at the start of a record run."""
    _VCACHE.clear()
    _VLM_CALLS["n"] = 0


def _norm(s: Any) -> str:
    return " ".join(str(s).lower().split())[:120]


def _is_filled(verdict: str) -> bool:
    """Centralised, whitespace/quote-tolerant parse of the VLM's {"filled": true} reply."""
    v = verdict.lower().replace("'", '"').replace(" ", "")
    return '"filled":true' in v


def _matches(verdict: str) -> bool:
    """Whitespace/quote-tolerant parse of the value-aware VLM's {"matches": true} reply.
    Used by callers that asked `want=...`: answers "does the field show the RIGHT value?"
    (not merely "is it non-blank?"). False for presence-only verdicts (no matches key)."""
    v = verdict.lower().replace("'", '"').replace(" ", "")
    return '"matches":true' in v


def _vlm() -> Any:
    import oa_llm

    from browser_use import ChatGoogle  # cheap VLM; swap to ChatOpenAI(base_url=…) for local Qwen2.5-VL

    return oa_llm.openai_primary_llm("vlm") or ChatGoogle(model=VERIFY_MODEL, api_key=os.environ.get("GOOGLE_API_KEY"))


async def _current_url(session: Any) -> str:
    try:
        return await session.get_current_page_url()
    except Exception:
        return ""


async def visual_check(
    session: Any, target: str, *, want: str | None = None, key: str | None = None, use_cache: bool = True
) -> str:
    """Core cheap-VLM check, reused by the action AND the deterministic loop hook.
    `target` is what to look for ("Cover Letter" / "646-678-9391"); `key` overrides the
    cache identity (default: target) so the hook (keyed by field label) and the action
    (keyed by field_label) hit the SAME cache entry for a field.

    VALUE-AWARE mode (`want` given): asks "does field `target` visibly contain the value
    `want`?" and returns {"filled": bool, "value": "<visible text>", "matches": bool}. This
    is what the Workday dropdown/repeater guards need — the frozen-portal bug commits the
    WRONG option, so a presence-only "filled" rubber-stamps a wrong value as done; `matches`
    is the question that actually catches it. Parse it with `_matches`.

    PRESENCE-ONLY mode (`want` None, the default): unchanged — "is `target` non-blank?",
    returns {"filled": bool, "value": ...}. Single-page (jobapply.py) calls it this way.

    Returns a short JSON-ish verdict string. Served from cache (no VLM, no $) on a repeat;
    returns a {"capped"} sentinel once the per-page VLM budget is spent. The cache key
    includes `want` so a value-check and a presence-check of the same field never collide."""
    ck = ""
    if use_cache:
        # cache hit / over-budget are the FAST paths: no screenshot, no import, no $.
        # `want` is in the key: a presence-check and a value-check of one field are distinct.
        ident = _norm(key if key is not None else target)
        ck = f"{await _current_url(session)}|{ident}|{_norm(want) if want is not None else ''}"
        if ck in _VCACHE:
            return _VCACHE[ck]
        if _VLM_CALLS["n"] >= VLM_MAX_CALLS:
            return '{"filled": null, "capped": true}'  # budget spent — caller stays silent

    import oa_llm as _oa_llm

    png = await _oa_llm.bounded_screenshot(session)  # bytes (PNG), bounded — None on timeout/error
    if png is None:
        return '{"filled": null, "error": "screenshot bounded-out"}'
    b64 = base64.b64encode(png).decode()
    if want is not None:
        prompt = (
            f'This is a job-application web form. Look at the field labeled "{target}". Set "matches"=true '
            f'if it shows the value "{want}" OR a clearly EQUIVALENT / closest-available option that means '
            f'the same thing — e.g. a fuller official name ("Computer and Information Science" for '
            f'"Computer Science"; "University of California, Berkeley" for "UC Berkeley"; "United States of '
            f'America (+1)" for "United States"), or all of several comma-separated items present as pills. '
            # typed-residue false-positive (verified live): raw text sitting in a tag/typeahead SEARCH BOX
            # looks 'filled' but is NOT committed — only a pill/tag chip (with its x/remove control) or a
            # selected option counts. Plain text inputs (name/city) still count by their text.
            f"IMPORTANT: for a tag/multi-select field, TEXT STILL SITTING IN THE SEARCH BOX does NOT count "
            f"— it must appear as a COMMITTED pill/tag chip (usually with an x/remove control) or a "
            f"selected option; if the value is only typed in the search box, set matches=false. "
            f'Set "matches"=false ONLY if the field is blank, uncommitted as above, or shows a clearly '
            f'DIFFERENT thing. Reply STRICT JSON: {{"filled": true|false, "value": "<visible text>", '
            f'"matches": true|false}}.'
        )
    else:
        prompt = (
            f"This is a job-application web form. Is '{target}' currently filled in / visibly present "
            f'in an input (not blank)? Reply STRICT JSON: {{"filled": true|false, "value": "<visible text>"}}.'
        )
    try:  # production always has browser_use; the guard only lets offline tests fake _vlm()
        from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

        msg = UserMessage(
            content=[
                ContentPartTextParam(type="text", text=prompt),
                ContentPartImageParam(
                    type="image_url",
                    image_url=ImageURL(url=f"data:image/png;base64,{b64}", detail="low", media_type="image/png"),
                ),
            ]
        )
    except Exception:
        msg = prompt
    # BOUNDED + vision-fallback-capable: a stalled gemini-vision fails fast (OA_LLM_TIMEOUT) and a
    # vision fallback answers when keyed; None -> a bounded error sentinel (never an unbounded ainvoke).
    try:
        import oa_llm as _oa_llm

        resp = await _oa_llm.resilient_vlm([msg], primary=_vlm())
        if resp is None:
            return '{"filled": null, "error": "vlm bounded-out (no fallback)"}'
        verdict = (getattr(resp, "completion", None) or str(resp)).strip()
    except Exception as exc:
        return f'{{"filled": null, "error": "{type(exc).__name__}: {exc}"}}'
    if use_cache:
        _VLM_CALLS["n"] += 1
        _VCACHE[ck] = verdict
    return verdict


def _parse_str_list(raw: str) -> list[str]:
    """Tolerant parse of the VLM's JSON array reply -> [str]. The model sometimes wraps the array
    in prose or a ```json fence, so slice to the outermost [...] and json.load; fall back to a
    line-split. Returns de-duped, stripped, non-empty strings in order (the rendered top-to-bottom)."""
    import json
    import re as _re

    s = (raw or "").strip()
    out: list[str] = []
    m = _re.search(r"\[.*\]", s, _re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                out = [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            out = []
    if not out:  # no JSON array — split lines, drop bullets/numbering
        for ln in s.splitlines():
            t = _re.sub(r'^[\s\-\*\d\.\)"]+', "", ln).strip().strip('",')
            if t and not t.startswith("[") and not t.startswith("{"):
                out.append(t)
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq


async def flagged_fields_visually(session: Any) -> list[str]:
    """FIX-LOOP vision reader (user directive: 'VLM can help here too'): when the DOM/text readers
    can't tie an advance-blocking message to concrete fields, ONE low-detail screenshot names the
    fields VISIBLY flagged as invalid (red border/asterisk banner/error text under the input).
    Uncached (post-write state), bounded like every VLM call. [] on any failure — never blocks."""
    import oa_llm as _oa_llm

    png = await _oa_llm.bounded_screenshot(session)
    if png is None:
        return []
    b64 = base64.b64encode(png).decode()
    prompt = (
        "This job-application form failed to advance. Some fields are visibly marked as having "
        "validation errors (red outline, error text beneath them, or listed in an error banner). "
        "Reply ONLY a STRICT JSON array of the LABELS of those flagged fields, exactly as shown, "
        'e.g. ["Postal Code", "State"]. If none are visibly flagged, reply [].'
    )
    try:
        from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

        msg = UserMessage(
            content=[
                ContentPartTextParam(type="text", text=prompt),
                ContentPartImageParam(
                    type="image_url",
                    image_url=ImageURL(url=f"data:image/png;base64,{b64}", detail="low", media_type="image/png"),
                ),
            ]
        )
        res = await _oa_llm.resilient_vlm([msg], primary=_vlm())
        return _parse_str_list(res or "[]")[:8]
    except Exception:
        return []


async def read_options_visually(session: Any, *, key: str | None = None, use_cache: bool = True) -> list[str]:
    """Read the ACTUALLY-RENDERED options of the currently-open dropdown/menu from ONE low-detail
    screenshot — no DOM lag, no stale shared portal. This is the matching fix: the DOM option portal
    LAGS / freezes (filling 'Kubernetes' re-serves the previous 'Go' options; 'UC Berkeley' reads []),
    so we ask a cheap VLM to TRANSCRIBE the visible option texts top-to-bottom as a JSON string array.

    Cached + capped exactly like visual_check: keyed by (url | key). The caller passes a `key` that
    identifies the open widget + the typed filter (e.g. f"{field_label}:{typed}") so a re-read after a
    new keystroke is a DISTINCT entry (the whole point — the previous read is stale), while a repeat
    read of the same open state is served free. Over the per-page VLM budget -> returns [] (caller
    falls back to its DOM read rather than spend). Returns [] on any error (caller degrades, never crashes)."""
    ck = ""
    if use_cache:
        ident = _norm(key) if key is not None else "open-menu"
        ck = f"{await _current_url(session)}|opts|{ident}"
        if ck in _VCACHE:
            return _parse_str_list(_VCACHE[ck])
        if _VLM_CALLS["n"] >= VLM_MAX_CALLS:
            return []  # budget spent — caller uses its DOM read

    import oa_llm as _oa_llm

    png = await _oa_llm.bounded_screenshot(session)
    if png is None:
        return []
    b64 = base64.b64encode(png).decode()
    prompt = (
        "This is a job-application web form with a dropdown/combobox menu currently OPEN. "
        "List the option texts CURRENTLY VISIBLE in that open menu, top to bottom, EXACTLY as shown. "
        'Reply ONLY a STRICT JSON array of strings, e.g. ["Bachelor\'s Degree", "Master\'s Degree"]. '
        "If no menu is open or no options are visible, reply []."
    )
    try:
        from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

        msg = UserMessage(
            content=[
                ContentPartTextParam(type="text", text=prompt),
                ContentPartImageParam(
                    type="image_url",
                    image_url=ImageURL(url=f"data:image/png;base64,{b64}", detail="low", media_type="image/png"),
                ),
            ]
        )
    except Exception:
        msg = prompt
    # BOUNDED + vision-fallback-capable (see visual_check). None -> [] (caller uses its DOM read).
    try:
        import oa_llm as _oa_llm

        resp = await _oa_llm.resilient_vlm([msg], primary=_vlm())
        if resp is None:
            return []
        raw = (getattr(resp, "completion", None) or str(resp)).strip()
    except Exception:
        return []
    if use_cache:
        _VLM_CALLS["n"] += 1
        _VCACHE[ck] = raw
    return _parse_str_list(raw)


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


def make_loop_verify_hook(verify_at: int = 2) -> Any:
    """Return an on_step_end(agent) callback that NUDGES the agent off a false-empty re-do
    loop for ANY field action — text input, dropdown select, or duplicate option click
    (multi-select). Keyed by the action's value (or click#index), counted across the whole
    run (interleaving-proof).

    SAFE by construction — it NEVER stops the agent. A single looping field must never abort
    a multi-field application (an earlier version called agent.stop() on the 3rd re-do of the
    flaky phone widget and killed the run with most of the form unfilled). The hook only ever
    injects ONE corrective nudge per field and lets the agent finish the form / call done().

    Cost-safe:
      * EXACTLY ONCE per field (the `verify_at`-th re-do) it consults the cheap visual_check;
        further re-dos of the same field add nothing (the nudge is already in context).
      * visual_check is CACHED + CAPPED per page — a field costs one VLM call at most and the
        page is capped at VLM_MAX_CALLS; over budget it returns capped -> the hook stays silent.
      * It nudges ONLY when vision CONFIRMS the field is already filled (a true false-empty),
        naming the FIELD + the VALUE so bu-2-0 cannot re-hallucinate it empty. If vision says
        empty (or budget spent) it stays SILENT — the agent is legitimately filling.
    The per-page VLM budget resets on navigation (new page == fresh visual truth)."""
    from collections import defaultdict

    counts: dict[str, int] = defaultdict(int)
    nudged: set[str] = set()
    last_url = {"u": None}

    async def on_step_end(agent: Any) -> None:
        out = getattr(agent.state, "last_model_output", None)
        if not out or not getattr(out, "action", None):
            return
        session = agent.browser_session

        url = await _current_url(session)  # new page -> reset the per-page VLM budget
        if last_url["u"] is not None and url != last_url["u"]:
            _VLM_CALLS["n"] = 0
        last_url["u"] = url

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
            if counts[key] != verify_at or key in nudged:
                continue  # nudge each looping field at most ONCE; never on the 1st do

            label = await _field_label(session, idx)
            verdict = await visual_check(session, f"the '{label}' field", key=label)
            if not _is_filled(verdict):
                continue  # empty (agent legitimately filling) or budget spent -> SILENT
            nudged.add(key)
            shown = str(val)[:60] if val else label
            agent.add_new_task(
                f"LOOP GUARD (automatic visual check): the '{label}' field ALREADY has \"{shown}\" "
                f"set (you did this {counts[key]}x; its state read-back is a false-empty). Vision model "
                f"confirms for '{label}': {verdict}. '{label}' IS DONE — do NOT input or re-select "
                f"\"{shown}\" into '{label}' again (avoid duplicate entries); move to the next unfilled field."
            )

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
