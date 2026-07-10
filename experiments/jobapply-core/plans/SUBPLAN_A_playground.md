# SUBPLAN A — Playground: "100% here ⇒ a NEW live page cannot surprise us"

Team A (Playground). Sub-plan only — no code in this doc. All paths relative to
`experiments/jobapply-core/` unless absolute.

Ground rules inherited from `HANDOFF_observe_act.md` (binding on every item below):

- **No static pattern matching** anywhere — locate by STRUCTURE, match by MEANING (LLM/VLM).
- **Every fixture ships its own `data-read` oracle** (JS over `f` returning painted truth).
- **Every fixture is selfcheck-gated** (winnable) before it may score.
- **Two oracles in ISO** (DOM read + verdict-consistency); both must stay 0/0.
- **Compare passing SETS across runs**, never just counts.

Current verified base: 125/125 ISO PASS ALL, false-green 0 / false-red 0; selfcheck 64 PASS / 0 FAIL / 61 SKIP.

---

## 1. COVERAGE GAP MAP — the finite combination space

### 1.1 The dimensions (finite by construction)

A form field's difficulty is fully described by six orthogonal dimensions. An ATS page is a bag of
cells from this space plus a page-structure wrapper. The space is finite; we enumerate it.

| Dim | Name | Values |
|---|---|---|
| **W** | Widget archetype | text, textarea, richtext, masked-text, native-select, custom-select (react-select / MUI / antd / radix / downshift / aria-APG / shadow), multi-select (7 shapes), radio (native / aria / styled / pill / card / matrix), checkbox (single / group / consent), toggle/switch, slider, rating, date (native / picker-grid / mask / select-pair / spinbutton-triplet), time, number (7 mask shapes), phone (3), address/geo (5), file, password, OTP-segmented, signature, ranking/drag, honeypot, captcha |
| **L** | Label/question binding | for/id, wrapping label, aria-labelledby (near / remote / multi-id chain), aria-label only, placeholder-only, floating overlay, heading-in-separate-card, table th row-header, table td left-cell, caption, legend (near / deep-nested / absent-with-sibling-p), right-of, below, sr-only, proximity-only (no programmatic bond), tooltip/describedby, duplicate labels, group From/To label, split-across-spans with decoy fragments |
| **O** | Option coupling (options vs trigger) | same container, sibling list, portal-to-body (with aria-controls / aria-owns / **blind** no-aria), portal collision (2 menus open), aria-activedescendant-only, virtualized window, scroll-to-load, async/debounced, min-query-length gate, remote-search-empty-until-typed, cascade-dependent, optgroup, checkbox-options-in-dropdown, iframe-hosted, shadow-hosted |
| **P** | Page structure | single field, multi-field flat, twins/dup-labels, grouped section (First/Middle/Last bleed), long-scroll (form below JD), accordion/tab-hidden, modal, iframe (field / whole-form), wizard steps w/ validation gates, error-summary-on-top, account-creation gate, repeaters (add-another), review page, conditional branch across sections |
| **G** | Language/locale | en, de, fr, es, ja/zh (CJK), ar/he (RTL), locale-specific date/number formats, localized options w/ English profile |
| **D** | Dynamic behavior | static, next-tick paint, delayed mount, async options, reveal-on-answer, disabled-until, remount-wipe-after-fill, blur-reformat, stale-node-replacement, layout-shift mid-interaction, async validator reject, autosave-disable window, overlay/banner interception, focus trap |

Full cross-product is large but **ATS-realizable cells are what live pages can actually serve**.
Coverage strategy (standard combinatorial testing, not exhaustive product):
1. **Every dimension VALUE covered ≥ 1 fixture** (1-wise: this is where all remaining gaps are).
2. **Pairwise for high-risk pairs only**: W×O (widget×coupling), W×D (widget×dynamic), L×P
   (binding×structure) — these pairs are where every historical live failure lived (pill-beside-
   combobox = L×P; portal-blind = W×O; remount-wipe = W×D).
3. Machine-readable ledger `runs/fixtures/coverage.json`: for each fixture, its (W,L,O,P,G,D)
   tuple. This file is both the gap report generator and the novelty gate for §4's miner.

