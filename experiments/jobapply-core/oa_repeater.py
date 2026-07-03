"""oa_repeater — DETERMINISTIC generic repeater filler (Experience / Education / ... behind an
'Add another' button). Replaces the slow, crash-prone completeness-agent for repeaters.

User: a real form is 10-20 fields — experience, education, various sections — not 5. Collapsed
repeater sections (hibob: 6 of them, each just an 'Add' button until clicked) are invisible to
flat discovery, so we under-count AND under-fill. This fills them the deterministic way the
Workday adapter does, but host-agnostic:

  1. find every 'Add' affordance + the SECTION it belongs to (its nearest heading — section
     IDENTITY, the one place heading text is legitimate: we must know Education vs Experience to
     route the right profile rows).
  2. for each section we have profile data for (experience / education), for each profile entry:
     click Add -> wait for the new row -> discover the row's NEW fields -> map that ONE entry ->
     fill each via observe_act.
  bounded: max entries/section, per-section time budget.

Reuses discover_fields + map_fields + observe_act — no new fill primitives.
"""

import asyncio
import contextlib
import re as _re_mod
import time
from typing import Any

import ats_engine as eng

_PERSONAL_RX = _re_mod.compile(r'first name|last name|full name|\bemail\b|\bphone\b|country|linkedin|preferred name|middle name')

_SECTION_KEYS = {
    "experience": "experience",
    "employment": "experience",
    "work": "experience",
    "education": "education",
    "school": "education",
    "academic": "education",
}

# find Add affordances + their section heading (nearest preceding heading-ish text). Structural
# affordance + heading only for SECTION IDENTITY (which profile list to place), never for fields.
_FIND_SECTIONS_JS = r"""() => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  const addRx = /^\+?\s*add\b/i;
  const out = [];
  const adds = [...document.querySelectorAll('button,a,[role=button]')].filter(e => {
    const r=e.getBoundingClientRect(); if(r.width<8||r.height<8) return false;
    const t=norm(e.innerText||e.getAttribute('aria-label')||'');
    return t.length<24 && addRx.test(t); });
  const addWords = /\s*(add file|add another|add more|add)\s*$/i;
  for (const a of adds) {
    // The section label is the Add's nearest ancestor whose text is "<SectionName> Add"
    // (hibob renders 'Education Add' / 'Experience Add' as a div, no h-tag). Walk up, take the
    // smallest ancestor whose text is short and ends with an add-verb, minus that verb.
    let sec='', p=a;
    for (let i=0; i<6 && p; i++) {
      p = p.parentElement; if (!p) break;
      const t = norm(p.innerText || '');
      if (t && t.length < 60 && addWords.test(t)) { sec = norm(t.replace(addWords, '')); if (sec) break; }
    }
    const r=a.getBoundingClientRect();
    out.push({section: sec, x: r.left+r.width/2, y: r.top+r.height/2});
  }
  return JSON.stringify(out);
}"""


def _entry_profile(profile: dict, key: str, entry: dict) -> dict:
    """A ONE-entry mini-profile the flat map_fields can map (labels School/Company/Degree/... ->
    this entry's values)."""
    base = {k: v for k, v in profile.items() if not isinstance(v, (list, dict))}
    if key == "education":
        base.update({
            "school": entry.get("school", ""), "university": entry.get("school", ""),
            "degree": entry.get("degree", ""), "field_of_study": entry.get("field_of_study", ""),
            "major": entry.get("field_of_study", ""),
            "start_date": entry.get("start_date", ""),
            "end_date": entry.get("graduation_date", entry.get("end_date", "")),
            "graduation_date": entry.get("graduation_date", ""), "gpa": entry.get("gpa", ""),
        })
    else:
        base.update({
            "company": entry.get("company", ""), "employer": entry.get("company", ""),
            "title": entry.get("title", ""), "job_title": entry.get("title", ""),
            "location": entry.get("location", ""),
            "start_date": entry.get("start_date", ""),
            "end_date": "Present" if entry.get("current") else entry.get("end_date", ""),
            "description": "; ".join(entry.get("highlights", []))[:400],
        })
    return base


_SAVE_JS = r"""() => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const rx = /^(save|done|confirm|save entry|save and close|save & close)$/;
  // a row-commit verb ONLY — NEVER apply/submit/finish/next (those finalize the application).
  const forbidden = /(apply|submit|finish|next|continue)/;
  const btns = [...document.querySelectorAll('button,[role=button],input[type=submit]')].filter(e => {
    const r=e.getBoundingClientRect(); if(r.width<8||r.height<8) return false;
    const t=norm(e.innerText||e.value||e.getAttribute('aria-label')||'');
    return t && t.length<16 && rx.test(t) && !forbidden.test(t) && t!=='cancel'; });
  const el = btns[btns.length-1];  // the open form's Save is usually the LAST such button
  if(!el) return '';
  el.scrollIntoView({block:'center'}); const r=el.getBoundingClientRect();
  return JSON.stringify({x:r.left+r.width/2, y:r.top+r.height/2, t:norm(el.innerText||el.value||'')});
}"""


