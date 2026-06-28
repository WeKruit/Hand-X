# Widgets & Repeaters — Build Plan

Synthesis of five parallel investigations into the schema-driven ATS filler
(`ats_engine.py` + `GreenhouseAdapter` / `AshbyAdapter` / `LeverAdapter`).
All claims below were re-grounded against the actual source in
`experiments/jobapply-core/` — line numbers and the `page.evaluate` return-type
root cause are verified, not trusted.

**Scope discipline (per project rules):** every fix below is a targeted,
per-field/per-widget change. No broad rewrites of the shared fill code, no
compatibility shims, no speculative fallbacks. The engine's invariant pipeline
(ONE map call → L1/L2/L3 ladder → read-back → instrument) is correct and stays
untouched except where explicitly noted.

---

## 0. The single root cause that dominates everything

**`page.evaluate()` in this vendored browser_use returns the Python `str()` of the
JS result, NOT a lowercase JSON string.** Verified directly in
`.venv/.../browser_use/actor/page.py::evaluate()`:

```python
value = result.get('result', {}).get('value')
if value is None:        return ''
elif isinstance(value, str): return value
else:                    return json.dumps(value) if isinstance(value, (dict, list)) else str(value)
```

So a JS `true` → Python `str(True)` → **`'True'`** (capital T), and `false` →
**`'False'`**. Booleans are *not* `'true'`/`'false'`.

Every adapter that returns a bare JS boolean from `page.evaluate` and compares it
with `== "true"` is **permanently False**. The code comments in `ats_ashby.py`
("page.evaluate ALWAYS returns a string ('true'/'false')") are factually wrong.

This one defect, fixed in 5 lines across 2 files, flips most of the currently-failing
Ashby and Lever fields from FAIL to L1. It is the highest-leverage change in this
entire plan and the first thing to do.

**Canonical fix everywhere a JS boolean comes back through `page.evaluate`:**
```python
return str(res).lower() == "true"     # accepts 'True', 'true', True
```

---

## 1. Ashby / Lever status

### Lever — **PARTIAL → trivially production-trustworthy** (1 line + 1 handler)

Verified end-to-end via `eng.run(LeverAdapter(), …)` on two live postings (Apollo
Research "Backend Engineer (Product)" 21 fields; WHOOP "Android Engineer" 20
fields). Both reached `status=FILLED` at ~$0.003 each, 1 map call, 0 escalations.
Core machinery works: all contact text, `urls[]` links, pronouns checkbox, yes/no
radios (work-auth / sponsorship reasoned correctly from profile), and all
open-ended textareas fill at L1 and are visually confirmed.

Exactly **two** real defects:

| # | Defect | Location | Fix | Severity |
|---|--------|----------|-----|----------|
| L-1 | Native `<select>` boolean-return bug. `_select_native` ends `return res is True or res == "true"`; `res` is the string `"True"`, so it returns False even though the option **is** selected. Mislabels every EEO select (gender/race/veteran) and "where did you hear" as FAIL, and would needlessly escalate to L3. | `ats_lever.py:309` | `return str(res).lower() == "true"` | **CRITICAL, 1 line** |
| L-2 | `location` typeahead genuinely doesn't fill. Mapped as `source=standard/type=text` and filled with `el.fill()`, but Lever's React autocomplete (`.location-input` + `.dropdown-results`) clears the raw value → DOM reverts to `''` → read_back correctly returns False. **Required on WHOOP.** | `ats_lever.py` fill path | New location handler keyed on `field.name == "location"` (see §2.1, Lever variant) | **HIGH** |

Notes (not bugs): `consent[marketing]` opt-in on Apollo was checked by MAP's
"required acknowledgement" safe-default — it's an *optional* marketing box and
should be left unchecked unless `required`. Worth a one-line tightening of the
`_MAP_SYSTEM` consent rule, but not blocking. The engine screenshot clip is
anchored on `#first_name,[name=first_name],#email,[name=email]` (`ats_engine.py:343`)
and renders a too-narrow left strip for Lever's right-shifted layout — cosmetic,
engine-owned, fields *are* filled.

**Verdict after L-1 + L-2: production-trustworthy.** L-1 is the whole-board
unblocker; L-2 is the one remaining true gap.