### 1.2 What the 125 already cover (dense)

W: text/textarea/richtext/masks/native+custom selects (6 libs)/multi (6 shapes)/radios (7)/
checkbox/toggle/slider/rating/dates (6)/numbers (7)/phone (3)/geo (5)/EEO (9)/1 file shape.
L: for/id, remote aria-labelledby, placeholder-only, floating, separate-card heading, th row-header,
legend, right-of, below, dup-labels. O: same-container, sibling, portals (aria + blind), aria-owns,
virtualized, async, min-3, cascade, shadow. P: single-field only (+1 two-step fixture). G: en + 1 de.
D: next-tick, async, reveal, disabled-until, banner-dismiss, modal, occlusion.

### 1.3 UNCOVERED CELLS → the new-fixture worklist (~44 single-field fixtures)

**W gaps (15):**
| # | kind | trap |
|---|---|---|
| 1 | `repeater_workhistory_add` | Add-another employment block; commit-then-add (Oracle P3 loop lesson) |
| 2 | `repeater_education_remove` | pre-seeded row must be edited, phantom row removed |
| 3 | `file_native_visible` | plain visible `input[type=file]` (only the dropzone-hidden shape exists) |
| 4 | `file_replace_existing` | uploaded chip + Replace affordance; must not double-attach |
| 5 | `time_select_pair` | interview time as HH + MM selects |
| 6 | `datetime_local_native` | `input[type=datetime-local]` ISO round-trip |
| 7 | `otp_segmented_code` | 6 one-char boxes, auto-advance focus |
| 8 | `password_rules_checklist` | live rules checklist gates validity paint |
| 9 | `scroll_to_enable_consent` | T&C box: checkbox disabled until scrolled to bottom |
| 10 | `ranking_drag_order` | drag-to-rank — expected outcome = ESCALATE/NEEDS_HUMAN (a *negative* fixture) |
| 11 | `matrix_checkbox_grid` | multi-select per row (likert exists only as radio) |
| 12 | `language_proficiency_rows` | paired select+select per row + add-another |
| 13 | `honeypot_hidden_input` | visually-hidden decoy input; **expected = BLANK** (fill = FAIL) |
| 14 | `signature_typed_name` | type-name-as-signature with legal text binding |
| 15 | `captcha_placeholder` | inert captcha box; expected outcome = NEEDS_HUMAN, never false-COMPLETE |

**L gaps (6):** 16 `aria_label_only_no_text`, 17 `sr_only_label_icon`, 18
`proximity_only_paragraph` (no programmatic bond at all — pure structure/geometry),
19 `label_tooltip_asterisk_nodes` (label text fragmented across spans + required-star + info icon),
20 `td_label_left_cell` (2-col table, plain td not th), 21 `fromto_group_label_pair` (one label,
two inputs, value must split).

**O gaps (6):** 22 `activedescendant_only_combobox` (no aria-selected paint; selection is
aria-activedescendant), 23 `optgroup_native_select`, 24 `checkbox_options_dropdown`,
25 `dual_open_menu_collision` (two comboboxes, first menu left open — the Greenhouse
phone-country vs country collision, memory-documented), 26 `scrolltoload_listbox` (target option
only mounts after listbox scroll), 27 `options_portal_sibling_of_form` (portal not to body).

**D gaps (7):** 28 `remount_wipe_after_fill` (React resets value 300 ms post-commit; engine must
observe & re-commit — the wipe-gate class), 29 `blur_reformat_field`, 30 `late_mount_field`
(field mounts 2 s after load — discovery must not be one-shot), 31 `stale_node_replaced` (widget
re-created after first interaction; stale nodeId), 32 `layout_shift_midfill` (banner mounts and
shifts geometry between locate and click — coordinate staleness), 33 `async_validator_reject`
(server-style invalid paints 500 ms later; verdict must flip WRONG), 34 `autosave_disable_window`.

**G gaps (5):** 35 `localized_select_fr`, 36 `localized_form_ja` (CJK labels + options, EN
profile), 37 `rtl_radio_group_ar` (RTL layout), 38 `localized_date_de_dots` (DD.MM.YYYY),
39 `localized_native_yesno_es`.

