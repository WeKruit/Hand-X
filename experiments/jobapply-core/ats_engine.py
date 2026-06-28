"""Generic, ATS-agnostic application-filler engine.

The invariant pipeline lives here; everything platform-specific is a pluggable
ATSAdapter. The split (第一性原理):

  INVARIANT (this module, shared across every ATS)
    - MAP        : ONE structured LLM call mapping profile -> field values by LABEL.
    - LADDER     : per-field L1 fill -> L2 re-try -> L3 single-field browser-use Agent.
    - VERIFY     : read-back compare (delegated primitive per adapter).
    - INSTRUMENT : per-field tier + running $; measures the real escalation rate.

  VARIANT (each ATSAdapter)
    - extract(url)        : produce the normalized [Field] list (schema-API or DOM-scrape).
    - locate / fill / read_back : drive that platform's widgets & locators.
    - reveal (optional)   : pre-fill DOM toggles (e.g. "Enter manually").

Cost model:  total ≈ 1 map call + (escalation_rate x per-field agent cost).
On a clean schema-driven ATS escalation -> 0 and total -> the map call (~$0.0015).
The instrument step is the feedback loop: it shows which widget/adapter bleeds $ so
you add a deterministic routine THERE and drive escalation back to zero.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# Field sources whose value is produced by the ONE structured LLM mapping call.
# `standard` already carries a deterministic profile value; `file` is an upload;
# `skip` is dropped. Everything else (select / input_text / open_ended) is mapped.
MAP_SOURCES = {"select", "input_text", "open_ended"}


# ---------------------------------------------------------------------------
# Normalized field descriptor — every adapter's extract() yields these.
# ---------------------------------------------------------------------------
@dataclass
class FormField:
    name: str  # stable id used to locate the element on the page
    label: str  # human label (what MAP reasons over)
    type: str  # adapter-native type tag (text/textarea/file/single_select/…)
    source: str  # standard | select | input_text | open_ended | file | skip
    required: bool = False
    options: list[str] | None = None
    option_values: dict | None = None  # {option_label: option_value_id} — to check the right checkbox
    value: str | None = None  # deterministic value already known at extract (standard fields)

    @property
    def needs_map(self) -> bool:
        return self.source in MAP_SOURCES


# ---------------------------------------------------------------------------
# Wizard value types (multi-page adapters only).
# ---------------------------------------------------------------------------
@dataclass
class Credentials:
    email: str
    password: str  # never via CLI args — env / secret bootstrap (see project memory)


@dataclass
class AuthResult:
    ok: bool
    needs_verification: bool = False  # emailed link/code follows -> HITL halt
    reason: str = ""


@dataclass
class Step:
    index: int  # 1-based active step (from the progress bar)
    total: int  # total steps M
    name: str  # e.g. "My Information"
    fields: list[FormField]
    is_review: bool  # name == 'Review' or index == total -> STOP, never submit


@dataclass
class AdvanceResult:
    ok: bool
    page: Any = None
    blocked_reason: str = ""


# ---------------------------------------------------------------------------
# Adapter contract.
# ---------------------------------------------------------------------------
class ATSAdapter(abc.ABC):
    hosts: tuple[str, ...] = ()  # url hostnames this adapter claims
    multi_page: bool = False  # single-page (Greenhouse/Lever/Ashby) leave False
    advance_label: str = "Save and Continue"  # the step-advance button (agent repair clicks it)

    @abc.abstractmethod
    async def extract(self, url: str, profile: dict) -> tuple[str, list[FormField]]:
        """Return (job_title, fields) WITHOUT a browser where possible (schema API),
        else by classifying the live DOM. For wizards, returns (title, []) — fields come
        per-step from extract_step()."""

    async def open_form(self, session: Any, page: Any) -> Any:
        """Reach the actual form after the initial navigation, returning the page the form
        lives on. Default: the page unchanged. Override to drill into an iframe-embedded
        form, dismiss a wall, click "Apply", or (for wizards) create an account."""
        return page

    @abc.abstractmethod
    async def locate(self, page: Any, field: FormField) -> Any | None:
        """Return the live element for this field (or None). Used by the engine's
        form-present pre-flight and by fill/read_back."""

    @abc.abstractmethod
    async def fill(self, session: Any, page: Any, field: FormField, value: str, resume: str | None) -> bool:
        """L1/L2 fill mechanism for this field type. Return whether the mechanism ran."""

    @abc.abstractmethod
    async def read_back(self, session: Any, page: Any, field: FormField, value: str) -> bool:
        """Read the value back off the live DOM and confirm it took."""

    # --- wizard-only hooks (multi_page=True). Safe defaults keep single-page untouched. ---
    async def authenticate(self, session: Any, page: Any, creds: Credentials | None) -> AuthResult:
        """Create/sign into the account that gates the wizard. Single-page: no-op."""
        return AuthResult(ok=True)

    async def extract_step(self, session: Any, page: Any, profile: dict) -> Step:
        """Classify the CURRENTLY-MOUNTED wizard step's live DOM into a Step."""
        raise NotImplementedError

    async def next_step(self, session: Any, page: Any) -> AdvanceResult:
        """Click this step's advance control and wait for the next step to mount."""
        raise NotImplementedError

    async def is_complete(self, session: Any, page: Any) -> bool:
        """At-Review / terminal detection. HARD STOP — never click Submit. Single-page: True."""
        return True

    async def validation_errors(self, page: Any) -> list[str]:
        """Step-level validation messages currently blocking advance (e.g. 'Enter a valid format
        for Phone Number'). Used to trigger generic agent-mode repair when next_step fails. The
        messages should name the offending field(s). Default: none."""
        return []

    async def fill_repeaters(self, session: Any, page: Any, profile: dict) -> dict:
        """Optional: fill 'Add another' repeater sections (education / work experience) that
        are NOT in the flat field schema — they exist only in the live DOM and need an
        add-row loop, not the per-field map. Default: none. Kept structurally separate from
        the flat FormField fill so it never perturbs form_present / read_back."""
        return {}


