# Multi-Page ATS Filler — Design Doc

Status: design. Grounds the multi-page (wizard) evolution of the existing
single-page schema-driven engine (`ats_engine.py`) without breaking the proven
single-page path (Greenhouse, `ats_greenhouse.py`).

Anchor facts from the current engine (do not regress):

- `ATSAdapter` ABC: `extract(url, profile) -> (title, [FormField])`, `open_form(session, page) -> page`,
  `locate(page, field) -> el|None`, `fill(session, page, field, value, resume) -> bool`,
  `read_back(session, page, field, value) -> bool`.
- `run(adapter, ...)`: `extract` ONCE → ONE structured `map_fields(...)` call
  (`gemini-3-flash-preview`, `thinking_level="minimal"`, ~$0.0015) → per-field
  `fill_with_ladder` (L1 `fill` → L2 re-query+retry → L3 single-field `browser_use.Agent`)
  → `read_back` verify → per-field tier+cost row in `_print_report`.
- `form_present(...)` pre-flight aborts BEFORE the ladder if the form is not on the page
  (redirect / WAF / login) so absent fields never burn L3 $.
- Proven: Greenhouse at 0% escalation / $0.0015.

The whole point of the architecture (per the module docstring): **INVARIANT pipeline,
VARIANT adapter.** Multi-page must keep the invariant pipeline as the per-step body and
add ONE outer loop around it.

---

## 1. The Multi-Page Engine

### 1.1 Core idea: a wizard is N single-pages behind navigation + auth

A Workday wizard is not a new pipeline — it is the **existing single-page pipeline run once
per step**, wrapped by (a) an account/auth gate before step 1 and (b) a "click Next, wait for
the next step to mount" transition between steps. So we do **not** rewrite `run()`; we add a
`run_wizard()` that calls the same `map_fields` + `fill_with_ladder` + `read_back` primitives
per step, and we extend `ATSAdapter` with a few new optional methods that single-page adapters
never implement.

**MAP runs PER-STEP, not once.** This is forced by the recon, not a preference:

- Workday exposes **no public form schema** (ATS map: "Schema: NO" for Workday/iCIMS/Taleo/
  Oracle/Phenom). Fields for step N+1 do not exist in the DOM until you have authenticated and
  clicked through steps 1..N. You physically cannot extract all fields up front.
- Step field sets are **tenant- and location-dependent** (recon: Visa has no Self-Identify
  step; Blue Origin splits Application Questions into 1-of-2 / 2-of-2). The step list itself is
  read at runtime from the `progressBar`, not hardcoded.
- Therefore each step does its own `extract_step` (live-DOM classify) → its own small MAP call
  over only that step's fields → its own fill ladder. N steps ⇒ N small MAP calls.

This is the honest cost: a wizard cannot be a single $0.0015 call because the schema is not
knowable in advance. It is N calls, each over a *small* slice (My Information ≈ 8–12 fields,
Voluntary Disclosures ≈ 3–5), so each call is *cheaper* than a flat Greenhouse page. See §3.

### 1.2 Contract extension (backward-compatible)

Add a `multi_page` class flag (default `False`) and new methods with **safe defaults on the
base class** so every existing single-page adapter keeps working untouched.

```python
class ATSAdapter(abc.ABC):
    hosts: tuple[str, ...] = ()
    multi_page: bool = False          # NEW. Greenhouse/Lever/Ashby leave this False.

    # --- existing, unchanged ---
    @abc.abstractmethod
    async def extract(self, url, profile) -> tuple[str, list[FormField]]: ...
    async def open_form(self, session, page) -> Any: return page
    @abc.abstractmethod
    async def locate(self, page, field) -> Any | None: ...
    @abc.abstractmethod
    async def fill(self, session, page, field, value, resume) -> bool: ...
    @abc.abstractmethod
    async def read_back(self, session, page, field, value) -> bool: ...

    # --- NEW: wizard-only hooks. Base defaults make them no-ops for single-page. ---

    async def authenticate(self, session, page, creds: "Credentials") -> "AuthResult":
        """Create or sign into the candidate account that gates the wizard.
        Single-page adapters never call this (multi_page=False)."""
        return AuthResult(ok=True, needs_verification=False)   # base: nothing to do

    async def extract_step(self, session, page, profile) -> "Step":
        """Classify the CURRENTLY-MOUNTED step's live DOM into a Step:
        (step_index, step_total, step_name, [FormField], is_review).
        DOM-only — NO schema API exists for wizard ATSes. This is the per-step
        analogue of extract(); for single-page, run() never calls it."""
        raise NotImplementedError   # only multi_page adapters implement

    async def next_step(self, session, page) -> "AdvanceResult":
        """Click this step's advance control (Workday 'Save and Continue' /
        pageFooterNextButton / bottom-navigation-next-button), then WAIT for the
        progressBar active step to change. Return the new (page, ok, blocked_reason)."""
        raise NotImplementedError

    async def is_complete(self, session, page) -> bool:
        """At-Review / terminal detection. For Workday: progressBar active step
        name == 'Review' OR active N == total M. HARD STOP: never click Submit."""
        return True   # base: single-page is 'complete' after its one fill pass
```

