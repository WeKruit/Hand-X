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
import os
from typing import Any

import ats_engine as eng
from ats_engine import AdvanceResult, ATSAdapter, AuthResult, Credentials, FormField, Step

_DBG = bool(os.environ.get("WD_DEBUG"))

# Generic, tenant-independent step enumerator (see extract_step for the rationale).
# Emits {index,total,name, fields:[{name=<full wrapper aid>, label, type, required, options}]}.
_EXTRACT_STEP_JS = r"""
() => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  // chrome / non-field aids to never treat as a form field
  const CHROME = /utility|hammy|header|mainMenu|MenuButton|menuItem|navigation|adventure|progressBar|pageFooter|file-upload|fileUpload|legalNotice|cookie|searchBox|relatedActions/i;
  // labels that are an OPTION, not the question (so radio/checkbox group label != "Yes")
  const isOpt = t => /^(yes|no|prefer not.*|decline.*|i don'?t.*|choose one|select one|select\.\.\.|on|off)$/i.test(t);

  // ---- progress bar: active step index / total / name ----
  const bar = document.querySelector('[data-automation-id="progressBar"]');
  let index=1, total=1, name='';
  if (bar){
    const steps=[...bar.querySelectorAll('[data-automation-id^="progressBar"]')].filter(s=>/step/i.test(s.textContent||''));
    total = steps.length || 1;
    const active = bar.querySelector('[data-automation-id="progressBarActiveStep"]') || steps[0];
    if (active){
      const m=(active.textContent||'').match(/step\s+(\d+)\s+of\s+(\d+)\s*(.*)/i);
      if (m){ index=+m[1]; total=+m[2]; name=norm(m[3]||''); } else { name=norm(active.textContent||''); }
      steps.forEach((s,i)=>{ if(s===active) index=i+1; });
    }
  }

  // ---- generic wrapper discovery (NOT limited to the formField- prefix) ----
  const wrappers = new Map();   // UNIQUE key -> wrapper element (insertion order = DOM order)
  document.querySelectorAll('[data-automation-id^="formField-"]').forEach(w => {
    // KEY by data-fkit-id (UNIQUE per instance, e.g. "workExperience-225--jobTitle") so repeater ROWS
    // don't collide on the shared data-automation-id ("formField-jobTitle") — that collision is why the
    // ladder could fill only ONE row and the agent had to do the rest. Fall back to the aid.
    const key = w.getAttribute('data-fkit-id') || w.getAttribute('data-automation-id');
    if (key && !wrappers.has(key)) wrappers.set(key, w);
  });
  // secondary: editable controls not inside any formField-* wrapper (tenants without the
  // prefix, or standalone inputs) -> adopt their nearest in-content [data-automation-id] ancestor.
  const EDITABLE='input:not([type=hidden]):not([type=button]):not([type=submit]):not([type=search]),'
               +' textarea, select, button[aria-haspopup="listbox"], [data-uxi-widget-type="selectinput"]';
  document.querySelectorAll(EDITABLE).forEach(c => {
    if (c.closest('[data-automation-id^="formField-"]')) return;
    if (!c.closest('[data-automation-id$="Page"],[data-automation-id*="applyFlow"],[data-automation-id*="Content"]')) return;
    const w = c.closest('[data-automation-id]'); if (!w) return;
    const aid = w.getAttribute('data-automation-id'); if (!aid || CHROME.test(aid)) return;
    const key = w.getAttribute('data-fkit-id') || aid; if (wrappers.has(key)) return;
    wrappers.set(key, w);
  });

  const typeOf = w => {
    if (w.querySelector('button[aria-haspopup="listbox"]')) return 'single_select';
    if (w.querySelector('[data-uxi-widget-type="selectinput"],[data-automation-id="multiSelectContainer"]')) return 'multi_select';
    if (w.querySelector('input[type=radio],[role=radio],[role=radiogroup]')) return 'radio';
    if (w.querySelector('input[type=checkbox],[role=checkbox]')) return 'checkbox';
    if (w.querySelector('[data-automation-id*="dateSection"],[data-automation-id*="dateInput"]')) return 'date';
    if (w.querySelector('select')) return 'select_native';
    if (w.querySelector('input[type=file]')) return 'file';
    if (w.querySelector('textarea')) return 'textarea';
    return 'input_text';
  };

  // a label that is just the control's PLACEHOLDER (e.g. "Select One") is not the question.
  const isPlaceholder=t=>/^\*?\s*(select one|choose one|select\.\.\.|select|please select|--|—)\s*\*?$/i.test(t);
  const meaningful=(t,type)=>t && t.length>=3 && !isPlaceholder(t) && !((type==='radio'||type==='checkbox')&&isOpt(t));
  const labelOf = (w, type) => {
    const labels=[...w.querySelectorAll('label')].map(l=>norm(l.textContent)).filter(Boolean);
    // 1. explicit question/label elements
    const lg=w.querySelector('legend'); if (lg){ const t=norm(lg.textContent); if (meaningful(t,type)) return t; }
    const fl=w.querySelector('[data-automation-id*="formLabel"],[data-automation-id*="questionText"],[data-automation-id$="-label"]');
    if (fl){ const t=norm(fl.textContent); if (meaningful(t,type)) return t; }
    // 2. a meaningful <label> (skip the dropdown placeholder / a radio option)
    const q=labels.find(l=>meaningful(l,type)); if (q) return q;
    // 3. aria
    const grp=w.querySelector('[role=group],[role=radiogroup],fieldset')||w;
    const lb=grp.getAttribute('aria-labelledby')||w.getAttribute('aria-labelledby');
    if (lb){ const t=lb.split(' ').map(x=>{const e=document.getElementById(x);return e?norm(e.textContent):'';}).join(' ').trim(); if (meaningful(t,type)) return t; }
    const al=grp.getAttribute('aria-label')||w.getAttribute('aria-label'); if (al && meaningful(norm(al),type)) return norm(al);
    // 4. the wrapper's OWN text minus controls/options (DomHand-style) — catches a question
    //    rendered as plain text (numbered screening questions) rather than a <label>.
    const clone=w.cloneNode(true);
    clone.querySelectorAll('input,textarea,select,button,ul,ol,li,[role=option],[role=listbox],[role=radio],[role=checkbox],[data-automation-id="promptOption"]').forEach(x=>{ if(x.remove) x.remove(); });
    const own=norm(clone.textContent); if (meaningful(own,type) && own.length<=240) return own;
    // 5. fallbacks
    if (labels[0]) return labels[0];
    return (w.getAttribute('data-automation-id')||'').replace(/^formField-/,'')
      .replace(/[-_]+/g,' ').replace(/([a-z])([A-Z])/g,'$1 $2').trim();
  };

  const out=[];
  for (const [aid, w] of wrappers){
    const type=typeOf(w);
    const label=labelOf(w, type).replace(/\*/g,'').trim().slice(0,90);
    let req=/\*/.test([...w.querySelectorAll('label')].map(l=>l.textContent).join(' '))
            || !!w.querySelector('[aria-required="true"],[required]');
    let options=null;
    if (type==='radio' || type==='checkbox'){
      options=[...w.querySelectorAll('label')].map(l=>norm(l.textContent)).filter(Boolean);
      if (!options.length) options=null;
    } else if (type==='select_native'){
      const s=w.querySelector('select');
      if (s) options=[...s.options].map(o=>norm(o.textContent)).filter(t=>t && !/^select/i.test(t));
      if (options && !options.length) options=null;
    }
    out.push({name:aid, label, type, required:!!req, options});
  }
  // ENTRY-INDEX repeater fields so MAP can tell row 1 from row 2 (both share a label like "Job Title").
  // A repeater field's name is "<section>-<rownum>--<field>" (e.g. workExperience-225--jobTitle). Group
  // by <section>, order the distinct <rownum>s by DOM appearance, tag each label with its 1-based entry.
  const rowRe = /^([A-Za-z]+)-(\d+)--/;
  const order = {};
  out.forEach(f => { const m=(f.name||'').match(rowRe); if(m){ (order[m[1]] = order[m[1]] || []); if(!order[m[1]].includes(m[2])) order[m[1]].push(m[2]); } });
  out.forEach(f => { const m=(f.name||'').match(rowRe); if(m && order[m[1]].length>1){ f.label = f.label + ' (entry ' + (order[m[1]].indexOf(m[2])+1) + ')'; } });
  return JSON.stringify({index, total, name, fields:out});
}
"""

