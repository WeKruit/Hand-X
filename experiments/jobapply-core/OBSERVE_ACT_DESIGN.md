# `observe_act(field, value)` — Unified Form-Fill Primitive (Design)

> Status: DESIGN. No engine code is changed by this doc. The deliverable is this spec plus the
> offline-proof harness plan in §9. Every line/function reference below was checked against the tree on
> 2026-06-29; the previous draft cited primitives that DO NOT EXIST — those are corrected here and
> re-scoped as **new work**.

---

## GROUND-TRUTH CORRECTIONS (read first — the design rests on these, not on the earlier draft's fictions)

The six component drafts and their earlier line citations were audited against the actual code. The
following are FACTS in the tree; the design is built only on these:

- **There is NO delta machinery today.** `mark_visible`, `_READ_DELTA_JS`, `_MARK_VISIBLE_JS`,
  `_UNMARK_VISIBLE_JS`, `pick_option_visually` return **zero** hits across all `*.py` (verified by grep).
  The "reuse the existing seed" framing in every draft was false. **The overlay-cluster delta detector,
  the mark/click/read loop, and the option-coordinate reader are NET-NEW code** and are specified as
  such in §3. Their hardest sub-problem — *binding the clicked trigger to the cluster it spawned, with
  no `aria-controls`* — is solved in §3, not hand-waved.
- **What actually exists** (and is reused, with correct refs):
  - `pick_dropdown` (`ats_engine.py:340`) — the real shared picker. The caller *has already opened the
    widget and typed the filter*; it reads DOM options via a caller `read_dom_options(page)->[str]`, or
    falls back to `read_options_visually` (VLM transcribes the screen) when the DOM is empty/lagged;
    matches **LLM-only** via `wd_repeaters._llm_pick` + `_locate_idx` (exact-equality location only);
    commits via a caller `commit(idx, options)` or trusted `press_enter_trusted`; value-verifies via
    `visual_check(want=)` **but SKIPS verify when the match came from a fresh DOM read** (`used_vision==False`,
    line 449). This skip is a real wrong-commit hole the design must close (§6).
  - `FormField` (`ats_engine.py:49`): `name, label, type, source, required, options, option_values, value`.
    There is **no** `nature`/`free_text_ok` field — the classifier must add one (§4).
  - `FieldFill`/`map_fields` (`ats_engine.py:461,526`): the ONE structured map call. It emits `name/value/why`
    only — it does **not** classify nature today; §4 extends it.
  - `fill_with_ladder` (`ats_engine.py:863`, **not 964**): `adapter.fill -> read_back -> _vlm_filled rescue
    -> L2 retry -> _vlm_filled -> L3 escalate`. We swap only the body of `adapter.fill`.
  - `escalate` (`:626`, `max_steps=4`), `agent_fill_section` (`:669`, `max_steps` larger), `_FREEZE_FILLED_JS`
    (`:554`), `_wizard_agent_kit` (`:605`), `_vlm_filled` (`:850`), `_unfreeze` (`:575`).
  - `_FREEZE_FILLED_JS` (`:554`) chip-detection is **hard-wired to** `data-automation-id="multiSelectContainer"`,
    `data-uxi-widget-type="selectinput"`, `data-automation-id="selectedItem"` — the exact renameable Workday
    attributes the whole project exists to stop depending on. On any non-Workday tenant a committed chip
    reads as an empty input and the agent re-types it. §6 replaces this with a neutral `data-gh-done` marker.
  - `click_trusted` (`:271`) takes a **DOM element handle** and clicks its `getBoundingClientRect()` center;
    it returns False on a zero-box element. There is **no** "click at VLM x,y" primitive, and
    `read_options_visually` (`vision_verify.py:183`) returns **text strings only, no coordinates**. So
    "LLM-pick then trusted-click its on-screen coords" had no implementation; §3.4 defines the one real
    commit path (Enter-on-highlight) and §6 forbids the coordinate-click fiction.
  - `VLM_MAX_CALLS=6` (`vision_verify.py:31`) is reset by `reset_visual_cache()` **per wizard STEP**
    (`ats_engine.py:1196`), not per app. The cost model in §8 is re-derived from this.
  - `visual_check`'s `want=` prompt is deliberately **lenient** (accepts "a fuller official name OR a
    clearly equivalent option"). It is NOT a precise wrong-vs-right oracle; §6 cross-checks the known
    committed option string against `value` before trusting a fuzzy `matches=true`.
  - `salesforce_jordan_myexp.html` exists; **`hp_jordan_myexp.html` does NOT** (only `hp_jordan_step1/2.png`).
    The PayPal dump was overwritten. The salesforce DOM's 16 `role="option"` nodes are **not a closed
    dropdown**: `[0]` is `data-automation-id="selectedItem"` (a committed PILL — `aria-label="…, press
    delete to clear value"`), the other 15 are `REMOTE_SKILL menuItem` **multiselect** rows whose text is
    in `aria-label="PostgreSQL not checked"` (not "PostgreSQL"). All dropdowns in the capture are CLOSED
    (`aria-expanded="true"` count = 0). §9 rebuilds the proof plan around what these files can actually prove.
  - The existing offline harness `tools/offline_dom_harness.py` loads a saved DOM into **real headless
    Chromium via Playwright `set_content`** but **STRIPS all `<script>`** (`re.sub(r"<script…>","")`).
    So computed layout/`getBoundingClientRect` on *already-rendered* nodes IS available offline, but
    **click→React→menu-mount is NOT** (no JS). §9 uses this fact precisely.
  - The current `_listbox` (`ats_workday.py:620`) ALREADY detects the frozen shared portal and hands off
    to `pick_dropdown` (visual). So observe_act's Workday-select baseline is *stronger* than the drafts
    assumed; §9.6 measures against it honestly.

---

## 1. Problem + 底层逻辑

**The break.** Today the INTERACTION (physically filling a field) is implemented per-adapter (Workday,
Greenhouse, Lever, Ashby) × per `field.type`, and each handler leans on DOM **structure/labels** —
`aria-controls`/`aria-owns` to scope a listbox (`ats_workday._listbox/_opt_selector`),
`data-automation-id="promptOption|menuItem|selectedItem"` to read options, `label[for]` to map radio
options. These are **renameable**. A new tenant (PayPal-class: selects with no `aria-controls`) makes the
engine read the **wrong shared body portal**, which re-serves a frozen list from a *different* field →
a wrong/no commit. The ORCHESTRATION (`fill_with_ladder`) is already unified and correct; only the
per-field interaction is brittle.