Supporting value types (small dataclasses, live next to `FormField`):

```python
@dataclass
class Credentials:
    email: str
    password: str
    # never passed via CLI args — read from env / secret bootstrap (project memory:
    # feedback_no_secrets_in_cli_args). Email must be a real inbox for verification.

@dataclass
class AuthResult:
    ok: bool
    needs_verification: bool = False     # emailed link/code step follows (HITL)
    reason: str = ""

@dataclass
class Step:
    index: int                 # 1-based active step number (from progressBar)
    total: int                 # total steps M (from progressBar)
    name: str                  # e.g. "My Information"
    fields: list[FormField]
    is_review: bool            # name == 'Review' or index == total

@dataclass
class AdvanceResult:
    ok: bool
    page: Any
    blocked_reason: str = ""   # validation error on this step / no advance observed
```

Nothing above changes the single-page path: `run()` is untouched, `GreenhouseAdapter`
(and future Lever/Ashby) set `multi_page=False` and never implement the wizard hooks.

### 1.3 Dispatch: one entry point, two loops

`run()` becomes a thin dispatcher that picks the loop by the flag. The single-page body is
exactly today's `run()`, renamed `run_single_page()`. No behavioral change for Greenhouse.

```python
async def run(adapter, *, url, profile, resume, headless, creds=None,
              screenshot_path=None, allow_escalation=True) -> dict:
    if adapter.multi_page:
        return await run_wizard(adapter, url=url, profile=profile, resume=resume,
                                creds=creds, headless=headless,
                                screenshot_path=screenshot_path,
                                allow_escalation=allow_escalation)
    return await run_single_page(adapter, url=url, profile=profile, resume=resume,
                                 headless=headless, screenshot_path=screenshot_path,
                                 allow_escalation=allow_escalation)
```

### 1.4 Wizard run loop (pseudocode)

The per-step body reuses the **existing** `map_fields` + `fill_with_ladder` + `read_back`
primitives verbatim — the only new code is the outer loop, auth, and navigation.

