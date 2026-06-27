"""Lever adapter — the platform-specific surface behind the generic engine.

EXTRACT  : Lever has NO public form-schema API. The public Postings API
           (`api.lever.co/v0/postings/<org>/<id>?mode=json`) returns only the job
           DESCRIPTION (text, lists, salary) — it does NOT carry the application's
           custom questions. Those `cards[<uuid>][fieldN]` controls exist ONLY in the
           live `/apply` DOM. So extract spins up a short-lived browser, scrapes the
           rendered form once, and emits the normalized [FormField] list. (The engine's
           contract permits a browser in extract: "schema API … else by classifying the
           live DOM".) The Postings API is still used — for the job TITLE that feeds the
           MAP call's context.

FILL     : Lever keys EVERY input by its `name` attribute. The scheme is fixed:
             - contact   : name, email, phone, location, org (current company)
             - urls      : urls[LinkedIn] urls[GitHub] urls[Twitter] urls[Portfolio] urls[Other]
             - resume    : <input type=file name="resume">  (CDP upload)
             - custom Q  : cards[<uuid>][field0] …  (text / textarea / radio / checkbox)
             - native    : <select> (location pick, EEO) → el.select_option
           Radios/checkboxes carry value="<option label>" and are wrapped in a <label>
           whose text == the value; we pick by matching that value.

READ-BACK: text/textarea → el.value; native select → selected option text;
           radio/checkbox → the `checked` input's value.

Single page (multi_page=False): the whole application is on the one /apply route.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import urllib.request
from typing import Any

import ats_engine as eng
from ats_engine import ATSAdapter, FormField

# ---------------------------------------------------------------------------
# URL parsing + Postings API (title only — questions are not in the API).
# ---------------------------------------------------------------------------
POSTINGS_API = "https://api.lever.co/v0/postings/{org}/{job_id}?mode=json"

# Standard Lever contact fields → profile keys. Same `name` scheme on every board.
# value resolved deterministically at extract (source="standard"); no LLM, no upload.
_CONTACT = {
    "name": "full_name",
    "email": "email",
    "phone": "phone",
    "location": "location",
    "org": None,  # "current company" — derive from latest experience below
    "urls[LinkedIn]": "linkedin",
    "urls[GitHub]": "github",
    "urls[Twitter]": None,
    "urls[Portfolio]": "website",
    "urls[Other]": None,
}

# Hidden / machine / anti-bot controls Lever ships in the form — never fill these.
_SKIP_NAMES = {
    "selectedLocation",
    "accountId",
    "linkedInData",
    "origin",
    "referer",
    "timezone",
    "socialReferralKey",
    "socialSource",
    "resumeStorageId",
    "h-captcha-response",
    "source",
    "resumeText",
}
# Hidden bookkeeping sub-fields inside card / survey groups.
_SKIP_SUBFIELDS = ("[baseTemplate]", "[surveyId]", "[candidateSelectedLocation]")

_OPEN_ENDED = re.compile(r"why|describe|tell us|cover|what (drew|motivat)|interest|expectations|anything else", re.I)


def parse_job_url(url: str) -> tuple[str, str]:
    """jobs.lever.co/<org>/<id>[/apply] → (org, id). The id is a uuid."""
    m = re.search(r"lever\.co/([\w-]+)/([0-9a-fA-F-]{36}|[\w-]+)", url)
    if not m:
        raise SystemExit(f"Could not parse org/job_id from {url!r}")
    return m.group(1), m.group(2)


def fetch_title(org: str, job_id: str) -> str:
    """Pull the job title from the public Postings API (the ONLY thing it gives us
    that's useful for the form). Best-effort — a missing title just weakens MAP context."""
    try:
        ctx = ssl.create_default_context()
        try:
            import certifi

            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            pass
        with urllib.request.urlopen(POSTINGS_API.format(org=org, job_id=job_id), timeout=15, context=ctx) as r:
            return json.loads(r.read().decode()).get("text", "") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# CSS escaping — Lever names contain [], spaces, and other CSS-special chars.
# A `[name="..."]` attribute selector quotes the VALUE, so only the embedded
# quote/backslash need escaping; brackets inside a quoted attribute value are fine.
# ---------------------------------------------------------------------------
def _attr_value(name: str) -> str:
    return name.replace("\\", "\\\\").replace('"', '\\"')


def _sel(name: str) -> str:
    return f'[name="{_attr_value(name)}"]'


# ---------------------------------------------------------------------------
# Live-DOM form schema scrape (runs in extract's own browser).
# ---------------------------------------------------------------------------
# One JS pass: every named control on the /apply page, grouped enough to recover
# the question LABEL, the control TYPE, required-ness, and (for radio/checkbox/select)
# the option set. Lever renders each question inside a `.application-question` whose
# `.application-label` holds the human text; the field's name is on the input.
_SCHEME_JS = r"""
() => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  const fieldName = el => el.getAttribute('name') || '';
  // collect named controls in document order, de-duped by name (radios/checkbox groups
  // share a name; we keep ONE descriptor per name and gather its option labels).
  const byName = new Map();
  const order = [];
  document.querySelectorAll('input[name], textarea[name], select[name]').forEach(el => {
    const name = fieldName(el);
    if (!name) return;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type')||'').toLowerCase();
    if (type === 'hidden') return;
    const q = el.closest('.application-question');
    const label = norm(q ? (q.querySelector('.application-label')||q).textContent : '');
    if (!byName.has(name)) {
      byName.set(name, {name, tag, type, label, required: false, options: []});
      order.push(name);
    }
    const d = byName.get(name);
    // required: input flag, aria, or the question wrapper's `required` class
    if (el.required || el.getAttribute('aria-required') === 'true' ||
        (q && q.classList.contains('required'))) d.required = true;
    if (tag === 'select') {
      [...el.options].forEach(o => { const t = norm(o.textContent);
        if (t && !/^select\s*\.*$/i.test(t)) d.options.push(t); });
    } else if (type === 'radio' || type === 'checkbox') {
      // the option label for a Lever radio/checkbox == its value attr (label wraps it)
      const wrap = el.closest('label');
      const opt = norm(el.getAttribute('value') || (wrap ? wrap.textContent : ''));
      if (opt) d.options.push(opt);
    }
  });
  // clean the contact-field labels (they include helper/error text we don't want)
  return JSON.stringify(order.map(n => byName.get(n)));
}
"""


def _classify(ctrl: dict, profile: dict) -> FormField | None:
    """One live control descriptor → a normalized FormField (or None to drop)."""
    name = ctrl["name"]
    tag, type_, label = ctrl["tag"], ctrl["type"], (ctrl.get("label") or "").strip()
    options = ctrl.get("options") or None
    required = bool(ctrl.get("required"))

    # drop machine / hidden bookkeeping controls
    if name in _SKIP_NAMES or any(sub in name for sub in _SKIP_SUBFIELDS):
        return None

    # ---- standard contact fields (deterministic profile value, no LLM) ----
    if name in _CONTACT:
        pk = _CONTACT[name]
        value = _current_company(profile) if name == "org" else (profile.get(pk) if pk else None)
        return FormField(name=name, label=label or name, type="text", source="standard", required=required, value=value)

    # ---- resume file upload ----
    if name == "resume" or type_ == "file":
        return FormField(name=name, label=label or "Resume", type="input_file", source="file", required=required)

    # ---- everything else is a custom question (cards[...] / surveysResponses[...]) ----
    if tag == "select":
        return FormField(
            name=name, label=label, type="single_select", source="select", required=required, options=options
        )
    if type_ == "radio":
        return FormField(name=name, label=label, type="radio", source="select", required=required, options=options)
    if type_ == "checkbox":
        return FormField(name=name, label=label, type="checkbox", source="select", required=required, options=options)
    if tag == "textarea":
        return FormField(name=name, label=label, type="textarea", source="open_ended", required=required)
    # short text custom question
    src = "open_ended" if _OPEN_ENDED.search(label) else "input_text"
    return FormField(name=name, label=label, type="text", source=src, required=required)


def _current_company(profile: dict) -> str | None:
    for e in profile.get("experience", []) or []:
        if e.get("current") or (e.get("end_date") or "").lower() in ("present", "current"):
            return e.get("company")
    exp = profile.get("experience") or []
    return exp[0].get("company") if exp else None


class LeverAdapter(ATSAdapter):
    multi_page = False
    hosts = ("jobs.lever.co", "jobs.eu.lever.co")

    # -- step 1: schema extract (scrape the live /apply DOM — no schema API) ----
    async def extract(self, url: str, profile: dict) -> tuple[str, list[FormField]]:
        org, job_id = parse_job_url(url)
        title = fetch_title(org, job_id)

        apply_url = url if url.rstrip("/").endswith("/apply") else f"https://jobs.lever.co/{org}/{job_id}/apply"
        controls = await self._scrape_scheme(apply_url)
        fields: list[FormField] = []
        for c in controls:
            f = _classify(c, profile)
            if f is not None:
                fields.append(f)
        return title, fields

    async def _scrape_scheme(self, apply_url: str) -> list[dict]:
        """Short-lived browser pass: render /apply, dump the named controls, close."""
        from browser_use import BrowserProfile, BrowserSession

        session = BrowserSession(browser_profile=BrowserProfile(headless=True, keep_alive=True))
        await session.start()
        try:
            await session.navigate_to(apply_url)
            await asyncio.sleep(3.5)
            page = await session.must_get_current_page()
            raw = await page.evaluate(_SCHEME_JS)
            return json.loads(raw) if raw else []
        except Exception as exc:
            print(f"   [lever:extract] scheme scrape failed: {exc}")
            return []
        finally:
            await session.kill()

    # -- reach the form (navigate posting → /apply if needed) ------------------
    async def open_form(self, session: Any, page: Any) -> Any:
        url = await page.get_url()
        if url.rstrip("/").endswith("/apply"):
            return page
        # On the posting page: drive to /apply (the "Apply" link points there).
        org, job_id = parse_job_url(url)
        await session.navigate_to(f"https://jobs.lever.co/{org}/{job_id}/apply")
        await asyncio.sleep(3)
        return await session.must_get_current_page()

    # -- locate ----------------------------------------------------------------
    async def locate(self, page: Any, field: FormField) -> Any | None:
        return await eng.first(page, _sel(field.name))

    # -- fill ------------------------------------------------------------------
    async def fill(self, session: Any, page: Any, field: FormField, value: str, resume: str | None) -> bool:
        type_, name = field.type, field.name

        if type_ == "input_file" or field.source == "file":
            fel = await self.locate(page, field) or await eng.first(page, 'input[type="file"]')
            return await eng.upload_file(session, page, fel, resume) if (fel and resume) else False

        if type_ == "single_select":  # native <select>
            return await self._select_native(page, name, value)

        if type_ in ("radio", "checkbox"):
            return await self._click_option(page, name, value)

        # text / textarea / input_text / open_ended
        el = await self.locate(page, field)
        if not el:
            return False
        try:
            await el.fill(value)
            return True
        except Exception:
            return False

    async def _select_native(self, page: Any, name: str, value: str) -> bool:
        """Pick a native <select> option by TEXT and fire a `change` event.

        browser_use's el.select_option() matches by option VALUE via CDP and SILENTLY
        no-ops on Lever's EEO selects (returns without error, selection unchanged). The
        deterministic path is to set selectedIndex by option-text match in JS and dispatch
        the `change` Lever's React listener needs. Closest-match (exact → substring) so an
        LLM value like 'Decline to self-identify' lands even with minor whitespace drift."""
        js = r"""
        (name, want) => {
          const e = document.getElementsByName(name)[0];
          if (!e || e.tagName !== 'SELECT') return false;
          const norm = s => (s||'').replace(/\s+/g,'').toLowerCase();
          const w = norm(want);
          const opts = [...e.options];
          let idx = opts.findIndex(o => norm(o.textContent) === w || norm(o.value) === w);
          if (idx < 0 && w) idx = opts.findIndex(o => { const t = norm(o.textContent); return t && (t.includes(w) || w.includes(t)); });
          if (idx < 0) return false;
          e.selectedIndex = idx;
          e.dispatchEvent(new Event('input', {bubbles: true}));
          e.dispatchEvent(new Event('change', {bubbles: true}));
          return true;
        }
        """
        try:
            res = await page.evaluate(js, name, value)
        except Exception:
            return False
        return str(res).lower() == "true"  # evaluate stringifies: JS true -> "True"

    async def _click_option(self, page: Any, name: str, value: str) -> bool:
        """Radio/checkbox group: click the input in THIS group whose value matches.
        Lever wraps each input in a <label>; matching on the input's value attr is exact."""
        want = eng.norm(value)
        els = await page.get_elements_by_css_selector(_sel(name))
        exact, partial = None, None
        for el in els:
            try:
                val = (await el.get_attribute("value")) or ""
            except Exception:
                val = ""
            if eng.norm(val) == want:
                exact = el
                break
            if partial is None and want and (want in eng.norm(val) or eng.norm(val) in want):
                partial = el
        target = exact or partial
        if not target:
            return False
        try:
            await target.click()
            return True
        except Exception:
            return False

    # -- read-back -------------------------------------------------------------
    async def read_back(self, session: Any, page: Any, field: FormField, value: str) -> bool:
        type_, name = field.type, field.name

        if type_ == "input_file" or field.source == "file":
            return (await self.locate(page, field)) is not None  # CDP upload has no readable .value

        if type_ in ("radio", "checkbox"):
            # confirm the group has a checked input whose value matches.
            js = (
                "() => { const els = [...document.getElementsByName(%r)];"
                " const c = els.find(e => e.checked); return c ? (c.value || 'on') : ''; }"
            ) % name
            try:
                got = await page.evaluate(js)
            except Exception:
                got = ""
            want = eng.norm(value)
            return bool(want) and (want in eng.norm(got) or eng.norm(got) in want)

        if type_ == "single_select":
            js = (
                "() => { const e = document.getElementsByName(%r)[0];"
                " if(!e) return ''; const o = e.options[e.selectedIndex]; return o ? o.text : ''; }"
            ) % name
            try:
                got = await page.evaluate(js)
            except Exception:
                got = ""
            return eng.norm(value) in eng.norm(got) if value else bool(eng.norm(got))

        # text / textarea
        el = await self.locate(page, field)
        if not el:
            return False
        try:
            got = await el.evaluate("() => this.value")
        except Exception:
            return False
        if not (got or "").strip():
            return False
        return eng.norm(value) in eng.norm(got) or eng.norm(got) in eng.norm(value)