**Verdict-layer gaps (3):** 40 `required_marker_variants` (required signalled ONLY by a separate
asterisk node / "(optional)" suffix inversion / aria-required — completeness verdict must agree),
41 `optional_unanswerable_mix` (optional field w/ no profile data on same page shape — SKIP must
not poison verdict), 42 `error_inline_after_commit` (field-level error node appears after fill;
verdict must read it).

**Live-caught mid-sweep (2) — evidence `runs/newats/sweep500b/004.json/.png` (Anthropic Greenhouse):**
| # | kind | trap |
|---|---|---|
| 43 | `alias_label_duplicate_field` | ONE textarea discoverable under TWO labels: visible `<label>` "Why Anthropic?" + a different aria/schema label ("What is your motivation for applying…") via aria-labelledby. Engine must fill ONCE; on encountering the alias row it must recognize the SAME control (DOM-identity cross-check, allowed by doctrine) — not re-escalate it as "occupied by foreign text". Oracle: data-read asserts the mapped value painted exactly once + results[] contains no ESCALATE for the alias. |
| 44 | `hidden_required_section` | required-marked radio group inside a `display:none` (conditionally-revealed, never revealed) section. Correct behavior = NOT_RENDERED/DEAD-class outcome, EXCLUDED from scoreable — not ESCALATE, and no blind click on unrendered inputs. Oracle: data-read asserts NO selection ever painted AND the result row's outcome classification is the not-rendered class. Generalizes directly to §2 flows: fields on not-yet-active wizard steps must classify the same way (asserted by the flow lane's gate/read oracles). |

All 44: same `all_fixtures.json` schema (kind/label/html/profile_value/expected/data-read/why_hard),
each selfcheck-gated on entry. For actuator-undrivable shapes (repeaters, drag), add the optional
`data-actuate` attribute (§4.3) so selfcheck can still PROVE winnability instead of SKIP.

### 1.4 The emergent-locate lane (ISO's admitted blind spot)

§4.3 of the handoff: single-field pages cannot reproduce emergent locate bugs (mis-binding to a
neighbor). Fix = **composed pages**: deterministic manifests of 6–10 existing fixtures per page,
adversarially ordered (dup labels adjacent, First/Middle/Last split across cards, country field
next to phone-with-dial-code, two portal selects adjacent, long JD text block above the form to
force scroll-locate). ~12 manifests cover the risky adjacencies.
**Bleed oracle** (new, cheap): every fixture on the page must read its OWN expected AND no fixture
may read another fixture's value (cross-field-bleed assert — the Sweep500 false-green root).
Reuse `build_playground.py` (already builds a combined page) — parameterize it into
`pages/manifest_*.json` + emit per-page HTML; extend `run_playground_iso.py` with a `--pages` lane
that runs each composed page as one engine run and evaluates all its data-reads.

---

## 2. MULTI-PAGE / WORKDAY-MIMIC SUPPORT

Key realization: Workday/Ashby wizards are **SPAs** — steps are client-side DOM swaps, not server
navigations. So a multi-page fixture is ONE html file with embedded step logic. No server change,
no routing — the static `http.server` in the runners already suffices.

### 2.1 Flow-fixture format — new file `runs/fixtures/flow_fixtures.json`

```json
{
  "id": "wizard_gate_workday_mimic",
  "kind": "flow_wizard_gate",
  "steps": 3,
  "html": "<one SPA document: step containers, Next/Back buttons, gate JS, error-summary JS>",
  "profile_values": {"First Name": "…", "Emergency contact name": "…"},
  "expected": {"First Name": "…", "Emergency contact name": "…"},
  "data_read": "window.__flowRead()",
  "gate_read": "window.__gateLog",
  "why_hard": "…"
}
```

- `data_read` = a whole-flow oracle the FIXTURE defines: returns `{label: painted}` across all
  steps (reads hidden steps' DOM too — SPA keeps them mounted or serializes on step-exit).
- `gate_read` = the fixture's gate log: every Next click is recorded as `blocked` (required
  missing) or `passed`. Oracle asserts the engine never advanced through a blocked gate and never
  false-advanced (a Next that silently no-ops is the flow version of the dead-handler bug).