```python
async def run_wizard(adapter, *, url, profile, resume, creds, headless,
                     screenshot_path=None, allow_escalation=True) -> dict:
    title, _ = await adapter.extract(url, profile)     # title only; fields come per-step
    tc, llm = init_tokencost_and_map_llm()             # SAME llm as single-page (minimal thinking)

    session = await start_session(headless)
    await session.navigate_to(url)
    page = await session.must_get_current_page()
    page = await adapter.open_form(session, page)       # job page -> Apply -> Apply Manually

    # ---- AUTH GATE (step 1) ----
    auth = await adapter.authenticate(session, page, creds)
    if not auth.ok:
        return blocked(result, "AUTH_FAILED", auth.reason, tc)
    if auth.needs_verification:
        # emailed link/code. HITL or inbox-poll integration. Park, do not fake.
        return halted(result, "EMAIL_VERIFICATION_REQUIRED", tc)

    steps_report = []                                   # per-step roll-up of per-field rows
    MAX_STEPS = 12                                       # guardrail vs infinite loop
    seen = set()                                         # (index,total) progress-monotonicity check

    for _ in range(MAX_STEPS):
        step = await adapter.extract_step(session, page, profile)   # live-DOM classify (no API)

        if step.is_review or await adapter.is_complete(session, page):
            break                                        # STOP. Never click Submit.

        # progress-monotonicity guard: if we re-enter the same step index after an
        # advance, navigation stalled (validation error) -> bail rather than loop.
        if step.index in seen:
            return blocked(result, "STEP_STALLED", f"re-entered step {step.index}", tc)
        seen.add(step.index)

        # --- INVARIANT PIPELINE, scoped to THIS step ---
        if not await form_present(adapter, page, step.fields):
            return blocked(result, "STEP_FORM_ABSENT", step.name, tc)

        map_rows = [f for f in step.fields if f.needs_map]
        mapped = await map_fields(llm, map_rows, profile, title) if map_rows else {}   # 1 small call/step

        rows = []
        for f in step.fields:
            if f.source == "skip":
                continue
            value, src = _resolve(f, mapped, resume)
            tier = await fill_with_ladder(adapter, session, page, f, value, llm,
                                          resume, allow_escalation)   # L1->L2->L3 unchanged
            rows.append(_Row(name=f.name, type=f.type, src=src, tier=tier))
        steps_report.append((step.name, rows))

        if screenshot_path:                               # per-step proof
            await _screenshot(session, page, step_png(screenshot_path, step.index))

        # --- ADVANCE ---
        adv = await adapter.next_step(session, page)
        if not adv.ok:
            return blocked(result, "ADVANCE_FAILED", adv.blocked_reason, tc)
        page = adv.page
        await settle(page)                                # wait progressBar active step to change

    usage = await tc.get_usage_summary()
    _print_wizard_report(adapter, title, steps_report, usage)   # per-step + aggregate instrumentation
    await stop_or_review(session, headless)
    return wizard_result(steps_report, usage, status="FILLED_TO_REVIEW")
```

Key invariants preserved:

- **MAP is per-step**, over only that step's mapped fields.
- **`fill_with_ladder` is byte-for-byte the same** (L1/L2/L3). Note its existing caveat: L3
  stops the shared CDP client. For a wizard that is worse — losing the session mid-wizard kills
  the rest of the run. So wizard mode should run with `allow_escalation` gated per-field and
  prefer deterministic Workday widget routines (§2) so L3 rarely fires; fixing L3 session
  re-attach is a hard prerequisite for relying on L3 inside a wizard.
- **STOP before Submit** is enforced in two places: the loop breaks on `is_review`, and
  `next_step` must refuse to click any control whose text matches `/^submit$/i` or that is the
  Review-page footer button.
- **No infinite loop**: `MAX_STEPS` cap + `seen`-index monotonicity guard.

---

## 2. WorkdayAdapter Spec

`class WorkdayAdapter(ATSAdapter): multi_page = True`,
`hosts = ("*.myworkdayjobs.com", "*.myworkday.com", "myworkdaysite.com")`.

Primary locator everywhere: `data-automation-id` (aid). It is stable and tenant-independent
(recon verified on NVIDIA wd5 + Intel wd1 + Blue Origin wd5: every interactive element carried
an aid; the only aid-less ones were social icons). aid is **not unique** — scope by the
`formField-<key>` wrapper, and read popup options from the detached body portal
`activeListContainer`, NOT from inside the field.

### 2.1 `open_form` — job page → application wizard

Deterministic across tenants (recon):

1. Click `a[data-automation-id="adventureButton"]` (the Apply button).
2. In the revealed menu, click `a[data-automation-id="applyManually"]`
   (ignore `autofillWithResume`, `useMyLastApplication`).
   - Equivalent deep-link: navigate to `<jobUrl>/apply/applyManually`.
3. Dismiss tenant pre-modals if present: Visa cookie/legal modal
   `data-automation-id="legalNoticeAcceptButton"`.

Returns the page now showing the Create Account / Sign In step.

### 2.2 `authenticate` — the step-1 wall (the single biggest blocker)

Recon: form fields (My Information onward) are gated behind a mandatory Create Account /
Sign In. Use **native email/password only — never Google/SSO** (`GoogleSignInButton`).

Auth state machine (mirrors the existing `ghosthands/platforms/workday.py` 5-state machine):

