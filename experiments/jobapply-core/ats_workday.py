"""Workday adapter — multi-page wizard behind a mandatory account gate.

Grounded in MULTIPAGE_DESIGN.md (live recon of NVIDIA/Blue Origin/Visa). Workday tags
every interactive element with `data-automation-id` (aid), stable across tenants. The
application is a 7±1 step wizard: Create Account/Sign In -> My Information -> My Experience
-> Application Questions -> Voluntary Disclosures -> [Self Identify] -> Review (STOP).

HONEST LIMIT: step 1 is a MANDATORY Create Account / Sign In gate (+ usually email
verification, possibly CAPTCHA). Without real credentials + a reachable inbox the wizard
cannot be entered, so `authenticate` halts at the wall. open_form + the auth-screen + the
progressBar step model are what's verifiable without an account.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import ats_engine as eng
from ats_engine import AdvanceResult, ATSAdapter, AuthResult, Credentials, FormField, Step


class WorkdayAdapter(ATSAdapter):
    hosts = ("myworkdayjobs.com", "myworkday.com", "myworkdaysite.com")
    multi_page = True

    # -- extract: title only; fields come per-step --------------------------
    async def extract(self, url: str, profile: dict) -> tuple[str, list[FormField]]:
        return "Workday Application", []

    # -- open_form: job page -> Apply -> Apply Manually ---------------------
    async def open_form(self, session: Any, page: Any) -> Any:
        # Visa-style cookie/legal modal first.
        await self._click_aid(page, "legalNoticeAcceptButton")
        # Apply -> Apply Manually. Deep-link is the most reliable: <jobUrl>/apply/applyManually
        with contextlib.suppress(Exception):
            url = await page.get_url()
            if "/apply" not in url:
                base = url.split("?")[0].rstrip("/")
                await session.navigate_to(base + "/apply/applyManually")
                await asyncio.sleep(3)
                page = await session.must_get_current_page()
        # fallback: click the Apply adventure button + Apply Manually
        if not await eng.first(
            page, '[data-automation-id="email"], [data-automation-id="formField-legalNameSection_firstName"]'
        ):
            await self._click_aid(page, "adventureButton")
            await asyncio.sleep(1)
            await self._click_aid(page, "applyManually")
            await asyncio.sleep(2.5)
            page = await session.must_get_current_page()
        return page

    # -- authenticate: the step-1 account wall -----------------------------
    async def authenticate(self, session: Any, page: Any, creds: Credentials | None) -> AuthResult:
        # Detect the account gate by ANY of its signals (email aid may not have mounted /
        # differs per tenant; the submit button + sign-in link are reliable).
        name = (await self._active_step_name(page)).lower()
        at_account = (
            "create account" in name
            or "sign in" in name
            or await eng.first(page, '[data-automation-id="email"]')
            or await eng.first(page, '[data-automation-id="createAccountSubmitButton"]')
            or await eng.first(page, '[data-automation-id="signInSubmitButton"]')
            or await eng.first(page, '[data-automation-id="signInLink"]')
            or await eng.first(page, '[data-automation-id="SignInWithEmailButton"]')
        )
        if not at_account:
            return AuthResult(ok=True)  # already past the gate (signed-in session)
        if not creds:
            return AuthResult(
                ok=False,
                reason=(
                    "Workday Create Account / Sign In is mandatory and no credentials were provided. "
                    "Needs a real deliverable email inbox (+ usually emailed verification) per tenant."
                ),
            )
        # native email/password ONLY (never Google SSO). Honeypots stay empty.
        await self._fill_aid(page, "email", creds.email)
        await self._fill_aid(page, "password", creds.password)
        await self._fill_aid(page, "verifyPassword", creds.password)  # create-account only
        await self._click_aid(page, "createAccountCheckbox")  # Visa consent (toggle on)
        await self._click_aid(page, "createAccountSubmitButton") or await self._click_aid(page, "signInSubmitButton")
        await asyncio.sleep(3)
        page = await session.must_get_current_page()
        # email-verification step blocks autonomy -> HITL halt
        if await eng.first(page, '[data-automation-id="verificationCode"], [data-automation-id="emailVerification"]'):
            return AuthResult(
                ok=True,
                needs_verification=True,
                reason="Workday requires an emailed verification code/link before the form.",
            )
        # success iff we advanced off the account screen
        if await eng.first(page, '[data-automation-id="email"]'):
            return AuthResult(ok=False, reason="Create Account submit did not advance (validation/CAPTCHA?).")
        return AuthResult(ok=True)

    # -- extract_step: progressBar + formField enumeration -----------------
    async def extract_step(self, session: Any, page: Any, profile: dict) -> Step:
        meta = await page.evaluate(
            """() => {
              const bar = document.querySelector('[data-automation-id="progressBar"]');
              let index=1, total=1, name='';
              if (bar){
                const steps=[...bar.querySelectorAll('[data-automation-id^="progressBar"]')]
                  .filter(s=>/step/i.test(s.textContent||''));
                total = steps.length || 1;
                const active = bar.querySelector('[data-automation-id="progressBarActiveStep"]') || steps[0];
                if (active){ const m=(active.textContent||'').match(/step\\s+(\\d+)\\s+of\\s+(\\d+)\\s*(.*)/i);
                  if (m){ index=+m[1]; total=+m[2]; name=(m[3]||'').trim(); } else { name=(active.textContent||'').trim(); }
                  steps.forEach((s,i)=>{ if(s===active) index=i+1; });
                }
              }
              const out=[];
              for (const w of document.querySelectorAll('[data-automation-id^="formField-"]')){
                const aid=w.getAttribute('data-automation-id')||'';
                const key=aid.replace(/^formField-/,'');
                const lab=(w.querySelector('label')||{}).textContent || w.getAttribute('aria-label') || key;
                const ctrl=w.querySelector('input,textarea,select,button[aria-haspopup]');
                let type='input_text';
                if (ctrl){
                  const tag=ctrl.tagName.toLowerCase(); const it=(ctrl.getAttribute('type')||'').toLowerCase();
                  if (tag==='select') type='select_native';
                  else if (tag==='textarea') type='textarea';
                  else if (it==='checkbox') type='checkbox';
                  else if (it==='radio' || w.querySelector('[role=radiogroup]')) type='radio';
                  else if (it==='file') type='file';
                  else if (ctrl.getAttribute('aria-haspopup')==='listbox' || w.querySelector('button[aria-haspopup=listbox]')) type='single_select';
                  else if (w.querySelector('[data-automation-id=dateInputWrapper],[data-automation-id$=dateInput]')) type='date';
                  else type='input_text';
                }
                const req = (ctrl && ctrl.getAttribute('aria-required')==='true') || /\\*/.test(lab);
                out.push({name:key, label:(lab||'').replace(/\\*/g,'').trim().slice(0,80), type, required:!!req});
              }
              return JSON.stringify({index, total, name, fields:out});
            }"""
        )
        import json

        d = json.loads(meta)
        fields = [self._to_field(f) for f in d["fields"]]
        is_review = d["name"].lower() == "review" or (d["total"] and d["index"] == d["total"])
        return Step(
            index=d["index"],
            total=d["total"],
            name=d["name"] or f"Step {d['index']}",
            fields=fields,
            is_review=bool(is_review),
        )

    def _to_field(self, f: dict) -> FormField:
        t = f["type"]
        source = (
            "file"
            if t == "file"
            else "select"
            if t in ("single_select", "select_native", "multi_select", "radio", "checkbox")
            else "open_ended"
            if t == "textarea"
            else "input_text"
        )
        return FormField(name=f["name"], label=f["label"], type=t, source=source, required=f["required"])

    async def next_step(self, session: Any, page: Any) -> AdvanceResult:
        before = await self._active_index(page)
        # never click Submit — only advance off non-Review steps
        clicked = await self._click_aid(page, "pageFooterNextButton") or await self._click_aid(
            page, "bottom-navigation-next-button"
        )
        if not clicked:
            return AdvanceResult(ok=False, blocked_reason="no advance button found")
        for _ in range(16):  # wait for the active step index to change
            await asyncio.sleep(0.25)
            if await self._active_index(page) != before:
                return AdvanceResult(ok=True, page=await session.must_get_current_page())
        return AdvanceResult(ok=False, blocked_reason="step did not advance (validation error?)")

    async def is_complete(self, session: Any, page: Any) -> bool:
        d = await page.evaluate(
            """() => { const bar=document.querySelector('[data-automation-id=progressBar]'); if(!bar) return '';
              const a=bar.querySelector('[data-automation-id=progressBarActiveStep]'); const m=(a&&a.textContent||'').match(/step\\s+(\\d+)\\s+of\\s+(\\d+)\\s*(.*)/i);
              return m ? JSON.stringify({i:+m[1],t:+m[2],n:(m[3]||'').trim()}) : ''; }"""
        )
        if not d:
            return False
        import json

        s = json.loads(d)
        return s["n"].lower() == "review" or s["i"] == s["t"]

    # -- locate / fill / read_back -----------------------------------------
    async def locate(self, page: Any, field: FormField) -> Any | None:
        sel = f'[data-automation-id="formField-{field.name}"]'
        return (
            await eng.first(page, f"{sel} input")
            or await eng.first(page, f"{sel} textarea")
            or await eng.first(page, f"{sel} select")
            or await eng.first(page, f"{sel} button")
        )

    async def fill(self, session: Any, page: Any, field: FormField, value: str, resume: str | None) -> bool:
        t = field.type
        if t == "file":
            fel = await eng.first(page, '[data-automation-id="file-upload-input-ref"]')
            return await eng.upload_file(session, page, fel, resume) if (fel and resume) else False
        if t in ("input_text", "textarea"):
            el = await self.locate(page, field)
            if not el:
                return False
            with contextlib.suppress(Exception):
                await el.fill(value)
                return True
            return False
        if t == "select_native":
            el = await self.locate(page, field)
            if el:
                with contextlib.suppress(Exception):
                    await el.select_option(value)
                    return True
            return False
        if t == "checkbox":
            el = await self.locate(page, field)
            if el:
                with contextlib.suppress(Exception):
                    state = await el.evaluate("() => this.checked ? 'C' : 'U'")
                    if (state == "C") != (eng.norm(value) in ("yes", "true", "1", "y")):
                        await el.click()
                    return True
            return False
        if t == "radio":
            return await self._click_radio(page, field, value)
        if t in ("single_select",):
            return await self._listbox(page, field, value)
        if t == "date":
            return await self._date(page, field, value)
        return False

    async def _listbox(self, page: Any, field: FormField, value: str) -> bool:
        """Workday button-listbox: click trigger -> options mount in the body portal
        `activeListContainer` -> click the matching promptOption."""
        trig = await eng.first(page, f'[data-automation-id="formField-{field.name}"] button')
        if not trig:
            return False
        with contextlib.suppress(Exception):
            await trig.click()
        want = eng.norm(value)
        for _ in range(10):
            await asyncio.sleep(0.3)
            opts = await page.get_elements_by_css_selector(
                '[data-automation-id="activeListContainer"] [role="option"],'
                ' [data-automation-id="promptOption"], [data-automation-id="menuItem"]'
            )
            if not opts:
                continue
            exact = None
            for o in opts:
                txt = eng.norm((await o.evaluate("() => this.textContent")) or "")
                if txt == want:
                    exact = o
                    break
                if exact is None and want and want in txt:
                    exact = o
            target = exact or opts[0]
            with contextlib.suppress(Exception):
                await target.click()
                return True
            return False
        return False

    async def _click_radio(self, page: Any, field: FormField, value: str) -> bool:
        want = eng.norm(value)
        sel = f'[data-automation-id="formField-{field.name}"]'
        for r in await page.get_elements_by_css_selector(
            f'{sel} [role="radio"], {sel} input[type="radio"], {sel} label'
        ):
            with contextlib.suppress(Exception):
                txt = eng.norm(
                    (await r.evaluate("() => this.textContent || this.getAttribute('aria-label') || ''")) or ""
                )
                if want and want in txt:
                    await r.click()
                    return True
        return False

    async def _date(self, page: Any, field: FormField, value: str) -> bool:
        """Segmented MM/DD/YYYY spinbuttons — type continuous digits (Workday auto-advances)."""
        digits = ""
        parts = (value or "").split("-")  # ISO YYYY-MM-DD or YYYY-MM
        if len(parts) >= 2:
            mm = parts[1].zfill(2)
            dd = parts[2].zfill(2) if len(parts) >= 3 else "01"
            digits = f"{mm}{dd}{parts[0]}"
        if not digits:
            return False
        seg = await eng.first(
            page, f'[data-automation-id="formField-{field.name}"] [data-automation-id$="Month-input"]'
        )
        if not seg:
            return False
        with contextlib.suppress(Exception):
            await seg.click()
            await seg.fill(digits)
            return True
        return False

    async def read_back(self, session: Any, page: Any, field: FormField, value: str) -> bool:
        t = field.type
        sel = f'[data-automation-id="formField-{field.name}"]'
        if t == "file":
            return (await eng.first(page, f'{sel} [data-automation-id="file-upload-successful"]')) is not None
        if t in ("input_text", "textarea", "select_native"):
            el = await self.locate(page, field)
            if not el:
                return False
            with contextlib.suppress(Exception):
                got = await el.evaluate(
                    "() => this.tagName==='SELECT' ? (this.options[this.selectedIndex]||{}).text||'' : (this.value||'')"
                )
                return (
                    eng.norm(value) in eng.norm(got) or eng.norm(got) in eng.norm(value)
                    if (got or "").strip()
                    else False
                )
            return False
        if t in ("single_select", "radio"):
            with contextlib.suppress(Exception):
                txt = await page.evaluate(
                    f"() => {{ const w=document.querySelector('{sel}'); return w ? (w.textContent||'') : ''; }}"
                )
                return eng.norm(value) in eng.norm(txt)
            return False
        if t == "checkbox":
            el = await self.locate(page, field)
            if el:
                with contextlib.suppress(Exception):
                    return (await el.evaluate("() => this.checked ? 'C' : 'U'")) == "C"
            return False
        if t == "date":
            with contextlib.suppress(Exception):
                txt = await page.evaluate(
                    f"() => {{ const w=document.querySelector('{sel}'); return w ? (w.textContent||'') : ''; }}"
                )
                return bool(eng.norm(txt)) and (value or "")[:4] in txt
            return False
        return False

    # -- helpers -----------------------------------------------------------
    async def _click_aid(self, page: Any, aid: str) -> bool:
        el = await eng.first(page, f'[data-automation-id="{aid}"]')
        if not el:
            return False
        with contextlib.suppress(Exception):
            await el.click()
            return True
        return False

    async def _fill_aid(self, page: Any, aid: str, value: str) -> bool:
        el = await eng.first(page, f'[data-automation-id="{aid}"]')
        if not el:
            return False
        with contextlib.suppress(Exception):
            await el.fill(value)
            return True
        return False

    async def _active_step_name(self, page: Any) -> str:
        with contextlib.suppress(Exception):
            return (
                await page.evaluate(
                    """() => { const a=document.querySelector('[data-automation-id=progressBarActiveStep]');
                  const m=(a&&a.textContent||'').match(/step\\s+\\d+\\s+of\\s+\\d+\\s*(.*)/i);
                  return m ? (m[1]||'').trim() : (a?a.textContent.trim():''); }"""
                )
            ) or ""
        return ""

    async def _active_index(self, page: Any) -> int:
        with contextlib.suppress(Exception):
            d = await page.evaluate(
                """() => { const a=document.querySelector('[data-automation-id=progressBarActiveStep]');
                  const m=(a&&a.textContent||'').match(/step\\s+(\\d+)/i); return m ? m[1] : '0'; }"""
            )
            return int(d or 0)
        return 0