async def form_present(adapter: ATSAdapter, page: Any, fields: list[FormField]) -> bool:
    """Pre-flight: is the form actually on this page? Guards the expensive L1->L2->L3
    ladder from running on a redirect / WAF wall / login page / wrong host (where every
    field would 'escalate' and burn agent $). True if any of the first few real fields
    can be located."""
    probes = [f for f in fields if f.source not in ("skip",)][:4]
    for f in probes:
        if await adapter.locate(page, f) is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# Generic DOM utilities adapters may reuse.
# ---------------------------------------------------------------------------
async def first(page: Any, selector: str) -> Any | None:
    try:
        els = await page.get_elements_by_css_selector(selector)
        return els[0] if els else None
    except Exception:
        return None


def norm(s: str) -> str:
    return "".join((s or "").split()).lower()


async def click_by_text(page: Any, text: str) -> int:
    """Click every short button/link whose visible text matches (CSS has no :has-text)."""
    want = text.strip().lower()
    clicked = 0
    for b in await page.get_elements_by_css_selector('button, [role="button"]'):
        try:
            t = ((await b.evaluate("() => this.textContent")) or "").strip()
        except Exception:
            continue
        if t and len(t) < 30 and want in t.lower():
            try:
                await b.click()
                clicked += 1
                await asyncio.sleep(0.3)
            except Exception:
                pass
    return clicked


async def upload_file(session: Any, page: Any, file_el: Any, path: str) -> bool:
    """File upload via CDP DOM.setFileInputFiles (no high-level wrapper exists)."""
    bnid = getattr(file_el, "_backend_node_id", None) or getattr(file_el, "backend_node_id", None)
    if not bnid:
        return False
    sid = (
        getattr(file_el, "_session_id", None)
        or getattr(file_el, "session_id", None)
        or getattr(page, "session_id", None)
    )
    if hasattr(sid, "__await__"):  # page.session_id is a COROUTINE — the prior code passed it
        sid = await sid  # un-awaited, so CDP got a coroutine as session_id and failed
    try:
        await session.cdp_client.send.DOM.setFileInputFiles(
            params={"files": [str(Path(path).resolve())], "backendNodeId": bnid},
            session_id=sid,
        )
        return True
    except Exception as exc:
        print(f"   [upload] CDP setFileInputFiles failed: {exc}")
        return False


async def press_enter_trusted(session: Any, page: Any) -> bool:
    """A TRUSTED Enter via CDP Input.dispatchKeyEvent on the focused element. react-select
    (and similar geocomplete widgets) IGNORE synthetic page.press / JS-dispatched keys — only
    a real CDP key commits the highlighted option. Caller must have focused/typed first."""
    try:
        sid = await page.session_id
        for ev in (
            {
                "type": "rawKeyDown",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
                "code": "Enter",
                "key": "Enter",
            },
            {"type": "keyUp", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13, "code": "Enter", "key": "Enter"},
        ):
            await session.cdp_client.send.Input.dispatchKeyEvent(params=ev, session_id=sid)
        return True
    except Exception as exc:
        print(f"   [trusted-enter] {exc}")
        return False


# ---------------------------------------------------------------------------
# Step 2 — the ONE structured LLM call (generic).
# ---------------------------------------------------------------------------
class FieldFill(BaseModel):
    name: str = Field(description="echo the field name verbatim")
    value: str = Field(description="value to type/select, or '' if the profile gives no basis")
    why: str = Field(default="", description="one short clause: which profile fact this came from")


class FillMap(BaseModel):
    fields: list[FieldFill]


_MAP_SYSTEM = """You map an applicant PROFILE onto a job-application FORM. You are given the \
job title and a list of fields; each field has a human LABEL, a TYPE, whether it is required, \
and (for dropdowns) the exact allowed OPTIONS.

For EVERY field, return an object {name, value, why}. Rules:
- Echo `name` exactly as given.
- Decide a field's meaning from its LABEL, never from its machine name.
- Use ONLY facts present in the profile. Never invent or assume facts not in the profile.
- If the profile gives no basis for the field, return value "" (empty string).
- If the field has OPTIONS, `value` MUST be EXACTLY one of those option strings, copied \
verbatim. Pick the option the profile best supports. For a yes/no question, reason from the \
profile (e.g. "authorized to work in Japan?" -> the profile is US-authorized only -> "No"). \
For demographic / EEO questions (gender, race/ethnicity, veteran, disability, sexual \
orientation): if the profile DISCLOSES that attribute, pick the option matching it; ONLY if the \
profile does not disclose it, choose a "Prefer not to say" / "I don't wish to answer" / "Decline" \
option.
- SCREENING / ELIGIBILITY yes-no questions: answer the safe, TRUTHFUL default for an ordinary \
applicant rather than leaving a required question blank. Work-authorization / visa-sponsorship / \
citizenship / export-control questions -> answer from the profile (work_authorization, \
authorized_to_work_us, requires_sponsorship, visa_status, citizenship). An age question \
("18 or older?") -> "Yes". Questions about a prior tie the profile does NOT mention — prior/current \
employment at a NAMED company, a family/relationship or conflict-of-interest tie, owning or \
controlling intellectual property, being a current/former government or military/DOD employee, an \
existing non-compete / non-disclosure / non-solicitation agreement, criminal or disciplinary \
history — default to "No" (the applicant has no such tie unless the profile says so). For a \
"select all that apply" / checkbox question the profile does not cover, choose the neutral \
none-of-the-above option if one is present ("Neither" / "None" / "None of the above" / "Not \
applicable"); otherwise leave it unchecked.
- SAFE DEFAULTS when there is no exact profile basis (do NOT leave a reasonable field blank, but \
NEVER invent specific data — zip, salary, employee id, address, references): \
"Preferred name"/"preferred first name" -> the profile's first name; \
"How did you hear about us/this job?" -> pick the most neutral truthful OPTION present, preferring \
"LinkedIn" when the profile has a LinkedIn, else "Company Website"/"Other" (free text -> "LinkedIn"); \
a required acknowledgement/consent option (label starts with "I ", "By ", "Acknowledge", or says \
agree/confirm/consent) -> return that option's label verbatim to select it; \
a "phone device type"/"phone type" field, when the profile gives a phone but no device type -> \
"Mobile" (a personal contact number is a mobile/cell — pick the option matching that if OPTIONS \
are given); \
a "country/region phone code" / "phone code" / "dialing code" field -> the profile's COUNTRY NAME \
(these widgets are searched by country name, e.g. country "United States" -> "United States", NOT \
the numeric "+1"); \
a specific data field the profile lacks -> "" (blank, never fabricated).
- If TYPE is `textarea` (an open-ended question like "Why are you interested?"), WRITE a \
concise, specific answer of 3-5 sentences, first person, plain text, grounded ONLY in the \
profile. Do not use markdown.
- For short text fields, copy the matching profile value verbatim (e.g. a LinkedIn / Website \
/ GitHub URL); blank if the profile has none.
- PHONE NUMBER field: if the field list ALSO contains a separate "phone code" / "country/region \
phone code" / "dialing code" field, the phone-number value MUST EXCLUDE the country/dial code \
(e.g. profile "+1 415 555 0142" -> "415 555 0142") — the code lives in its own field, and a \
number with a duplicate code fails validation. If there is NO separate code field, keep the full \
number as the profile has it.
Return one entry per field, no extras."""


