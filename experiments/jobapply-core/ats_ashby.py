"""Ashby adapter — the platform-specific surface behind the generic engine.

EXTRACT  : public `non-user-graphql` API (free, no browser). The careers SPA's own
           `ApiJobPosting` op returns `jobPosting.applicationForm.fieldEntries`; each
           entry's `field` is a raw JSON blob carrying {id, path, title, type,
           selectableValues}. No auth, no DOM heuristics. (Probed live — see module docstring
           in scratchpad probes.) Ashby's field `type` taxonomy:
             String / Email / Phone / LongText / Boolean / File /
             SingleSelect|ValueSelect / MultiValueSelect / Location / Date.
FILL     : the live form lives at `<posting-url>/application` (a sub-route, NOT an inline
           reveal). Every input is keyed by `id==path` AND `name==path`:
             - text / email / tel / textarea -> el.fill()
             - File          -> #_systemfield_resume input[type=file] via CDP upload
             - Boolean        -> a <button>Yes</button><button>No</button> pair + a hidden
                                 display:none <input type=checkbox name=path>; click the
                                 Yes/No button (that drives the React state + checkbox.checked)
             - MultiValueSelect (few options) -> labeled checkboxes
                                 id="{fieldId}_{optionPath}-labeled-checkbox-{n}"
             - SingleSelect / Location / Date -> custom comboboxes: click -> type -> click option
READ-BACK: text via this.value; Boolean via the hidden checkbox.checked (Yes=true/No=false);
           select via the chosen option label; checkbox-group via :checked.

The schema is the SAME shape on `jobs.ashbyhq.com` and `jobs.eu.ashbyhq.com`.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import Any

import ats_engine as eng
import httpx
from ats_engine import ATSAdapter, FormField

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# The careers SPA's own application-form query. `field` is the JSON! scalar (fetched whole);
# `isRequired` is the per-posting required flag (overrides the field's own nullability).
_API_QUERY = """
query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
    id
    title
    applicationForm { fieldEntries { field isRequired } }
  }
}
"""


def _parse_url(url: str) -> tuple[str, str, str]:
    """(api_base, org_slug, job_id) from a jobs.ashbyhq.com / jobs.eu.ashbyhq.com posting URL."""
    m = re.search(r"https?://(jobs(?:\.eu)?\.ashbyhq\.com)/([^/]+)/([0-9a-f-]{36})", url, re.I)
    if not m:
        raise ValueError(f"not an Ashby posting URL: {url}")
    host, org, jid = m.group(1), m.group(2), m.group(3)
    return f"https://{host}/api/non-user-graphql", org, jid


# Ashby field `type` -> (engine source, adapter-native type tag). `standard` carries a
# deterministic profile value; `file` is an upload; the rest are LLM-mapped (input_text /
# open_ended / select). Name/Email/Phone are deterministic contact fields.
def _classify(ftype: str, path: str, title: str, options: list[str] | None) -> tuple[str, str]:
    t = (ftype or "").lower()
    if path == "_systemfield_resume" or t == "file":
        return "file", "file"
    if path == "_systemfield_name" or t == "name":
        return "standard", "name"
    if path == "_systemfield_email" or t == "email":
        return "standard", "email"
    if t == "phone":
        return "standard", "phone"
    if t == "boolean":
        return "select", "boolean"  # yes/no -> map picks "Yes"/"No"
    if "multivalueselect" in t or (options and "multi" in t):
        return "select", "multi_select"
    if t in ("valueselect", "singleselect", "select") or options:
        return "select", "single_select"
    if t == "location":
        return "input_text", "location"  # combobox autocomplete
    if t == "date":
        return "input_text", "date"
    if t in ("longtext", "richtext"):
        return "open_ended", "textarea"
    return "input_text", "text"  # String / Number / URL / etc.


# Deterministic standard-field values from the profile (name/email/phone).
def _standard_value(kind: str, profile: dict) -> str:
    if kind == "name":
        return profile.get("full_name") or " ".join(
            x for x in (profile.get("first_name"), profile.get("last_name")) if x
        )
    if kind == "email":
        return profile.get("email", "")
    if kind == "phone":
        return profile.get("phone", "")
    return ""


class AshbyAdapter(ATSAdapter):
    multi_page = False
    hosts = ("jobs.ashbyhq.com", "jobs.eu.ashbyhq.com")

    # -- step 1: schema extract (no browser) -------------------------------
    async def extract(self, url: str, profile: dict) -> tuple[str, list[FormField]]:
        api, org, jid = _parse_url(url)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "apollographql-client-name": "frontend_non_user",
            "apollographql-client-version": "1.0",
            "Accept": "*/*",
            "Origin": f"https://{api.split('/')[2]}",
            "Referer": url,
        }
        payload = {
            "operationName": "ApiJobPosting",
            "query": _API_QUERY,
            "variables": {"organizationHostedJobsPageName": org, "jobPostingId": jid},
        }
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
            r = await c.post(api, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        jp = ((data or {}).get("data") or {}).get("jobPosting")
        if not jp:
            raise RuntimeError(f"Ashby posting not found/closed (org={org} id={jid}): {data}")

        title = jp.get("title", "")
        entries = ((jp.get("applicationForm") or {}).get("fieldEntries")) or []
        fields: list[FormField] = []
        for e in entries:
            fld = e.get("field") or {}
            if fld.get("isDeactivated"):
                continue
            path = fld.get("path", "")
            label = (fld.get("title") or fld.get("humanReadablePath") or "").replace("\xa0", " ").strip()
            options = None
            sv = fld.get("selectableValues")
            if isinstance(sv, list) and sv:
                options = [s.get("label", s.get("value", "")) for s in sv if not s.get("isArchived")]
            required = bool(e.get("isRequired") or not fld.get("isNullable", True))
            source, ntype = _classify(fld.get("type", ""), path, label, options)
            value = _standard_value(ntype, profile) if source == "standard" else None
            fields.append(
                FormField(
                    name=path,
                    label=label,
                    type=ntype,
                    source=source,
                    required=required,
                    options=options,
                    value=value,
                )
            )
        return title, fields

    # -- reach the form: navigate to the /application sub-route ------------
    async def open_form(self, session: Any, page: Any) -> Any:
        try:
            cur = await page.get_url()
        except Exception:
            cur = ""
        # The form is rendered behind an "Apply for this Job" link that routes to
        # <posting-url>/application. Go straight there (top-frame, no iframe).
        if "/application" not in cur:
            base = re.sub(r"/application/?$", "", cur or "").rstrip("/")
            if _UUID.search(base):
                await session.navigate_to(base + "/application")
                await asyncio.sleep(3)
                page = await session.must_get_current_page()
        return page

    # -- locate ------------------------------------------------------------
    async def locate(self, page: Any, field: FormField) -> Any | None:
        path = field.name
        if field.type == "boolean":
            return await eng.first(page, f'input[type="checkbox"][name="{path}"], input[type="checkbox"][id="{path}"]')
        if field.type == "multi_select" and field.options:
            # labeled-checkbox group: ids start with the field path
            return await eng.first(page, f'input[type="checkbox"][id^="{path}_"], input[type="checkbox"][id*="{path}"]')
        if field.type == "file" or field.source == "file":
            return await eng.first(page, f'input[type="file"][id="{path}"]') or await eng.first(
                page, 'input[type="file"]'
            )
        # text / textarea / select / combobox: keyed by id==path or name==path, else by the
        # field wrapper [data-field-path] — Ashby's location/date comboboxes have NO id/name.
        return await eng.first(page, f'[id="{path}"], [name="{path}"]') or await eng.first(
            page, f'[data-field-path="{path}"] input, [data-field-path="{path}"] textarea'
        )

    # -- fill --------------------------------------------------------------
    async def fill(self, session: Any, page: Any, field: FormField, value: str, resume: str | None) -> bool:
        ntype, path = field.type, field.name

        if ntype == "file" or field.source == "file":
            fel = await self.locate(page, field)
            return await eng.upload_file(session, page, fel, resume) if (fel and resume) else False

        if ntype == "boolean":
            return await self._fill_boolean(page, path, value)

        if ntype == "multi_select" and field.options:
            ok = False
            for part in [p.strip() for p in value.split(";") if p.strip()] or [value]:
                ok = await self._check_option(page, path, part) or ok
            return ok

        if ntype in ("single_select", "location", "date"):
            return await self._fill_combobox(page, field, value)

        # text / email / tel / textarea / url
        el = await self.locate(page, field)
        if not el:
            return False
        try:
            await el.fill(value)
            return True
        except Exception:
            return False

    async def _fill_boolean(self, page: Any, path: str, value: str) -> bool:
        """Boolean renders as a <button>Yes</button><button>No</button> pair driving a
        hidden checkbox. Click the button whose text matches the mapped Yes/No value."""
        want = "yes" if eng.norm(value) in ("yes", "true", "y", "1") else "no"
        res = await page.evaluate(
            """([p, want]) => {
              const w = document.querySelector(`[data-field-path="${p}"]`);
              if (!w) return false;
              const b = [...w.querySelectorAll('button')]
                .find(x => (x.textContent || '').trim().toLowerCase() === want);
              if (!b) return false;
              b.click();
              return true;
            }""",
            [path, want],
        )
        return str(res).lower() == "true"

    async def _check_option(self, page: Any, path: str, want: str) -> bool:
        """MultiValueSelect with a handful of options renders as labeled checkboxes whose
        id starts with the field path. Tick the one whose label matches `want`."""
        w = eng.norm(want)
        res = await page.evaluate(
            """([p, w]) => {
              const boxes = [...document.querySelectorAll(`input[type=checkbox][id*="${p}"]`)];
              for (const cb of boxes) {
                const lbl = (cb.getAttribute('name') || '') +
                  ((document.querySelector(`label[for="${CSS.escape(cb.id)}"]`)||{}).textContent || '');
                const n = lbl.replace(/\\s+/g,'').toLowerCase();
                if (!w || n.includes(w)) {
                  if (!cb.checked) (cb.closest('label') || cb).click();
                  if (!cb.checked) cb.click();
                  return true;
                }
              }
              return false;
            }""",
            [path, w],
        )
        return str(res).lower() == "true"

    async def _fill_combobox(self, page: Any, field: FormField, value: str) -> bool:
        """SingleSelect / Location / Date custom widget: focus the input, type to filter,
        click the matching listbox option. Falls back to leaving the typed text if no
        option list appears (Date pickers accept typed text)."""
        inp = await self.locate(page, field)
        if not inp:
            return False
        try:
            with contextlib.suppress(Exception):
                await inp.click()
            await inp.fill(value)
        except Exception:
            return False
        want = eng.norm(value)
        for _ in range(6):
            await asyncio.sleep(0.4)
            opts = await page.get_elements_by_css_selector('[role="option"], [class*="option"]')
            if not opts:
                continue
            exact = partial = None
            for o in opts:
                try:
                    txt = ((await o.evaluate("() => this.textContent")) or "").strip()
                except Exception:
                    continue
                n = eng.norm(txt)
                if n == want:
                    exact = o
                    break
                if partial is None and want and want in n:
                    partial = o
            target = exact or partial
            if target:
                with contextlib.suppress(Exception):
                    await target.click()
                    return True
        # Date / free-typed comboboxes keep the typed text — count that as filled.
        return field.type == "date"

    # -- read-back ---------------------------------------------------------
    async def read_back(self, session: Any, page: Any, field: FormField, value: str) -> bool:
        ntype, path = field.type, field.name

        if ntype == "file" or field.source == "file":
            return (await self.locate(page, field)) is not None  # CDP upload exposes no readable .value

        if ntype == "boolean":
            want_yes = eng.norm(value) in ("yes", "true", "y", "1")
            # A-2: Ashby Yes/No are <button>s + a hidden checkbox React never flips. The
            # SELECTED button gets an `_active*` class (verified live). Read its text.
            sel = await page.evaluate(
                """(p) => { const w=document.querySelector(`[data-field-path="${p}"]`); if(!w) return '';
                  const b=[...w.querySelectorAll('button')].find(x=>/active/i.test(x.className));
                  return b ? b.textContent.trim() : ''; }""",
                path,
            )
            if not sel:
                return False
            return (eng.norm(sel) in ("yes", "true", "y", "1")) == want_yes

        if ntype == "multi_select" and field.options:
            want = eng.norm(value.split(";")[0].strip())
            got = await page.evaluate(
                """([p, w]) => {
                  const boxes = [...document.querySelectorAll(`input[type=checkbox][id*="${p}"]`)];
                  for (const cb of boxes) {
                    if (!cb.checked) continue;
                    const lbl = (cb.getAttribute('name') || '') +
                      ((document.querySelector(`label[for="${CSS.escape(cb.id)}"]`)||{}).textContent || '');
                    const n = lbl.replace(/\\s+/g,'').toLowerCase();
                    if (!w || n.includes(w)) return true;
                  }
                  return false;
                }""",
                [path, want],
            )
            return str(got).lower() == "true"

        if ntype in ("single_select", "location"):
            # chosen label lives in the combobox input value or a sibling selected pill
            el = await self.locate(page, field)
            if not el:
                return False
            try:
                got = await el.evaluate("() => this.value || (this.textContent||'')")
            except Exception:
                got = ""
            want = eng.norm(value)
            return bool(want) and want in eng.norm(got)

        # text / email / tel / textarea / date
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