### Ashby — **PARTIAL → production-trustworthy after 3 fixes**

Verified on two live postings (OpenAI "SWE Codex App" 13 fields; Taekus "Senior
Fullstack" 8 fields). Both reached the live form; schema extract via the
`non-user-graphql` API is solid ($0). All text-class fields fill flawlessly at L1
(name, email, phone, preferred-name, LinkedIn URL, plain-text location, textareas).
Screenshots confirm booleans are *genuinely empty on the page*, not just read-back
false-negatives.

Three defects, in priority order:

| # | Defect | Location | Fix | Severity |
|---|--------|----------|-----|----------|
| A-1 | `page.evaluate` case-sensitivity. `res == "true"` is always False, killing **every** boolean and multi_select fill *and* read-back. | `ats_ashby.py:240, 263, 324, 342` | `str(res).lower() == "true"` in all four; for the read-back at line 322–324 also fix the `null`/`""` guard (it now compares `checked == ""` and `checked == "true"`; with the str repr a real value is `'True'`/`'False'`/`''`). | **CRITICAL, 4 spots** |
| A-2 | Boolean read-back relies on a checkbox Ashby never updates. Yes/No render as `<button>Yes</button><button>No</button>` + a **hidden `display:none` `<input type=checkbox name=path>`**. Clicking Yes does NOT flip `checkbox.checked` (stays false) and adds no `aria-pressed`/`aria-checked`/`data-state` — React keeps selection internal. So `read_back` via `cb.checked` (lines 314–324) can NEVER pass even after A-1. The fill *click* targets the correct button; only confirmation is broken. | `ats_ashby.py:314-324` (read), and `_fill_boolean` confirm | Detect the SELECTED button by its distinguishing class / computed-style after click (the active `<button>` gets a different style than the inactive one) and read THAT for both fill-confirm and read_back. | **HIGH** |
| A-3 | Location & date comboboxes use the wrong locator. `locate()` queries `[id='{path}'], [name='{path}']` but these widgets have **neither**. Verified live: location = `<input type=text role=combobox placeholder='Start typing...'>`, date = `<input type=text placeholder='Pick date...'>`, both id-less and name-less. `locate()` returns None → fill never runs → FAIL. | `ats_ashby.py:177-187` (`locate`) | Every Ashby field is wrapped in `<div class='_fieldEntry…' data-field-path='{path}'>`. Locate via that container: `[data-field-path='{path}'] input` (e.g. `[data-field-path='_systemfield_location'] input[role=combobox]`). `data-field-path` is present on ALL fields. | **HIGH** |

Multi_select fill+click already works in isolation (`checked_after_label_click=true`);
its only failure is A-1, so it passes for free once A-1 lands.

**Verdict after A-1/A-2/A-3: production-trustworthy.** A-1 is one-line-per-spot and
unblocks multi_selects immediately; A-2 and A-3 are small, well-specified
per-widget changes.

### Summary

| Adapter | Now | After fixes | Blocking work |
|---------|-----|-------------|---------------|
| **Lever** | PARTIAL | **WORKS** | L-1 (1 line) + L-2 (location handler) |
| **Ashby** | PARTIAL | **WORKS** | A-1 (4 spots) + A-2 (selected-button read) + A-3 (`data-field-path` locator) |

---

## 2. New widget routines for `GreenhouseAdapter` / engine

All routines below are **deterministic, no-LLM, fill-only**, and reuse the
adapter's existing `_combobox` / text-fill machinery wherever possible. The common
theme: these widgets are NOT in any schema API — they exist only in the live DOM
and must be discovered/handled at the `locate`/`fill` stage, keyed on a recognizable
field name or label.

### 2.1 Location geocomplete — **HIGH confidence**

**Greenhouse (react-select v5 geocomplete).** Schema field `name="location"`, but
the live DOM input is `id="candidate-location"` (no `name`). The current
`GreenhouseAdapter._locate('location')` (`[id="location"],[name="location"]`)
**cannot even find it**, and even if it could, `el.fill()` alone sets the visible
text but does NOT geocode → the form is rejected on submit for missing lat/long.

**Critical correction to the original premise:** selecting a suggestion does NOT
populate DOM-observable `#longitude`/`#latitude` on `job-boards.greenhouse.io`.
Exhaustively checked: no `#longitude`, `#latitude`, `[name=longitude/latitude/location]`
nodes exist before OR after selection; `input[type=hidden]` is empty; `FormData(form)`
has no lon/lat/loc keys. Lat/long live in React state and serialize only at submit.
**The only observable success signal is `.select__single-value`.** Any read_back
asserting populated hidden lat/long is wrong.

**Drive routine** (add a branch keyed on `field.name == "location"` or label
containing "Location"):
1. Locate: `el = page.get_elements_by_css_selector('#candidate-location')[0]`. Anchor on the id; do NOT rely on name.
2. Open + type: `await el.click()`; sleep ~0.3s; `await el.fill(city)` (profile location verbatim, e.g. `"San Francisco, CA, USA"` — surfaces the SF municipality as option-1).
3. Wait for suggestions (deterministic poll, not fixed sleep): up to ~16×@0.25s until `page.get_elements_by_css_selector('[id^="react-select-candidate-location-option"]')` is non-empty (geocode round-trip done).
4. **Commit option-1 via a TRUSTED Enter key — this is the crux.** Dispatch CDP `Input.dispatchKeyEvent` twice on the focused input: `{type:"rawKeyDown", windowsVirtualKeyCode:13, code:"Enter", key:"Enter"}` then `{type:"keyUp", …}`, `session_id = await page.session_id`. **Send NO ArrowDown first** — react-select pre-highlights option-1, so Enter alone selects the exact top suggestion (one ArrowDown overshoots to "South San Francisco"). Do NOT use `Element.click()` on the option div and do NOT JS-dispatch synthetic Mouse/Keyboard events — react-select ignores untrusted events and the menu closes with nothing selected (all three failed in testing; only a trusted CDP key commits).
5. Settle: poll up to ~10×@0.35s reading `.select__single-value` text until non-empty.

**Read-back:**
```js
() => { const ci=document.getElementById('candidate-location');
  const cont=ci.closest('.select-shell')||ci.closest('[class*=container]');
  const sv=cont.querySelector('.select__single-value,[class*=single-value]');
  return sv?sv.textContent:''; }
```
SUCCESS = `sv` non-empty AND `norm(profile_city)` is a substring of `norm(sv)`
(e.g. `"sanfrancisco"` in `"sanfrancisco,california,unitedstates"`). Corroborating:
`#aria-selection` reads `"option <sv>, selected."` and the input's `aria-expanded === "false"`.
A populated single-value is the transitive guarantee that lat/long are set (no DOM
value to assert for them).

**Confidence: HIGH** — reproduced identically twice; proved synthetic clicks/keys
fail while trusted CDP Enter works; proved hidden lat/long are not DOM-observable.

**Lever variant (for L-2).** Lever's location is `input id="location-input"` (also
class `.location-input`) with a `.dropdown-results` autocomplete and a hidden
`selectedLocation` companion. Same shape: focus → type city → wait for
`.dropdown-results` → commit the first suggestion (click first result OR trusted
Enter) so the hidden `selectedLocation`/`candidateSelectedLocation` populates. Key
this handler on `field.name == "location"` in `LeverAdapter.fill()`. (Note: Lever's
location is *not* react-select, but the type-then-pick pattern is identical;
`el.fill()` alone is what fails.)