async def map_fields(llm: Any, fields: list[FormField], profile: dict, title: str) -> dict[str, FieldFill]:
    """The single paid step. Returns {field_name: FieldFill}."""
    from browser_use.llm.messages import SystemMessage, UserMessage

    descriptors = [
        {
            "name": f.name,
            "label": f.label,
            "type": f.type,
            "required": f.required,
            **({"options": f.options} if f.options else {}),
        }
        for f in fields
    ]
    ctx = {"job_title": title, "applicant_profile": profile, "fields": descriptors}
    res = await llm.ainvoke(
        [SystemMessage(content=_MAP_SYSTEM), UserMessage(content=json.dumps(ctx, ensure_ascii=False))],
        output_format=FillMap,
    )
    return {f.name: f for f in res.completion.fields}


# ---------------------------------------------------------------------------
# L3 — escalate a single field to a browser-use Agent (generic fallback).
# ---------------------------------------------------------------------------
# Freeze every already-FILLED field so an agent — which can misread a React-controlled input as
# empty (the bu-2-0 false-empty problem) — physically CANNOT re-fill or disturb completed work.
# Empty fields (the failed target, or a not-yet-filled box) stay editable. Restored after the agent.
_FREEZE_FILLED_JS = (
    "() => { let n=0; document.querySelectorAll('input,textarea,select').forEach(e => {"
    " const filled = (e.type==='checkbox'||e.type==='radio') ? e.checked : ((e.value||'').trim().length>0);"
    " if (filled && !e.readOnly && !e.disabled) {"
    "   const lock = (e.tagName==='SELECT'||e.type==='checkbox'||e.type==='radio'||e.type==='file');"
    "   e.setAttribute('data-gh-froze', lock ? 'd' : 'r'); if (lock) e.disabled = true; else e.readOnly = true; n++; }"
    " }); return n; }"
)
_UNFREEZE_JS = (
    "() => document.querySelectorAll('[data-gh-froze]').forEach(e => {"
    " if (e.getAttribute('data-gh-froze') === 'd') e.disabled = false; else e.readOnly = false;"
    " e.removeAttribute('data-gh-froze'); })"
)


async def _unfreeze(session: Any) -> None:
    with contextlib.suppress(Exception):
        p = await session.must_get_current_page()
        await p.evaluate(_UNFREEZE_JS)


async def escalate(session: Any, agent_llm: Any, page: Any, field: FormField, value: str) -> bool:
    from browser_use import Agent

    label = field.label or field.name
    with contextlib.suppress(Exception):
        await page.evaluate(_FREEZE_FILLED_JS)  # lock filled fields — the agent can only touch the target
    task = (
        f"You are already on the application page. Every other field is LOCKED — fill ONLY the single "
        f"form input labeled '{label}' and put this exact text into it: {value!r}. "
        "It is a form field on THIS page — never navigate, never open a URL, even if the value looks like a link. "
        "Do not submit the form and do not touch any other field. Call done once that field shows the value."
    )
    try:
        # use_vision='auto' lets the agent pull a screenshot to SEE field state (avoids re-typing a
        # field the serialized DOM falsely reads empty) — it observes, the deterministic layer fills.
        agent = Agent(task=task, llm=agent_llm, browser_session=session, use_vision="auto")
        await agent.run(max_steps=4)
        return True
    except Exception as exc:
        print(f"   [L3] agent failed for {field.name}: {exc}")
        return False
    finally:
        # browser_use.Agent teardown stops the shared CDP client even on a keep_alive
        # session (agent/service.py close()), which would break every field/screenshot
        # after this one. Re-attach to the still-running browser via the stored cdp_url.
        with contextlib.suppress(Exception):
            if not session.is_cdp_connected:
                await session.connect()
        await _unfreeze(session)  # ALWAYS unlock — else subsequent deterministic fills hit frozen fields