```
DETECT  -> is this Create Account or Sign In? (signInLink toggles to sign-in;
           createAccountLink toggles to create). Prefer Sign In if the account
           already exists for this tenant (accounts are PER-TENANT).
FILL    -> aid=email           (input type=text)   el.fill(email)
           aid=password        (input type=password) el.fill(password)
           aid=verifyPassword  (input type=password) el.fill(password)   # create only
CONSENT -> Visa: tick aid=createAccountCheckbox (may need aid=createAccountExpandButton
           'Read More' first to enable it). Read state first; click only to toggle ON.
HONEYPOT-> Blue Origin: a hidden field 'for robots only, do not enter...' -> LEAVE EMPTY.
           Sign-in honeypots aid=beecatcher / aid=click_filter -> LEAVE UNTOUCHED.
SUBMIT   -> create: aid=createAccountSubmitButton ; sign-in: aid=signInSubmitButton
VERIFY   -> if an email verification code/link step appears -> AuthResult(needs_verification=True).
           Park for HITL / inbox poll. Do not fabricate a code.
```

Password complexity varies by tenant (Blue Origin min 8, Visa min 12; all require
upper/lower/numeric/special). The supplied/generated throwaway password must satisfy the
strictest (≥12, all classes) to pass every tenant. Verify success by the progressBar advancing
off "Create Account/Sign In".

### 2.3 `extract_step` — per-step live-DOM field discovery (no API)

1. **Read the progress bar** (`data-automation-id="progressBar"`): parse each
   `progressBarActiveStep` / `progressBarInactiveStep` innerText `"(current )step N of M <Name>"`
   → `(index, total, name)`. This is readable at every step (recon-verified). `is_review =`
   `name.lower()=="review"` OR `index==total`.
2. **Enumerate this step's fields**: query all `[data-automation-id^="formField-"]` wrappers in
   the active step container. For each wrapper derive:
   - `name` = the `formField-<key>` suffix (e.g. `addressSection_city`, `education-1--school`).
   - `label` = wrapper label text (strip trailing `*`).
   - `required` = `aria-required="true"` on the control OR `*`/`abbr` in the label.
   - `type` = classify the control (see §2.4 table) by inspecting the inner element.
   - `options` = for native `<select>`, read `<option>`s now; for button-listbox widgets,
     leave `None` (options live in a portal that only mounts on click — discovered at fill time).
   - `source` = `select` | `input_text` | `open_ended` | `file` | `standard` | `skip`,
     matching the existing `MAP_SOURCES` so `needs_map`/`_resolve` work unchanged.
3. Return `Step(index, total, name, fields, is_review)`.

Because options for listbox widgets aren't known until click, the MAP call for those fields
passes the label/type without `options`; the deterministic 5-pass matcher in `fill` snaps the
mapped value to the real option (§2.4). For yes/no radios and native selects whose options ARE
known at extract, pass them so MAP picks an exact allowed string (same rule as today).

### 2.4 Widget drive + read_back routines (deterministic)

Each row mirrors the recon's verified driving rules and the in-tree DomHand executors. `fill`
dispatches on the widget type detected in `extract_step`.

