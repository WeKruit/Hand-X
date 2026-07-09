"""Greenhouse adapter — the platform-specific surface behind the generic engine.

EXTRACT  : public boards-api schema (free, no browser) via greenhouse_schema.
FILL     : job-boards.greenhouse.io keys inputs by id==field-name; selects are
           react-select comboboxes (NOT native <select>); the legacy embed keys by name.
READ-BACK: a select's chosen label lives in the control's select__single-value div
           (the input's own .value stays empty) — anchor the read on getElementById(name).

See memory: project_greenhouse_jobboards_dom for the hard-won DOM facts.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import ats_engine as eng
import greenhouse_schema as gh
from ats_engine import ATSAdapter, FormField


class GreenhouseAdapter(ATSAdapter):
    hosts = ("job-boards.greenhouse.io", "boards.greenhouse.io")

    # -- step 1: schema extract (no browser) -------------------------------
    async def extract(self, url: str, profile: dict) -> tuple[str, list[FormField]]:
        org, jid = gh.parse_job_url(url)
        schema = gh.fetch_schema(org, jid)
        plan = gh.classify(schema, profile)
        fields = []
        for r in plan:
            name, typ, req = r["name"], r["type"], r.get("required", False)
            if name in ("cover_letter", "cover_letter_text") and not req:
                source = "skip"  # optional cover letter -> don't attempt (skip unless required)
            elif name in ("resume", "cover_letter") or typ == "input_file":
                source = "file"
            else:
                source = r["source"]
            fields.append(
                FormField(
                    name=name,
                    label=r.get("label", ""),
                    type=typ,
                    source=source,
                    required=req,
                    options=r.get("options"),
                    option_values=r.get("option_values"),
                    value=r.get("value"),
                )
            )
        return schema.get("title", ""), fields

    # -- reach the form (handles iframe-embed on company sites) -------------
    async def open_form(self, session: Any, page: Any) -> Any:
        # Many orgs' job-boards URL redirects to their OWN site and embeds the Greenhouse
        # form in an <iframe> (greenhouse.io/embed/job_app). Our CSS query is top-frame
        # only, so drill in: navigate the top frame straight to the embed src.
        if not await self._locate(page, "first_name"):
            src = await page.evaluate(
                "() => { const f=[...document.querySelectorAll('iframe')]"
                ".find(f=>/greenhouse\\.io\\/embed\\/job_app/.test(f.src||'')); return f?f.src:''; }"
            )
            if src:
                await session.navigate_to(src)
                await asyncio.sleep(3)
                page = await session.must_get_current_page()
        # reveal the cover-letter textarea behind the "Enter manually" toggle
        await eng.click_by_text(page, "Enter manually")
        return page

    # -- locate ------------------------------------------------------------
    async def locate(self, page: Any, field: FormField) -> Any | None:
        return await self._locate(page, field.name)

    async def _locate(self, page: Any, name: str) -> Any | None:
        # job-boards keys inputs by id==name; legacy embed keys by name — match either.
        return await eng.first(page, f'[id="{name}"], [name="{name}"]')

    # -- fill --------------------------------------------------------------
    async def fill(self, session: Any, page: Any, field: FormField, value: str, resume: str | None) -> bool:
        ftype, name = field.type, field.name
        if ftype == "input_file" or field.source == "file":
            fel = await self._locate(page, name) or await eng.first(page, 'input[type="file"]')
            return await eng.upload_file(session, page, fel, resume) if (fel and resume) else False

        if name == "location" or (ftype == "input_text" and "location" in (field.label or "").lower()):
            return await self._geocomplete(session, page, value)  # geocomplete combobox, not plain text

        if ftype in ("multi_value_single_select", "multi_value_multi_select"):
            parts = [p.strip() for p in value.split(";") if p.strip()] or [value]
            # Greenhouse renders a select as one of: react-select combobox, a CHECKBOX list
            # (esp. multi_value_multi_select + acknowledgements), or a native <select>. Detect.
            if await eng.first(page, f'input[type="checkbox"][name="{name}"], input[type="checkbox"][name="{name}[]"]'):
                ok = False
                for part in parts:
                    ok = await self._check_option(page, field, part) or ok
                return ok
            el = await self._locate(page, name)
            tag = ""
            if el:
                with contextlib.suppress(Exception):
                    tag = (await el.evaluate("() => this.tagName")).lower()
            if tag == "select":  # legacy native <select>
                with contextlib.suppress(Exception):
                    await el.select_option(parts if ftype.endswith("multi_select") else parts[0])
                    return True
            ok = False
            for part in parts:
                ok = await self._combobox(page, name, part) or ok
            return ok

        # text / textarea / input_text
        el = await self._locate(page, name)
        if not el:
            return False
        try:
            await el.fill(value)
            return True
        except Exception:
            return False

    async def fill_repeaters(self, session: Any, page: Any, profile: dict, allow_escalation: bool = True) -> dict:
        """Education repeater. The boards-api schema can't enumerate rows and the
        school/degree/discipline are searchable closed taxonomies living below the fold (NOT in
        browser-use's selector map) — deterministic string-match gets them wrong. So drive this
        section with a focused browser-use Agent (eng.agent_fill_section), which scrolls to it and
        uses browser-use's native dropdown handling. Fill-only stays guaranteed: submit is disabled
        for the duration. Only runs when an education repeater is actually present."""
        edu = profile.get("education") or []
        if not edu:
            return {}
        if not await eng.first(page, '[id^="react-select-school--0"], [class*="education--"]'):
            return {}  # new-template boards turn education into flat questions — no repeater here
        if not allow_escalation:
            # the kalepa leak: this agent used to run even under escalate=False, overrunning the
            # runner's wall clock and poisoning the next browser session. Honor the gate; the miss
            # is reported, not hidden.
            print("   [education] repeater needs the section agent but escalation is off — skipped")
            return {"education_rows": 0, "education_skipped": "escalation_off"}
        entries = "; ".join(
            f"entry {i + 1}: School='{e.get('school', '')}', Degree='{e.get('degree', '')}', "
            f"Discipline='{e.get('field_of_study', '')}'"
            for i, e in enumerate(edu)
        )
        return {
            "education_rows": len(edu),
            **await eng.agent_fill_section(session, page, section="Education", instructions=entries, max_steps=14),
        }

    async def _combobox(self, page: Any, name: str, value: str) -> bool:
        """react-select: open, type (filters menu), click the field-scoped option."""
        inp = await self._locate(page, name) or await eng.first(page, f"#react-select-{name}-input")
        if not inp:
            return False
        try:
            with contextlib.suppress(Exception):
                await inp.click()
            await inp.fill(value)
        except Exception:
            return False
        want = value.strip().lower()
        for i in range(8):  # observe: poll the menu until a real match shows (geocode/search settles)
            await asyncio.sleep(0.35)
            # Scope to THIS field's react-select menu. A bare [role="option"] also matches
            # the phone widget's 245-item country <li> list and would mis-click a country.
            opts = await page.get_elements_by_css_selector(
                f'[id^="react-select-{name}-option"], [class*="select__option"]'
            )
            if not opts:
                continue
            exact = partial = None
            for o in opts:
                try:
                    txt = ((await o.evaluate("() => this.textContent")) or "").strip().lower()
                except Exception:
                    continue
                if txt == want:
                    exact = o
                    break
                # bidirectional: the option may EXTEND or ABBREVIATE the mapped value
                # ('Bachelor of Science' vs option 'B.S.', or 'Yes' vs 'Yes, I ...').
                if partial is None and want and (want in txt or txt in want):
                    partial = o
            # pick the best; only fall to the first filtered option once the search has settled
            # (single option, or after a few polls) — never click opts[0] prematurely.
            target = exact or partial or (opts[0] if (len(opts) == 1 or i >= 4) else None)
            if target:
                with contextlib.suppress(Exception):
                    await target.click()
                    return True
        return False
        return False

    async def _geocomplete(self, session: Any, page: Any, value: str) -> bool:
        """Greenhouse Location: react-select geocomplete (id=candidate-location). Type the city,
        wait for the geocode menu, commit the pre-highlighted option-1 with a TRUSTED CDP Enter
        (eng.press_enter_trusted) — synthetic page.press / JS keys are ignored by react-select,
        and el.fill alone sets text without geocoding (form rejects on submit). Success signal is
        the .select__single-value text (lat/long live only in React state, never in the DOM)."""
        inp = await eng.first(page, "#candidate-location") or await self._locate(page, "location")
        if not inp:
            return False
        # Type the CITY portion only. The full "San Francisco, CA, USA" (commas) does NOT match
        # the geocode results ("San Francisco, California, United States") so the menu never opens;
        # the bare city "San Francisco" surfaces the municipality as the pre-focused option-0.
        city = (value or "").split(",")[0].strip() or value
        try:
            await inp.click()
            await asyncio.sleep(0.3)
            await inp.fill(city)
        except Exception:
            return False
        got_menu = False
        for _ in range(16):  # geocode round-trip — poll, don't fixed-sleep
            await asyncio.sleep(0.25)
            if await page.get_elements_by_css_selector('[id^="react-select-candidate-location-option"]'):
                got_menu = True
                break
        if not got_menu:
            return False
        # NO ArrowDown — react-select pre-highlights option-1; one ArrowDown overshoots the city.
        if not await eng.press_enter_trusted(session, page):
            return False
        for _ in range(10):  # settle: single-value renders after commit
            await asyncio.sleep(0.35)
            if (await self._single_value(page, "candidate-location")).strip():
                return True
        return False

    async def _single_value(self, page: Any, el_id: str) -> str:
        """Read a react-select control's chosen label (the input's own .value stays '')."""
        try:
            return await page.evaluate(
                "() => { const a=document.getElementById(%r); if(!a) return '';"
                " const c=a.closest('[class*=select__control]')||a.closest('[class*=container]');"
                " const s=c&&c.querySelector('[class*=single-value]'); return s?s.textContent:''; }" % el_id
            )
        except Exception:
            return ""

    async def _checkboxes(self, page: Any, name: str) -> list:
        return await page.get_elements_by_css_selector(
            f'input[type="checkbox"][name="{name}"], input[type="checkbox"][name="{name}[]"]'
        )

    async def _option_box(self, page: Any, field: FormField, label: str) -> Any | None:
        """The checkbox for option `label`: by schema value-id, then by label text, then (if a
        lone box) that box (acknowledgement)."""
        boxes = await self._checkboxes(page, field.name)
        if not boxes:
            return None
        vid = (field.option_values or {}).get(label)
        if vid:
            for b in boxes:
                bid = (await b.get_attribute("id")) or ""
                bval = (await b.get_attribute("value")) or ""
                if bval == vid or bid.endswith("_" + vid):
                    return b
        want = label.strip().lower()
        if want:
            for b in boxes:
                txt = ""
                with contextlib.suppress(Exception):
                    txt = (
                        await b.evaluate(
                            "() => { const l=this.closest('label')||(this.id&&document.querySelector('label[for=\"'+CSS.escape(this.id)+'\"]'));"
                            " return (l?l.textContent:(this.parentElement?this.parentElement.textContent:''))||''; }"
                        )
                    ) or ""
                if want in txt.strip().lower():
                    return b
        return boxes[0] if len(boxes) == 1 else None

    async def _check_option(self, page: Any, field: FormField, label: str) -> bool:
        """Check the checkbox matching option `label` (a select rendered as a checkbox list)."""
        box = await self._option_box(page, field, label)
        if box is None:
            return False
        checked = False
        with contextlib.suppress(Exception):
            # NB: el.evaluate stringifies the JS result -> a JS boolean comes back as the
            # Python str "True"/"False", and bool("False") is True. Return a string sentinel
            # and compare it, never bool() the raw result.
            checked = (await box.evaluate("() => this.checked ? 'C' : 'U'")) == "C"
        if not checked:
            with contextlib.suppress(Exception):
                await box.click()
        return True

    # -- read-back ---------------------------------------------------------
    async def read_back(self, session: Any, page: Any, field: FormField, value: str) -> bool:
        ftype, name = field.type, field.name
        if ftype == "input_file" or field.source == "file":
            # Greenhouse REMOVES the <input> on a SUCCESSFUL upload and renders the filename in a
            # .file-upload__wrapper / .field-wrapper. So input-existence is backwards (it's gone
            # exactly when upload worked). Confirm by the uploaded basename — the same rendered
            # filename the agent path visually verifies (probe: "resume.pdf" in .file-upload__wrapper).
            base = (value or "").replace("\\", "/").rstrip("/").split("/")[-1]
            if not base:
                return (await self._locate(page, name)) is not None
            want = eng.norm(base)
            for _ in range(6):  # the filename renders ~1-2s after the CDP upload — poll, don't race
                try:
                    txt = await page.evaluate(
                        "() => [...document.querySelectorAll('[class*=file-upload], .field-wrapper')]"
                        ".map(e => e.textContent || '').join(' ')"
                    )
                except Exception:
                    txt = ""
                if want in eng.norm(txt):
                    return True
                await asyncio.sleep(0.4)
            return False

        if name == "location" or (ftype == "input_text" and "location" in (field.label or "").lower()):
            try:
                text = await page.evaluate(
                    "() => { const a=document.getElementById('candidate-location'); if(!a) return '';"
                    " const c=a.closest('[class*=select__control]')||a.closest('[class*=container]');"
                    " const s=c&&c.querySelector('[class*=single-value]'); return s?s.textContent:''; }"
                )
            except Exception:
                text = ""
            want = (value or "").split(",")[0].strip()  # city portion, e.g. "San Francisco"
            return eng.norm(want) in eng.norm(text) if want else bool(eng.norm(text))

        if ftype in ("multi_value_single_select", "multi_value_multi_select"):
            # checkbox-rendered select: success = the matching option's box is checked.
            if await self._checkboxes(page, name):
                part = value.split(";")[0].strip()
                box = await self._option_box(page, field, part)
                if box is None:
                    return False
                with contextlib.suppress(Exception):
                    return (await box.evaluate("() => this.checked ? 'C' : 'U'")) == "C"
                return False
            # Anchor on id (== name), NOT the [id],[name] union: after a react-select pick
            # the union can resolve to a hidden name-input with no select__control ancestor,
            # reading back ''. The chosen label lives in the control's single-value div.
            js = (
                "() => { const a = document.getElementById(%r); if(!a) return '';"
                " if(a.tagName==='SELECT'){ const o=a.options[a.selectedIndex]; return o?o.text:''; }"
                " const c = a.closest('[class*=select__control]')||a.closest('[class*=control]')||a.closest('[class*=container]');"
                " if(c){ const s=c.querySelector('[class*=single-value]'); return (s?s.textContent:c.textContent)||''; }"
                " return a.value||''; }"
            ) % name
            try:
                text = await page.evaluate(js)
            except Exception:
                text = ""
            want = value.split(";")[0].strip()
            return eng.norm(want) in eng.norm(text) if want else bool(eng.norm(text))

        # text / textarea
        el = await self._locate(page, name)
        if not el:
            return False
        try:
            got = await el.evaluate("() => this.value")
        except Exception:
            return False
        if not (got or "").strip():
            return False
        return eng.norm(value) in eng.norm(got) or eng.norm(got) in eng.norm(value)