async def agent_fill_section(session: Any, page: Any, *, section: str, instructions: str, max_steps: int = 10) -> dict:
    """Hand a hard, NON-schema section (an education / experience REPEATER whose rows + searchable
    closed-taxonomy comboboxes exist only in the live DOM, below the fold and NOT in the selector
    map) to a FOCUSED browser-use Agent. The agent scrolls to the section and drives the comboboxes
    with browser-use's native dropdown actions + reasoning — robust where deterministic string-match
    fails ('B.S.' -> 'Bachelor of Science', huge searchable school lists).

    FILL-ONLY is enforced STRUCTURALLY, not by prompt alone: every submit control is DISABLED before
    the agent runs (it physically cannot submit) and restored after. Runs LAST, so the agent's CDP
    teardown can't perturb earlier deterministic fields."""
    from browser_use import Agent, ChatGoogle

    disable_js = (
        "() => { let n=0; document.querySelectorAll('button[type=submit],input[type=submit]')"
        ".forEach(b => { b.setAttribute('data-gh-was', b.disabled ? '1':'0'); b.disabled = true; n++; }); return n; }"
    )
    restore_js = (
        "() => document.querySelectorAll('[data-gh-was]').forEach(b => {"
        " b.disabled = b.getAttribute('data-gh-was') === '1'; b.removeAttribute('data-gh-was'); })"
    )
    with contextlib.suppress(Exception):
        await page.evaluate(disable_js)  # neutralise submit so the agent CANNOT submit the form
    with contextlib.suppress(Exception):
        await page.evaluate(_FREEZE_FILLED_JS)  # lock already-filled fields — agent can't disturb them

    task = (
        f"You are already on a job-application page. Fill ONLY the {section} section: {instructions}. "
        f"Each {section} field (School, Degree, Discipline, etc.) is a SEARCHABLE dropdown — scroll to "
        "it, click it, type, and pick an option. CRITICAL: these are CLOSED lists, so you must pick the "
        "CLOSEST AVAILABLE option, not insist on an exact string. Map abbreviations (Degree 'B.S.' -> "
        "'Bachelor's Degree' / 'Bachelor of Science'; 'M.S.' -> 'Master's Degree' / 'Master of Science'). "
        "If a search shows 'No options', the list doesn't have that exact term — RETRY with a SHORTER or "
        "broader term (e.g. 'Electrical and Computer Engineering' -> 'Electrical' -> 'Engineering' -> "
        "'Computer'), then pick the nearest option offered. Do not leave a dropdown with text typed but no "
        "option selected. Use 'Add another' before each additional entry. Touch NOTHING outside this "
        "section. The Submit button is DISABLED on purpose — do NOT submit and do NOT navigate. Call done "
        "once every dropdown in the section shows a SELECTED value."
    )
    ok = True
    try:
        llm = ChatGoogle(model="gemini-3-flash-preview", api_key=os.environ.get("GOOGLE_API_KEY"))
        agent = Agent(task=task, llm=llm, browser_session=session, use_vision="auto")
        await agent.run(max_steps=max_steps)
    except Exception as exc:
        print(f"   [agent:{section}] {exc}")
        ok = False
    finally:
        with contextlib.suppress(Exception):  # Agent.close() drops the shared CDP client — re-attach
            if not session.is_cdp_connected:
                await session.connect()
        with contextlib.suppress(Exception):
            await page.evaluate(restore_js)
        await _unfreeze(session)  # unlock the frozen filled fields
    return {"section": section, "agent_ok": ok}


async def repair_and_advance(
    session: Any, page: Any, errors: list[str], advance_label: str, agent_llm: Any = None, max_steps: int = 22
) -> bool:
    """Agent-driven recovery for a step that FAILED validation on advance. KEY finding: once a
    platform (Workday) rejects a Save, NO deterministic re-fill re-arms it — not browser-use
    el.fill, not the native value-setter + input/change/blur, not clicking Save repeatedly. Only a
    real, coherent interaction context (fix the field AND click the advance button as one human-like
    sequence) re-arms the form. So we hand the WHOLE recovery to a browser-use Agent: it reads the
    validation messages, corrects the flagged fields, and clicks the step-advance button itself.
    Only the FINAL submit is disabled (the agent must still be able to Save-and-Continue past the
    step); the agent is told never to navigate and never to finalize. The agent's CDP teardown is
    re-attached in finally. Returns True if the agent ran (advancement is verified by the caller)."""
    from browser_use import Agent, ChatGoogle

    # STRUCTURAL submit-guard (belt-and-suspenders to install_submit_guard already running on the
    # session): re-disable any final-submit button now, in case the guard interval isn't installed.
    await install_submit_guard(page)

    bullet = "\n- ".join(errors[:12])
    task = (
        "You are on one step of a multi-step job-application form. It FAILED to advance because of "
        f"these validation errors:\n- {bullet}\n"
        "Fix ONLY the field(s) named by these errors so they become valid, then click the "
        f"'{advance_label}' button EXACTLY ONCE to advance ONE step, and then IMMEDIATELY call done. "
        "Work efficiently — fix all the flagged fields, then advance; do not re-verify endlessly.\n"
        "ABSOLUTE STOP RULES (a human reviewer must submit, not you):\n"
        "- Advance only ONE step. After the page advances once, call done. Do NOT fill or advance a "
        "second step.\n"
        "- NEVER click a button labelled 'Submit', 'Submit Application', 'Submit Apply', 'Finish', or "
        "anything that finalizes the application — these are FORBIDDEN.\n"
        "- If clicking advance brings you to a REVIEW / summary page, or the only remaining action is "
        "a Submit/Finish button, call done IMMEDIATELY WITHOUT clicking anything.\n"
        "Reason from the error + the other fields already filled. Two common cases:\n"
        "1. FORMAT: a phone number rejected as invalid, when a separate country/dial-code field is "
        "already set, should DROP the leading dial code: '+1 415 555 0142' -> '415 555 0142'.\n"
        "2. SEARCHABLE DROPDOWN (School, Degree, Field of Study, etc.): click it, type, and pick an "
        "option. These are CLOSED lists — pick the CLOSEST available option, do not insist on an "
        "exact string. Map abbreviations (Degree 'B.S.' -> \"Bachelor's Degree\"/'Bachelor of "
        "Science'; 'M.S.' -> \"Master's Degree\"). If it shows 'No options', RETRY with a SHORTER or "
        "broader term (e.g. 'Electrical and Computer Engineering' -> 'Electrical' -> 'Engineering'), "
        "then pick the nearest option. Never leave a dropdown with text typed but no option selected.\n"
        "3. SCREENING / ELIGIBILITY yes-no questions (18 or older?, prior employee of a named "
        "company?, own intellectual property?, government/DOD employee?, non-compete agreement?, "
        "work authorization / visa sponsorship?): answer the safe, TRUTHFUL default for an ordinary "
        "applicant — '18 or older' -> Yes; questions about a prior tie the resume does not mention "
        "(prior employment at a named company, family/conflict ties, owning IP, gov/military "
        "employment, non-compete/NDA) -> No; authorized to work in the US -> Yes, require visa "
        "sponsorship -> No (unless the form data says otherwise). For a 'select all that apply' / "
        "checkbox question the resume doesn't cover, tick the none-of-the-above option ('Neither' / "
        "'None' / 'None of the above' / 'Not applicable') if present. Do NOT leave required questions "
        "blank.\n"
        "Prefer values already on the form / resume; for the screening defaults above use the stated "
        "ordinary-applicant answer. CRITICAL: every field is "
        "on THIS page — NEVER open a URL, navigate, search the web, or go back/forward (it loses the "
        "form). Do NOT submit a FINAL application (any 'Submit Application' is disabled). Call done "
        "as soon as the page has advanced to the next step."
    )
    try:
        agent = Agent(
            task=task,
            llm=agent_llm or ChatGoogle(model="gemini-3-flash-preview", api_key=os.environ.get("GOOGLE_API_KEY")),
            browser_session=session,
            use_vision=True,
        )
        await agent.run(max_steps=max_steps)
    except Exception as exc:
        print(f"   [agent:repair] {exc}")
    finally:
        with contextlib.suppress(Exception):  # Agent.close() drops the shared CDP client — re-attach
            if not session.is_cdp_connected:
                await session.connect()
        # NOTE: the submit-guard is intentionally LEFT installed — the final Submit must stay
        # disabled for the rest of the wizard so nothing finalizes the application.
    return True