- Winnability: `selfcheck.py` grows a flow branch — actuate each step's controls via each field's
  `data-actuate`/generic actuator, click Next, assert terminal step + `__flowRead()` == expected.
  Same PASS/FAIL/SKIP semantics; a flow fixture whose own gate JS can never unlock is caught in
  minute 1, exactly like the `(fn,0)` pill bug.

### 2.2 Runner change — `run_flow_iso.py` (new, ~120 lines, clones run_playground_iso structure)

Per flow fixture: serve the html; run the engine with `OA_FIXTURE_VALUES` = the flow's
label→value map; **loop**: engine fill-pass → engine clicks the advance control → repeat until
terminal or cap (steps+2 iterations). Two integration options, decided by what exists:
- If `oa_planner.py` already exposes an advance/next capability in the generic lane: drive it.
- If not: the flow runner grows the engine's first `advance()` primitive (locate the enabled
  step-advance button **by structure** — `button[type=submit]`, `[data-automation-id*=next]`
  prefix, role+enabled-state — never by its text). This is the same primitive the live Workday
  integration needs, so the playground drives the capability into existence.
Oracle per flow: `data_read` map equality per label (same `_match` normalizer) + `gate_read`
contains zero bypasses + verdict-consistency check unchanged. Additionally: fields on
not-yet-active steps must classify NOT_RENDERED/DEAD (excluded from scoreable), never ESCALATE
and never blind-clicked — the flow generalization of fixture #44.

### 2.3 The flow fixture set (10)

| kind | mimics |
|---|---|
| `flow_wizard_gate` | Workday: Next disabled/erroring until required fields valid |
| `flow_error_summary_top` | GOV.UK/Workday: submit → error list on top with anchor links; engine must repair the named fields |
| `flow_account_gate` | Workday account creation: email+pw+confirm+checkbox → form (cred policy: fixed test password; flow NEVER reaches submit) |
| `flow_reveal_across_steps` | answer on step 1 changes step 2's field set (conditional branch) |
| `flow_repeater_workhistory` | Add-another inside a wizard step; commit-then-add |
| `flow_iframe_whole_form` | entire wizard inside a same-origin iframe (Greenhouse embed shape) |
| `flow_review_page` | terminal review step renders all answers as text — the oracle reads the REVIEW rendering (ultimate read-back: what the human sees) |
| `flow_back_preserves` | Back then Next: values persist; engine must not double-fill or wipe |
| `flow_progress_steps_wd` | Workday `data-automation-id` progress bar + step containers, localized step names (structure-only detection proof) |
| `flow_scroll_long_jd` | form after a 4000px job description; forces OA_SCROLL_LOCATE in-playground |

---

## 3. DECOUPLED / WEIRD-DOM CLASS (question ≁ control container)

Already covered: aria_labelledby_remote, table_th_row_control_td, heading_above_separate_card,
choice_pill_beside_combobox_locate, ownership_blind_dropdown, downshift sibling list.

New fixtures — 10 concrete shapes seen on real ATS (each: own data-read, selfcheck-gated):

| # | kind | real-world shape |
|---|---|---|
| 1 | `wd_formlabel_sibling_no_aria` | Workday: label in `div[data-automation-id="formLabel"]`, input in a SIBLING `div[data-automation-id^="formField-"]` subtree; **no for/id, no aria at all** — bond is DOM-order/structure only |
| 2 | `wd_multiselect_pill_container` | Workday prompt: typing box `[data-automation-id="searchBox"]`; committed values paint as pills in a SEPARATE `[data-automation-id="selectedItemList"]` container ABOVE it — read-back lives in a sibling subtree |
| 3 | `wd_date_spinbutton_triplet` | Workday date: three role=spinbutton sections (`dateSectionMonth/Day/Year-input`) inside a display wrapper; the question binds to the wrapper's grandparent |
| 4 | `gh_iframe_form_host_label` | Greenhouse embed: form fields inside an iframe while contextual/question text sits in the HOST document (same-origin playground stand-in for `#grnhse_iframe`) |
| 5 | `ashby_dual_portal_collision` | Ashby: two adjacent comboboxes portal their listboxes to detached body roots with NO aria-controls; both menus present at once — ownership must come from interaction causality, not DOM |
| 6 | `lever_label_li_control_li` | Lever: question `div.application-label` in one `<li>`, control in the NEXT `<li>` — different list items, no shared card |
| 7 | `caption_labeled_radio_table` | question in a `<table><caption>`, radio per row-cell (survey grids) |
| 8 | `labelledby_two_id_chain_decoy` | input's aria-labelledby concatenates a FAR section heading + remote label; the visually nearest text is a DECOY for anything doing proximity-text matching |
| 9 | `legendless_fieldset_sibling_p` | fieldset with NO legend; question is a preceding-sibling `<p><strong>` OUTSIDE the fieldset |
| 10 | `flat_sibling_cards_no_group` | radio option cards are FLAT siblings interleaved with unrelated content — no group container element exists at all; grouping is purely by name attr + geometry |