async def _click_save(page: Any, session: Any) -> bool:
    """Click the open Add-form's Save/Done/Confirm button (never Cancel). False if none."""
    import json as _j

    with contextlib.suppress(Exception):
        raw = await page.evaluate(_SAVE_JS)
        if not raw:
            return False
        c = _j.loads(raw)
        sid = await page.session_id
        for ev in (
            {"type": "mouseMoved", "x": c["x"], "y": c["y"], "buttons": 0},
            {"type": "mousePressed", "x": c["x"], "y": c["y"], "button": "left", "buttons": 1, "clickCount": 1},
            {"type": "mouseReleased", "x": c["x"], "y": c["y"], "button": "left", "buttons": 0, "clickCount": 1},
        ):
            await session.cdp_client.send.Input.dispatchMouseEvent(params=ev, session_id=sid)
        print(f"   [repeater] saved row (clicked '{c.get('t')}')")
        return True
    return False


async def _visual_click_add(session: Any, section_name: str, llm: Any) -> bool:
    """VISUAL locate + click the Add control for a section (user: use visuals, not DOM coords).
    Mark every clickable control, ask the VLM which numbered box ADDS an entry to `section_name`,
    scroll that node into view, click it. Robust to layout shifts (DOM coords go stale). False on
    miss. This is the primary Add-locate; the DOM-coord path is the fallback."""
    with contextlib.suppress(Exception):
        import base64

        import oa_action as act
        import oa_llm as _oa
        import oa_perception as perc
        from vision_verify import _vlm

        state = await perc.get_state(session)
        cands = {
            int(n.backend_node_id): n
            for n in state.selector_map.values()
            if getattr(n, "backend_node_id", None) is not None and perc.node_is_visible(n) and perc.node_rect(n) is not None
        }
        if not cands:
            return False
        png = await _oa.bounded_screenshot(session)
        if png is None:
            return False
        raw_b64 = base64.b64encode(png).decode() if isinstance(png, bytes) else str(png)
        from browser_use.browser.python_highlights import create_highlighted_screenshot

        marked = await create_highlighted_screenshot(raw_b64, cands, filter_highlight_ids=False)
        from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

        legend = ", ".join(str(i) for i in cands)
        prompt = (
            f"This job-application form screenshot has controls outlined by dashed numbered boxes "
            f"({legend}). Reply STRICT JSON {{\"mark\": <the NUMBER of the control that ADDS a new "
            f"entry/row to the '{section_name}' section — a button, link, or '+' icon next to that "
            f'section heading>}}. If none, {{"mark": -1}}.'
        )
        msg = UserMessage(content=[
            ContentPartTextParam(type="text", text=prompt),
            ContentPartImageParam(type="image_url", image_url=ImageURL(
                url=f"data:image/png;base64,{marked}", detail="high", media_type="image/png")),
        ])

        res = await _oa.resilient_vlm([msg], primary=_vlm())
        raw = str(getattr(res, "completion", res) or "")
        import re as _re

        m = _re.search(r'"mark"\s*:\s*(-?\d+)', raw)
        mk = int(m.group(1)) if m else -1
        node = cands.get(mk)
        txt = ""
        with contextlib.suppress(Exception):
            txt = (getattr(node, "node_value", "") or getattr(node, "get_all_children_text", lambda **k: "")(max_depth=2) or "")[:30]
        print(f"   [repeater] visual-add '{section_name}': VLM picked mark={mk} ({txt!r})")
        if node is None:
            return False
        # scroll the picked node into view, then click it (trusted, occlusion-aware)
        with contextlib.suppress(Exception):
            await session.cdp_client.send.DOM.scrollIntoViewIfNeeded(params={"backendNodeId": int(node.backend_node_id)})
        return await act.click_node(session, node)
    return False