async def install_submit_guard(page: Any) -> None:
    """Continuously DISABLE any button that could FINALIZE (submit/finish) or DESTROY (discard/
    cancel/sign-out/back-to-posting) the application, via a persistent 300ms interval. Workday's
    apply flow is an SPA: an agent can advance forward (e.g. into Review) WITHIN the same document,
    so a one-time disable wouldn't cover controls that mount on a later step — and a confused agent
    has been seen click Back -> "Discard Application?" -> Discard, which would wipe all work. The
    interval re-disables these every tick so the automation physically cannot submit OR discard; a
    human must do either. Forward controls (Save/Continue/Next/Add) stay enabled. Idempotent."""
    with contextlib.suppress(Exception):
        await page.evaluate(
            "() => { const kill=()=>{"
            "   document.querySelectorAll('button,input[type=submit],a[role=button]').forEach(b=>{"
            "     const t=((b.textContent||'')+' '+(b.value||'')+' '+(b.getAttribute('aria-label')||''));"
            "     const danger=/submit|finish|finali[sz]e|discard|cancel|sign ?out|log ?out|delete application|withdraw|\\bback\\b|\\bprevious\\b|go back/i;"
            "     const safe=/save|continue|next|add|search|upload|edit/i;"
            "     if (danger.test(t) && !safe.test(t)) b.disabled=true; });"
            '   document.querySelectorAll(\'[data-automation-id="progressBar"],[data-automation-id*="progressBar"],'
            "[role=navigation] ol,[role=navigation] ul').forEach(e=>{ e.style.pointerEvents='none'; }); };"
            "  kill(); if (!window.__ghSubGuard) window.__ghSubGuard=setInterval(kill, 300); }"
        )


# field types whose deterministic read-back is prone to false-negatives (custom widgets the
# serialized DOM mis-reads) — worth a cheap VLM glance before re-filling / escalating.
_VLM_RESCUE_TYPES = {"single_select", "multi_select", "radio", "checkbox", "date", "select_native"}


async def _vlm_filled(session: Any, field: FormField, value: str) -> bool:
    """Cheap, cached VLM read-back rescue (handoff R1): is the field VISIBLY filled? Only for the
    widget types that false-negative; silent on any error / over-budget (caller falls through)."""
    if field.type not in _VLM_RESCUE_TYPES:
        return False
    with contextlib.suppress(Exception):
        from vision_verify import _is_filled, visual_check

        verdict = await visual_check(session, target=field.label or field.name, key=field.name)
        return _is_filled(verdict)
    return False


async def fill_with_ladder(
    adapter: ATSAdapter,
    session: Any,
    page: Any,
    field: FormField,
    value: str,
    agent_llm: Any,
    resume: str | None,
    allow_escalation: bool = True,
) -> str:
    """Fill one field through L1 -> L2 -> L3. Return the tier that succeeded.

    NOTE: L3 runs a browser_use.Agent whose teardown stops the shared CDP client even on a
    keep_alive session — so after an L3 escalation, subsequent fields/screenshots on the same
    session fail ('Client is not started'). Set allow_escalation=False to cap the ladder at L2
    (used by the screenshot proof sweep so the session stays intact). Fixing the re-attach is
    tracked separately.
    """
    if not (value or "").strip():  # nothing to fill (incl. a file field with no path)
        return "blank"

    filled = await adapter.fill(session, page, field, value, resume)
    if filled and await adapter.read_back(session, page, field, value):
        return "L1"
    # READ-BACK RESCUE (handoff R1): a custom widget (Workday listbox/checkbox) is often visibly
    # filled while the serialized DOM reads it blank -> a FALSE read-back failure. A cheap, cached
    # VLM glance confirms it without RE-FILLING (re-picking a listbox can mis-select) or paying for
    # the agent. Only for the widget types that actually false-negative.
    if filled and await _vlm_filled(session, field, value):
        return "vlm"

    await asyncio.sleep(0.4)
    if await adapter.fill(session, page, field, value, resume) and await adapter.read_back(session, page, field, value):
        return "L2"
    if await _vlm_filled(session, field, value):
        return "vlm"

    if allow_escalation and await escalate(session, agent_llm, page, field, value):
        with contextlib.suppress(Exception):
            page = await session.must_get_current_page()  # re-acquire after agent + CDP re-attach
        await _unfreeze(session)  # belt-and-suspenders: ensure nothing stays locked for later fields
        if await adapter.read_back(session, page, field, value):
            return "L3"
    return "FAIL"