| Widget (type tag) | Locate | DRIVE (L1) | READ_BACK |
|---|---|---|---|
| **text input** (`input_text`): name/email/phone/city/ZIP/GPA/URL/free-text | `[data-automation-id="formField-<key>"] input` (auth: `aid=email/password` directly) | `el.click()` to focus, then `el.fill(value)` — **ONCE** (re-typing concatenates 'WuWu'). No Tab between fields; click whitespace then next field. | `el.evaluate('()=>this.value')` non-empty AND no sibling `aid*="error"` node. |
| **single-select listbox** (`single_select`): Country, Degree, Phone Device Type, screening pickers | trigger `button[aria-haspopup="listbox"]` inside `formField-<key>` (text 'Select One') | (1) click trigger; (2) sleep ~0.5–1s for body portal `activeListContainer` to mount; (3) read options from portal `[role="option"],[data-automation-id="promptOption"],[data-automation-id="menuItem"]`; list is **virtualized** — use `aria-setsize` and `container.scrollTop+=300` or type to filter; (4) 5-pass match exact>prefix>contains>synonym>word-overlap (+phone-code-strip, +closest-numeric for GPA); (5) click matched option. Nested category→sub-option: click the chevron sub-option, never a back arrow. NOT a native select — `select_option()` does not work. | re-read trigger `textContent`; success = changed away from 'Select One'/empty/opaque-hash to the chosen label (`isUnsetLike` rejects 'Select One'/'Choose One'/GUIDs). `aria-selected='true'` secondary. |
| **native `<select>`** (`select_native`): some tenants, simple enums | `[data-automation-id="formField-<key>"] select` (check `tagName==='SELECT'`) | `el.select_option(label=...)`. | `el.options[el.selectedIndex].text`. |
| **multiselect/typeahead pills** (`multi_select`): School, Skills, Languages | container `[data-automation-id="multiSelectContainer"]` → input `[data-uxi-widget-type="selectinput"]` | **commit-then-add** per value: (1) click selectinput; (2) `el.fill(value)` to trigger `responsiveMonikerPrompt`; (3) wait ~1s; (4) click matched `[role=option]/menuItem` (5-pass) → pill commits, input clears; repeat. NEVER keep typing into a dirty input (loops). | enumerate `[data-automation-id="selectedItemList"] [data-automation-id="selectedItem"] promptOption` → array of labels; success = target present as a pill. |
| **segmented date** (`date`): Start/End/DOB | wrapper `[data-automation-id="dateInputWrapper"]`, segments `dateSectionMonth/Day/Year-input` (`role=spinbutton`) | click MONTH segment, type **continuous digits, no slashes** (`01152026`, or `012026` for MM+YYYY) — Workday auto-advances. Avoid the calendar icon (non-deterministic). | read each segment `.value`/`aria-valuenow`; success = expected zero-padded numbers; `-display` node reflects it. |
| **checkbox** (`checkbox`): terms, 'I currently work here', consent | `input[type=checkbox]` in the formField; consent `aid=createAccountCheckbox` | read current state FIRST; click label/box only to toggle toward desired (clicking a checked box unchecks). Expand `createAccountExpandButton` if the box is gated behind 'Read More'. | `el.evaluate('()=>this.checked')` / `aria-checked` === desired. |
| **radio group** (`radio`): screening, voluntary disclosures | options under `[role=radiogroup]` / `input[type=radio][name=<group>]` | click the LABEL whose text matches the target (5-pass; Yes/No/decline synonyms). Exactly one click; mutually exclusive. | chosen `input.checked===true` / `aria-checked='true'`; exactly one selected. |
| **file upload** (`file`): resume/CV | drop zone `aid=file-upload-drop-zone`; real control `input[type=file][data-automation-id="file-upload-input-ref"]` | do NOT click the drop zone (OS dialog). Push bytes to the file input via CDP `DOM.setFileInputFiles` — reuse the existing `eng.upload_file(session, page, fel, path)`. | presence of `aid=file-upload-successful` + filename in `aid=file-upload-item-name`; or CDP read `inputNode.files.length>0`. |
| **country + phone** (`single_select` + `input_text`) | country `formField-addressSection_countryRegion` / `formField-country` (listbox button); phone `formField-phone > input` | country: drive as single-select listbox; matcher strips trailing dial code so 'United States' matches 'United States +1'. Phone number: fill digits only. Set country/code BEFORE the number so format validation passes. | country button `textContent` = chosen country; phone `input.value` = number; both non-unset. |

`fill` returns whether the mechanism ran; `read_back` confirms it took — exactly as the base
contract requires, so `fill_with_ladder`'s L1/L2 retry works without modification.

### 2.5 `next_step` — Next / Save-and-Continue, and Submit detection

- **Advance control** (query both spellings; tenants differ):
  `[data-automation-id="bottom-navigation-next-button"]`,
  `[data-automation-id="pageFooterNextButton"]` (inside `pageFooter`), or visible label
  "Save and Continue" / "Next". Back = `bottom-navigation-previous-button` / `pageFooterBackButton`.
- After click, **wait for the `progressBarActiveStep` index to change** before returning
  (mounting lags; poll up to ~4s). If a validation error renders (`aid*="error"` under a
  required field) and the step does not advance → `AdvanceResult(ok=False,
  blocked_reason="validation:<field>")`.