async def fill_repeaters(session: Any, page: Any, profile: dict, resume: str | None, llm: Any, *, budget_s: float = 150.0, planner_keys: list | None = None) -> dict:
    """Fill Experience/Education repeater sections. Add control located VISUALLY (VLM marks —
    robust to layout shift), DOM-coord as fallback. Returns {sections: {name: rows_filled}}."""
    import json as _json

    import oa_observe_act as oa
    from oa_discover import discover_fields
    from oa_singlepage import _field_dict

    result: dict = {"sections": {}, "fields_filled": 0}
    deadline = time.monotonic() + budget_s

    async def _click_add_dom(section_key: str) -> bool:
        """Fallback: DOM-coord click (fresh-located)."""
        with contextlib.suppress(Exception):
            for add in _json.loads(await page.evaluate(_FIND_SECTIONS_JS) or "[]"):
                sec = str(add.get("section", "")).lower()
                if any(k in sec for k, v in _SECTION_KEYS.items() if v == section_key):
                    sid = await page.session_id
                    for ev in (
                        {"type": "mouseMoved", "x": add["x"], "y": add["y"], "buttons": 0},
                        {"type": "mousePressed", "x": add["x"], "y": add["y"], "button": "left", "buttons": 1, "clickCount": 1},
                        {"type": "mouseReleased", "x": add["x"], "y": add["y"], "button": "left", "buttons": 0, "clickCount": 1},
                    ):
                        await session.cdp_client.send.Input.dispatchMouseEvent(params=ev, session_id=sid)
                    return True
        return False

    # which section keys are present + have profile data. PLANNER (VLM) list wins — it sees an
    # icon-'+' or a localized Add the DOM regex misses (breezy/rippling); DOM detection is fallback.
    present: list[str] = []
    for k in (planner_keys or []):
        if k in ("experience", "education") and k not in present:
            present.append(k)
    if not present:
        with contextlib.suppress(Exception):
            for add in _json.loads(await page.evaluate(_FIND_SECTIONS_JS) or "[]"):
                sec = str(add.get("section", "")).lower()
                key = next((v for k, v in _SECTION_KEYS.items() if k in sec), None)
                if key and key not in present:
                    present.append(key)

    # section display names (for the visual prompt) keyed by canonical key
    names = {"experience": "Work Experience", "education": "Education"}
    for key in present:
        if time.monotonic() > deadline:
            break
        entries = profile.get(key) or (profile.get("work_experience") if key == "experience" else None) or []
        if not entries:
            continue
        rows_filled = 0
        for entry in entries[:4]:
            if time.monotonic() > deadline:
                break
            before = {f.name for f in await discover_fields(page)}
            # scroll the SECTION into view first, so its Add is on-screen -> gets marked -> the VLM
            # can pick it (after saving a prior row the page scrolls away; the Add was off-screen =
            # the mark=-1 experience miss).
            with contextlib.suppress(Exception):
                await page.evaluate(
                    "(wants) => { const norm=s=>(s||'').replace(/\\s+/g,' ').trim().toLowerCase();"
                    " const el=[...document.querySelectorAll('h1,h2,h3,h4,div,span,legend,p,label')]"
                    "   .find(e=>{const t=norm(e.innerText||''); return t.length<40 && wants.some(w=>t.includes(w));});"
                    " if(el) el.scrollIntoView({block:'center'}); }",
                    [k for k, v in _SECTION_KEYS.items() if v == key],
                )
                await asyncio.sleep(0.6)
            # VISUAL Add-click first (robust to layout shift), DOM-coord fallback
            clicked = await _visual_click_add(session, names.get(key, key), llm)
            if not clicked:
                clicked = await _click_add_dom(key)
            if not clicked:
                print(f"   [repeater] {key}: no Add control found -> stop")
                break
            with contextlib.suppress(Exception):
                await asyncio.sleep(1.8)
                new_fields = [f for f in await discover_fields(page) if f.name not in before]
                # SAFETY: a re-render can give a flat PERSONAL-INFO field a new id so it looks 'new';
                # the row map would then overwrite First Name with a row value (the 'United States'
                # in First Name bug). A repeater row is NEVER a personal-info field — drop those.
                new_fields = [f for f in new_fields if not _PERSONAL_RX.search((f.label or "").lower())]
                print(f"   [repeater] {key} row {rows_filled + 1}: Add clicked -> {len(new_fields)} new fields")
                if not new_fields:
                    break
                mini = _entry_profile(profile, key, entry)
                rows = [f for f in new_fields if f.needs_map]
                mapped = await eng.map_fields(llm, rows, mini, "") if rows else {}
                for f in new_fields:
                    value, _ = eng._resolve(f, mapped, resume)
                    if not (value or "").strip():
                        continue
                    fd = _field_dict(f, value, resume=resume, llm=llm, adapter=None, page=page)
                    with contextlib.suppress(Exception):
                        out = await oa.observe_act(session, fd)
                        if out == oa.DONE:
                            result["fields_filled"] += 1
                # COMMIT the row: many repeaters open an inline Add-form with a Save/Done button and
                # will NOT open the next Add (even for a different section) until this row is saved
                # (the hibob Education-form-open blocks Experience Add). Click Save/Done/Confirm.
                await _click_save(page, session)
                await asyncio.sleep(1.0)
                rows_filled += 1
        if rows_filled:
            result["sections"][key] = rows_filled
            print(f"   [repeater] filled {rows_filled} row(s) in '{key}' section")
    return result