# ---------------------------------------------------------------------------
# Value resolution + instrumentation.
# ---------------------------------------------------------------------------
def _resolve(field: FormField, mapped: dict[str, FieldFill], resume: str | None) -> tuple[str, str]:
    if field.source == "file":
        return (resume or ""), "file"
    if field.source == "standard":
        return ("" if field.value is None else str(field.value)), "profile"
    if field.needs_map:
        ff = mapped.get(field.name)
        return (ff.value if ff else ""), "llm-map"
    return "", "profile"


@dataclass
class _Row:
    name: str
    type: str
    src: str
    tier: str
    fields: dict = dc_field(default_factory=dict)


def _print_report(adapter_name: str, title: str, report: list[_Row], usage: Any, n_mapped: int) -> None:
    tiers = {t: sum(1 for r in report if r.tier == t) for t in ("L1", "L2", "L3", "vlm", "blank", "FAIL")}
    fillable = [r for r in report if r.tier != "blank"]
    escalated = tiers["L2"] + tiers["L3"] + tiers["FAIL"]
    esc_rate = (escalated / len(fillable) * 100) if fillable else 0.0

    print("\n" + "=" * 78)
    print(f"  {adapter_name.upper()} SCHEMA-DRIVEN FILL — PER-FIELD INSTRUMENTATION (fill-only)")
    print(f"  {title}")
    print("=" * 78)
    print(f"  {'FIELD':<24}{'TYPE':<28}{'VALUE-SRC':<10}{'TIER':<6}")
    print("  " + "-" * 74)
    for r in report:
        print(f"  {r.name[:23]:<24}{r.type[:27]:<28}{r.src:<10}{r.tier:<6}")
    print("  " + "-" * 74)
    print(f"  fields total            : {len(report)}")
    print(
        f"  fill tiers              : L1={tiers['L1']}  L2={tiers['L2']}  L3={tiers['L3']}  "
        f"blank={tiers['blank']}  FAIL={tiers['FAIL']}"
    )
    print(f"  escalation rate (L2+L3+FAIL / fillable) : {esc_rate:.0f}%  ({escalated}/{len(fillable)})")
    print(f"  fields mapped by the 1 structured call  : {n_mapped}")
    print(f"  LLM calls (map + any L3 escalations)    : {usage.entry_count}")
    print(f"  TOTAL LLM COST                          : ${usage.total_cost:.5f}")
    print(f"  prompt tok {usage.total_prompt_tokens:,} | completion tok {usage.total_completion_tokens:,}")
    print("=" * 78)
    print("  (schema + deterministic fill are $0; cost is the 1 mapping call, plus L3 only when a field escalates)")


# ---------------------------------------------------------------------------
# The run loop — wires an adapter through the invariant pipeline.
# ---------------------------------------------------------------------------
async def _screenshot(session: Any, page: Any, path: str) -> str | None:
    """Save a PNG of the form via CDP, CLIPPED to the form region (drops the long job
    description so the filled fields are readable). Falls back to full-page if no form."""
    import base64

    try:
        sid = await page.session_id
        params: dict = {"format": "png", "captureBeyondViewport": True}
        clip_json = await page.evaluate(
            "() => { const a=document.querySelector('#first_name,[name=first_name],#email,[name=email]');"
            " if(!a) return ''; const form=a.closest('form')||a.parentElement;"
            " const r=form.getBoundingClientRect();"
            " return JSON.stringify({x: Math.max(0, window.scrollX + r.left - 12),"
            " y: window.scrollY + r.top - 12, w: Math.min(1100, r.width + 24),"
            " h: Math.min(6500, form.scrollHeight + 24)}); }"
        )
        if clip_json:
            c = json.loads(clip_json)
            params["clip"] = {"x": c["x"], "y": c["y"], "width": c["w"], "height": c["h"], "scale": 1}
        res = await session.cdp_client.send.Page.captureScreenshot(params=params, session_id=sid)
        Path(path).write_bytes(base64.b64decode(res["data"]))
        return path
    except Exception as exc:
        print(f"   [screenshot] failed: {exc}")
        return None


async def run(
    adapter: ATSAdapter,
    *,
    url: str,
    profile: dict,
    resume: str | None,
    headless: bool,
    screenshot_path: str | None = None,
    allow_escalation: bool = True,
    creds: Credentials | None = None,
) -> dict:
    """Dispatch by adapter shape: single-page (one extract+fill pass) vs wizard (stepped)."""
    if adapter.multi_page:
        return await run_wizard(
            adapter,
            url=url,
            profile=profile,
            resume=resume,
            headless=headless,
            screenshot_path=screenshot_path,
            allow_escalation=allow_escalation,
            creds=creds,
        )
    return await run_single_page(
        adapter,
        url=url,
        profile=profile,
        resume=resume,
        headless=headless,
        screenshot_path=screenshot_path,
        allow_escalation=allow_escalation,
    )


