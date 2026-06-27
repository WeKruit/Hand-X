"""Workday adapter — multi-page wizard behind a mandatory account gate.

Grounded in MULTIPAGE_DESIGN.md (live recon of NVIDIA/Blue Origin/Visa). Workday tags
every interactive element with `data-automation-id` (aid), stable across tenants. The
application is a 7±1 step wizard: Create Account/Sign In -> My Information -> My Experience
-> Application Questions -> Voluntary Disclosures -> [Self Identify] -> Review (STOP).

AUTH: `authenticate` runs the create-account state machine (SSO-chooser reveal -> create-vs
-sign-in by verifyPassword -> fill email/password, NEVER the beecatcher honeypot -> submit ->
classify). VERIFIED END-TO-END on live Intel (autofillWithResume path): it created an account
with a throwaway email and reached the 7-step wizard with NO email verification — Intel-class
tenants need no mailbox infra. Tenants that DO require email verification return
needs_verification (poll the inbox / Agent Mail / HITL — see AUTH_DESIGN.md). CAPTCHA -> HITL.
`open_form` PREFERS Autofill-with-Resume (DomHand: applyManually is often broken). A DEAD-job
guard distinguishes an expired 404 from a real auth wall.
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

    # -- open_form: job page -> Apply -> (prefer) Autofill with Resume ------
    async def open_form(self, session: Any, page: Any) -> Any:
        # Reuses the in-tree DomHand guardrail (ghosthands/platforms/workday.py): PREFER
        # "Autofill with Resume" over "Apply Manually" — applyManually leads to a different,
        # often-broken flow. Click the main Apply button, then the resume path.
        await self._click_aid(page, "legalNoticeAcceptButton")  # Visa-style cookie/legal modal
        already = '[data-automation-id="email"], [data-automation-id="createAccountSubmitButton"], [data-automation-id^="formField-"]'
        with contextlib.suppress(Exception):
            url = await page.get_url()
            # only deep-link from a REAL job page (guard against about:blank / non-loaded page
            # producing 'about:blank/apply/...' which the security watchdog blocks).
            if "myworkdayjobs" in url and "/apply" not in url:
                base = url.split("?")[0].rstrip("/")
                await session.navigate_to(base + "/apply/autofillWithResume")  # preferred path
                await asyncio.sleep(3)
                page = await session.must_get_current_page()
        # fallback: click the Apply menu, prefer Autofill-with-Resume, else Apply Manually
        if not await eng.first(page, already):
            await self._click_aid(page, "adventureButton")
            await asyncio.sleep(1)
            if not (await self._click_aid(page, "autofillWithResume")):
                await self._click_aid(page, "applyManually")
            await asyncio.sleep(2.5)
            page = await session.must_get_current_page()
        return page

    # -- authenticate: the step-1 account wall -----------------------------
    async def authenticate(self, session: Any, page: Any, creds: Credentials | None) -> AuthResult:
        # Detect the account gate by ANY of its signals (email aid may not have mounted /
        # differs per tenant; the submit button + sign-in link are reliable).
        # DEAD-job guard (AUTH_DESIGN §1.2): an expired posting renders a 404 ("page doesn't
        # exist") with no auth/form DOM — must NOT be mistaken for an already-signed-in session.
        dead = await page.evaluate(
            "() => { const t=document.body.innerText||'';"
            ' const hasReal=document.querySelector(\'[data-automation-id="email"],[data-automation-id="adventureButton"],'
            '[data-automation-id="createAccountSubmitButton"],[data-automation-id^="formField-"],'
            '[data-automation-id="progressBar"]\');'
            " return (/page you are looking for does(n'?| no)t exist|invalid-content/i.test(t) && !hasReal); }"
        )
        if str(dead).lower() == "true":
            return AuthResult(ok=False, reason="JOB_EXPIRED: posting no longer exists (404).")

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
            # not on an auth screen — only "authed" if the wizard form is actually present;
            # otherwise it's a blank/redirect/unknown page, NOT a signed-in session.
            if await eng.first(page, '[data-automation-id="progressBar"], [data-automation-id^="formField-"]'):
                return AuthResult(ok=True)
            return AuthResult(ok=False, reason="no auth gate and no form (blank / redirect / unknown page).")
        if not creds:
            return AuthResult(
                ok=False,
                reason="Workday account gate is mandatory and no credentials were provided.",
            )

        # 1. Reveal the native email/password form. NEVER Google SSO. Some tenants (NVIDIA,
        #    Intel) show an SSO chooser / utility "Sign In" first.
        if not await eng.first(page, '[data-automation-id="email"]'):
            (
                await self._click_aid(page, "SignInWithEmailButton")
                or await self._click_aid(page, "utilityButtonSignIn")
                or bool(await eng.click_by_text(page, "Sign in with email"))
            )
            await self._settle(page)

        # 2. Ensure CREATE-ACCOUNT mode (we register a fresh per-tenant account). DomHand rule:
        #    verifyPassword present == Create Account; never toggle once there.
        if not await eng.first(page, '[data-automation-id="verifyPassword"]'):
            await self._click_aid(page, "createAccountLink")  # sign-in screen -> register
            await self._settle(page)

        # 3. Fill native fields ONLY (email/password/verifyPassword) — NEVER the beecatcher
        #    honeypot (aid-scoped fills, never "every input").
        await self._fill_aid(page, "email", creds.email)
        await self._fill_aid(page, "password", creds.password)
        await self._fill_aid(page, "verifyPassword", creds.password)
        await self._click_aid(page, "createAccountCheckbox")  # consent (Visa et al.) — only if present
        await self._click_aid(page, "createAccountSubmitButton")
        await self._settle(page)
        page = await session.must_get_current_page()

        # 4. Classify the post-submit screen
        if await eng.first(page, '[data-automation-id="verificationCode"], [data-automation-id="emailVerification"]'):
            return AuthResult(
                ok=True,
                needs_verification=True,
                reason="Workday wants an emailed verification code (poll the inbox / HITL).",
            )
        if await eng.first(page, '[data-automation-id="email"]'):  # still on the auth screen
            return AuthResult(ok=False, reason="Create Account did not advance (validation / CAPTCHA?).")
        return AuthResult(ok=True)  # reached the form (no-verification tenant, e.g. Intel)

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
        # never click Submit — only advance off non-Review steps. (DomHand WORKDAY_SELECTORS:
        # bottom-navigation-next-button / "Save and Continue".)
        clicked = (
            await self._click_aid(page, "pageFooterNextButton")
            or await self._click_aid(page, "bottom-navigation-next-button")
            or bool(await eng.click_by_text(page, "Save and Continue"))
            or bool(await eng.click_by_text(page, "Next"))
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
            # push bytes to the real input via CDP (never click the drop-zone -> OS dialog).
            # DomHand WORKDAY_SELECTORS: file-upload-input-ref / generic input[type=file].
            fel = (
                await eng.first(page, '[data-automation-id="file-upload-input-ref"]')
                or await eng.first(page, '[data-automation-id="file-upload-drop-zone"] input[type="file"]')
                or await eng.first(page, 'input[type="file"]')
            )
            return await eng.upload_file(session, page, fel, resume) if (fel and resume) else False
        if t in ("input_text", "textarea"):
            el = await self.locate(page, field)
            if not el:
                return False
            with contextlib.suppress(Exception):
                await el.click()  # DomHand rule: click before typing (focus), then fill ONCE
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
                ' [data-automation-id="promptOption"], [data-automation-id="menuItem"],'
                ' [role="listbox"] [role="option"]'  # DomHand WORKDAY_SELECTORS generic fallback
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
        # DomHand WORKDAY_SELECTORS month-segment variants: dateSectionMonth-input /
        # *dateSectionMonth* / placeholder MM.
        wrap = f'[data-automation-id="formField-{field.name}"]'
        seg = (
            await eng.first(page, f'{wrap} [data-automation-id$="Month-input"]')
            or await eng.first(page, f'{wrap} input[data-automation-id*="dateSectionMonth"]')
            or await eng.first(page, f'{wrap} input[placeholder*="MM"]')
            or await eng.first(page, f'{wrap} [role="spinbutton"]')
        )
        if not seg:
            return False
        with contextlib.suppress(Exception):
            await seg.click()  # click before typing (DomHand rule)
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
    async def _settle(self, page: Any, seconds: float = 2.0) -> None:
        """Let a Workday SPA transition settle after a click/submit/navigation."""
        await asyncio.sleep(seconds)

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