These are the fixtures that make "detect by structure, never label text" falsifiable: #8 actively
punishes text-proximity matching.

---

## 4. VLM-BUILT FIXTURES — mining live screenshots into new coverage

Corpus already on disk: `runs/newats/sweep500/*.png` (~156 runs) + per-run `.json` ledgers (traces,
labels, urls) + `failcap.py` DOM captures. New tool: `runs/fixtures/fixture_miner.py`.

### 4.1 Pipeline (5 stages)

1. **Shape inventory (VLM):** per screenshot, one gemini-flash vision call: "list every visible
   form widget: question text, widget archetype, label placement, option arrangement, anything
   unusual". Batched; ~$0.001/shot. → `mined_shapes.jsonl`.
2. **Novelty gate (LLM, by meaning):** feed each shape + the dimension definitions (§1.1) + the
   covered-cell list from `coverage.json`; the LLM assigns the (W,L,O,P,G,D) tuple and answers
   "is this cell covered?". Principle+example prompt, NO keyword lists (doctrine §8.3). Only
   uncovered-cell shapes proceed.
3. **HTML synthesis — real DOM first:** if the run's ledger/failcap captured the widget's DOM
   skeleton, harvest THAT (real skeleton beats imagination). Only when no DOM exists does the
   VLM+LLM synthesize fixture HTML from the screenshot, few-shot-prompted with the 2–3 nearest
   existing fixtures (they define the house style: working JS handlers, data-read, expected,
   why_hard).
4. **Winnability gate (hard):** generated fixture must get selfcheck **PASS — SKIP is not
   acceptance for generated fixtures.** If the generic actuator can't drive it, the fixture must
   ship a `data-actuate` JS attribute (self-describing win procedure — one small selfcheck
   extension: prefer `data-actuate` when present, §4.3). Regenerate up to 2×; still failing →
   quarantine `mined_rejects/`, never scored. This is the §7.6 lesson industrialized: no fixture
   enters the suite without a machine-proved path to its own `expected`.
5. **Dedup + review:** LLM comparison against existing kinds ("same trap as an existing fixture?")
   to prevent fixture inflation; human eyeballs the why_hard before the fixture lands in
   `all_fixtures.json`. Then the standard loop: `trace_one.py <kind>` → engine fix if red →
   full ISO gate → compare passing sets.

### 4.2 The closure test (measurable "finite space" proof)

After M1–M5 land: run the miner over the NEXT fresh sweep's screenshots. **Acceptance = the miner
finds ZERO shapes in uncovered cells.** That is the operational meaning of "a new live page cannot
surprise us" — new pages may still fail live (mapper, load, anti-bot), but never on an unseen
widget/binding/coupling/dynamic shape.

### 4.3 One shared enabling change: `data-actuate`

Optional per-fixture attribute: JS (bare arrow over `f`) that programmatically wins the fixture.
selfcheck prefers it over the generic actuator. Effect: drives the 61 current SKIPs toward 0
(each becomes provable), unlocks compound/repeater/flow winnability proofs, and gives generated
fixtures a hard acceptance bar. ~10 lines in selfcheck.py; fixtures self-describe, no per-widget
harness code (consistent with the data-read philosophy).

---