async def run_single_page(
    adapter: ATSAdapter,
    *,
    url: str,
    profile: dict,
    resume: str | None,
    headless: bool,
    screenshot_path: str | None = None,
    allow_escalation: bool = True,
) -> dict:
    title, fields = await adapter.extract(url, profile)  # step 1 (adapter)
    print(f"[fill:{adapter.__class__.__name__}] {title}  ({len(fields)} fields)")

    from browser_use import BrowserProfile, BrowserSession, ChatGoogle
    from browser_use.tokens.service import TokenCost

    tc = TokenCost(include_cost=True)
    await tc.initialize()
    # thinking_level='minimal': label->value mapping is deterministic reasoning, not a
    # puzzle — minimal thinking cuts thought tokens ~10x, holding the call near ~$0.0015.
    llm = tc.register_llm(
        ChatGoogle(model="gemini-3-flash-preview", api_key=os.environ.get("GOOGLE_API_KEY"), thinking_level="minimal")
    )

    map_rows = [f for f in fields if f.needs_map]  # step 2 (generic)
    mapped = await map_fields(llm, map_rows, profile, title) if map_rows else {}

    session = BrowserSession(browser_profile=BrowserProfile(headless=headless, keep_alive=True))
    await session.start()
    await session.navigate_to(url)
    await asyncio.sleep(2.5)
    page = await session.must_get_current_page()
    page = await adapter.open_form(session, page)  # reach the form (iframe-embed / wall / apply)

    result: dict = {
        "adapter": adapter.__class__.__name__,
        "title": title,
        "url": url,
        "fields_total": len(fields),
        "mapped": len(mapped),
        "screenshot": None,
    }

    if not await form_present(adapter, page, fields):
        # The form is not on this page — boards-api gave us the schema but the live form
        # is behind a redirect to the company site, an anti-bot wall (Cloudflare), a login,
        # or a different host. Abort BEFORE the ladder so we don't escalate every absent
        # field to the L3 agent (that path silently burns ~$0.01+/field, e.g. coinbase $0.22).
        try:
            final_url = await page.get_url()
        except Exception:
            final_url = url
        usage = await tc.get_usage_summary()
        if screenshot_path:
            result["screenshot"] = await _screenshot(session, page, screenshot_path)
        print("\n" + "=" * 78)
        print(f"  BLOCKED — form not reachable for {adapter.__class__.__name__}")
        print(f"  landed on: {final_url}")
        print("  cause: redirect to company site / anti-bot wall / login / iframe not drilled.")
        print(
            f"  fields in schema: {len(fields)}   mapped (paid): {len(mapped)}   cost so far: ${usage.total_cost:.5f}"
        )
        print("  (ladder skipped — no $ wasted escalating absent fields)")
        print("=" * 78)
        await session.kill()
        result.update(status="BLOCKED", final_url=final_url, cost=usage.total_cost, tiers={}, filled=0)
        return result

    report: list[_Row] = []
    for f in fields:
        if f.source == "skip":
            continue
        value, src = _resolve(f, mapped, resume)
        tier = await fill_with_ladder(adapter, session, page, f, value, llm, resume, allow_escalation)  # steps 3-4
        # ONLY refresh the page handle when an L3 escalation actually ran (it re-attaches the
        # CDP client). Doing it on every FAIL is harmful: must_get_current_page() can latch a
        # stray about:blank target, after which all remaining fields fill on a blank page.
        if allow_escalation and tier in ("L3", "FAIL"):
            with contextlib.suppress(Exception):
                page = await session.must_get_current_page()
        report.append(_Row(name=f.name, type=f.type, src=src, tier=tier))

    # repeater sections (education / experience) — separate add-row pass, not the flat loop
    with contextlib.suppress(Exception):
        rep = await adapter.fill_repeaters(session, page, profile)
        if rep:
            result["repeaters"] = rep
            print(f"  repeaters: {rep}")

    usage = await tc.get_usage_summary()
    _print_report(adapter.__class__.__name__.replace("Adapter", ""), title, report, usage, len(mapped))  # step 5
    if screenshot_path:
        result["screenshot"] = await _screenshot(session, page, screenshot_path)

    tiers = {t: sum(1 for r in report if r.tier == t) for t in ("L1", "L2", "L3", "vlm", "blank", "FAIL")}
    try:
        final_url = await page.get_url()
    except Exception:
        final_url = url
    result.update(
        status="FILLED",
        final_url=final_url,
        cost=usage.total_cost,
        tiers=tiers,
        filled=tiers["L1"] + tiers["L2"] + tiers["L3"] + tiers["vlm"],
    )

    if headless:
        await session.kill()
    else:
        print("\n  Browser left open for review. Ctrl+C to close.")
        with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
            while True:
                await asyncio.sleep(1)
        await session.kill()
    return result


