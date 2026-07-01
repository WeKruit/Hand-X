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

_MATCH_LLM: Any = None


def _match_llm() -> Any:
    """The cheap text LLM (gemini-3.1-flash-lite) that makes the option-MATCH decision. ats_engine.
    pick_dropdown is LLM-ONLY (directive #3): with llm=None it cannot decide and returns False, which
    breaks the live single_select / Degree / School / Field commit path that routes to the vision
    handoff. So _pick_option threads THIS real llm into pick_dropdown. Built once, reused."""
    global _MATCH_LLM
    if _MATCH_LLM is None:
        with contextlib.suppress(Exception):
            from vision_verify import _vlm

            _MATCH_LLM = _vlm()
    return _MATCH_LLM


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


async def _ordinary_answer(llm: Any, question: str, options: list[str]) -> str | None:
    """The cheap LLM DECIDES the ordinary external-applicant answer to a required screening / eligibility
    question — polarity-aware so 'prior employee of Intel?' -> No while '18 or older?' / 'authorized to
    work?' -> Yes. Returns the EXACT option text, or None. (Decision = LLM; the commit is deterministic.)"""
    import contextlib

    if llm is None or not options:
        return None
    from pydantic import BaseModel

    from browser_use.llm.messages import SystemMessage, UserMessage

    class _Ans(BaseModel):
        choice: str  # EXACT option text from the list, or "NONE"

    with contextlib.suppress(Exception):
        res = await llm.ainvoke(
            [
                SystemMessage(
                    content="A typical EXTERNAL job applicant is answering a REQUIRED application question. "
                    "Choose the option an ordinary applicant would pick, by question TYPE:\n"
                    "1) ELIGIBILITY / SCREENING (work authorization, age, prior/current employment with the "
                    "company, sponsorship, conflicts, relatives): answer TRUTHFULLY for the ordinary applicant "
                    "— is NOT a current/former employee and has no prior conflict (-> No); IS legally "
                    "authorized to work and IS 18+ (-> Yes); requires sponsorship -> No. Mind polarity.\n"
                    "2) VOLUNTARY DEMOGRAPHIC / EEO SELF-IDENTIFICATION (ethnicity, race, gender, "
                    "Hispanic/Latino, veteran status, disability, sexual orientation): DECLINE — pick the "
                    "option meaning 'I don't wish to answer' / 'Decline to self-identify' / 'Prefer not to "
                    "answer' / 'I do not wish to disclose'. If no such decline option exists, reply 'NONE'.\n"
                    "3) HOW/WHERE the applicant heard about the role, or their source/referral channel: prefer "
                    "'LinkedIn'; if not an option, 'Other'; if neither, the first non-placeholder option.\n"
                    "4) PREFERRED / SPOKEN LANGUAGE: pick the option for 'English' (or the one containing "
                    "'English'); if absent, the first non-placeholder option.\n"
                    "Reply the EXACT option text from the list, or 'NONE' if none fit."
                ),
                UserMessage(content=f"question: {question!r}\noptions: {options}"),
            ],
            output_format=_Ans,
        )
        c = (res.completion.choice or "").strip()
        return None if c.upper() == "NONE" or not c else c
    return None


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

        # 2a. SIGN IN to a TRACKED account (reuse — Workday rate-limits repeated creates, so a
        #     previously-created account must sign in, never re-register). Ensure we are on the
        #     SIGN-IN form (a verifyPassword field means we're on the Create form -> signInLink back).
        if creds.existing:
            if await eng.first(page, '[data-automation-id="verifyPassword"]'):
                await self._click_aid(page, "signInLink")
                await self._settle(page)
            await self._fill_aid(page, "email", creds.email)
            await self._fill_aid(page, "password", creds.password)
            await self._click_aid(page, "signInSubmitButton")
            await self._settle(page)
            page = await session.must_get_current_page()
            # success == past the gate: the wizard form is present and no password field remains.
            if await eng.first(
                page, '[data-automation-id="progressBar"], [data-automation-id^="formField-"]'
            ) and not await eng.first(page, '[data-automation-id="password"]'):
                return AuthResult(ok=True)
            # sign-in rejected (deleted account / wrong password) — signal the CALLER to rotate to a
            # fresh account + re-store (email generation belongs to the caller, not here).
            return AuthResult(ok=False, reason="SIGN_IN_FAILED: tracked account rejected — rotate + recreate.")

        # 2b. CREATE a fresh account. Ensure CREATE-ACCOUNT mode (verifyPassword present == Create form).
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
            return AuthResult(
                ok=False, reason="Create Account did not advance (validation / CAPTCHA / already exists?)."
            )
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
        # Workday transient error ("Something went wrong — please refresh the page and then try again"):
        # the step renders NO fields and NO advance button. RELOAD the current URL (deterministic
        # recovery — the browser-use agent would refresh too) then re-mount, and re-assert the
        # submit-guard (a reload clears injected JS). Bounded so a persistently-broken page can't loop.
        for _ in range(2):
            broken = False
            with contextlib.suppress(Exception):
                broken = (
                    str(
                        await page.evaluate(
                            "() => /something went wrong|please refresh the page/i.test((document.body||{}).innerText||'')"
                        )
                    ).lower()
                    == "true"
                )
            if not broken:
                break
            print("  [wd] step errored ('Something went wrong') — reloading page", flush=True)
            with contextlib.suppress(Exception):
                await session.navigate_to(await page.get_url())
                await self._await_step_mounted(page)
                await eng.install_submit_guard(page)
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

    # DomHand file-input selectors, MOST-specific first. The HandX/DomHand resume path NEVER failed:
    # scan the LIVE page fresh for the real <input type=file> and push bytes via CDP setFileInputFiles.
    _FILE_INPUT_SELECTORS = (
        '[data-automation-id="file-upload-input-ref"]',
        '[data-automation-id="file-upload-drop-zone"] input[type="file"]',
        'input[type="file"]',
    )

    async def upload_resume(self, session: Any, page: Any, resume: str | None) -> bool:
        """DETERMINISTIC-ONLY resume upload (directive #1). Runs FRESH at the top of EVERY wizard step
        and is IDEMPOTENT: scan the live DOM for the file input via the three DomHand selectors, skip if
        that input already shows a successful upload, otherwise push the resume bytes via CDP
        (eng.upload_file -> DOM.setFileInputFiles). NEVER escalates to an agent — the agent-driven upload
        network-errors and loops (intel_wd4.log). Re-uploads if the field reappears empty on a later step.

        Returns True iff an upload was performed THIS call (already-uploaded / no-field / no-resume -> False
        with no error). The file FormField is source='skip', so this is the ONLY path that touches it."""
        if not (resume or "").strip():
            return False
        for sel in self._FILE_INPUT_SELECTORS:
            fel = await eng.first(page, sel)
            if not fel:
                continue
            # IDEMPOTENT GATE: a "file-upload-successful" marker anywhere in this input's wrapper means
            # the resume is already attached for this step — do NOT re-push (avoids dup/overwrite loops).
            done = False
            with contextlib.suppress(Exception):
                done = bool(
                    await fel.evaluate(
                        "() => { const w=this.closest('[data-automation-id=\"file-upload-drop-zone\"]')"
                        " || this.closest('[data-automation-id^=\"formField-\"]') || this.parentElement;"
                        " return !!(w && w.querySelector('[data-automation-id=\"file-upload-successful\"]')); }"
                    )
                )
            if done:
                return False
            # Push bytes, then CONFIRM the success marker mounted. A Workday upload endpoint can
            # transiently NETWORK-ERROR (observed live on Alteryx step 3: "Alert - Upload a file:
            # Network Error", input left EMPTY while validation still demands it). The old code returned
            # True on marker lag, so the wizard advanced into a "file required" block it could not clear.
            # A manual re-push succeeded — so RETRY the push until the marker confirms, never declaring
            # success on an unconfirmed upload.
            for attempt in range(3):
                ok = await eng.upload_file(session, page, fel, resume)
                if _DBG:
                    print(f"   [upload_resume] sel={sel!r} attempt={attempt} pushed={ok}")
                if ok:
                    for _ in range(8):
                        await asyncio.sleep(0.4)
                        if await eng.first(page, '[data-automation-id="file-upload-successful"]'):
                            return True
                # marker never mounted (network error / lag) — re-scan the input and retry the push
                fel = await eng.first(page, sel) or fel
            return False
        return False

    # repeater sections are owned by fill_repeaters (wd_repeaters), NOT the per-field schema ladder —
    # match the data-fkit-id section token so the ladder doesn't DOUBLE-fill what the engine fills.
    _REPEATER_FKIT = ("workexperience-", "education-", "skills-", "skills--", "languages-", "certifications-")

    def _to_field(self, f: dict) -> FormField:
        t = f["type"]
        # RESUME UPLOAD is DETERMINISTIC-ONLY (directive #1): a file field is NEVER handed to the
        # per-field ladder/escalate — it would network-error then loop a browser_use.Agent. Mark it
        # 'skip' (like a repeater fkit) so run_wizard's ladder never touches it; the dedicated,
        # idempotent upload_resume() scans the live DOM fresh on EVERY step and pushes bytes via CDP.
        if t == "file" or any(f["name"].lower().startswith(p) for p in self._REPEATER_FKIT):
            source = "skip"  # single-owner: upload_resume() (file) / wd_repeaters (repeaters), not the ladder
        else:
            source = (
                "select"
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
    @staticmethod
    def _wsel(name: str, suffix: str = "") -> str:
        """Wrapper selector matching EITHER the unique data-fkit-id (field.name post row-aware change)
        or the data-automation-id (fallback); `suffix` is applied to BOTH branches."""
        return f'[data-fkit-id="{name}"]{suffix}, [data-automation-id="{name}"]{suffix}'

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
            ) and await self._click_radio(session, page, field, value):
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
            return await self._click_radio(session, page, field, value)
        if t == "single_select":
            return await self._listbox(session, page, field, value)
        if t == "multi_select":
            return await self._multiselect(session, page, field, value)
        if t == "date":
            # A bare "Date" field (Voluntary/Self-Identify signature date) or an empty required date
            # must be TODAY — Workday validates "Enter today's date". A specific date (Start Date, DOB)
            # keeps the mapped value. Uses the local system date.
            if not (value or "").strip() or eng.norm(field.label or "") in (
                "date",
                "todaysdate",
                "signaturedate",
                "dateofsignature",
            ):
                import datetime

                value = datetime.date.today().isoformat()
            return await self._date(page, field, value)
        return False

    async def _multiselect(self, session: Any, page: Any, field: FormField, value: str) -> bool:
        """Workday typeahead multiselect (`multiSelectContainer` + `selectinput`): type the value
        to filter, then commit the highlighted top match with a TRUSTED Enter (see below).
        Single-value (commit one); commit-then-add for repeaters is handled elsewhere."""
        inp = await eng.first(
            page, self._wsel(field.name, ' [data-uxi-widget-type="selectinput"] input')
        ) or await eng.first(page, self._wsel(field.name, " input"))
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
        await asyncio.sleep(0.5)  # let the typeahead filter + highlight the top match
        await eng.press_enter_trusted(session, page)
        await asyncio.sleep(0.3)
        ok = await self.read_back(session, page, field, value)
        if not ok:
            # Some Workday/Salesforce typeaheads (the multi-pill Skills field) do NOT auto-highlight the
            # top match, so a plain Enter commits nothing. ArrowDown highlights the first suggestion, then
            # Enter commits it — same primitive, just the keyboard-nav the widget needs.
            await eng.press_key_trusted(session, page, key="ArrowDown", code="ArrowDown", vk=40)
            await asyncio.sleep(0.2)
            await eng.press_enter_trusted(session, page)
            await asyncio.sleep(0.3)
            ok = await self.read_back(session, page, field, value)
        if _DBG:
            print(f"   [msel {field.name}] value={value!r} committed={ok}")
        return ok

    async def _listbox(self, session: Any, page: Any, field: FormField, value: str) -> bool:
        """Workday button-listbox: click trigger -> options mount in the body portal
        `activeListContainer` -> commit the matching option. SEARCHABLE listboxes (State, Country,
        Phone-type, How-did-you-hear, repeater Degree) show NO options until you TYPE — so type the
        value to filter first; an inline (non-searchable) listbox just shows them (no input).

        CRITICAL (w4hwdepap): the body portal is SHARED across every dropdown and re-serves a FROZEN
        list — the same options regardless of which field is open or what was typed — so an unscoped
        read + click commits a WRONG value (Python->'Computer Science') and never selects the right
        one. Fix: SCOPE the read to the listbox THIS input OWNS (aria-controls / aria-owns id), and
        for the searchable path COMMIT with a TRUSTED CDP Enter on the widget's pre-highlighted top
        match (the proven Greenhouse react-select mechanism) instead of clicking a node that may have
        re-rendered. The pre-type option snapshot lets _pick_option POLL until the list actually
        reflects the typed filter, then abort FAST if it stays stale — no fixed-sleep spin."""
        trig = await eng.first(page, self._wsel(field.name, " button"))
        if not trig:
            return False
        with contextlib.suppress(Exception):
            await trig.click()
        await asyncio.sleep(0.4)
        # The input the trigger owns. Prefer a field-scoped input; only fall back to the shared
        # portal input. Scope the option read to the container this input declares it controls.
        inp = (
            await eng.first(page, self._wsel(field.name, " input"))
            or await eng.first(page, '[data-automation-id="activeListContainer"] input')
            or await eng.first(page, 'input[aria-autocomplete="list"]')
        )
        owned_id = ""
        if inp:
            with contextlib.suppress(Exception):
                owns = (await inp.get_attribute("aria-controls")) or (await inp.get_attribute("aria-owns")) or ""
                owned_id = owns.split()[0] if owns.split() else ""
        if inp:
            # snapshot the option texts BEFORE typing — _pick_option polls until they CHANGE, so a
            # frozen shared list is detected (stale N times) instead of spun against. On that frozen
            # detection it HANDS OFF to the shared VISUAL primitive (reads the rendered options from a
            # screenshot — no DOM lag — and commits trusted Enter + value-verifies).
            before = await self._option_texts(page, owned_id)
            with contextlib.suppress(Exception):
                await inp.fill(value)
            return await self._pick_option(
                session, page, value, owned_id=owned_id, before=before, searchable=True, verify_label=field.label
            )
        # non-searchable inline listbox: options already shown, click the best (no Enter to commit).
        return await self._pick_option(session, page, value, owned_id=owned_id, before=None, searchable=False)

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

    def _opt_selector(self, owned_id: str) -> str:
        """Option selector SCOPED to the listbox the open input owns (aria-controls/owns id). The
        shared body portal serves a frozen list, so scoping to the owned container is what makes the
        read reflect THIS field. Falls back to the activeListContainer / generic option aids only
        when the widget exposes no owns-id (older inline listboxes)."""
        if owned_id:
            return f'#{owned_id} [role="option"], #{owned_id} [data-automation-id="promptOption"], #{owned_id} [data-automation-id="menuItem"]'
        return (
            '[data-automation-id="activeListContainer"] [role="option"],'
            ' [data-automation-id="promptOption"], [data-automation-id="menuItem"],'
            ' [role="listbox"] [role="option"]'  # DomHand WORKDAY_SELECTORS generic fallback
        )

    async def _read_options_live(self, page: Any, field: FormField) -> list[str]:
        """Open a single_select/multiselect's listbox, read its RAW (non-normalized) option texts,
        then close. Workday loads options ONLY on open, so extract_step reports none — this fetches
        them so answer_required_choices can decide a decline/default answer for a required field the
        LLM map left empty (e.g. Voluntary EEO). Best-effort; returns [] on any failure."""
        raw: list[str] = []
        with contextlib.suppress(Exception):
            trig = await eng.first(page, self._wsel(field.name, " button"))
            if not trig:
                return raw
            await trig.click()
            await asyncio.sleep(0.7)
            inp = await eng.first(page, self._wsel(field.name, " input")) or await eng.first(
                page, '[data-automation-id="activeListContainer"] input'
            )
            owned = ""
            if inp:
                owns = (await inp.get_attribute("aria-controls")) or (await inp.get_attribute("aria-owns")) or ""
                owned = owns.split()[0] if owns.split() else ""
            for o in await page.get_elements_by_css_selector(self._opt_selector(owned)):
                t = ((await o.evaluate("() => (this.textContent||'').trim()")) or "").strip()
                if t:
                    raw.append(t)
            with contextlib.suppress(Exception):
                await page.evaluate("() => document.body.click()")  # close the menu without committing
        return raw

    async def _option_texts(self, page: Any, owned_id: str) -> list[str]:
        """VISIBLE option texts in the scoped listbox (normalized). Used to snapshot the pre-type
        list so _pick_option can tell a filtered list from the frozen shared one."""
        out: list[str] = []
        with contextlib.suppress(Exception):
            for o in await page.get_elements_by_css_selector(self._opt_selector(owned_id)):
                vis = (await o.evaluate(self._VISIBLE_TEXT_JS)) or ""
                if vis.strip():
                    out.append(eng.norm(vis))
        return out

    async def _pick_option(
        self,
        session: Any,
        page: Any,
        value: str,
        owned_id: str = "",
        before: list[str] | None = None,
        searchable: bool = False,
        verify_label: str = "",
    ) -> bool:
        """Scoped option picker for listbox (+ inline). Reads ONLY the VISIBLE options of the listbox
        the open input OWNS (owned_id) — never the global shared portal, which re-serves a frozen
        list. Match exact -> prefix -> contains -> reverse-contains, shortest among equals.

        SEARCHABLE path (the State/Country/Degree fix): POLL until the visible options reflect the
        typed filter (differ from the pre-type `before` snapshot), bounded ~2s. If after the bound the
        list is still stale/identical for N=3 reads (frozen shared list) OR landed with no match, HAND
        OFF to the SHARED VISUAL primitive (eng.pick_dropdown): it reads the ACTUALLY-rendered options
        from a screenshot (no DOM lag), matches, commits a TRUSTED CDP Enter, and VALUE-verifies — the
        documented fix for the lagging portal. Once a fresh DOM list HAS a match, COMMIT with a trusted
        Enter on the widget's pre-highlighted top match (the proven Greenhouse mechanism).

        INLINE path (searchable=False): options are already shown and there is no input to Enter into,
        so click the best VISIBLE match directly (legacy behavior preserved)."""

        async def _scoped_dom(_page: Any) -> list[str]:
            out: list[str] = []
            for o in await _page.get_elements_by_css_selector(self._opt_selector(owned_id)):
                vis = (await o.evaluate(self._VISIBLE_TEXT_JS)) or ""
                if vis.strip():
                    out.append(eng.norm(vis))
            return out

        async def _vision_handoff() -> bool:
            # DOM is hopeless (frozen / no-match) — read the rendered options off a screenshot, commit
            # trusted Enter on the highlighted match, value-verify. The shared primitive owns this.
            return await eng.pick_dropdown(
                session,
                page,
                value,
                read_dom_options=_scoped_dom,
                llm=_match_llm(),  # REAL cheap text LLM — pick_dropdown is LLM-only, must not be None
                verify_label=verify_label or None,
                vis_key=f"{verify_label or owned_id or 'listbox'}:{value}",
            )

        want = eng.norm(value)
        before_set = set(before or [])
        stale_reads = 0
        deadline = 2.0  # bound the wait-for-filter (~2s) — replaces the fixed asyncio.sleep spin
        elapsed = 0.0
        step = 0.25
        while elapsed <= deadline:
            await asyncio.sleep(step)
            elapsed += step
            opts: list[tuple[Any, str]] = []
            for o in await page.get_elements_by_css_selector(self._opt_selector(owned_id)):
                vis = (await o.evaluate(self._VISIBLE_TEXT_JS)) or ""
                if vis.strip():
                    opts.append((o, eng.norm(vis)))
            if not opts:
                continue
            cur_set = {t for _, t in opts}
            # SEARCHABLE: require the list to have CHANGED from the pre-type snapshot before trusting
            # it (else it's the frozen shared list). After N=3 stale reads -> VLM screenshot read.
            if searchable and before is not None and cur_set == before_set:
                stale_reads += 1
                if _DBG:
                    print(f"      [pick] STALE (==before) read {stale_reads}/3 — frozen shared list?")
                if stale_reads >= 3:
                    return await _vision_handoff()
                continue
            if _DBG:
                print(f"      [pick] want={want!r} visible[:6]={[t for _, t in opts[:6]]}")
            # MATCH is LLM-ONLY (directive #3): the DOM gives only a fast EXACT-equality shortcut
            # (identity, not a heuristic). ANY non-exact decision — 'United States' vs 'United States
            # of America', 'BS' vs "Bachelor's Degree" — is the LLM's, made by the shared visual+LLM
            # picker (_vision_handoff reads the rendered options and lets the cheap flash-lite pick).
            # No startswith / contains / reverse-contains guessing here.
            exact = next((o for o, txt in opts if txt and txt == want), None)
            if exact is None:
                return await _vision_handoff()  # LLM picks over the real rendered options
            # COMMIT the exact match: searchable -> trusted Enter on the highlighted top match (input
            # still focused from _listbox's fill); inline -> click the matched node directly.
            if searchable:
                ok = await eng.press_enter_trusted(session, page)
                await asyncio.sleep(0.4)
                return ok
            with contextlib.suppress(Exception):
                await exact.click()
                return True
            return False
        # bound reached without a confident DOM match -> last-resort vision read (searchable only)
        return await _vision_handoff() if searchable else False

    async def _click_radio(self, session: Any, page: Any, field: FormField, value: str) -> bool:
        """Select a Workday choice control by option text — radios AND checkbox-GROUPS ("select all
        that apply"). Markup is `<input id=X value=true><label for=X>Yes</label>`; clicking the
        LABEL does NOT check the React input (verified), so we resolve each option's text via
        label[for]=input.id and click the INPUT directly. Match label text exact -> contains -> the
        value attr (yes->true / no->false)."""
        want = eng.norm(value)
        yn = {"yes": "true", "no": "false", "true": "true", "false": "false"}.get(want)
        radios = await page.get_elements_by_css_selector(
            ", ".join(
                self._wsel(field.name, x)
                for x in (' input[type="radio"]', ' [role="radio"]', ' input[type="checkbox"]', ' [role="checkbox"]')
            )
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
        if not target and yn:  # canonical yes/no via the value attr (true/false) — identity, not substring
            target = next((el for el, _, v in scored if v == yn), None)
        if not target:  # MATCH is LLM-ONLY (directive #3) — pick the best option label, no substring guess
            labels = [t for _, t, _ in scored if t]
            if labels:
                from vision_verify import _vlm
                from wd_repeaters import _llm_pick

                with contextlib.suppress(Exception):
                    choice = await _llm_pick(_vlm(), value, labels)
                    if choice:
                        target = next((el for el, t, _ in scored if t == eng.norm(choice)), None)
        if not target:
            return False
        # PRIMARY COMMIT: a TRUSTED CDP pointer event on the VISIBLE label (the <input> is 0x0/hidden
        # behind a styled label, and a SYNTHETIC .click()/label-click does NOT flip a React radio's
        # onChange state — the verified Workday failure that loops the vision agent). Click the label
        # for real, then verify the input actually checked.
        with contextlib.suppress(Exception):
            iid = await target.evaluate("() => this.id || ''")
            clicker = (await eng.first(page, f'label[for="{iid}"]')) if iid else None
            if await eng.click_trusted(session, page, clicker or target):
                await asyncio.sleep(0.2)
                if await target.evaluate("() => this.checked===true || this.getAttribute('aria-checked')==='true'"):
                    return True
        # FALLBACK (DomHand _CLICK_BINARY_FIELD_JS pattern): native .click() on the label/wrapper + fire
        # input/change so React registers it; last-resort plain CDP click. Skip if already checked.
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

    async def answer_required_choices(self, session: Any, page: Any, llm: Any = None) -> int:
        """Deterministically answer REQUIRED radio / checkbox-group screening questions the LLM map
        left empty — e.g. Intel gates My-Information on 'Are you currently or have you previously been
        employed by Intel?'. That React radio NEVER committed deterministically (it always fell to a
        flaky vision agent that loops on it). Here the cheap LLM DECIDES the ordinary external-applicant
        answer (polarity-aware: 'prior employee?'->No, '18 or older?'/'authorized to work?'->Yes) and
        the robust _click_radio commits it via CDP — BEFORE any agent. Generic: any required choice
        with no current selection. Returns the number answered (caller re-tries advance)."""
        if llm is None:
            with contextlib.suppress(Exception):
                from vision_verify import _vlm

                llm = _vlm()
        answered = 0
        with contextlib.suppress(Exception):
            step = await self.extract_step(session, page, {})
            for f in step.fields:
                if not f.required:
                    continue
                if f.type in ("radio", "checkbox"):
                    opts = [o for o in (f.options or []) if o and not eng.norm(o).lower().startswith("select")]
                    if not opts:
                        continue
                    already = await page.get_elements_by_css_selector(
                        self._wsel(f.name, " input:checked") + ", " + self._wsel(f.name, ' [aria-checked="true"]')
                    )
                    if already:
                        continue  # the map already answered it — don't disturb
                    choice = await _ordinary_answer(llm, f.label, opts)
                    if (
                        choice
                        and await self._click_radio(session, page, f, choice)
                        and await self.read_back(session, page, f, choice)
                    ):
                        answered += 1
                        print(f"  [wd] screening answered (verified): {f.label[:48]!r} -> {choice!r}", flush=True)
                elif f.type == "single_select":
                    # REQUIRED Workday DROPDOWN the map left on "Select One": Yes/No eligibility (authorized
                    # to work?, 18+?, prior worker?, relatives?, sponsorship?) OR a VOLUNTARY EEO self-ID
                    # (ethnicity / gender / Hispanic / veteran -> DECLINE). Workday loads options ONLY on
                    # open, so extract_step reports none — READ THEM LIVE, then _ordinary_answer decides
                    # (eligibility: truthful Yes/No; EEO: 'I don't wish to answer') and _listbox commits.
                    cur = ""
                    with contextlib.suppress(Exception):
                        btn = await eng.first(page, self._wsel(f.name, " button"))
                        if btn:
                            cur = (await btn.evaluate("() => (this.textContent||'').trim()")) or ""
                    if cur and not eng.norm(cur).startswith("selectone"):
                        continue  # already has a selection — don't disturb
                    opts = f.options or await self._read_options_live(page, f)
                    opts = [o for o in opts if o and not eng.norm(o).lower().startswith("select")]
                    if not opts:
                        continue
                    choice = await _ordinary_answer(llm, f.label, opts)
                    if (
                        choice
                        and await self._listbox(session, page, f, choice)
                        and await self.read_back(session, page, f, choice)
                    ):
                        answered += 1
                        print(
                            f"  [wd] required dropdown answered (verified): {f.label[:48]!r} -> {choice!r}", flush=True
                        )
                elif f.type == "multi_select":
                    # REQUIRED Workday multiselect the map left EMPTY — chiefly "How Did You Hear About
                    # Us?". Read options live; _ordinary_answer defaults (LinkedIn -> Other -> first) and
                    # _multiselect commits. Skip if a pill is already present, or the LLM returns NONE (so
                    # a genuine "select all that apply" multiselect is left untouched).
                    already = await page.get_elements_by_css_selector(
                        self._wsel(f.name, ' [data-automation-id="selectedItem"]')
                    )
                    if already:
                        continue
                    opts = f.options or await self._read_options_live(page, f)
                    opts = [o for o in opts if o and not eng.norm(o).lower().startswith("select")]
                    if not opts:
                        continue
                    choice = await _ordinary_answer(llm, f.label, opts)
                    if (
                        choice
                        and await self._multiselect(session, page, f, choice)
                        and await self.read_back(session, page, f, choice)
                    ):
                        answered += 1
                        print(
                            f"  [wd] required multiselect answered (verified): {f.label[:48]!r} -> {choice!r}",
                            flush=True,
                        )
        return answered

    async def _date(self, page: Any, field: FormField, value: str) -> bool:
        """Segmented date spinbuttons — type continuous digits (Workday auto-advances). SEGMENT-AWARE:
        Experience/Education dates are MM/YYYY (no Day segment); My-Information dates are MM/DD/YYYY.
        Inserting a day into a MM/YYYY widget overflows the Year ('2021-06' -> garbage 'Year 6012'),
        so only include the day when the widget actually HAS a Day segment."""
        parts = (value or "").split("-")  # ISO YYYY-MM-DD or YYYY-MM
        if len(parts) < 2:
            return False
        mm, yyyy = parts[1].zfill(2), parts[0]
        dd = parts[2].zfill(2) if len(parts) >= 3 else "01"

        async def _set(el: Any, v: str) -> bool:
            if not el or not v:
                return False
            with contextlib.suppress(Exception):
                await el.click()
                await el.fill(v)  # fill() clears the segment first — replaces stale garbage (e.g. Year 3020)
                await el.evaluate(
                    "() => { this.dispatchEvent(new Event('input',{bubbles:true}));"
                    " this.dispatchEvent(new Event('change',{bubbles:true})); }"
                )
                return True
            return False

        async def _seg(token: str) -> Any:
            return await eng.first(
                page, self._wsel(field.name, f' input[data-automation-id="{token}"]')
            ) or await eng.first(page, f'input[data-automation-id="{token}"]')

        # (1) SEGMENTED spinbuttons — set EACH segment SEPARATELY (mm, dd, yyyy). Typing the whole
        # "MMDDYYYY" into the Month segment relies on keystroke auto-advance and MANGLES some widgets
        # (Self-Identify signature date landed 'Year 3020'). Experience/Education are MM/YYYY (no Day).
        month = await _seg("dateSectionMonth-input")
        if month:
            day = await _seg("dateSectionDay-input")  # absent on MM/YYYY widgets
            year = await _seg("dateSectionYear-input")
            ok = await _set(month, mm)
            if day and len(parts) >= 3:
                await _set(day, dd)
            if year:
                await _set(year, yyyy)
            return ok

        # (2) PLAIN date <input> — type the displayed MM/DD/YYYY, like a human.
        inp = (
            await eng.first(page, self._wsel(field.name, ' input[type="text"]'))
            or await eng.first(page, self._wsel(field.name, " input"))
            or await self.locate(page, field)
        )
        return await _set(inp, f"{mm}/{dd}/{yyyy}")

    async def read_back(self, session: Any, page: Any, field: FormField, value: str) -> bool:
        # A widget's commit (listbox button text, radio check, pill) can lag the click by a
        # re-render, so an immediate single read false-negatives — POLL the check (returns the
        # instant it passes, so a correct fill costs nothing extra).
        tries = 1 if field.type in ("input_text", "textarea", "file") else 3
        for i in range(tries):
            if await self._read_once(page, field, value):
                return True
            if i + 1 < tries:
                await asyncio.sleep(0.25)
        # CHIP/SELECT: the DOM read-back compares by SUBSTRING and false-negatives a CANONICAL commit
        # ("Computer Science" is not a substring of the committed "Computer and Information Science";
        # "United States of America" vs "...(+1)"). That false-negative is what marks a committed chip
        # residual -> the agent re-does it -> the timeout. Confirm SEMANTICALLY with the value-aware VLM
        # (reuse the visuals primitive) before declaring failure.
        if field.type in ("single_select", "multi_select") and session is not None:
            with contextlib.suppress(Exception):
                from vision_verify import _matches, visual_check

                if _matches(await visual_check(session, field.label or field.name, want=value)):
                    return True
        return False

    async def _read_once(self, page: Any, field: FormField, value: str) -> bool:
        t = field.type
        sel = self._wsel(field.name)
        if t == "file":
            return (
                await eng.first(page, self._wsel(field.name, ' [data-automation-id="file-upload-successful"]'))
            ) is not None
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
        try:
            out["deterministic"] = await wr.fill_deterministic(self, session, page, profile, llm)
            print(f"  repeaters[deterministic]: {out['deterministic']}")
        except Exception as exc:  # surface (not swallow) so the engine's failure is diagnosable
            import traceback

            out["deterministic_error"] = f"{type(exc).__name__}: {exc}"
            print(f"  [wd_repeaters ERROR] {out['deterministic_error']}\n{traceback.format_exc()}")
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
            if is_tag:  # Skills / Languages / Certs: typeahead tags
                vals = ", ".join(self._item_str(it) for it in items)
                instr = (
                    f"Fill the {label} typeahead. For EACH value: type it, WAIT for the suggestion menu to "
                    f"show a matching option, then press Enter to commit it as a tag/pill. Do NOT add the "
                    f"same value twice. Values: {vals}"
                )
            else:  # Experience / Education: ROW repeater
                entries = "; ".join(f"entry {i + 1}: {self._item_str(it)}" for i, it in enumerate(items))
                # CRITICAL: the rows are ALREADY on the page (the deterministic engine mounted them). The
                # agent must NOT click 'Add Another'/'Add' — that creates DUPLICATE empty rows (the 9-row
                # bug). It only fills the fields still EMPTY in the existing rows.
                instr = (
                    f"The {label} rows already EXIST on the page — do NOT click 'Add Another' or 'Add' "
                    f"(that makes duplicate empty rows). Fill ONLY the fields that are still EMPTY in the "
                    f"existing rows, by label. For a searchable dropdown (School, Degree, Field of Study), "
                    f"type the value, WAIT for the option to appear, then press Enter to commit. Dates like "
                    f"'2021-06' go in the segmented month/year inputs. {entries}"
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


# --------------------------------------------------------------------------- #
# Offline self-test — the deterministic DECISION plumbing that needs no browser.
# `python ats_workday.py --selftest`  ($0, no network, no browser).
# --------------------------------------------------------------------------- #
async def _selftest() -> int:
    checks: list[tuple[str, bool]] = []

    def chk(name: str, ok: bool) -> None:
        checks.append((name, bool(ok)))

    # _ordinary_answer plumbing: hands the question+options to the LLM, returns the EXACT choice,
    # maps NONE / blank / no-llm / no-options -> None. (The DECISION policy lives in the system
    # prompt and is exercised live; here we pin the wiring so a refactor can't silently break it.)
    class _Comp:
        def __init__(self, choice: str) -> None:
            self.choice = choice

    class _Res:
        def __init__(self, choice: str) -> None:
            self.completion = _Comp(choice)

    class _FakeLLM:
        def __init__(self, choice: str) -> None:
            self._choice = choice
            self.last = ""

        async def ainvoke(self, messages, output_format=None):
            self.last = " ".join(str(getattr(m, "content", m)) for m in messages)
            return _Res(self._choice)

    yes = _FakeLLM("Yes")
    r = await _ordinary_answer(yes, "Are you authorized to work?", ["Yes", "No"])
    chk("returns the exact LLM choice", r == "Yes")
    chk("the option list is handed to the LLM", "Yes" in yes.last and "No" in yes.last)
    chk("the question is handed to the LLM", "authorized to work" in yes.last)
    chk("the system prompt carries the EEO-decline policy", "decline" in yes.last.lower())
    chk("the system prompt carries the source->LinkedIn policy", "linkedin" in yes.last.lower())
    chk("the system prompt carries the language->English policy", "english" in yes.last.lower())
    chk("NONE -> None", (await _ordinary_answer(_FakeLLM("NONE"), "q", ["a", "b"])) is None)
    chk("blank choice -> None", (await _ordinary_answer(_FakeLLM(""), "q", ["a", "b"])) is None)
    chk("no llm -> None", (await _ordinary_answer(None, "q", ["a"])) is None)
    chk("no options -> None", (await _ordinary_answer(_FakeLLM("a"), "q", [])) is None)
    rd = await _ordinary_answer(
        _FakeLLM("I don't wish to answer"),
        "Please select the ethnicity which most describes you",
        ["White", "Asian", "I don't wish to answer"],
    )
    chk("EEO decline choice passes through", rd == "I don't wish to answer")

    chk("Workday host recognized", any(h in "acme.wd1.myworkdayjobs.com" for h in WorkdayAdapter.hosts))
    chk("multi_page flag set", WorkdayAdapter.multi_page is True)

    ok = all(p for _, p in checks)
    print("\n=== ats_workday offline self-test (fake LLM, no browser, $0) ===")
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print(f"\n{'>>> ALL PASS' if ok else '>>> SOME FAIL'}  ({len(checks)} checks)")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(asyncio.run(_selftest()))