# Count the PRESENT rows of a repeater section, generically: find the section by heading keyword, then
# count distinct numbered row-prefixes among its data-fkit-id values (e.g. workExperience-225,
# workExperience-226 -> 2). No hardcoded section id — the prefix is derived from the DOM. Lets
# fill_repeaters skip the agent when the row-aware ladder already filled all present rows.
_ROW_COUNT_JS = r"""
(keywords) => {
  const low = s => (s||'').toLowerCase();
  const hit = s => { s = low(s); return keywords.some(k => s.includes(k)); };
  const heads = [...document.querySelectorAll('h2,h3,h4,[role="heading"],legend,[data-automation-id*="title" i]')];
  let root = null;
  for (const h of heads) {
    if (!hit(h.textContent)) continue;
    let s = h.parentElement;
    for (let u = 0; u < 6 && s; u++) { if (s.querySelector('[data-fkit-id]')) { root = s; break; } s = s.parentElement; }
    if (root) break;
  }
  if (!root) return 0;
  const prefixes = new Set();
  root.querySelectorAll('[data-fkit-id]').forEach(e => {
    const m = (e.getAttribute('data-fkit-id') || '').match(/^([A-Za-z]+-\d+)--/);
    if (m) prefixes.add(m[1]);
  });
  return prefixes.size;
}
"""


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

    # -- extract_step: progressBar + GENERIC field enumeration -------------
    # Workday's per-step DOM is NOT uniform across tenants: My Information widgets nest
    # inside section wrappers whose aid varies (`formField-country`, `formField-legalName--
    # firstName`, `formField-countryRegion` LABELLED "State", or standalone `phone-sms-opt-in`),
    # and a wrapper carries BOTH a listbox `button[aria-haspopup=listbox]` AND a search `input`
    # (so "grab the first input" mis-drives Country/State/Phone-Type as text). This enumerator
    # is therefore CONTROL-first + label-first, not formField-prefix-first:
    #   - discover wrappers two ways (every formField-* PLUS any editable control whose nearest
    #     aid ancestor isn't a formField wrapper — covers tenants that drop the prefix),
    #   - type by PRIORITY (listbox > multiselect > radio > checkbox > date > native-select >
    #     file > textarea > text) so a listbox is never mistaken for its inner text box,
    #   - label from the GROUP/question text (legend / formLabel / non-option <label>), never an
    #     option ("Yes"/"No"). The field meaning is decided by LABEL downstream, never the aid.
    async def extract_step(self, session: Any, page: Any, profile: dict) -> Step:
        await self._await_step_mounted(page)  # widgets mount async after a step transition
        meta = await page.evaluate(_EXTRACT_STEP_JS)
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

    async def _await_step_mounted(self, page: Any, stable_s: float = 1.8, max_s: float = 12.0) -> None:
        """A Workday step mounts its fields asynchronously AFTER the progress index flips.
        Measured curve (Intel My Information): the count sits at 1 for ~1s (the first widget),
        then JUMPS to 12. There is no spinner to gate on, so a naive "stable across two reads"
        returns during that 1-field plateau (the original bug). Instead, wait until the field
        count has held STEADY for `stable_s` (a window that outlasts the plateau) — any increase
        resets the window — bounded by `max_s`. Generic: counts every field-bearing control."""
        count_js = (
            '() => document.querySelectorAll(\'[data-automation-id^="formField-"],'
            ' [data-uxi-widget-type="selectinput"]\').length'
        )
        interval, elapsed, prev, steady_at = 0.3, 0.0, -1, 0.0
        while elapsed < max_s:
            n = -1
            with contextlib.suppress(Exception):
                n = int(await page.evaluate(count_js) or 0)
            if n != prev:  # the count changed (a widget mounted) — reset the stability window
                prev, steady_at = n, elapsed
            if n > 0 and (elapsed - steady_at) >= stable_s:
                return
            await asyncio.sleep(interval)
            elapsed += interval

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
        return FormField(
            name=f["name"],
            label=f["label"],
            type=t,
            source=source,
            required=f["required"],
            options=f.get("options") or None,
        )

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

    async def validation_errors(self, page: Any) -> list[str]:
        """Workday surfaces field errors after a blocked Save (e.g. 'Error-Phone Number Enter a
        valid format for Phone Number.'). Collect them so the engine can run agent-mode repair."""
        with contextlib.suppress(Exception):
            raw = await page.evaluate(
                "() => { const e=[...document.querySelectorAll("
                '\'[data-automation-id="errorMessage"],[data-automation-id*="rror"],[role="alert"]\')]'
                ".map(x=>(x.textContent||'').replace(/\\s+/g,' ').trim())"
                ".filter(t=>t && /error|valid|required|format/i.test(t));"
                " return JSON.stringify([...new Set(e)].slice(0,12)); }"
            )
            import json

            return json.loads(raw) if raw else []
        return []

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
        # field.name is the UNIQUE data-fkit-id (repeater-row safe, e.g. "workExperience-225--jobTitle")
        # or, as fallback, the data-automation-id. Match by either so a single name resolves both.
        n = field.name
        for suf in (" input", " textarea", " select", " button"):
            el = await eng.first(page, f'[data-fkit-id="{n}"]{suf}, [data-automation-id="{n}"]{suf}')
            if el:
                return el
        return None

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
            # checkbox GROUP ("select all that apply"): the value NAMES an option (e.g. "Neither")
            # rather than yes/no — click THAT option's checkbox. Single boolean checkbox: toggle.
            if eng.norm(value) not in (
                "yes",
                "true",
                "1",
                "y",
                "no",
                "false",
                "0",
                "n",
                "",
            ) and await self._click_radio(page, field, value):
                return True
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
        if t == "single_select":
            return await self._listbox(page, field, value)
        if t == "multi_select":
            return await self._multiselect(session, page, field, value)
        if t == "date":
            return await self._date(page, field, value)
        return False

    async def _multiselect(self, session: Any, page: Any, field: FormField, value: str) -> bool:
        """Workday typeahead multiselect (`multiSelectContainer` + `selectinput`): type the value
        to filter, then commit the highlighted top match with a TRUSTED Enter (see below).
        Single-value (commit one); commit-then-add for repeaters is handled elsewhere."""
        wrap = f'[data-automation-id="{field.name}"]'
        inp = await eng.first(page, f'{wrap} [data-uxi-widget-type="selectinput"] input') or await eng.first(
            page, f"{wrap} input"
        )
        if not inp:
            return False
        with contextlib.suppress(Exception):
            await inp.click()
            await inp.fill(value)
        # The multiselect's option portal also contains the committed PILL's sub-elements
        # (menuItem>selectedItem>promptOption), so clicking "an option" mis-targets the pill while
        # the widget auto-commits its highlighted top option on blur (observed: a wrong "Albania"
        # pill). Instead, type-to-filter then commit the highlighted top match with a TRUSTED CDP
        # Enter — synthetic keys are ignored by the widget. Verified: 'United States' -> pill
        # 'United States of America (+1)'.
        await asyncio.sleep(1.2)  # let the typeahead filter + highlight the top match
        await eng.press_enter_trusted(session, page)
        await asyncio.sleep(0.6)
        ok = await self.read_back(session, page, field, value)
        if _DBG:
            print(f"   [msel {field.name}] value={value!r} committed={ok}")
        return ok

    async def _listbox(self, page: Any, field: FormField, value: str) -> bool:
        """Workday button-listbox: click trigger -> options mount in the body portal
        `activeListContainer` -> click the matching promptOption."""
        trig = await eng.first(page, f'[data-automation-id="{field.name}"] button')
        if not trig:
            return False
        with contextlib.suppress(Exception):
            await trig.click()
        return await self._pick_option(page, value)

    # The option portal is shared: bare `promptOption`/`menuItem` aids from PREVIOUSLY-opened
    # widgets persist in the DOM (hidden), so an unscoped query mixes a closed widget's STALE
    # options into the current one (observed: phone-code options bleeding into the State list, and
    # a click landing on a detached/hidden element while the real widget auto-commits its first
    # option on blur). The active dropdown's options are the only VISIBLE ones — filter on that.
    _VISIBLE_TEXT_JS = (
        "() => { const r=this.getBoundingClientRect();"
        # a committed pill's sub-elements (menuItem>selectedItem>promptOption) also carry this aid —
        # they are NOT selectable options, so skip anything inside a selectedItemList.
        " if (this.closest('[data-automation-id=\"selectedItemList\"]')) return '';"
        " return (r.width>0 && r.height>0 && this.offsetParent!==null) ? (this.textContent||'') : ''; }"
    )

    async def _pick_option(self, page: Any, value: str, allow_fallback: bool = True) -> bool:
        """Shared option-portal picker for listbox + multiselect. Considers ONLY VISIBLE options
        (the active dropdown) — hidden stale options from closed widgets are skipped. Match exact ->
        contains; strip-and-contains tolerates trailing dial codes ('United States' vs '...(+1)').
        allow_fallback=True picks the first VISIBLE option when nothing matches (fine for a typed-
        filtered single-select); pass False where a wrong pick is harmful (multiselect commits a pill)."""
        want = eng.norm(value)
        for _ in range(10):
            await asyncio.sleep(0.3)
            raw = await page.get_elements_by_css_selector(
                '[data-automation-id="activeListContainer"] [role="option"],'
                ' [data-automation-id="promptOption"], [data-automation-id="menuItem"],'
                ' [role="listbox"] [role="option"]'  # DomHand WORKDAY_SELECTORS generic fallback
            )
            # keep only on-screen options (drops stale hidden options from other widgets)
            opts: list[tuple[Any, str]] = []
            for o in raw:
                vis = (await o.evaluate(self._VISIBLE_TEXT_JS)) or ""
                if vis.strip():
                    opts.append((o, eng.norm(vis)))
            if not opts:
                continue
            if _DBG:
                print(f"      [pick] want={want!r} visible[:6]={[t for _, t in opts[:6]]}")
            # BEST match, not first-contains: exact > prefix > contains > reverse-contains, and
            # among equals the SHORTEST option (closest). Critical so 'United States' resolves to
            # 'United States of America' rather than 'United States Minor Outlying Islands' — a
            # wrong country cascades a different address schema and breaks the whole step.
            best: tuple[int, int, Any] | None = None
            for o, txt in opts:
                if not txt:
                    continue
                if txt == want:
                    best = (0, 0, o)
                    break
                score = (
                    1
                    if (want and txt.startswith(want))
                    else 2
                    if (want and want in txt)
                    else 3
                    if (txt and txt in want)
                    else None
                )
                if score is not None:
                    cand = (score, len(txt), o)
                    if best is None or cand[:2] < best[:2]:
                        best = cand
            target = (best[2] if best else None) or (opts[0][0] if allow_fallback else None)
            if target is None:
                return False
            with contextlib.suppress(Exception):
                await target.click()
                return True
            return False
        return False

    async def _click_radio(self, page: Any, field: FormField, value: str) -> bool:
        """Select a Workday choice control by option text — radios AND checkbox-GROUPS ("select all
        that apply"). Markup is `<input id=X value=true><label for=X>Yes</label>`; clicking the
        LABEL does NOT check the React input (verified), so we resolve each option's text via
        label[for]=input.id and click the INPUT directly. Match label text exact -> contains -> the
        value attr (yes->true / no->false)."""
        sel = f'[data-automation-id="{field.name}"]'
        want = eng.norm(value)
        yn = {"yes": "true", "no": "false", "true": "true", "false": "false"}.get(want)
        radios = await page.get_elements_by_css_selector(
            f'{sel} input[type="radio"], {sel} [role="radio"], {sel} input[type="checkbox"], {sel} [role="checkbox"]'
        )
        scored: list[tuple[Any, str, str]] = []
        for el in radios:
            with contextlib.suppress(Exception):
                info = await el.evaluate(
                    "() => { let t=''; if(this.id){const l=document.querySelector('label[for=\"'+this.id+'\"]');"
                    " if(l) t=l.textContent||'';} if(!t) t=this.getAttribute('aria-label')||'';"
                    " return (t.replace(/\\s+/g,' ').trim())+'|~|'+(this.getAttribute('value')||''); }"
                )
                txt, _, val = (info or "").partition("|~|")
                scored.append((el, eng.norm(txt), eng.norm(val)))
        target = next((el for el, t, _ in scored if t == want and want), None)
        if not target:
            target = next((el for el, t, _ in scored if want and t and (want in t or t in want)), None)
        if not target and yn:
            target = next((el for el, _, v in scored if v == yn), None)
        if not target:
            return False
        # Robust check (DomHand _CLICK_BINARY_FIELD_JS pattern): a plain CDP click on a Workday
        # checkbox/radio often misses — the real <input> is hidden behind a styled label. Click the
        # VISIBLE label/wrapper with the native .click() (which performs the toggle), fall back to
        # the input, and fire input/change so React registers it. Skip if already checked.
        with contextlib.suppress(Exception):
            res = await target.evaluate(
                "() => { const el=this;"
                " const on=()=>el.checked||el.getAttribute('aria-checked')==='true';"
                " if(on()) return 'already';"
                " const lbl=el.id?document.querySelector('label[for=\"'+el.id+'\"]'):null;"
                " const node=lbl||(el.closest&&el.closest('label'))||el;"
                " if(node.scrollIntoView) node.scrollIntoView({block:'center'});"
                " if(node.click) node.click(); if(!on() && el.click) el.click();"
                " el.dispatchEvent(new Event('input',{bubbles:true}));"
                " el.dispatchEvent(new Event('change',{bubbles:true}));"
                " return on()?'ok':'fail'; }"
            )
            if res in ("ok", "already"):
                return True
        with contextlib.suppress(Exception):  # last resort: the plain CDP click
            await target.click()
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
        wrap = f'[data-automation-id="{field.name}"]'
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
        # A widget's commit (listbox button text, radio check, pill) can lag the click by a
        # re-render, so an immediate single read false-negatives — POLL the check (returns the
        # instant it passes, so a correct fill costs nothing extra).
        tries = 1 if field.type in ("input_text", "textarea", "file") else 6
        for i in range(tries):
            if await self._read_once(page, field, value):
                return True
            if i + 1 < tries:
                await asyncio.sleep(0.3)
        return False

    async def _read_once(self, page: Any, field: FormField, value: str) -> bool:
        t = field.type
        sel = f'[data-automation-id="{field.name}"]'
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
        if t == "radio":
            # Read the CHECKED input's label (NOT the wrapper text — both option labels are
            # always present there). Map Workday's true/false value attr to yes/no.
            with contextlib.suppress(Exception):
                got = await page.evaluate(
                    f"() => {{ const w=document.querySelector('{sel}'); if(!w) return '';"
                    " const c=[...w.querySelectorAll('input[type=radio],[role=radio]')]"
                    ".find(i=>i.checked||i.getAttribute('aria-checked')==='true'); if(!c) return '';"
                    " let t=''; if(c.id){{const l=document.querySelector('label[for=\"'+c.id+'\"]'); if(l) t=l.textContent;}}"
                    " return (t||c.getAttribute('value')||'').trim(); }}"
                )
                g = {"true": "yes", "false": "no"}.get(eng.norm(got), eng.norm(got))
                w = eng.norm(value)
                return bool(g) and bool(w) and (w in g or g in w)
            return False
        if t in ("single_select", "multi_select"):
            # single_select: the chosen label replaces "Select One" in the wrapper text.
            # multi_select: the committed pill(s) appear in `selectedItem` / the wrapper text.
            with contextlib.suppress(Exception):
                txt = await page.evaluate(
                    f"() => {{ const w=document.querySelector('{sel}'); if(!w) return '';"
                    " const pills=[...w.querySelectorAll('[data-automation-id=\\\"selectedItem\\\"]')]"
                    ".map(e=>e.textContent).join(' '); return (pills||'')+' '+(w.textContent||''); }}"
                )
                return bool(eng.norm(value)) and eng.norm(value) in eng.norm(txt)
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

    # -- repeaters: off-schema "Add Another" sections (My Experience) -------
    # GENERIC by design (第一性原理): a repeater is matched to a profile LIST by the section's
    # HEADING keyword — never a hardcoded aid — so "Work Experience" / "Professional Experience" /
    # "Employment History" all resolve to profile.experience, and a tenant/Oracle relabel just
    # re-keys. Each present section is handed to eng.agent_fill_section (the proven single-page
    # pattern: it scrolls to the section, clicks "Add Another" per entry, and drives the searchable
    # comboboxes — School/Degree — that deterministic string-match gets wrong). Already-filled
    # fields are frozen and Submit stays disabled for the duration, so it can't disturb prior steps
    # or finalize. Deterministic add-row fill (handoff A2) layers on later to cut cost.
    _REPEATERS = (
        ("experience", ("experien", "employ", "work history"), False),
        ("education", ("educat", "academ", "school"), False),
        ("skills", ("skill",), True),
        ("languages", ("language",), True),
        ("certifications", ("certif", "licen"), True),
    )

    async def fill_repeaters(self, session: Any, page: Any, profile: dict) -> dict:
        # HARD GATE: only run on a page that actually HAS a repeater affordance — an "Add"/"Add
        # Another" control. Without this, a keyword in some unrelated QUESTION text (e.g. "employ"
        # in an export-control question on Application Questions) would falsely fire the experience
        # agent on the wrong page, where it then tries to NAVIGATE to find the section (dangerous).
        has_add = await page.evaluate(
            '() => !!document.querySelector(\'[data-automation-id="Add"],[data-automation-id*="add-button"],'
            '[data-automation-id*="addButton"]\')'
            " || [...document.querySelectorAll('button')].some(b=>/^add( another)?$/i.test((b.textContent||'').trim()))"
        )
        if str(has_add).lower() != "true":
            return {}
        # DETERMINISTIC-FIRST (wd_repeaters): detect rows via data-fkit-id -> ONE semantic map call ->
        # fixpoint reconcile-and-repair loop (Add-Another dup-guarded, put() per archetype, read-back
        # verified). The section AGENT is now only a RESIDUAL backstop for whatever the loop can't close,
        # not the driver. NEVER submits (put never clicks Submit; the agent runs submit-disabled).
        import os

        import wd_repeaters as wr

        from browser_use import ChatGoogle

        gkey = os.environ.get("GOOGLE_API_KEY")
        llm = ChatGoogle(model="gemini-3.1-flash-lite", api_key=gkey) if gkey else None
        out: dict = {}
        with contextlib.suppress(Exception):
            out["deterministic"] = await wr.fill_deterministic(self, session, page, profile, llm)
        residual = {r.split("[")[0].split(".")[0] for r in (out.get("deterministic") or {}).get("residual", [])}

        # RESIDUAL backstop: only sections the deterministic loop could not fully close fall to the agent.
        headings = [h for h in await self._page_headings(page) if len(h) <= 40]
        for key, keywords, is_tag in self._REPEATERS:
            items = profile.get(key) or []
            if not items or key not in residual:
                continue
            if not any(any(kw in h for kw in keywords) for h in headings):
                continue  # section not on THIS page — generic gate, no hardcoded aid
            label = key.capitalize()
            if is_tag:  # Skills / Languages / Certs: typeahead tags (type + pick), one at a time
                vals = ", ".join(self._item_str(it) for it in items)
                instr = (
                    f"Add these {label} one at a time using the section's typeahead/'Add' control "
                    f"(type each, then pick the matching option so it becomes a tag/pill): {vals}"
                )
            else:  # Experience / Education: ROW repeater
                entries = "; ".join(f"entry {i + 1}: {self._item_str(it)}" for i, it in enumerate(items))
                instr = (
                    f"Add {len(items)} {label} entr{'y' if len(items) == 1 else 'ies'}. Use the "
                    f"'Add Another' button before each entry, then fill its fields by label. Dates "
                    f"like '2021-06' go in the segmented month/year inputs. {entries}"
                )
            res = await eng.agent_fill_section(session, page, section=label, instructions=instr, max_steps=18)
            out[label] = {"items": len(items), "residual_agent": True, **res}
        return out

    @staticmethod
    def _item_str(item: Any) -> str:
        """Flatten a profile list-item to a 'Key=Value, ...' string the agent maps by label."""
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            parts = []
            for k, v in item.items():
                if v in (None, "", [], {}):
                    continue
                parts.append(f"{k.replace('_', ' ')}='{str(v)[:300]}'")
            return ", ".join(parts)
        return str(item)

    async def _page_headings(self, page: Any) -> list[str]:
        """Lower-cased visible section headings/labels — used to GENERICALLY detect which repeater
        sections are present (heading keyword match), independent of any tenant-specific aid."""
        with contextlib.suppress(Exception):
            raw = await page.evaluate(
                "() => JSON.stringify([...document.querySelectorAll("
                '\'h1,h2,h3,h4,[data-automation-id*="title"],[data-automation-id*="Title"],'
                "[data-automation-id*=\"pageHeader\"]')].map(e=>(e.textContent||'').replace(/\\s+/g,' ')"
                ".trim().toLowerCase()).filter(Boolean).slice(0,60))"
            )
            import json

            return json.loads(raw) if raw else []
        return []

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