# ---------------------------------------------------------------------------
# Wizard run loop — N single-pages behind auth + step navigation. Reuses the
# invariant primitives (map_fields, fill_with_ladder, read_back) per step.
# ---------------------------------------------------------------------------
async def run_wizard(
    adapter: ATSAdapter,
    *,
    url: str,
    profile: dict,
    resume: str | None,
    headless: bool,
    screenshot_path: str | None = None,
    allow_escalation: bool = True,
    creds: Credentials | None = None,
) -> dict:
    from browser_use import BrowserProfile, BrowserSession, ChatGoogle
    from browser_use.tokens.service import TokenCost

    title, _ = await adapter.extract(url, profile)  # title only; fields come per-step
    print(f"[wizard:{adapter.__class__.__name__}] {title}")
    tc = TokenCost(include_cost=True)
    await tc.initialize()
    llm = tc.register_llm(
        ChatGoogle(model="gemini-3-flash-preview", api_key=os.environ.get("GOOGLE_API_KEY"), thinking_level="minimal")
    )
    # separate tc-registered LLM for the repair agent so its tokens count toward per-step cost.
    agent_llm = tc.register_llm(ChatGoogle(model="gemini-3-flash-preview", api_key=os.environ.get("GOOGLE_API_KEY")))
    result: dict = {"adapter": adapter.__class__.__name__, "title": title, "url": url, "steps": []}

    session = BrowserSession(browser_profile=BrowserProfile(headless=headless, keep_alive=True))
    await session.start()
    await session.navigate_to(url)
    await asyncio.sleep(2.5)
    page = await session.must_get_current_page()
    page = await adapter.open_form(session, page)  # job page -> Apply -> Apply Manually

    auth = await adapter.authenticate(session, page, creds)  # the account gate (Workday step 1)
    if not auth.ok:
        return await _wizard_halt(result, "AUTH_FAILED", auth.reason, tc, session)
    if auth.needs_verification:
        return await _wizard_halt(result, "EMAIL_VERIFICATION_REQUIRED", auth.reason, tc, session)

    # HARD SAFETY: keep the final Submit/Finish button disabled for the ENTIRE wizard (SPA-
    # persistent interval) so neither the deterministic path nor an agent can finalize the
    # application — we always STOP at Review for a human to submit.
    await install_submit_guard(page)

    seen: set[int] = set()
    for _ in range(12):  # MAX_STEPS guardrail
        await install_submit_guard(page)  # re-assert each iteration (cheap; idempotent)
        with contextlib.suppress(Exception):
            from vision_verify import reset_visual_cache

            reset_visual_cache()  # fresh per-step VLM budget for the read-back rescue
        t0 = time.monotonic()
        c0 = (await tc.get_usage_summary()).total_cost
        step = await adapter.extract_step(session, page, profile)
        if step.is_review or await adapter.is_complete(session, page):
            result["status"] = "FILLED_TO_REVIEW"  # STOP — never submit
            shot = None
            if screenshot_path:  # capture the final Review page as proof of completion
                shot = await _screenshot(session, page, screenshot_path.replace(".png", "_review.png"))
                result["review_screenshot"] = shot
            result["steps"].append(
                {
                    "name": step.name or "Review",
                    "index": step.index,
                    "total": step.total,
                    "tiers": {},
                    "seconds": round(time.monotonic() - t0, 1),
                    "cost": round((await tc.get_usage_summary()).total_cost - c0, 5),
                    "agent_used": False,
                    "screenshot": shot,
                }
            )
            break
        if step.index in seen:  # progress-monotonicity guard
            return await _wizard_halt(result, "STEP_STALLED", f"re-entered step {step.index}", tc, session)
        seen.add(step.index)

        map_rows = [f for f in step.fields if f.needs_map]
        mapped = await map_fields(llm, map_rows, profile, title) if map_rows else {}
        rows: list[_Row] = []
        for f in step.fields:
            if f.source == "skip":
                continue
            value, src = _resolve(f, mapped, resume)
            tier = await fill_with_ladder(adapter, session, page, f, value, llm, resume, allow_escalation)
            rows.append(_Row(name=f.name, type=f.type, src=src, tier=tier))

        # off-schema repeater sections on this step (My Experience: work experience / education /
        # skills / languages). No-op (returns {}) on steps without a repeater — the adapter gates
        # on section headings. The agent freezes filled fields + submit stays disabled.
        repeaters_used = False
        with contextlib.suppress(Exception):
            rep = await adapter.fill_repeaters(session, page, profile)
            if rep:
                repeaters_used = True
                print(f"  repeaters: {rep}")
            page = await session.must_get_current_page()  # agent_fill_section re-attaches CDP

        # screenshot the deterministically-filled step BEFORE advancing (the agent, if invoked,
        # advances to the NEXT page, so capture this page now).
        shot = None
        if screenshot_path:
            shot = await _screenshot(session, page, screenshot_path.replace(".png", f"_step{step.index}.png"))

        # Deterministic fill got the values in but the platform may reject a field's FORMAT/choice
        # on advance — and once it rejects a Save, NO deterministic re-fill re-arms it (verified).
        # So on a validation block, hand the recovery to an agent that fixes the flagged field(s)
        # AND clicks the advance button itself (the only thing that re-arms the form). Advancement
        # is verified by re-reading the step; if the agent didn't advance, the monotonicity guard
        # at the top of the loop turns the repeated step into an honest STEP_STALLED halt.
        agent_used = False
        adv = await adapter.next_step(session, page)
        if not adv.ok:
            errs = await adapter.validation_errors(page)
            if errs:
                print(f"  [agent-repair] advance blocked by validation: {errs}")
                agent_used = True
                await repair_and_advance(session, page, errs, adapter.advance_label, agent_llm=agent_llm)
                page = await session.must_get_current_page()
                moved = await adapter.extract_step(session, page, profile)
                if moved.index != step.index or moved.is_review or await adapter.is_complete(session, page):
                    adv = AdvanceResult(ok=True, page=page)  # the agent advanced the step

        result["steps"].append(
            {
                "name": step.name,
                "index": step.index,
                "total": step.total,
                "tiers": {t: sum(1 for r in rows if r.tier == t) for t in ("L1", "L2", "L3", "vlm", "blank", "FAIL")},
                "seconds": round(time.monotonic() - t0, 1),
                "cost": round((await tc.get_usage_summary()).total_cost - c0, 5),
                "agent_used": agent_used or repeaters_used,
                "repeaters": repeaters_used,
                "screenshot": shot,
            }
        )
        if not adv.ok:
            return await _wizard_halt(result, "ADVANCE_FAILED", adv.blocked_reason, tc, session)
        page = adv.page or await session.must_get_current_page()

    usage = await tc.get_usage_summary()
    result.setdefault("status", "FILLED_TO_REVIEW")
    result["cost"] = usage.total_cost
    print(f"  wizard steps filled: {len(result['steps'])}   cost ${usage.total_cost:.5f}   (stopped before Submit)")
    await session.kill()
    return result


async def _wizard_halt(result: dict, status: str, reason: str, tc: Any, session: Any) -> dict:
    usage = await tc.get_usage_summary()
    result.update(status=status, reason=reason, cost=usage.total_cost)
    print(f"  WIZARD HALT: {status} — {reason}   (cost ${usage.total_cost:.5f})")
    with contextlib.suppress(Exception):
        await session.kill()
    return result