## 5. WHAT "PLAYGROUND 100%" MEANS + ORDERED MILESTONES

### 5.1 The measurable definition (all six, simultaneously)

1. **Winnability:** selfcheck = 0 FAIL and 0 unproven fixtures (every fixture PASS, via generic
   actuator or its own `data-actuate`; SKIP count driven to 0).
2. **ISO:** PASS ALL over the full suite (~214 fixtures: 125 + 44 gap + 10 weird-DOM + 10 flows +
   ~25 mined), false-green = 0 AND false-red = 0 on the verdict-consistency oracle.
3. **Composed-pages lane:** all ~12 manifests pass with the bleed oracle (own value painted, zero
   cross-field bleed) — the emergent-locate blind spot closed.
4. **Flow lane:** all flow fixtures reach terminal state, all expectations painted, gate log shows
   zero gate bypasses and zero silent non-advances.
5. **Map lane:** every fixture tagged `map_critical` (consent, EEO, open-text, data-gap SKIP
   cases) passes through the REAL mapper (`run_maplane.py`, batch version of map_test.py) —
   closes the "ISO injects values" blind spot. Nightly, not per-commit (costs real LLM calls).
6. **Determinism:** 3 consecutive full runs produce the IDENTICAL passing set (not just count).

Plus the external closure test (§4.2): fresh-sweep mining yields zero novel uncovered cells.

Explicitly OUT of playground scope (honest §4.3 carryover): mapper quality at scale, VLM
liveness under load, anti-bot/WAF, cross-origin iframes, machine-load flakes — those live in the
sweep + audit lanes.

### 5.2 Milestones (ordered by payoff-per-effort; each gated before the next starts)

| M | Deliverable | Files touched | Gate |
|---|---|---|---|
| **M1** | 44 gap fixtures (§1.3, incl. the two live-caught sweep500b classes #43/#44) + 10 weird-DOM (§3) + `data-actuate` selfcheck extension + `coverage.json` ledger | `all_fixtures.json`, `selfcheck.py` (+~10 lines), new `coverage.json` | selfcheck 0 FAIL; ISO PASS ALL ~179; both oracles 0/0 |
| **M2** | Composed-pages lane: ~12 adversarial manifests + bleed oracle | `build_playground.py` (parameterize), `run_playground_iso.py` `--pages` lane, `pages/manifest_*.json` | all manifests pass; 0 bleed |
| **M3** | Flow lane: `flow_fixtures.json` (10 flows §2.3) + `run_flow_iso.py` + selfcheck flow branch (+ engine `advance()` primitive if absent) | new runner + fixtures; possibly `oa_planner.py` | all flows terminal + gates clean |
| **M4** | Map lane: tag `map_critical` fixtures; `run_maplane.py` batch | fixture tags + new ~60-line runner | map lane green (nightly) |
| **M5** | VLM miner: `fixture_miner.py` over sweep500 PNGs; ~25 mined fixtures landed via the §4.1 gates | new miner + fixture additions | every mined fixture selfcheck-PASS before scoring |
| **M6** | Green-bar target: one command runs selfcheck → ISO → pages → flows; 3× determinism check; closure test on next fresh sweep | thin driver script | §5.1 all six green + closure test 0 novel |

Sequencing rationale: M1 is pure fixture work — biggest coverage jump, zero runner risk. M2 closes
the one blind spot the handoff itself admits. M3 is the only item that may touch engine code
(advance primitive) — isolated behind its own lane. M4/M5 convert the remaining blind spots
(mapper injection, unknown-unknowns) into gated, repeatable checks. M6 makes "100%" a single
command instead of a claim.

### 5.3 Standing rules while executing

- New fixtures land via the §4.4 handoff loop: selfcheck first, trace_one until green, full ISO as
  the regression gate, compare passing SETS.
- Any engine change triggered by a new fixture: scope narrowly (per-field-type bypass), never
  broad edits to shared fill code; revert any patch family netting ≤ 0.
- Negative fixtures (honeypot, captcha, ranking) assert the engine's RESTRAINT — expected =
  BLANK/NEEDS_HUMAN; a fill there is a FAIL. These are the anti-false-green fixtures.