### 2.2 Date selection — **HIGH confidence**

**This is the employment/education-history date widget**, a **2-part split control**
(month combobox + year text), NOT `input[type=date]`, NOT a 3-segment spinner, NOT a
calendar popup. It is injected client-side and **does not appear in the boards-api
schema at all** — discover it on the live DOM.

- MONTH: react-select combobox, `id="start-date-month-0"` (and `end-date-month-0`); options are full month names `January`…`December`; chosen label renders into `[class*=single-value]`; the input's own `.value` stays `""`.
- YEAR: plain text input, `id="start-date-year-0"`, `maxlength=4`.
- Indexed by `-0`, `-1`, … inside a `div.date-row`. No day component (Greenhouse history dates are month+year only). Education variant uses a double-dash pattern, e.g. `start-year--0`.

**Important disambiguation:** questions whose *label* mentions a date ("When is the
earliest you can start?") are NOT date widgets — boards-api gives them `input_text`
and they render as a plain `<input type=text maxlength=255>`. Drive those as ordinary
text (e.g. `"2026-08-01"` from `profile.available_start_date` verbatim); no date
handling. The month/year split widget only appears via the live DOM inside
employment/education repeaters.

**Drive routine** — given ISO `"YYYY-MM-DD"` (or `"YYYY-MM"`) and part index `i`:
1. Split: `year = value[:4]`; `month_name = calendar.month_name[int(value[5:7])]` ("August" for 08). Day discarded.
2. YEAR: `await first(page, f'#start-date-year-{i}').fill(year)`.
3. MONTH (reuse the existing `_combobox` mechanism against `id="start-date-month-{i}"`): click → `fill(month_name)` → poll `[id^="react-select-start-date-month-{i}-option"], [class*="select__option"]` up to ~8×@0.35s → pick the option whose text `== month_name` (exact), else `opts[0]` → click.
4. Education variant: same logic, ids `start-month--{i}`/`start-year--{i}` or `month--{i}`/`year--{i}`; detect by which id exists.

The month combobox is the *same* react-select pattern `GreenhouseAdapter._combobox`
already drives, and the year is the *same* plain-text path — **no new mechanism is
needed, only value-splitting + per-part locate.**

**Read-back** (two independent, both pass):
- YEAR: `got = el('#start-date-year-{i}').value`; success = `got.strip() == year`.
- MONTH: read the single-value div (input `.value` stays `""`):
  ```js
  () => { const a=document.getElementById('start-date-month-{i}');
    const c=a.closest('[class*=select__control]')||a.closest('[class*=control]')||a.closest('[class*=container]');
    const s=c&&c.querySelector('[class*=single-value]'); return s?s.textContent:''; }
  ```
  success = `month_name.lower() in text.lower()`.
- Composite = `ok_year AND ok_month`.

Value source = `profile.experience[].start_date / end_date` and
`education[].start_date / graduation_date` (already ISO `YYYY-MM` in `rich_profile`,
so the split is trivial).

**Confidence: HIGH** — drove the real cloudflare widget headless: `read_year="2026"`,
`read_month="August"`, PASS=true; also filled and read back the End pair; split
structure confirmed in raw DOM across employment (cloudflare) and education
(databricks) variants. **Workday date is UNTESTED** (segmented MM/DD/YYYY
`[role=spinbutton]` ×3 behind auth; HARD RULES forbid sign-in). Spec if reached:
focus the month spinbutton, type continuous digits `08 01 2026` (auto-advances), do
NOT open the calendar, read back each spinbutton's `aria-valuetext`.

### 2.3 Signature (typed-name + date) — **HIGH confidence; draw-canvas → HITL**

Across Greenhouse / Lever / Ashby, real signatures are **typed-name + date**, never
draw-canvas. **Zero `<canvas>` signatures across ~25 boards.** Modern Greenhouse
`job-boards` has NO typed-name/date/canvas signature at all — self-ID renders as
react-select dropdowns. The signature surface lives on Lever's OFCCP CC-305 block
and as date-of-signature text fields.

**Lever CC-305 (the load-bearing case):**
- Gate `<select name="eeo[disability]" id="disabilitySelectElement">`. The Name/Date row starts under a `display:none` div (rect 0×0, `offsetParent=null`, `required=false`) and `el.fill()` no-ops while hidden.
- Setting disability to "Yes, I have a disability" reveals the row → inputs become width 489, `offsetParent != null`, `required=true`.
- **The select is React-controlled:** `el.select_option` (CDP-by-value) and Playwright click+select_option both revert; **only the native `HTMLSelectElement.prototype` value-setter + bubbling `input`+`change` sticks** — which `LeverAdapter._select_native` (lines 281–309) **already does** (modulo the L-1 boolean-return bug). So **no adapter change is needed for the gate** once L-1 is fixed.
- Both signature inputs are `type=text`; `el.fill()` works once visible. The engine fills in document order and disability precedes the signature, so reveal-then-fill already works.

**Drive routine:**
- **Typed-name signature:** treat fields whose name ends `…Signature` (e.g. `eeo[disabilitySignature]`) as deterministic `profile.full_name`; `el.fill(full_name)`; read_back `this.value`.
- **Date-of-signature:** treat `…SignatureDate` as today. Lever: `el.fill(today as MM/DD/YYYY)`. Ashby: `el.fill(today as ISO)` (the "When can you start" `placeholder='Pick date'` input is not readonly; `el.fill('2026-06-26')` sticks; the picker emits MM/DD/YYYY). Generic `input[type=date]`: `el.fill('YYYY-MM-DD')`.
- **Recommendation:** treat signature-suffixed/date-suffixed EEO fields as deterministic (full_name / today) rather than LLM-mapped, because MAP may return empty for the bare "Name"/"Date" CC-305 labels. This is a small per-field name-suffix rule in the adapter's value resolution, not an engine change.

**Read-back** (verified live): Lever `eeo[disabilitySignature].value == "Jordan Avery"`;
`eeo[disabilitySignatureDate].value == "06/26/2026"`; Ashby date `.fill 2026-06-26 → "2026-06-26"`.
Reveal precondition: after disability=Yes, assert both inputs `offsetParent != null`
and `required=true` before filling.

**Draw-canvas:** not deterministically fillable → **flag to agent/HITL, never
fabricate strokes.** Absent on all three in-scope ATSes, so this is a guardrail, not
active work.

**Confidence: HIGH** for Greenhouse/Lever/Ashby (load-bearing Lever Name+Date and
Ashby date fills executed and read back, with a screenshot showing "Jordan Avery" +
"06/26/2026" in the revealed row). **MEDIUM** only on whether MAP returns
full_name/today for bare Name/Date labels — hence the deterministic-treatment
recommendation, which removes the uncertainty.

---

## 3. Experience-repeater verdict

**Is it solvable purely schema-driven? NO.** The boards-api schema **cannot
enumerate repeater rows** — it emits only a top-level string flag
(`education_required`/`employment_required`). The per-row subfields
(`school--N`/`degree--N`/`discipline--N`) and their option taxonomies exist ONLY in
the live DOM, and ONLY after you click "Add another." The engine's "ONE map call →
flat list of FormField" model **fundamentally does not cover repeaters.**

### Recommended architecture: **HYBRID** — a stateful repeater add-row loop layered on the schema-driven engine

Not pure schema-driven (the schema lacks rows), not full-agent (deterministic DOM
mechanics are proven and cheap), but a **separate repeater pass** that reuses:
- the engine's MAP step to map ONE profile entry → `{school, degree, discipline}` values per row,
- the existing `GreenhouseAdapter._combobox` routine for each subfield,
- the orchestration shape and anti-loop guards from `ghosthands/actions/domhand_fill_repeaters.py`.

**Do NOT shoehorn rows into the flat `FormField` list.** Naming a field `school--0`
and bolting it into the existing list would break `form_present` / read_back /
map assumptions. Keep the repeater pass structurally separate.

### Concrete plan to enter N experiences

1. **DETECT** in `greenhouse_schema`: read the top-level `education`/`employment`
   string flag (currently ignored). If present, mark a *synthetic repeater section*;
   the engine must NOT flatten it into FormFields.
2. **CONFIRM** in DOM: `div.education--container` / `button.add-another-button` exist.
3. **PULL** N entries from `profile["education"]` (or `["experience"]`); map
   `field_of_study → discipline`.
4. **LOOP** `i` in `0..N-1`:
   a. If `i > 0`, click `button.add-another-button` and wait until `input#school--i`
      exists. **Fill-before-add is mandatory** — clicking "Add another" on an empty
      row is a silent no-op (proven). Interleave; never click Add N times up front.
   b. For each subfield (school, degree, discipline), run the combobox routine:
      locate `#<field>--i` → click → `fill(query)` → poll
      `[id^="react-select-<field>--i-option"]` → pick exact-else-partial-else-first →
      click → read_back via `[class*=single-value]`.
   c. Verify all three read-backs non-empty before advancing.
5. **NO final "Save"** — Greenhouse rows are inline/persisted on the page (distinct
   from Oracle/Workday which DO need a per-row commit).
6. **STOP** at N rows.

**Cost: ZERO extra LLM** when profile values map to the taxonomy by string match;
escalate a single combobox to the in-tree LLM ranker only when the typeahead menu
has multiple near-matches.

### What to reuse from `domhand_fill_repeaters.py`
- Overall orchestration shape: read entries → observe existing rows → add only the delta (`entries[existing:]`) → fill inline → advance.
- Existing-row counting (adapt `_COUNT_SAVED_TILES_JS` to count `div.education--form` / `school--N` inputs) so re-runs don't duplicate rows.
- Anti-loop guards: `consecutive_same_value_count` break, "zero fields found → stop", skip-save-on-failure — directly applicable to avoid infinite "Add another" loops.
- `_observe_existing_entries` anchor-matching (anchor = school for education, company for experience) + `normalize_name` exact-match → `batch_match_entries_llm` fallback — exactly the taxonomy ranker for the "Berkeley → Acupuncture College - Berkeley" mismatch.
- `_get_entries_for_section` profile extraction.
- **NOT reusable as-is:** the tile-badge selectors (`.apply-flow-profile-item-tile--saved`) target a *different* Greenhouse surface; use row-input counting here.

### Honest limits
- School/Degree/Discipline are **closed server taxonomies** — a no-match profile value yields a wrong partial or nothing; the in-tree exact-then-LLM ranker is required, not optional.
- boards-api gives row PRESENCE but never row COUNT or options — row count comes from the profile, options come from live typeahead. The engine cannot pre-plan rows.
- Per-board variance: new-template Greenhouse (anthropic, databricks) turns education into flat custom questions with NO repeater — detection must branch.
- Rapid combobox fills triggered a CDP WebSocket reconnect once — add small waits/retry.
- **Workday** (untested, account-gated): numbered "Work Experience N" headings, per-section "Add" buttons, rows often pre-filled from resume parse — the loop must count existing rows and add only the delta. Needs its own commit-per-row, unlike Greenhouse.

**Bottom line:** repeaters need a mechanism *beyond* the current flat-field model — a
stateful add-row loop. It is solvable, cheap, and the in-tree
`domhand_fill_repeaters.py` already encodes the hard-won pattern; the work is
adapting it to the `--container`/`--form` board surface and wiring detection off the
ignored schema flag.

---

## 4. Prioritized build order (effort vs coverage)

Ordered by **(coverage unblocked) ÷ (effort)**. The first two items are
near-free and unblock whole ATSes — do them immediately.

| # | Item | Effort | Coverage unlocked | Confidence | Notes |
|---|------|--------|-------------------|------------|-------|
| **1** | **Lever L-1 + Ashby A-1** — the `str(res).lower()=='true'` fix (1 line Lever, 4 spots Ashby) | **Trivial (5 lines)** | Every native-select EEO field on Lever; every boolean + multi_select on Ashby | HIGH | Single highest-leverage change. Flips most current FAILs to L1. Do first. |
| **2** | **Ashby A-3** — `[data-field-path='{path}'] input` locator | Small | Ashby location + date comboboxes (currently dead) | HIGH | One change in `locate()`. |
| **3** | **Ashby A-2** — selected-button class/style read for boolean confirm + read_back | Small | Ashby Yes/No read-back (fill click already works) | HIGH | Needed for booleans to *verify*, not just fill. |
| **4** | **Greenhouse location geocomplete** (§2.1) + **Lever L-2** (same pattern) | Medium | Required Location field on Greenhouse + Lever (WHOOP-blocking) | HIGH | type→trusted-CDP-Enter→single-value read. Reusable across both. |
| **5** | **Greenhouse date split-control** (§2.2) | Medium | Employment/education history dates (prereq for repeaters) | HIGH | Reuses `_combobox` + text path; only value-split + per-part locate. |
| **6** | **Signature typed-name + date** (§2.3) | Small | Lever CC-305 Name/Date; Ashby date-of-signature | HIGH | Mostly a name-suffix → deterministic-value rule; gate already handled by `_select_native` after #1. Draw-canvas → HITL guard. |
| **7** | **Experience-repeater hybrid loop** (§3) | **Large** | Education + employment history on classic Greenhouse boards | MED-HIGH | Needs the separate add-row pass + `domhand_fill_repeaters.py` reuse + taxonomy ranker. Depends on #5 (dates live inside rows). |
| **8** | **Multi-page / Workday adapter** | **Largest, still-pending** | Workday tenants (huge ATS share) but auth-gated | LOW (untested) | Segmented date spinbuttons, commit-per-row repeaters, login wall (HARD RULES forbid sign-in in test). Treat as its own milestone after 1–7 land. |

**Rationale:** items 1–3 are hours of work and make Ashby + Lever fully
trustworthy — the biggest coverage-per-effort win. 4–6 are the medium, high-confidence
Greenhouse widget routines that close the open single-page gaps (location/date/signature)
and share machinery. 7 is the one genuinely large single-page item and depends on the
date work. 8 (Workday/multi-page) is the largest and least-known; it should not block
the single-page hardening and belongs in a separate milestone.

---

## 5. Risks & unknowns (honest)

1. **A-2 selected-button styling is heuristic.** Ashby keeps Yes/No selection in
   React-internal state with no `aria-*`/`data-state`. We rely on the active button
   getting a *different computed style/class* than the inactive one. This held on the
   two probed postings but is the least-robust of the Ashby fixes — Ashby could ship a
   theme where active/inactive differ only by a property we don't read. **Mitigation:**
   read multiple distinguishing signals (background-color + border + class delta), and
   if none differ, fall back to L3 agent for that one field.

2. **Trusted-CDP-Enter is the only thing that commits react-select** (location,
   geocomplete). Synthetic clicks/keys are silently ignored. This is verified but
   means the routine is tightly coupled to CDP `Input.dispatchKeyEvent` and the
   pre-highlight-option-1 behavior. If a board pre-highlights differently, "Enter
   alone" picks the wrong row. **Mitigation:** read `aria-activedescendant` before
   committing and ArrowDown to the exact match if option-1 isn't the intended city.

3. **Hidden lat/long are React-state-only.** We can only assert the visible
   `single-value` label. We are *trusting* that a committed suggestion means lat/long
   serialize at submit. Since HARD RULES forbid actual submission, this is **unverified
   end-to-end** — we have not confirmed a geocompleted form is accepted on submit. This
   is the single biggest unknown for the location work.

4. **Repeater taxonomy mismatch.** Closed server taxonomies mean a profile value with
   no clean match yields a wrong partial ("Berkeley" → "Acupuncture College - Berkeley")
   or nothing. The exact-then-LLM ranker mitigates but doesn't eliminate this; some
   schools/degrees simply aren't in the taxonomy.

5. **Per-board variance is pervasive.** Education is a repeater on classic boards
   (coinbase/dropbox/discord/elastic) but flat custom questions on new-template boards
   (anthropic/databricks). Date and signature widgets are injected client-side and
   never appear in the schema. Every new-widget routine must detect-then-branch on the
   live DOM, which means coverage claims are per-board, not per-ATS.

6. **Workday is entirely untested** — segmented date spinbuttons, commit-per-row
   repeaters, and an auth wall that HARD RULES forbid us from crossing in testing. All
   Workday specs in this doc are *read-only inference*, not verified. Treat the Workday
   adapter as research-grade until we have a non-auth-gated probe path.

7. **CDP instability under rapid fills.** Repeater combobox bursts triggered a
   WebSocket reconnect once. The engine already re-attaches after L3
   (`ats_engine.py:247-249`), but the repeater pass runs its own tight loop outside the
   ladder and needs its own small waits + reconnect guard.

8. **MAP over-eager consent.** The `_MAP_SYSTEM` "required acknowledgement/consent"
   safe-default checked an *optional* marketing opt-in on Apollo. Low-risk but a
   business-logic leak — tighten the rule to only auto-check `required` consent
   options.

9. **Screenshot clip is Greenhouse-shaped.** `_screenshot` anchors on
   `#first_name`/`#email` (`ats_engine.py:343`); Lever's right-shifted layout clips the
   filled values out of the proof image. Cosmetic (fields *are* filled) but it
   undermines visual verification on non-Greenhouse boards — worth a generic
   form-bounding-box anchor eventually.
