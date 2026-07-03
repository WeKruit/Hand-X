"""oa_planner — FIRST-LOOK PAGE PLANNER (user 顶层设计: '第一次看到一个页面,然后决定如何填写').

One VISUAL read of the whole page decides HOW to fill it, before any filling:
  - what kind of page this is, and any blockers (cookie banner, verify wall)
  - which repeatable HISTORY sections exist (Experience / Education / ...), and — by pointing at a
    NUMBERED MARK on the screenshot — WHICH control adds a row to each. The VLM points at the
    control it SEES ('+' icon beside Experience, localized text, anything); zero text patterns.
  - how many fields a complete fill should touch (expected_total_fields) — the honest DENOMINATOR
    for the completeness eval (fill_rate over discovered-only systematically under-counted).

The plan then DRIVES execution: dismiss blockers -> flat fill -> per planned section, click the
marked add-control (by node coords), discover the new row, fill it from the matching profile
entries. Reuses set-of-marks (browser_use.create_highlighted_screenshot), discover_fields,
map_fields, observe_act — the planner only decides; existing primitives act.
"""

import base64
import contextlib
import json
import re
from typing import Any

import oa_perception as perc

_PLAN_PROMPT = """This is a FULL-page screenshot of a web page (a job posting or application form).
The applicant's profile has: {profile_summary}

Reply STRICT JSON only:
{{
 "page_kind": "application_form" | "job_description" | "careers_landing" | "login_or_captcha" | "other",
 "blockers": [{{"kind": "cookie_banner"|"modal"|"other"}}],
 "repeater_sections": [
   {{"name": "<section heading exactly as displayed>",
     "profile_key": "experience" | "education" | "other",
     "has_add_control": true|false,
     "add_control_looks_like": "<what the add control is, e.g. 'button Add', '+ icon', 'link Add another'>",
     "entries_visible": <how many filled entries this section already shows>}}
 ],
 "expected_total_fields": <integer: how many individual inputs a COMPLETE application here would
   involve — count the flat fields PLUS each repeater section's per-row fields times the profile's
   entry count (e.g. Experience with 5 fields/row and 2 profile jobs = 10)>
}}

Rules: repeater_sections = ONLY sections holding a LIST of dated history entries (work experience,
education, volunteering, certifications, languages). The add control may be a button, a link, or a
bare '+' ICON — set has_add_control true whenever you can see ANY way to add an entry. Be generous
with expected_total_fields: a real application is usually 12-25 fields, not 5."""


def _profile_summary(profile: dict) -> str:
    exp = profile.get("experience") or []
    edu = profile.get("education") or []
    return (
        f"{len(exp)} work-experience entries, {len(edu)} education entries, "
        f"skills: {', '.join((profile.get('skills') or [])[:6])}"
    )


def _clickable_candidates(state: Any) -> dict[int, Any]:
    """EVERY visible interactive node (buttons, links, inputs) — the planner needs add-buttons and
    banner buttons, not just fillable controls."""
    out: dict[int, Any] = {}
    for n in state.selector_map.values():
        bid = getattr(n, "backend_node_id", None)
        if bid is None or not perc.node_is_visible(n) or perc.node_rect(n) is None:
            continue
        out[int(bid)] = n
    return out


async def plan_page(session: Any, profile: dict, llm: Any = None) -> dict:
    """ONE marked-screenshot VLM read -> the fill plan. {} on any failure (caller falls back to
    the unplanned path). Never raises."""
    try:
        # FULL-page: repeater sections sit BELOW the fold; a viewport shot sees only the top flat
        # fields and under-counts (expected_fields=6, sections=[] — the exact under-estimate bug).
        import asyncio

        import oa_llm as _oa
        from vision_verify import _vlm

        png = None
        with contextlib.suppress(Exception):
            sh = await asyncio.wait_for(session.take_screenshot(full_page=True), timeout=15.0)
            png = base64.b64decode(sh) if isinstance(sh, str) else sh
        if png is None:
            png = await _oa.bounded_screenshot(session)
        if png is None:
            return {}
        raw_b64 = base64.b64encode(png).decode() if isinstance(png, bytes) else str(png)
        from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

        msg = UserMessage(
            content=[
                ContentPartTextParam(type="text", text=_PLAN_PROMPT.format(profile_summary=_profile_summary(profile))),
                ContentPartImageParam(
                    type="image_url",
                    image_url=ImageURL(url=f"data:image/png;base64,{raw_b64}", detail="high", media_type="image/png"),
                ),
            ]
        )
        plan = {}
        for _ in range(2):  # ONE retry — the plan is load-bearing for the coverage denominator
            res = await _oa.resilient_vlm([msg], primary=_vlm())
            raw = str(getattr(res, "completion", res) or "")
            m = re.search(r"\{.*\}", raw, re.S)
            with contextlib.suppress(Exception):
                cand = json.loads(m.group(0)) if m else {}
                if isinstance(cand, dict) and cand.get("expected_total_fields"):
                    plan = cand
                    break
        if not isinstance(plan, dict) or not plan:
            return {}
        secs = [
            f"{s.get('name')}({s.get('profile_key')},add={s.get('has_add_control')})"
            for s in (plan.get("repeater_sections") or [])
        ]
        print(
            f"   [plan] kind={plan.get('page_kind')} expected_fields={plan.get('expected_total_fields')} "
            f"sections={secs} blockers={len(plan.get('blockers') or [])}"
        )
        return plan
    except Exception as exc:
        print(f"   [plan] failed: {exc}")
        return {}
