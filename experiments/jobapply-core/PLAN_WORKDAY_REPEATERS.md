# Workday Repeater Filler — Design Plan & Assumptions

**Goal:** make the Workday "My Experience" mega-page (Experience / Education / Skills / Languages)
**deterministic + generic + fast** — today it falls to a vision agent (~13–17 min, ~$0.10/app, 95% of
all cost+time). Target: ~$0.002/page, seconds, fill-only (stop at Review, **never submit**).

## North-star principle (底层逻辑)
> **Anything a human can do, the agent (LLM + vision) can decide; the deterministic layer simply
> replays that same decision-pattern via CDP.**

- The **deterministic layer = cheap hands** — clicks/types/reads through CDP, no LLM in the loop.
- The **LLM = context + visual understanding**, used ONLY at genuine decision points (label→value,
  semantic equivalence, ambiguous option, last-resort vision).
- Every agent rescue is **recorded → derived into a new deterministic routine** (escalation → 0).

## Architecture — 3 pillars

### 1. `put(control, value)` — one verb, multi-level observe-act
Human model: "put value X in this field" — same intent whether dropdown / chip / radio / date.
```
put(control, value):
  click control → OBSERVE what appeared → classify (role/aria-haspopup/aria-autocomplete/tag):
     select  → portal options → best-match (exact>prefix>contains, bidirectional) → click
     chip    → type → options → best-match → CLICK option → verify pill count++   (no-match → SKIP item)
     date    → type continuous digits MMDDYYYY into segmented spinbuttons
     check   → click label, fire input/change
     text    → fill + verify
  MULTI-LEVEL: if target is behind a branch (cascade "Referral→Employee"), match branch → click →
               observe next level → pick leaf. depth-bounded (2–3) → else escalate THIS control.
  no real match → SKIP (leave blank if optional), never wrong-pick, never fail the section.
```
Caller never branches on archetype — the archetype is the mechanical tail hidden inside `put`.

### 2. Plan = seed hypothesis (1 cheap map call)
`map_plan(llm, detected_fields, profile)` → ONE `gemini-3.1-flash-lite` (thinking_budget=0) structured
call → the PLAN: label→value **+ target row counts**.
```
{ experience:[{row:0, fields:{"Job Title":"Senior SWE","Company":"Acme","From":"2021-06",
                              "To":"2024-01","I currently work here":true}}, ...4 rows...],
  education:[{row:0, fields:{"School":"UC Berkeley","Degree":"Bachelor's Degree","Field":"CS"}}],
  skills:["Python","Go","Kubernetes"],
  languages:[{name:"English",Comprehension:"Fluent",Overall:"Fluent",Reading:"Fluent",...}] }
```
LLM = semantics ONLY ("Summary of duties"→description, "Firm"→company). It never picks a DOM element.

### 3. reconcile-and-repair = fixpoint loop (the engine)
The plan is a **hypothesis**; the **live DOM + the app's own validation errors are ground truth.**
The executor is NOT a one-pass plan walk — it is a fixpoint loop over a **growing worklist**, because
the DOM is a **state machine** (a click can reveal sub-options or new required fields).
```
seed worklist from plan
loop:
   reconcile(DOM read-back + app validation):     # re-read AFTER each act → sees dynamic reveals
     planned field → read actual:
        semantic-equal(intended, actual) → DONE     (equivalence, NOT string equality)
        present but wrong               → DIVERGED  (re-pick)
        empty                           → MISSING   (fill)
        no such option                  → SKIP
     REQUIRED control with no plan entry → UNPLANNED (incremental map → fill)  # conditional reveals
     app validation errors               → authoritative MISSING/WRONG (names the field)
   dup-guard: rows_present >= target → NEVER click "Add Another"
   repair: fill MISSING · re-pick DIVERGED · incr-map UNPLANNED · cascade · skip nonexistent
until (no new fields appear) AND (validation clean)        # DOM stable = fixpoint
then advance.
   no-progress guard: same error set after a pass → escalate THAT ONE control to agent (+vision) → else stop.
```

**Ledger** (the reconcile state) blocks redo/dup AND is the **context handed to the agent** on escalation:
```
"Experience row 4 on screen. Rows 1–3 COMPLETE — do not touch, do NOT Add Another.
 Row 4 filled EXCEPT 'Field of Study'. Put exactly 'Computer Science'. That's the only task."
+ freeze every DONE control (physically can't redo).
```
The same **semantic matcher** does triple duty: (a) which option to pick, (b) did the app accept an
acceptable value, (c) is this committed pill the item we meant. One primitive, three uses. (拉通)

## Assumptions to VERIFY against a live Workday DOM (offline)
| # | Assumption | Verify |
|---|-----------|--------|
| A1 | Sections detectable structurally (heading keyword + subtree): Exp/Edu/Skills/Lang | `_SECTION_ROWS_JS` over saved DOM |
| A2 | Rows = repeated containers w/ per-row delete + ordinal ("Work Experience 1/2") | count rows == fixture |
| A3 | Each control's visible label resolvable structurally (label[for]/aria/nearest text) | label map == fixture |
| A4 | Archetype classifiable by role/aria-haspopup/aria-autocomplete/tag → select\|chip\|date\|check\|text | classify() per control |
| A5 | Dropdown options in a portal (activeListContainer/promptOption/menuItem); best-match works | offline match cases |
| A6 | Chips: type→options→click match→pill in `selectedItem`; count-verifiable; no-match→skip | pill DOM shape |
| A7 | Dates = segmented spinbuttons; continuous digits MMDDYYYY auto-advance | input structure |
| A8 | "Add Another" mounts a new empty row; rows==target → dup-guard holds | control present |
| A9 | Read-back returns committed value (option/pill/input) for semantic equivalence | read fns |
| A10 | App validation errors readable + name the field (authoritative MISSING/WRONG) | error markers in DOM |
| A11 | Cascade widget ("How did you hear about us") = multi-level menu, observable per level | menu DOM |
| A12 | Conditional reveal: answering reveals new required controls → reconcile catches | reveal fixture |
| A13 | Languages proficiency = 5 single-selects, SAME listbox archetype as A5 | classify == select |