- **HARD STOP at Review**: `next_step` must refuse to click any control whose visible text
  matches `/^submit$/i` / "Submit Application", or the Review-page footer button (Workday
  reuses `pageFooterNextButton` as Submit on Review). This is the irreversible final
  submission. The loop never reaches `next_step` on the Review step anyway (it breaks on
  `is_review`), but `next_step` enforces the guard defensively too.

### 2.6 `is_complete` — at-Review detection

`progressBar` active step `name.lower()=="review"` OR active `index == total`. Secondary
signal: the Review footer button text is "Submit" not "Save and Continue". Either triggers the
loop break → STOP.

### 2.7 Where L3 plugs in

Unchanged from single-page: per field, `fill_with_ladder` runs L1 `fill` → re-query+retry L2 →
`escalate(...)` L3 (`browser_use.Agent` scoped to that one labeled field, `max_steps=4`, never
navigate/submit). For Workday the deterministic routines in §2.4 should keep escalation near
zero; L3 is the safety net for shadow-DOM/custom widgets a routine misses. **Caveat carried
forward and amplified for wizards:** L3 currently stops the shared CDP client, which in a
wizard would abort all remaining steps. Until the session re-attach is fixed, wizard mode
should default `allow_escalation=False` for non-critical fields (capping at L2) and only allow
L3 on a required field where L2 failed AND the run is willing to restart the session. Fixing L3
re-attach is a prerequisite to depending on L3 mid-wizard.

---

## 3. Cost Model & Instrumentation

### 3.1 Per-step cost

Single-page total (today): `≈ 1 map call + escalation_rate × per-field agent cost`.
Multi-page total:

```
total ≈ Σ_{step s with mapped fields} map_call(s)
        + escalation_rate × per-field L3 agent cost
        + 0  for auth/navigation (deterministic, no LLM)
```

- **N MAP calls, each small.** A step's MAP call is priced like today's single call but over
  fewer fields. Today's flat Greenhouse page (~all fields) costs ~$0.0015. A Workday wizard
  spreads a comparable field count across ~5 paid steps (Create Account = 0 mapped fields;
  My Information ≈ 8–12; My Experience ≈ repeaters; Application Questions ≈ tenant-specific;
  Voluntary Disclosures ≈ 3–5; Self-Identify ≈ 3–4; Review = 0). Each call is over a *small*
  slice, so per-call cost is *below* $0.0015. Rough envelope: **5 paid steps × ~$0.0008–0.0015
  ≈ $0.004–0.0075 per Workday application** at 0% escalation — single-digit tenths of a cent,
  same order of magnitude as single-page, just multiplied by step count.