**底层逻辑.** A human does not read `aria-controls`. They read the **visible label text** ("Degree",
"Why do you want to work here?"), look at the **widget's intrinsic kind** (a file button, a radio, a
text box), click it, and **watch what visibly happens** (a menu drops, suggestions appear, nothing
appears → it's a text box). The fix is to replace the per-archetype structural interaction with **ONE
primitive that perceives like a human**:

1. **Intrinsic HTML type first** — `input[type=file|radio|checkbox]`, `<select>`, `input[type=date]`,
   `role=radio|checkbox` are **W3C standards, not renameable**. A tenant can rename a class but cannot
   ship a checkbox that is not a checkbox without breaking its own a11y. These short-circuit to safe,
   non-destructive handlers (no blind click, no OS dialog).
2. **Otherwise classify by the visible LABEL's MEANING** (closed list / searchable / free-text / date),
   not by any DOM attribute.
3. **Act by behavior + visuals**: click → did a **field-scoped option overlay** appear (the DELTA)? →
   closed list. No overlay → type a few letters → did suggestions appear? → searchable; still nothing
   **and the label nature is positively free-text on an editable element** → text.
4. **Verify with the cheap value-aware VLM** and route on a 3-way verdict; bounded retries; agent only
   as a genuine last resort, carrying visuals + a freeze-ledger so it cannot loop or clobber.

This keeps `fill_with_ladder` byte-for-byte and swaps only the body of `adapter.fill`. It is the same
structure-free, human-like perception across all four adapters, so a renamed tenant does not break it —
because CSS physics (an option menu floats above the form, on top, near its trigger) is invariant where
attribute names are not.

---

## 2. The complete state machine (the spine)

`observe_act` is the body of `adapter.fill`. Signature mirrors `ATSAdapter.fill` (`ats_engine.py:122`):

```
observe_act(session, page, field: FormField, value: str, resume: str|None, *, llm=None) -> Outcome
Outcome ∈ { DONE, OTHER, SKIP, ESCALATE }   # mapping to the ladder in §6
```

Single threaded context (one global per-field budget, the anti-spin backstop the drafts lacked):

```
@dataclass
class Ctx:
  field; value; label; required
  nature: ""                       # set by CLASSIFY: CLOSED_LIST|SEARCH|FREE_TEXT|DATE|BOOLEAN|INTRINSIC_*
  free_text_ok: bool = False       # POSITIVE free-text license (§4); default False = never type blindly
  trigger_el = None                # the located editable/control element (DOM handle)
  trigger_box = None               # its rect at click time (for field-scoped delta binding, §3)
  commit_kind = ""                 # enter_on_highlight | trusted_click_option | cdp_upload | typed_text | toggle
  committed_text = ""              # the EXACT option string we committed (DOM-known), for verify cross-check (§6)
  queries_tried: list[str] = []    # variant dedupe (gap D/E); shared across front+verify re-searches
  # PER-AXIS caps:
  commit_tries=0; search_tries=0; revalue_tries=0; cascade_depth=0; scroll_reads=0
  # GLOBAL per-field backstops (NEW — close L1 of the state-machine critique):
  steps=0                          # every state entry increments; STEP_CAP terminates
  t0 = now()                       # wall-clock; FIELD_DEADLINE terminates
  vlm_used=0                       # per-FIELD VLM sub-budget (≤ FIELD_VLM_CAP), independent of page cap
```

### States (on-entry action → guarded transitions). Terminals: `DONE / OTHER / SKIP / ESCALATE`.

Every state first runs the **global guard**: `steps+=1; if steps>STEP_CAP or now()-t0>FIELD_DEADLINE:
→ TERMINATE`. This single guard bounds the free DOM/click/settle cycles that no per-axis cap touches.

- **S0_GUARD** — if `not value.strip()` → `SKIP` (mirrors the ladder's `"blank"` short-circuit). Else → S1.

- **S1_LOCATE** — find the control by **visible label text** (`_locate_by_label`, §3.1): rank visible
  `input/textarea/select/[contenteditable]/[role=radio|checkbox|combobox|textbox]` by token-overlap of
  their *rendered* label (associated `<label>` text, nearest preceding heading/text, or placeholder)
  against `field.label`. Records `trigger_el` + `trigger_box`. **Uniqueness flag**: if ≥2 controls tie
  on the top label score (label collision in repeaters, e.g. two "Degree"), mark `ambiguous=True` →
  forces a value-verify later (closes the same-label fast-path silent-fill, §6).
  - found → S2; none found & required → ESCALATE; none found & optional → SKIP.

- **S2_CLASSIFY** — set `nature` (§4). Intrinsic HTML type wins deterministically; else label-meaning via
  the (already-paid) map call's extended `nature`; `free_text_ok` is set **only** by §4's positive test.
  - `INTRINSIC_FILE` → S_FILE; `INTRINSIC_RADIO|CHECKBOX` → S_CHOICE; `INTRINSIC_SELECT` → S_NATIVE;
    `DATE` → S_DATE; `CLOSED_LIST` → S3_OPEN; `SEARCH` → S4_SEARCH; `FREE_TEXT` → S_TEXT_GUARD.

- **S_FILE** — CDP `upload_file(session,page,trigger_el,path)` (`ats_engine.py:213`). **No click ever**
  (a click opens the OS picker CDP cannot drive). Tolerates a hidden/zero-box `input[type=file]`
  (file inputs are routinely `display:none`). → S_VERIFY (presence check only); upload failed → ESCALATE.

- **S_CHOICE** — radio/checkbox: options are **already on screen**. Read every option in the group with
  its label text + element handle (§3.5 handles the **styled hidden-input** case: the real
  `input[type=radio]` is often `display:none` behind a styled `<label>`/SVG — `click_trusted` on a
  zero-box input returns False, so we click the **visible proxy** = the associated `<label>`/control,
  resolved by `label[for]=input.id` **or** nearest-ancestor label, then verify the input's `checked`).
  Group cardinality (§4.6): pick-one (radiogroup / yes-no) vs pick-many (checkbox set) vs consent. LLM-pick
  the matching option(s); for a yes/no map `value∈{yes,true,"I agree"}→check`. `click_trusted` the
  resolved clickable; **never** the field container ("the bar"). Drop `_FORBIDDEN_CLICK` matches.
  - chosen+clicked → S_VERIFY; no match & required → S_OTHER_GUARD; no match & optional → SKIP.

- **S_NATIVE** — native `<select>`: read `<option>` texts (in-DOM, free), `_llm_pick`, `page.select_option`.
  No delta machinery (native popup is OS-rendered, invisible to DOM/screenshot). → S_VERIFY; refused → ESCALATE.

- **S_DATE** — never delta-probed (a calendar grid would be mistaken for an option list). Two shapes (§4.5):
  *segmented* (`[role=spinbutton]` / multiple adjacent numeric inputs → Workday `_date`) reads the on-screen
  segment order, builds digits **segment-aware** (omit day digits if no Day segment — the proven
  "Year 6012" overflow fix), trusted-types into the first segment; *picker* types `MM/DD/YYYY`/locale form,
  and if typing is refused the day cells are themselves an option overlay → S3_OPEN on the grid. → S_VERIFY.

- **S3_OPEN** — universal "click and watch for a FIELD-SCOPED delta" (closed list):
  1. `observe_delta.mark_before(page)` (snapshot visible world, §3.2).
  2. `click_trusted(trigger_el)`.
  3. `observe_delta(... , trigger_box=ctx.trigger_box)` → `DeltaResult` (§3): the **overlay cluster bound
     to this trigger** (near it, in a new high-z/portal layer, appearing AFTER this click), VLM-confirmed
     to be an option list.
  - `kind==OPTION_CLUSTER` → S_CLOSED_LIST; `kind==NO_DELTA` → S4_SEARCH (DISAMBIGUATE — never assume text).

- **S_CLOSED_LIST** — `pick_dropdown`-style, generalized:
  - **Long list** (cluster ≥ `LIST_LONG=12`, or label is a known long taxonomy Country/State): **type-to-filter
    first** — type a few chars, settle, re-read the narrowed cluster, then pick (avoids scrolling 200 rows).
  - **Short list**: read all option texts + handles; `_llm_pick`; if no on-screen match, **scroll the
    overlay one page** (not the page) + re-read, bounded `SCROLL_CAP=2` (gap E, virtualized/off-screen).
  - **Commit** = trusted Enter on the widget's pre-highlighted match (the one real path), OR `click_trusted`
    the matched **element handle** (never VLM coords — there are none). Record `committed_text` = the exact
    matched option string. Drop `_FORBIDDEN_CLICK`.
  - committed → S_CASCADE; no match after scroll → S_OTHER_GUARD.

- **S4_SEARCH** — no-overlay-on-click: DISAMBIGUATE searchable-vs-text (the gap-B junction). Runs the
  **search loop** (§5): incremental type (first few letters), bounded settle, overlay-cluster + VLM-confirm,
  type-to-filter for long, variant retries (capped), Other/skip. It returns one of:
  - committed a suggestion → S_CASCADE;
  - all variants exhausted with no overlay **AND** `free_text_ok` (positive editable-text signal) → S_TEXT;
  - exhausted, nature SEARCH/CLOSED & required → S_OTHER_GUARD; optional → SKIP.
  - **It NEVER types the raw value into a no-overlay field unless `free_text_ok` is positively true** (§4.4).

- **S_TEXT_GUARD / S_TEXT** — free-text. `S_TEXT_GUARD` re-checks the positive editable-text signal (§4.4:
  `<textarea>`/`contenteditable`/`input[type=text]` with **no** `role=combobox`/`aria-autocomplete`); if
  the element is actually a combobox, route to S4_SEARCH instead (defends a map mis-tag). `S_TEXT`:
  trusted-type `value`; if the field opened an overlay at any point earlier, attempt trusted-Enter to
  commit (the geocomplete "City" case where blur discards). → S_VERIFY.

- **S_CASCADE** — did the commit reveal a SUB-field (e.g. "How did you hear" → "Referral" → employee name;
  "Other" → a specify box)? Detect via a fresh field-scoped delta / a newly-visible labeled input absent
  pre-commit. **Same-overlay guard**: if the revealed cluster equals the one we just picked from (a commit
  that didn't register, re-rendering the same menu) → do NOT recurse → S_VERIFY (EMPTY will re-commit).
  - new sub-field & `cascade_depth<CASCADE_CAP=2` & global budget left → recurse: child `Ctx` (depth+1,
    its value derived from the same profile), re-enter at S1; on child terminal return to parent S_VERIFY.
  - none, or cap/budget hit → S_VERIFY.

- **S_MULTI_LOOP** (NEW, for multi-value chips — Skills/Languages): when nature/label says the field takes
  **N values** (a comma-joined `value`, or a known multi-select label), after each successful commit
  **re-open and pick the next value** until all committed or `MULTI_CAP=8`. Verify is **set-aware** (§6:
  all wanted pills present). This closes the "committed only the first, marked DONE" silent drop. Entered
  from S_CLOSED_LIST/S4_SEARCH when `nature==MULTI`.

- **S_VERIFY** — value-aware VLM + 3-way routing (§6). Fast-path skip ONLY when **all** hold:
  `nature∈{CLOSED_LIST,INTRINSIC_RADIO,INTRINSIC_CHECKBOX}` **and** `committed_text` came from a **fresh
  DOM read** (not VLM) **and** `norm(committed_text)` ∈ the option set **and** `not ambiguous` (no label
  collision). Else value-verify. Routes: CORRECT→DONE; EMPTY→S_RECOMMIT; WRONG_VALUE→S_REVALUE;
  UNKNOWN→§6 routing (escalate for SEARCH/lagged; accept only for optional).

- **S_RECOMMIT** — EMPTY (click didn't register). **Re-issue the SAME commit on the resolved element/coords**
  — never a new query (a new query here is the gap-B mis-fill in reverse). For chips, first check
  "committed-but-reads-empty" via the §6 pill detector (a false-empty must not double-add). Bounded
  `COMMIT_CAP=2`; then TERMINATE. Uses a **fresh verify key** (`label:commit#n`) so the re-read is not a
  cached stale EMPTY (closes the verify-cache critique).

- **S_REVALUE** — WRONG_VALUE (committed a different, non-blank option). Clear the field (deselect the wrong
  pill/value — a defined `clear` action, since re-pick on a persistent-chip widget would ADD not replace),
  then re-search with the **next unused** variant (`queries_tried` dedupe). Bounded `REVALUE_CAP=2` and the
  shared `VARIANT_CAP=3`. No monotonic-improvement oracle exists, so the cap is the only guarantee; on
  exhaustion → TERMINATE (never DONE-on-second-wrong; see §6).

- **S_OTHER_GUARD → S_OTHER** — required, no match. `S_OTHER_GUARD` **forbids the Other-escape on
  demographic/screening/legal labels** (EEO race/gender/disability/veteran, "Authorized to work?",
  "18 or older?", consent) — for those a no-match → ESCALATE, never a silent substitution. For
  free-taxonomy fields only, `S_OTHER` LLM-picks an "Other"/"Prefer not to say"/"N/A" escape **and
  requires the escape to be a genuine member of the rendered options** (no fabrication); if selecting it
  reveals a specify box, fill it with `value` via a depth-bounded child S1. → S_VERIFY → OTHER terminal.
  No escape exists → ESCALATE (or SKIP if optional).

- **TERMINATE** (budget/step/deadline exhausted) — required & locatable → ESCALATE; optional → SKIP.

```
S0─►S1─►S2─┬─INTRINSIC_FILE──►S_FILE──────────────────────────────►S_VERIFY
           ├─RADIO/CHECKBOX──►S_CHOICE──────────────────────────►S_VERIFY
           ├─SELECT─────────►S_NATIVE──────────────────────────►S_VERIFY
           ├─DATE───────────►S_DATE───────────────────────────►S_VERIFY
           ├─CLOSED_LIST────►S3_OPEN─►(delta)──►S_CLOSED_LIST─┬►S_CASCADE/S_MULTI_LOOP►S_VERIFY
           │                     └─(no delta)─►S4_SEARCH       │
           ├─SEARCH─────────►S4_SEARCH─►(overlay)─►pick────────┘
           │                     ├─(no overlay & free_text_ok)─►S_TEXT_GUARD►S_TEXT►S_VERIFY
           │                     └─(no overlay & !free_text_ok & required)─►S_OTHER_GUARD
           └─FREE_TEXT──────►S_TEXT_GUARD─►S_TEXT─────────────►S_VERIFY
S_VERIFY ─┬─CORRECT───────────────────────────────────────────►DONE
          ├─EMPTY──────►S_RECOMMIT─(≤2)─►(committing state, fresh key)
          ├─WRONG──────►S_REVALUE──(≤2, dedup variant, clear-first)
          └─UNKNOWN────►(SEARCH/lagged→ESCALATE; optional→SKIP; else ESCALATE)
S_OTHER_GUARD ─(free-taxonomy)─►S_OTHER─►S_VERIFY─►OTHER   ;  (demographic/screening)─►ESCALATE
TERMINATE(step/deadline/budget) ─(required)►ESCALATE  ;  (optional)►SKIP
```

---

## 3. Overlay-cluster delta detection (React-proof) + field-scoped binding + VLM confirm

**This is NET-NEW code** (no `_READ_DELTA_JS` exists). It answers exactly: *"what option overlay did
clicking/typing THIS field just spawn?"* — robust to React re-render churn (gap C) **and** to the shared
body portal that re-serves another field's frozen options (the field-scoping the drafts dropped).

### 3.1 `_locate_by_label(page, label) -> {el, box, score, ambiguous}` (new)
Text-first: enumerate visible form controls, derive each one's *rendered* label (associated `<label>`
text → nearest preceding text/heading in the visual block → placeholder), rank by normalized token-overlap
with `field.label`. The **one** structural read in the system, and it is a *ranking hint* re-confirmed
visually downstream (gap H, accepted as human-like). Emits `ambiguous` on a top-score tie.

### 3.2 `mark_before(page)` (new) — `() => mark every visible laid-out element with data-gh-pre`.
Visible = `getBoundingClientRect` (w,h>2; intersects viewport) AND not `display:none|visibility:hidden|opacity:0`.
Called immediately before the click/keystroke. Always paired with `unmark(page)` in a `finally`.

### 3.3 `observe_delta(session, page, *, trigger_box, settle_ms, post_type=False) -> DeltaResult` (new)
```
DeltaResult{ kind: OPTION_CLUSTER|NO_DELTA|AMBIGUOUS ; options:[{text, el, in_view}] ; container_el ;
             scrollable: bool ; vlm_confirmed: bool ; reason }
```
A **delta node** = visible & laid-out & NOT `data-gh-pre`. The signal is whether delta nodes form an
**option cluster** bound to the trigger, requiring **ALL** of:

- **C0 — Field-scoped (closes the frozen-shared-portal silent wrong-fill).** The cluster's bounding box
  must be **geometrically adjacent to `trigger_box`**: the cluster's top edge within `±CLUSTER_GAP=24px`
  of the trigger's bottom (or its left within the trigger's x-band), i.e. the menu that dropped *from this
  control*. A stale shared portal still showing field A's options is positioned under field A, not field
  B → fails C0 → ignored. This is the temporal+geometric binding that replaces `aria-controls` scoping.
- **C1 — On-top, not just present (CSS-physics, not z-index theming).** For the cluster's center point,
  `document.elementFromPoint(cx,cy)` must resolve **into the cluster** (the node is actually rendered on
  top and hit-testable). This is the invariant a renamed tenant cannot defeat — an option list intercepts
  the pointer; a re-rendered in-flow form region does not float over its neighbors. (We do NOT key on
  `z-index>=10`: transform/opacity/will-change create stacking contexts with `zIndex==auto`, so a numeric
  threshold both false-negatives real menus and is a styling choice.)
- **C2 — Sibling run of short distinct leaves.** ≥2 (post-bare-click) delta **leaf-text** nodes (a node
  whose every child's trimmed text ≠ its own), text ≤ `LEAF_MAX=60` chars, distinct, sharing a parent.
  On a **post-type filter read** the threshold relaxes to ≥1 (a filtered single exact match is valid;
  flagged by `post_type=True`, never on the bare click).
- **C3 — Column geometry.** Sorted by y: consecutive `Δy∈[12,64]px`, x-centers within `±40px`. A vertical
  x-aligned column, not scattered re-render debris or a horizontal toolbar.

**Text extraction note (from the salesforce capture):** option text frequently lives in `aria-label`
(e.g. `aria-label="PostgreSQL not checked"`), not a direct text leaf. The reader takes `aria-label`
stripped of trailing state suffixes (` not checked`, `, press delete to clear value`, ` selected`) OR
the leaf text, whichever is the human option label. **Committed pills** (`aria-label` contains "press
delete to clear value", or `data-automation-id="selectedItem"`-class state) are **excluded** — they are
already-chosen values, not selectable options.

Pass C0∧C1∧C2∧C3 → `OPTION_CLUSTER`. Delta exists but fails C0/C1 (re-render churn, stale foreign portal,
in-flow region) → `NO_DELTA(reason)`. C2∧C3 pass but C1 borderline → `AMBIGUOUS` → VLM confirm.

### 3.4 VLM confirm (the human's eyes) — cheap, capped, and the wrong-list catch
Invoke `read_options_visually(session, key=…)` (`vision_verify.py:183`, low-detail ~one cheap call,
cached + `VLM_MAX_CALLS`-capped) when `kind==AMBIGUOUS`, OR before any commit on a cluster whose options
will be **clicked by element handle** (a wrong cluster → click into the wrong field). Logic:
- VLM returns `[]` (no menu seen) → downgrade to `NO_DELTA` (catches skeleton/toast/foreign-portal FPs).
- overlap(VLM texts, cluster texts) ≥ 0.5 → `vlm_confirmed=True`.
- VLM sees a list that **disagrees** with our cluster → the DOM grabbed the wrong nodes; **VLM wins** →
  drop element handles, set `commit_kind=enter_on_highlight` (Enter commits the widget's own highlight,
  immune to our node mis-selection). This is the real frozen-portal defense, reusing the exact handoff
  `pick_dropdown` already performs.
**Commit reality:** `read_options_visually` returns **text only, no coords**. Therefore the **primary
commit is trusted-Enter on the widget's highlight** (after typing the filter so the widget pre-highlights
the match) OR `click_trusted` on a **DOM element handle** when C0+C1+VLM agree the cluster is ours. There
is **no** "click at VLM x,y" path — that fiction is removed.

### 3.5 Settle (`_settle`, gaps D/G) — bounded poll, no fixed sleeps
Re-read delta every `POLL=120ms`; settle on 2 identical consecutive reads OR `settle_ms`. Static menu
`settle_ms=600`; async search `settle_ms=900`. **STABLE refinement to avoid early-settle on a streaming
virtualized list:** two identical reads count as settled **only if** the cluster's scroll container is
not actively growing (`scrollHeight` stable across the two reads); if it is still growing, keep polling to
the deadline. This is the only place we wait.

### 3.6 React-pollution / wrong-fill defenses, enumerated
| Pollution / risk | Neutralizer |
|---|---|
| Form section re-renders (hundreds of new in-flow nodes) | C1 (`elementFromPoint` not on top) + C0 (not adjacent as a floating menu) → NO_DELTA |
| Toast / inline validation (1–2 nodes) | C2 (<2 distinct short leaves) or C3 (not a Δy-column) |
| Skeleton/shimmer in a portal | C2 (blank/identical placeholders de-dupe) → else VLM "no menu" |
| **Stale SHARED body portal showing field A's options (the PayPal/frozen bug)** | **C0** (positioned under field A, not adjacent to field B's trigger) → ignored; if it slips, VLM-overlap<0.5 → VLM wins (§3.4) |
| Tooltip/hovercard (positioned, on-top, multi-line prose) | C3 (prose Δy>64 / single leaf >60 chars) → else VLM "not a list" |
| Virtualized list (8 of 250 rendered) | OPTION_CLUSTER `scrollable=True` → scroll-reread (S_CLOSED_LIST) or type-to-filter |

---

## 4. Field classification (intrinsic first; visible-label nature next) — resolving Gap B

Emits a `FieldClass{ intrinsic, nature, free_text_ok, date_shape, cardinality, options, required }`
struct **parallel to** `FormField` (FormField is NOT mutated). Intrinsic wins; nature only routes when no
intrinsic signal.

### 4.1 Tier-A — intrinsic (DOM standards, deterministic, $0, runs over the located region)
A pure `page.evaluate` predicate over the located region — **no** `data-automation-id`/`[for]`/`aria-labelledby`:
```
'file'      if input[type=file]
'select'    if a NATIVE <select>
'radio'     if input[type=radio] | role=radio | role=radiogroup
'checkbox'  if input[type=checkbox] | role=checkbox
'date'      if input[type=date] | a cluster of [role=spinbutton] | ≥2 adjacent short numeric inputs
''          otherwise → Tier-B
```
**Selector hygiene** (closes the throwing-selector critique): the predicate uses only valid CSS;
the earlier draft's `[data-*dateSection]?` is invalid and would throw, silently nulling ALL intrinsic
detection — it is removed. Date is confirmed structurally by `role=spinbutton`/multi-segment (unambiguous),
not round-tripped through the LLM.

### 4.2 Tier-B — nature, folded into the existing single map call (no new round-trip)
Extend `FieldFill` (`ats_engine.py:461`) with `nature` and `free_text_ok`, and `_MAP_SYSTEM` with the rubric:
```
class FieldFill(BaseModel):
    name: str; value: str; why: str = ""
    nature: Literal["closed_list","searchable","free_text","date","boolean"]
    free_text_ok: bool = False      # POSITIVE license to type the raw value (see 4.4)
    date_shape: Literal["segmented","picker",""] = ""
    cardinality: Literal["one","many"] = "one"
```
Rubric (decide from the LABEL meaning, never the machine name):
- `closed_list` — pick one from a SHORT fixed menu rendered in full on a single click (Degree, Gender,
  Employment type, State, "How did you hear about us"). If `options` is non-empty → coerced to closed_list
  (deterministic guard).
- `searchable` — look up ONE (or `many`) value by TYPING into a combobox over a LARGE vocabulary (School,
  University, Field of Study, Country, City, Location, Skill, Employer, Language). When unsure between
  searchable and closed_list, **prefer searchable** (degrades gracefully).
- `free_text` — prose or an arbitrary string with no controlled vocabulary (a "?" question, Why/Describe/
  Tell us/Additional, cover letter; URL/LinkedIn/GitHub/preferred name/salary/address line).
- `date` / `boolean` as named.

### 4.3 Deterministic post-conditions (code, not trusted to the model)
1. `options` non-empty → `nature` coerced to `closed_list` (or `boolean` if options ⊆ {yes,no}); `free_text_ok=False`.
2. Intrinsic file/radio/checkbox/select/date → nature discarded (intrinsic wins).
3. `cardinality=="many"` or a known multi-label (Skills/Languages/Technologies) → routes to S_MULTI_LOOP.

### 4.4 `free_text_ok` is a POSITIVE signal, not the absence of a delta (the real Gap-B close)
The draft's fatal flaw: "no delta → type the value" gated only on an LLM label guess. Here typing the raw
value is licensed **only when BOTH**:
- (LLM) `nature==free_text`, **AND**
- (DOM, at S_TEXT_GUARD) the element is positively a **plain text input**: `<textarea>` OR
  `[contenteditable]` OR `input[type=text|email|url|tel]` with **no** `role=combobox` and **no**
  `aria-autocomplete` and **no** option overlay ever observed for it.
If the LLM said free_text but the element is a combobox → route to S4_SEARCH, never type. If a searchable
field exhausts variants with no overlay and is NOT a positive text element → it is a genuine no-match →
S_OTHER_GUARD/SKIP, **never** typed. A misclassification therefore cannot silently type into a closed
widget; the worst case is a loud ESCALATE/SKIP, caught by verify regardless.

### 4.5 Date shapes — segmented vs picker (see S_DATE). ISO value end-to-end; read on-screen segment order.
### 4.6 Checkbox cardinality — pick-one (radiogroup/yes-no) vs pick-many (set) vs consent. A blanket
"check one box" on a multi-select set is wrong; S_CHOICE reads cardinality and either single-picks,
multi-picks (S_MULTI_LOOP), or, for an unrelated consent/decorative checkbox not matching the field
meaning, leaves it.

---

## 5. Typeahead search-loop (incremental, type-first for long, variants capped, scroll-reread, Other/skip)

The sub-machine for S4_SEARCH. **Precondition (closes the native-`<select>` mis-route):** the located
element must be a **text-editable input/contenteditable** (Tier-A would have caught a native `<select>`
and routed to S_NATIVE; a native OS popup never reaches here). Reuses `_settle`/`observe_delta`/
`read_options_visually`/`_llm_pick`/trusted-key family. Adds one primitive: `type_text_trusted(per_char=True)`
(CDP per-keystroke so the debounce/XHR fires) + `clear_field_trusted` (select-all+Backspace; never `fill('')`).

```
PROBE  : type first P=min(len(q),4) chars (per_char) of the current variant
SETTLE : _settle(900ms) → delta?  no → NO_DELTA_DECISION
READ   : delta LONG(≥12) → FILTER (type more, ≤FILTER_TYPES=3, then read narrowed) ; short → MATCH (read all)
MATCH  : _llm_pick(value, options) → pick → COMMIT ; NONE → (off-screen? SCROLL_REREAD≤2) → NEXT_VARIANT
COMMIT : trusted-Enter on highlight (or click matched element handle); record committed_text → VERIFY (§6)
NEXT_VARIANT : clear → next of queries (≤VARIANT_CAP=3, deduped) → PROBE ; exhausted → NO_DELTA_DECISION
NO_DELTA_DECISION :
   free_text_ok(positive, §4.4) → S_TEXT      # the ONLY no-delta→text transition
   else required → S_OTHER_GUARD ; optional → SKIP
```
Variant plan is built **once** by a cached LLM call on `(label,value)` (e.g. School → `["University of
California, Los Angeles","UCLA","California Los Angeles","Los Angeles"]`), most-canonical first. A variant
whose first SETTLE is empty is abandoned immediately (don't filter/scroll nothing) and advances.

**Server-cap guard (closes the truncated-set silent pick):** if a prefix returns a delta at the server's
result cap (`len==cap` and the wanted token is absent from the read set), **keep typing more specific
chars** before letting `_llm_pick` choose — never pick the closest of a truncated dozen that excludes the
target. **Geocomplete-commit guard:** for any field that opened an overlay, COMMIT is trusted-Enter, not
blur (blur discards typed city — the verified Greenhouse/Ashby case).

---

## 6. Verify + 3-way routing + bounded retries + agent-of-last-resort

### 6.1 The 3-way verdict (Gap F) — value-aware, with a wrong-list net the cheap path lacked today
```
Verdict ∈ {CORRECT, EMPTY, WRONG_VALUE, UNKNOWN}
```
`visual_check(session, field.label, want=value)` → `{filled,value,matches}` parsed by `_matches`/`_is_filled`:
- `matches==true` → CORRECT
- `filled==false` → EMPTY (commit never registered)
- `filled==true && matches==false` → WRONG_VALUE (committed the wrong option)
- capped/error/`filled:null` → UNKNOWN

**Fast-path skip — TIGHTENED (closes the lenient-VLM and same-label silent fills):** skip the VLM only when
`nature∈{CLOSED_LIST,radio,checkbox}` AND `committed_text` came from a **fresh DOM read** AND
`norm(committed_text)` is a member of the rendered option set AND `not ambiguous`. The skip then rests on a
**deterministic string identity** (we committed an exact known option), not on a fuzzy VLM. For radio/checkbox
we additionally read the input's `checked` (free DOM). Multi-value → **set-aware** verify: require every
wanted member visibly present as a pill (one `visual_check` with `want=` the comma-joined set; the prompt
already treats "all comma-separated items present" as match) **and** no extra committed pill beyond the
wanted set (read the pill container, free DOM) — closes the multiselect under/over-fill silent error.

**Lenient-VLM cross-check (closes "wrong UC campus passes"):** when `source==dom` and we know `committed_text`,
and the VLM says `matches=true`, also assert `norm(committed_text)` is `value`-equivalent by the LLM picker's
own canonical set membership (the same `_llm_pick` choice we made). A VLM that forgives "University of
California, Los Angeles" for a Berkeley value is overridden by the fact that our committed option string was
the LA campus → treated as WRONG_VALUE → S_REVALUE. Pure-`source==vlm` (no committed_text) stays VLM-judged.

### 6.2 Bounded retries (Gap D) — per-axis + GLOBAL backstop
| Budget | Cap | Bounds |
|---|---|---|
| `_settle` | 600/900ms | settle wait (G) |
| `VARIANT_CAP` | 3 | query variants (shared front+revalue, deduped) |
| `SCROLL_CAP` | 2 | off-screen/virtualized reread (E) |
| `COMMIT_CAP` | 2 | EMPTY re-commit, fresh verify key each time |
| `REVALUE_CAP` | 2 | WRONG_VALUE re-search, clear-first |
| `CASCADE_CAP` | 2 | sub-option recursion |
| `MULTI_CAP` | 8 | multi-value pick loop |
| **`STEP_CAP`** | **40** | **GLOBAL per-field state entries (covers free DOM/click cycles + cascade product)** |
| **`FIELD_DEADLINE`** | **15 s** | **GLOBAL per-field wall-clock** |
| **`FIELD_VLM_CAP`** | **3** | **per-FIELD VLM sub-budget (so one hard field can't starve the page)** |
| `VLM_MAX_CALLS` | 6/page (existing) | page VLM spend; over → UNKNOWN routing below |

Child cascade frames **inherit** `steps`, `t0`, and the page+field VLM counters (NOT reset) — the global
backstop bounds the cascade×verify cross-product the per-axis caps missed.

### 6.3 UNKNOWN routing (closes the "rubber-stamp every capped commit" silent fill)
The earlier draft routed UNKNOWN→DONE ("trust the commit"), which on a long form (page cap hit early) marks
the **majority** of remaining fields DONE unverified — exactly the lagged/searchable widgets the redesign
targets, since `committed` is the unreliable signal. Corrected:
- UNKNOWN on `nature∈{SEARCH, lagged closed via VLM source}` & **required** → **ESCALATE** (agent), never DONE.
- UNKNOWN on `nature∈{INTRINSIC_*, native select}` where the commit is mechanically reliable (CDP upload set
  files; native `select_option` returned; radio `checked` reads true via free DOM) → DONE (the truth is a
  free non-VLM read, not a guess).
- UNKNOWN & **optional** → SKIP (record "assumed-blank", NOT frozen — leaves it agent-repairable).
The per-FIELD VLM sub-budget (`FIELD_VLM_CAP=3`) plus `reset_visual_cache()` per wizard STEP means UNKNOWN
from page-cap exhaustion is rare and never silently passes a required searchable field.

### 6.4 Agent-of-last-resort — `escalate` with visuals + neutral freeze-ledger (no engine signature change)
`observe_act` never runs the agent itself; it returns `ESCALATE` and the unchanged `fill_with_ladder`
owns L2 retry then L3 `escalate` (`ats_engine.py:626`). The agent already carries the three anti-loop
artifacts (`use_vision="auto"`, `verify_field_visually` tool, `make_loop_verify_hook`, `available_file_paths`).
**Two required hardenings** (design-level; they sit in the freeze layer, not the ladder shell):
1. **Neutral freeze marker (closes the Workday-attribute-coupling critique).** `_FREEZE_FILLED_JS` today
   detects committed chips only via `data-automation-id="multiSelectContainer|selectedItem"` — useless on a
   renamed tenant, so a committed chip reads empty and the agent re-types it. Replace with: at every
   successful commit, `observe_act` **stamps the committed element (and its pill container) with a neutral
   `data-gh-done` attribute**; the freeze JS locks by `data-gh-done` (and the existing input-value/checked
   test), tenant-agnostic. The ledger of committed `(name,label,value)` seeds the agent prompt
   (`done_summary`) and is reset per page like the VLM cache.
2. **Agent success = read_back OR a final `_vlm_filled` (`ats_engine.py:850`)**, never read_back alone —
   read_back false-negatives on the very chip/listbox widgets that forced escalation (that is why
   `_vlm_filled` exists). The current ladder already chains `_vlm_filled`; the design keeps it.
3. Section-vs-field budget arena: a field covered by an `agent_fill_section` run is marked
   **agent-attempted** (consumes its single agent budget) whether or not it was filled, so no field spawns
   a second agent after a section pass (prevents the 480s/$7 multi-agent cluster).

### 6.5 Terminal → ladder mapping
| Terminal | `fill_with_ladder` consequence |
|---|---|
| DONE | `adapter.fill`→True; ladder runs `read_back` → L1 (or `_vlm_filled` rescue) |
| OTHER | →True; read_back confirms the escape value → L1 |
| SKIP | no-op; engine records "blank" |
| ESCALATE | →False → ladder L2 retry, then L3 `escalate` (visuals + neutral freeze) |

---

## 7. Gaps A–H — each resolved (not just listed)

| Gap | Resolution in this design |
|---|---|
| **A** click-destructive (radio/checkbox/file) | Intrinsic Tier-A (DOM standards) routes **before any click**: S_FILE = CDP `upload_file`, **no click** (no OS dialog); S_CHOICE LLM-picks the option and `click_trusted`s the **resolved clickable** (visible proxy `<label>` when the real input is `display:none`/zero-box — §3.5), never the bar. Demographic/screening get no silent Other (§S_OTHER_GUARD). |
| **B** "no delta == text" silent mis-fill | Killed twice: `free_text_ok` is a **POSITIVE** signal = LLM-`free_text` **AND** a DOM-confirmed plain-text editable element (`<textarea>`/contenteditable/text input with no combobox/autocomplete). A no-overlay searchable/closed field → S_OTHER_GUARD/SKIP, **never typed**. A map mis-tag is caught at S_TEXT_GUARD (combobox → re-route) and by verify. |
| **C** React re-render pollutes whole-page delta | `observe_delta` keeps only an **on-top (`elementFromPoint`), trigger-adjacent (C0), short-leaf column (C2/C3)** cluster, VLM-confirmed. In-flow re-renders fail C1/C0; toasts fail C2/C3. |
| **D** unbounded search loop | Per-axis caps **+** GLOBAL `STEP_CAP=40` / `FIELD_DEADLINE=15s` / `FIELD_VLM_CAP=3` that child cascades inherit (not reset). Free DOM/click cycles increment `steps`. |
| **E** off-screen/virtualized/long lists | Long → **type-to-filter first**; short no-match → bounded **scroll-the-overlay** reread (`SCROLL_CAP=2`) with re-`read_options_visually`. Server-cap guard prevents picking from a truncated set. |
| **F** verify empty vs wrong | 3-way `Verdict` via `_matches`/`_is_filled`; EMPTY→re-commit (fresh key), WRONG→re-search (clear-first), and a **lenient-VLM cross-check** against the known `committed_text` so a wrong-but-similar option is caught, not rubber-stamped. |
| **G** settle latency | Single bounded `_settle` (600/900ms, 2-stable + scroll-not-growing); fast-path skips VLM on deterministic DOM-identity commits. |
| **H** location/nature from visible label | `_locate_by_label` ranks by rendered label text; `nature` from label meaning. The lone structural read is a soft, re-confirmed ranking hint. Accepted as human-like. |

**Silent-wrong-fill risks the critiques surfaced — each now has a net:** frozen-shared-portal wrong-commit
(C0 binding + VLM-wins handoff); no-match searchable typed as text (positive `free_text_ok`); UNKNOWN-after-cap
rubber-stamp (UNKNOWN→ESCALATE for required searchable; per-field VLM budget); Other on demographic/legal
(S_OTHER_GUARD forbids); WRONG re-search oscillation (clear-first + capped + cross-check, terminate never
DONE-on-second-wrong); multi-value first-only drop (S_MULTI_LOOP + set-aware verify); custom date to S3
(S_DATE owns dates, never delta-probed); styled hidden radio (visible-proxy click + `checked` read); lenient
VLM passing wrong campus/title (committed_text cross-check); verify-cache stale EMPTY on re-commit (fresh key).

---

## 8. Cost + latency budget (re-derived from the real numbers)

**Cost atoms (verified):** delta read / mark / click / scroll / native select / read_back / `checked`
read = **$0** (DOM/CDP). One cheap VLM call (`visual_check` or `read_options_visually`, `gemini-3.1-flash-lite`
low-detail) ≈ **$0.0006**, **cached per (url|label[|want])**, **capped at 6/page** and **reset per wizard
STEP** (`ats_engine.py:1196`). `_llm_pick` (tiny text prompt) ≈ **$0.0002**. The one map call/app ≈ **$0.002**.

**Per-archetype (happy path):**
| Archetype | VLM | LLM-pick | Cost | Latency |
|---|---|---|---|---|
| text / file | 0 | 0 | $0 | 0.3–0.5 s |
| radio/checkbox | 0 (free `checked` read; VLM only if blank) | 1 | ~$0.0002 | ~0.8 s |
| native select | 0 | 1 | ~$0.0002 | ~0.5 s |
| closed select (DOM-identity commit) | 0 (skip per §6 fast path) | 1 | ~$0.0002 | ~1.5 s |
| closed select (VLM source / lagged) | 1 read + 1 verify | 1 | ~$0.0014 | ~2.5 s |
| searchable | 1 read + 1 verify | 1–2 | ~$0.0016 | ~3–4 s |
| worst hard field (capped by `FIELD_VLM_CAP=3`) | ≤3 | ≤3 | ~$0.0024 | ≤ ~15 s (deadline) |

**Per-page / per-app (the corrected envelope).** `VLM_MAX_CALLS=6` is **per STEP**, so vision spend scales
with steps, not a flat 24/app. With `FIELD_VLM_CAP=3`, **no single field can consume more than 3 of a step's
6 VLM calls** — so one hard school field cannot starve the rest of the step into UNKNOWN (the page/field-cap
contradiction in the drafts is resolved by the per-field sub-budget + UNKNOWN→ESCALATE, never→DONE). A
Workday step ≈ 25–40 fields, ~6–10 widgets: vision is hard-bounded at 6×$0.0006 = **$0.0036/step**. A typical
4-step wizard ≈ **$0.0144 vision + ~$0.002 picks + ~$0.002 map ≈ $0.018/app**, under the $0.02 target; a
deeper wizard scales linearly (~$0.0036/step) — the number is **per-step bounded**, stated honestly, not a
flat-24 fiction. Latency: 3–5 s/widget × ~8 widgets + 0.3 s × ~25 text ≈ 35–50 s fill/step, inside the
accepted 3–5 s/field. The `$0.0006/call` anchor is an **assumption to be measured** in §9 OA-cost before
promotion (no per-call cost is hard-coded in the tree today).

---

## 9. Migration + OFFLINE-proof plan + sequencing + rollback

### 9.1 Migration — keep `fill_with_ladder`, swap only `adapter.fill`'s body
`adapter.fill()` becomes a one-line delegation `return (await observe_act(...)).committed_bool`. `read_back`
and the `_vlm_filled`/L2/L3 ladder are untouched, so observe_act inherits the rescue net for free. Migration
unit = **(adapter × archetype)** behind a routing flag `GH_OBSERVE_ACT="wave0,wave1,…"` read by each
adapter's `fill()`; legacy handlers are **kept alongside** until an archetype is promoted, so rollback is a
flag flip (empty → 100% legacy, no redeploy).

| Wave | adapter × archetype | Why / gate |
|---|---|---|
| 0 | (all) `file` | Most dangerous (gap A OS-dialog); smallest surface (uploaded/not). Gate: offline locator + CDP-upload assert. |
| 1 | (all) `radio`/`checkbox` | Gap A; options pre-visible. Gate: offline visible-proxy resolution + `checked` assert. **Needs a real radio fixture (salesforce has 0 radio).** |
| 2 | Workday `single_select` (`_listbox`) | Core PayPal fix (C0 field-scoping). Gate: offline overlay-cluster + classify + C0 binding. **Baseline is the existing `_listbox`+`pick_dropdown` visual fallback — measure against it (§9.6).** |
| 3 | Workday `multi_select`+cascade | S_MULTI_LOOP + set-aware verify. Gate: offline multi-pick + cascade-cap asserts. |
| 4 | Greenhouse/Lever/Ashby `single_select` (react-select) | Proves the search branch on a different widget family. Gate: offline against a GH/Lever react-select DOM captured during an existing live run (no NEW account). |
| 5 | (all) `date` | Lowest payoff; last. Gate: offline segment-order assert. |

### 9.2 OFFLINE-proof — what the captured DOMs CAN and CANNOT prove (honest)
The offline harness extends the **real** `tools/offline_dom_harness.py`: Playwright headless Chromium,
`set_content(saved_html)`, **scripts stripped**. Consequences, faced head-on:
- **CAN prove offline (no JS needed; computed layout IS available on already-rendered nodes):**
  - **OA1 intrinsic-first / Tier-A** over `salesforce_jordan_myexp.html` (and the intel fixture): every
    `type=file/radio/checkbox`/`role=spinbutton`/native `<select>` → correct intrinsic branch; **grep the
    predicate JS asserts ZERO `data-automation-id`/`[for]`/`aria-controls`** (structural-rename resilience
    proven by absence). Salesforce has 1 file + 17 checkbox + 8 spinbutton to assert on; **0 radio → Wave 1
    needs a NEW captured radio DOM (a Workday EEO/screening step) before promotion.**
  - **OA4 label-classify (Tier-B)** with the **REAL** cheap LLM over the captured labels (frozen profile),
    snapshotted; "School→searchable, Degree→closed_list, Why…→free_text, Start Date→date+segmented,
    Authorized to work?→boolean/radio". This must use the real model (a stubbed LLM proves only plumbing,
    never that the model classifies correctly) — it is one cheap call, run in CI behind a key, snapshot-diffed.
  - **OA3 committed-pill / aria-label text extraction**: assert the reader **excludes** the
    `data-automation-id="selectedItem"` pill ("…press delete to clear value") and reads `REMOTE_SKILL`
    rows as "PostgreSQL" not "PostgreSQL not checked". (This is a static-DOM text-parsing assert — fully offline.)
  - **OA-override** (deterministic, no model): options-present⇒closed_list; intrinsic⇒nature-discarded;
    spinbutton⇒date-segmented; invalid-selector-throw regression (the predicate must not throw).
- **CANNOT prove offline (requires a live JS runtime — the click→React→menu-mount the harness strips):**
  - **OA2 overlay-cluster on a CLOSED capture**: the saved DOMs have all dropdowns CLOSED
    (`aria-expanded="true"`==0) and no script to open them. So C0/C1/C3 **geometry of an OPEN menu cannot be
    asserted against `salesforce_jordan_myexp.html`**. We do NOT pretend otherwise. Instead:
    **(a) self-hosted synthetic fixtures** under `wd_offline/fixtures/delta/*.html` — tiny hand-built pages
    **with real JS** (a click that mounts a portal menu, a type that renders react-select suggestions, a
    re-rendering form, a toast, a virtualized list, a stale-foreign-portal, a styled-hidden-radio), served
    via Playwright with scripts **intact** (offline, no tenant). Each asserts the exact `DeltaResult.kind`
    and that C0 rejects the foreign portal. This is where delta/settle/scroll/virtualize logic is proven.
    **(b)** The static salesforce/intel captures prove Tier-A, Tier-B, text-extraction, and the
    structural-rename-resilience invariant — the PayPal stand-in (no-aria, renameable structure) on the DOM
    side. **VLM is stubbed** in all offline runs (canned `read_options_visually`/`visual_check` verdicts) so
    routing (`_parse_str_list`/`_matches`/`_is_filled`, EMPTY/WRONG/UNKNOWN) is exercised at **$0**.
  - **OA-geometry calibration** (`CLUSTER_GAP`, `Δy`, `X_ALIGN`) needs **one** live render — done on the
    synthetic JS fixtures first, then confirmed on the FIRST live tenant; it is NOT claimed as static-DOM-provable.

### 9.3 The OA gate (merge-blocking)
A wave's routing flag may not flip until its OA asserts are green: Wave 0 (OA1-file), Wave 1 (OA1-radio on
a NEW captured radio DOM + visible-proxy), Wave 2 (OA2 on synthetic-JS + OA3 + OA4 + C0-foreign-portal),
Wave 3 (multi+cascade synthetic), Wave 4 (GH react-select synthetic), Wave 5 (date). The static
salesforce/intel asserts are the standing rename-resilience regression; the synthetic-JS fixtures are the
delta-logic regression. **No live tenant for a wave before its OA asserts are green.**

### 9.4 A/B (live, minimal — accounts are rate-limited)
1. **Shadow (no new accounts):** during an existing legacy live run, also run `observe_act` in **observe-only**
   on each field — it locates+classifies+delta-reads+"would-pick" and **logs**, **without committing**
   (fill-only preserved). Caveat (from the critique): for a CLOSED-select archetype the legacy commit tears
   down the portal, so shadow can replay delta only **before** the legacy commit, on the still-open widget,
   or not at all — shadow agreement is therefore meaningful for classify/locate/would-pick on every field,
   and for delta only on fields observe_act opens first. Honest scope, not "free A/B on everything".
2. **Per-archetype swap behind the flag:** one live run with `GH_OBSERVE_ACT=wave2`, compared on the existing
   `_print_report` tier table (L1/L2/vlm/FAIL) + cost vs the pre-flag baseline.
3. **Promotion:** offline OA green **and** ≥1 shadow-agreement run **and** the live swap matches-or-beats the
   **existing `_listbox`+visual-fallback** baseline (§9.6) on correct-commit rate and $/field with no new FAILs.

### 9.5 Sequencing & rollback
Wave 0/1 (intrinsic, offline-provable, kills gap A) → land OA harness + synthetic fixtures → Wave 2 (core
PayPal fix) → shadow → live swap → promote → Wave 3 → Wave 4 → Wave 5. Rollback during the risky window =
flag flip to legacy (config, no redeploy); after promotion (legacy deleted) = single-commit revert of that
wave's PR. Archetypes that touch pages 1–2 stability (date/multiselect) migrate **last**.

### 9.6 Honest baseline note
The existing `_listbox` already detects the frozen shared portal and hands off to `pick_dropdown` (visual).
Wave 2's value is **field-scoped C0 binding without `aria-controls`** (works when the owned-id is absent/renamed,
the true PayPal case) — measured as: on a synthetic no-`aria-controls` foreign-portal fixture, legacy
`_listbox` reads the wrong portal (or aborts), observe_act's C0 rejects it and commits the right field. If
Wave 2 shows **no** improvement on salesforce/HP (where `aria-controls` may still be present), that is
expected — the win is on the rename/absent-aria tenant, which is why a captured no-aria fixture is the gate.

---

## 10. Open questions for the human

1. **Missing fixtures.** `hp_jordan_myexp.html` is absent and the PayPal dump was overwritten. We need
   captures of (a) a no-`aria-controls` select tenant (PayPal-class) and (b) a Workday EEO/screening step
   **with radios** before Waves 1–2 can be gated. Can we capture these via `GH_DUMP` on the next live runs
   (1–2 accounts) before broad migration? Without them, Wave 1 (radio) and the C0 PayPal proof have no
   offline regression.
2. **Synthetic-JS fixtures vs live.** Is a self-hosted set of tiny real-JS pages (portal menu, react-select,
   stale-foreign-portal, virtualized, styled-hidden-radio) an acceptable stand-in for the
   delta-geometry/settle logic that the script-stripped captures cannot exercise? They are the only way to
   prove the delta detector offline; the alternative is more live runs.
3. **`free_text_ok` source of truth.** Confirm the map call may emit `nature`/`free_text_ok`/`cardinality`
   (extending `FieldFill`) — i.e. classification rides the existing single paid call, and the DOM-positive
   text test at S_TEXT_GUARD is the deterministic veto. Agreed?
4. **Neutral freeze marker.** Replacing `_FREEZE_FILLED_JS`'s `data-automation-id` chip detection with a
   `data-gh-done` stamp set at commit time changes the freeze contract for the L3 agent across ALL adapters.
   OK to land that in Wave 0 (it's tenant-agnostic and strictly safer), gated by the existing agent tests?
5. **Demographic/screening allow-list.** S_OTHER_GUARD must know which labels are EEO/legal/consent (no
   silent Other). Is a curated label-matcher acceptable, or should the map call tag `sensitive: bool`?
6. **Per-call VLM cost.** The $0.0006 anchor is unmeasured in-tree. Should OA-cost measure
   `gemini-3.1-flash-lite` low-detail per-call cost in CI and assert the per-step ≤6 / per-app ≤$0.02
   envelope before promotion?
7. **Multi-value detection.** Is `cardinality` from the label reliable enough to enter S_MULTI_LOOP, or do
   we also need a DOM signal (a pill container present) to confirm a field is multi-value before looping?

---

## 11. REUSE vs BUILD — `observe_act` is a deterministic orchestrator over browser-use's OWN primitives (research, 3 read-only agents over `browser_use/`)

KEY FINDING: the PERCEPTION + ACTION layers this doc drafted from scratch **already exist** in the vendored browser-use library, and its interactivity detection is **already structure-agnostic** (the exact "don't rely on labels" goal). The EXPENSIVE part of browser-use is its **AGENT** (the vision loop we avoid); its **PRIMITIVES** (`DomService` + `tools` + `python_highlights`) are cheap, trusted-CDP, and tenant-agnostic. `observe_act` must **COMPOSE** them deterministically — NOT reinvent `_READ_DELTA_JS` / `mark_visible` / `pick_option_visually`.

### REUSE (already in `browser_use/`, delete the drafted from-scratch versions)
| Drafted as net-new | Replaced by browser-use | file:line |
|---|---|---|
| `mark_visible` / `_READ_DELTA_JS` (THE DELTA) | serializer `previous_cached_state` diff → `is_new` (`*[id]`) | `dom/serializer/serializer.py:617-727` |
| element coords + visibility + "is it clickable" | `EnhancedDOMTreeNode`: backend_node_id + absolute bounds + is_visible + **structure-AGNOSTIC** interactivity (event-listeners / cursor:pointer / role / a11y props — NOT class/data-\*) | `dom/service.py`, `dom/serializer/clickable_elements.py:4-247` |
| read the option list | `dropdown_options` — native `<select>` + ARIA combobox + `role=option/menuitem` + **custom/“Oracle Workday” widgets**, child-depth-4 | `tools/service.py:1555` |
| select an option | `select_dropdown` — case-insensitive match, multi-strategy commit (value+events / aria-selected / custom-click) | `tools/service.py:1604` |
| `pick_option_visually` trusted-click-at-coords | `_click_by_index` / `_click_by_coordinate` — **TRUSTED CDP** dispatchMouseEvent, scroll-into-view, **occlusion via `elementFromPoint` + reroute-to-topmost** | `tools/service.py:540/584`, `watchdogs/default_action_watchdog.py:1061` |
| trusted Enter / ArrowDown / char-type | `input` (char-by-char trusted keys, React-aware clear+events) + `send_keys` | `tools/service.py:658/1385` |
| scroll dropdown for off-screen / virtualized | `scroll` (page + element/wheel) | `tools/service.py:1280` |
| set-of-marks "see option N → click element N" bridge | `create_highlighted_screenshot_async` + `selector_map[backend_node_id] → node` (**code EXISTS**, just not wired into the agent loop) | `browser/python_highlights.py:502`, `serializer.py:713` |

### BUILD (the genuine net-new — a thin brain + the orchestrator)
1. **Field-nature CLASSIFY** (closed-list / free-text / date / boolean / multi) from the VISIBLE label meaning + value — resolves Gap B. browser-use has **no** nature classifier.
2. **Typeahead SEARCH-LOOP** — type partial → settle → re-read delta → variant retry (UCLA vs full name) → Other/skip. browser-use reads+selects but has **no** search/filter loop.
3. **Value-aware VLM VERIFY + 3-way routing** (correct / empty=re-commit / wrong=re-search). browser-use has **no** deterministic vision read-back (only the agent loop + a post-input value compare); the highlighted-screenshot + cheap VLM glue exists, just compose it.
4. **“wait for delta to SETTLE”** gate (browser-use uses fixed 0.3s/0.4s delays — no settle primitive).
5. The **deterministic `observe_act` orchestrator** (the §2 state machine) composing 1–4 + the reused primitives, **without** the browser-use agent loop.

### Net effect
~**70% of the drafted from-scratch perception/action code disappears** (delta, coords, visibility, dropdown read/select, trusted click/type/scroll, set-of-marks — all reused). `observe_act` becomes a deterministic state machine calling browser-use's `DomService` + `tools`, driven by **1 cheap classify LLM + 1 cheap VLM verify**, skipping the expensive agent. Smaller, more robust, and **tenant-agnostic by construction** (browser-use's interactivity detection already is). The earlier §9 migration "Wave 1 = build the perception primitives" is REPLACED by "Wave 1 = thin adapters over `DomService`/`tools` + the classify/search/verify brain."

### Caveats the research flagged (don't over-assume)
- browser-use's `dropdown_options` ARIA path keys on `aria-controls`; the **no-aria-controls / virtualized** PayPal case still needs validation (its custom-widget + child-depth-4 fallback may or may not catch it — must test on a captured PayPal-class DOM).
- visibility filter uses bounds+scroll, **not** z-index; the **click** layer does do `elementFromPoint` occlusion — so commit-time on-top is covered, read-time isn't (a hidden-but-bounded stale option could be listed → the VLM verify is the backstop).
- the highlighted-screenshot is **not** wired into any loop today — composing it for the deterministic verify is part of BUILD #3.