## Verification method (offline-first — the rule that killed last round if ignored)
1. `GH_DUMP` ONE Intel mega-page DOM (single short live nav to capture, NOT a 14-min fill run).
2. Build `detect()` / `classify()` / `map_plan()` / `reconcile()` / `put()` as **pure functions over the
   saved DOM** → assert A1–A13 in **milliseconds**.
3. Only go LIVE to confirm the **acts** that need a real browser (trusted-Enter commit, cascade reveal,
   validation-error surfacing). No blind 14-min iteration.

## Hard invariants (never violate)
- **NEVER submit** — stop at Review; submit-guard enforces. Native email/password only; never SSO; never
  fill the beecatcher honeypot. Secrets via env/stdin, never CLI args. CAPTCHA → HITL. Throwaway emails, no real PII.

## Models (settled — do not re-litigate)
- Agent (repair/escalate): `gemini-3-flash-preview` (+ `use_vision="auto"`). bu-2-0 lost the bake-off.
- MAP / semantic: `gemini-3.1-flash-lite`, `thinking_budget=0` (flash-lite REJECTS `thinking_level="minimal"`).

## VERIFICATION RESULTS (offline, real Intel My-Experience DOM — `wd_offline/`)
Harness `wd_offline/detect_verify.py` (pure lxml, no browser, ms) over the captured
`fixtures/intel_step03_my_experience.html`. browser_use's SecurityWatchdog blocks `file://` AND
re-runs the saved SPA scripts (wipes/hangs the DOM) — so offline DOM testing is **pure-lxml, not a
browser session.** That itself is a finding: the offline harness must parse, not navigate.

| Assumption | Result | Evidence |
|---|---|---|
| A1 sections | **PASS** | headings found: my experience, work experience, education, **languages**, skills, resume/cv, websites |
| A3 labels (structural) | **PASS** | 14/15 controls labeled, incl. required `*` (Job Title*, School or University*, Degree*) |
| A4 archetype classify | **PASS** | `text·5 chip·4 select·2 date·2 check·1 textarea·1` — all correct |
| A2 rows | **partial** | 1 experience row + 1 education row mounted initially; "Add Another"×2 present (text, not aria-label) |
| A13 lang proficiency | **deferred (proves A12)** | Languages section = **0 inputs + an Add button** → the 5 proficiency selects mount ONLY after add. A static plan can't see them → the fixpoint loop is REQUIRED. |
| A10 validation | **deferred** | this dump is pre-Next → 0 error markers; needs a post-Next dump (the observe-after-act loop produces it) |

**2 detector nits caught offline in ms (would be 14-min live each):**
1. **Degree dup** — one widget matched as both `select` (button[haspopup]) and `text` (hidden input).
   Fix: collapse a select+input pair sharing a wrapper to ONE control.
2. **`selectedItemList` false-chip** ("items selected") — the pill CONTAINER matched as an input.
   Fix: restrict chip detection to the typeahead `<input>`, exclude the selectedItem container.

**Verdict:** the structural spine (detect sections / rows / controls / labels / archetypes) is **proven on
the real DOM**. Dynamic sections (Languages proficiency, validation) are correctly NOT in the initial
plan — which is exactly why the engine is a **fixpoint reconcile loop, not a one-shot plan.** Design holds.

## BUILD STATUS (implemented + verified)
**Decision layer — BUILT & OFFLINE-VERIFIED on the real DOM** (`wd_repeaters.py`, `wd_offline/`):
- `extract_offline`/`extract_live` (data-fkit-id row-aware), `dedup`, `reconcile` (row-aware, semantic
  equivalence, overflow), `make_plan` (real `gemini-3.1-flash-lite` semantic map). 13/13 asserts green
  (`test_detect.py`): 2 experience rows (225/226) + education + skills; start/end dates distinct; 3 dedup
  nits fixed. LLM map proven offline: natural keys mapped, `BS`->"Bachelor's Degree" canonicalized.
- **Act layer — BUILT** (`put` per archetype located row-safe by fkit, `ensure_rows` dup-guarded,
  `fill_deterministic` fixpoint loop) and **WIRED** into `ats_workday.fill_repeaters` deterministic-first
  (agent = residual backstop only). ruff clean, never submits.

**Live run (`wd_offline/live_verify.py`, Intel CXS API + throwaway account): BLOCKED UPSTREAM, not at the
repeaters.** Auth + autofill OK, but every Intel job gates My Information on a screening radio ("previously
employed by Intel?") that the *repair agent* couldn't register (the known `_click_radio` "label-click
doesn't check the React input" issue) → never advanced to My Experience, so `fill_deterministic` wasn't
exercised live. This is a My-Information-step defect, separate from the repeater engine.

## Status / NEXT
Repeater engine **built + comprehensively offline-verified**; **live observation pending** an unblock of
the My-Information screening radio (deterministic-answer that question instead of leaving it to the agent,
or pick a tenant without it), then watch `fill_deterministic`'s ledger (rounds/filled/rows_added/residual)
on a live My-Experience page. Dynamic cases (A10 validation, A11 cascade, A12 reveal) need a post-advance
dump — produced by the loop's observe-after-every-act once live.