- **Auth and navigation add $0** (pure DOM/CDP, no LLM).
- **Open-ended steps cost slightly more** (Application Questions may include "Why do you want
  to work here?" textareas → more completion tokens), but `thinking_level="minimal"` keeps it
  bounded, identical to today.

### 3.2 Instrumentation carries over per-step

Reuse the existing `_Row` (name/type/src/tier) and `TokenCost` usage. Add a per-step roll-up:

```
WORKDAY WIZARD — PER-STEP INSTRUMENTATION
  STEP                     FIELDS  L1  L2  L3  blank  FAIL   esc%
  Create Account/Sign In        3   3   0   0      0     0     0%
  My Information               11  11   0   0      2     0     0%
  My Experience                 9   8   0   1      0     0    11%   <- bleeds here
  Application Questions          4   4   0   0      1     0     0%
  Voluntary Disclosures          4   4   0   0      0     0     0%
  Self Identify                  3   3   0   0      0     0     0%
  ----------------------------------------------------------------
  TOTAL fields filled : 33   |  MAP calls: 5   |  L3 escalations: 1
  TOTAL LLM COST      : $0.0061
  aggregate escalation rate (L2+L3+FAIL / fillable) : 3%
```

The escalation-rate definition is unchanged: `(L2+L3+FAIL) / fillable`. The feedback loop is
identical to single-page — the per-step table localizes *which step + which widget* bleeds $,
so you add a deterministic routine THERE (e.g. a better multiselect commit on My Experience)
and drive that step's escalation back to zero. Per-step screenshots
(`step_<N>.png`) give visual proof at each stage. `form_present` pre-flight per step prevents
a mis-mounted step from escalating every field.

---

## 4. Adapter Roadmap — Prioritized by Market Coverage

Driver: the official-site ATS map. Count distinct big-name companies whose **official apply
path** each ATS unlocks. Build the adapter that unlocks the most names first.

Tally from the ATS map (companies whose careers route to each host):

| ATS | Big-name companies in the map | Arch | Schema? | Current state |
|---|---|---|---|---|
| **Workday** | Nvidia, Salesforce, Adobe, Workday, Uber, Capital One, Walmart, Target — **8** | multi-page wizard + account | NO | partial: `platforms/workday.py` guardrails + auth machine exist; **build `WorkdayAdapter` first** |
| **Greenhouse** | Stripe, Databricks, Anthropic, Coinbase, Airbnb, DoorDash, Snowflake — **7** | single-page | YES | **DONE** (`ats_greenhouse.py`, 0% / $0.0015) |
| **custom/in-house** | Google, Apple, Microsoft(Eightfold), Amazon, Meta, Netflix(Eightfold), JPMorgan(Oracle Fusion), Goldman — **8** | multi-page, mostly account | NO | each is bespoke; **not a single adapter** — deprioritize except Oracle Fusion (shared engine, JPMorgan) |
| **Lever** | Palantir — **1** | single-page | partial | cheap single-page win |
| **Ashby** | OpenAI — **1** | single-page (embed) | YES (strong) | cheap, schema-driven single-page win |

Prioritized build order:

1. **WorkdayAdapter (multi-page) — HIGHEST ROI.** Unlocks 8 marquee names (Nvidia, Salesforce,
   Adobe, Uber, Capital One, Walmart, Target, Workday) with ONE adapter. Deterministic
   cross-tenant flow + stable `data-automation-id` + existing in-tree guardrails make it the
   best effort-to-coverage trade despite the account wall. This is the whole reason for the
   multi-page engine. **Build first.**
2. **AshbyAdapter (single-page, schema) — cheapest win, validates contract reuse.** Public
   `jobPosting.info` returns an explicit `applicationForm` field spec (like Greenhouse's
   `?questions=true`). Reuses the proven single-page pipeline verbatim with `multi_page=False`;
   unlocks OpenAI and proves the new contract didn't regress single-page. Low effort.
3. **LeverAdapter (single-page) — cheap.** One clean page, Postings API gives custom questions;
   fixed contact fields. Unlocks Palantir. `multi_page=False`. Low effort; do alongside Ashby.
4. **OracleFusionAdapter (multi-page) — second wizard, high single-name value.** Oracle
   Recruiting Cloud (`*.oraclecloud.com`) unlocks JPMorgan and is a common enterprise ATS; the
   in-tree `ghosthands/platforms/oracle.py` + `dom/oracle_combobox_llm.py` already have the
   deepest custom handling (cx-select comboboxes need real keyboard typing, reject JS
   value-set). Build after Workday proves the wizard loop, reusing `run_wizard`.
5. **iCIMS / Taleo / SuccessFactors (multi-page wizards) — coverage fill-in.** No public
   schema; standard stepped Personal→Work→Education→Questions→Review wizards. Each reuses
   `run_wizard`; add as demand warrants. (SmartRecruiters/Workable already have in-tree
   platform files and are config-dependent — treat per the `generic.py` heuristic:
   Next/Continue ⇒ wizard, bare Submit ⇒ single-page.)

Custom in-house portals (Google/Apple/Microsoft/Amazon/Meta/Netflix/Goldman) are each bespoke,
account-gated, and often anti-bot-hardened (Eightfold for MSFT/Netflix). They are NOT a shared
adapter and give one name each — **deprioritize** behind Workday + the single-page trio + Oracle.

**Sequencing rationale:** ship the two single-page wins (Ashby+Lever, ~3 names, days) in
parallel with WorkdayAdapter (8 names, the strategic build). That validates the
`multi_page`-flag contract split from both sides — single-page adapters keep `run_single_page`
untouched, while Workday exercises `run_wizard` — before investing in Oracle/iCIMS/Taleo.

---

## 5. Risks & Unknowns (honest)

**Login / account wall (CONFIRMED, structural).** Workday's step 1 Create Account / Sign In is
mandatory and was adversarially re-verified on Blue Origin, Cadence, NVIDIA, Intel — the form
steps literally do not exist in the DOM until authenticated. Consequences:
- Every Workday application needs a **real, deliverable email inbox**, not a throwaway string.
- Accounts are **per-tenant** — Nvidia and Uber are separate accounts. Need account
  storage/lookup per (user, tenant) and a sign-in-vs-create decision. This is real state, not
  in scope of the fill engine and must be designed alongside (credential store + secret
  bootstrap, see V2 claim/secrets split in memory).

**Email verification (CONFIRMED likely, blocks autonomy).** Many tenants require clicking an
emailed link or entering a code before the wizard proceeds. The engine cannot fabricate this.
`authenticate` returns `needs_verification=True` and the run **halts for HITL or an inbox-poll
integration** (IMAP/API mailbox the worker can read). Without that integration, Workday runs
stall at step 1. This is the single biggest autonomy blocker, above any widget concern.

**CAPTCHA / anti-bot on account creation (UNKNOWN — not exercised).** The read-only recon
never created an account, so we have NOT observed whether Create Account triggers a CAPTCHA,
hCaptcha, or device-fingerprint challenge. Known anti-bot artifacts already seen: Blue Origin's
**honeypot** field ('for robots only') and sign-in honeypots `beecatcher` / `click_filter` —
the filler must leave these empty (handled in §2.2), but a hidden honeypot is far weaker than a
visible CAPTCHA. **Assume a CAPTCHA is possible at account creation and treat it as a HITL
halt, not a solvable step.** Eightfold-powered custom sites (MSFT, Netflix) and bank portals
are more likely to be hardened — another reason to deprioritize them.

**`data-automation-id` stability (LOW risk, but assumed not guaranteed).** aid was
byte-identical across NVIDIA wd5 and Intel wd1 (different tenants/data centers) and matches the
in-tree `WORKDAY_SELECTORS` reverse-engineered from production — strong evidence it is stable.
But it is Workday's internal hook, not a public contract; a Workday platform release could
rename ids. Mitigation: aid is the primary locator with ARIA role/label fallback (recon's own
recommendation for aid-less elements), and per-step `form_present` + the instrumentation table
will surface a selector regression immediately as a step-wide escalation spike.

**Portal/iframe timing & virtualization (MEDIUM, mostly solved).** `get_elements_by_css_selector`
queries the TOP frame only and the option portal (`activeListContainer`) mounts late and is
virtualized. Recon's mitigations (sleep 1–4s after click, read the portal not the field, use
`aria-setsize` + scroll) are encoded in §2.4, but timing is inherently flaky and is the most
likely source of L2 retries. Some tenants may embed widgets in iframes (recon: confirm
per-tenant) — those would need iframe drilling like Greenhouse's embed handling.

**In-form widgets unverified live (MEDIUM — evidence is indirect).** The dropdown/multiselect/
date/file widget DOM and driving rules are grounded in (a) the live Workday filter-bar widgets
which use the SAME UXI library, (b) the in-tree toy fixture, and (c) the production DomHand
executors — but the actual auth-gated My Information / My Experience widgets were NOT driven in
the read-only recon (no account was created). First live run behind a real account is where
these routines get truly validated; budget for an iteration pass on the per-step instrumentation.

**L3-inside-a-wizard session teardown (KNOWN bug, prerequisite).** `fill_with_ladder`'s L3
stops the shared CDP client; in a wizard that aborts every remaining step. Until session
re-attach is fixed, wizard mode caps at L2 for non-critical fields. Tracked as a prerequisite,
not a new risk, but it directly limits how much the wizard can lean on L3.

**Step-count / shape drift across tenants (LOW — handled by design).** Step lists differ
(Visa 6 / Nvidia 7 / Blue Origin 8; Self-Identify present only on US roles; Application
Questions split on Blue Origin). The design reads the `progressBar` at runtime and never
hardcodes a step list, so this is absorbed — but it means the engine must tolerate steps it has
no specific routine for, falling back to generic field classification + L2/L3.
