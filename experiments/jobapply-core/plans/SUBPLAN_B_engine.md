# SUBPLAN B — Engine: VLM-first commits, zero string matching

Team B sub-plan. Question: WHY do false-greens and repeated failure patterns persist, and how does the
fill engine become VLM-first and fully generic (ZERO string matching)?

Evidence base: `runs/newats/sweep500/` — **464 run JSONs, 8,080 per-field ledger rows**
(DONE 6,387 / SKIP 1,597 / ESCALATE 96), `failure_clusters.json` (70 clusters, 122 bad fields),
plus a code audit of the full commit path. sweep500b sampled (5 early runs, all clean-FILLED — too
few to cluster yet; re-run the miner below when it lands).

Reproduce every number here:
```bash
cd experiments/jobapply-core/runs/newats && python3 - <<'EOF'
# cluster all ledger rows by (outcome, compressed trace signature) — see §1 method
EOF
```
(the miner is 30 lines: for each `sweep500/NNN.json`, for each `results[]` row, signature =
outcome + last-4 trace steps with payloads stripped at the first `:`; Counter + 2 example URLs.)

---

## 1. REPEATED-PATTERN MINING (measured, not theorized)

### The one-line answer to "why do we still get false-greens"

**Every DONE verdict but 37 rests on DOM state, and DOM state provably decouples from paint.**
Of 6,387 DONE rows: 3,922 verified via `verify-src:dom`, **2,428 had NO verify step at all**
(fast paths: `S_FILE_GLOBAL` 784, `native-identity-match` 267, `S0_GUARD already-correct`,
adapter read-backs), and only **37** were VLM-verified. The single pixels-based defense — the
end-of-run vision gate (`oa_singlepage.py:1039`) — covers only choice-kind rows, is downgrade-on-
BLANK only, and is wrapped in `contextlib.suppress(Exception)` with `llm=None`: when the VLM times
out or errors, the gate silently vanishes and every DOM-trusted green stands unchallenged. That is
the false-green machine: DOM-trust everywhere + a vision check that fails open.

And the repeated ESCALATEs are the same machine seen from the other side: the vision gate WORKING
(catching DOM-green/paint-blank commits) but having no repair path, so it burns completion instead.

### Top repeating patterns (by trace-signature count; P4b/P4c confirmed live in sweep500b)

**P1 — DOM-verified commit, vision says BLANK on screen: 59× (61% of all ESCALATEs).**
Signatures: `choice-label-anchor→vision-gate` 26×, `choice-dom-direct→vision-gate` 14×,
`S_VERIFY verdict→vision-gate` 9×, `native-identity fast-path-DONE→vision-gate` 6×,
`consent-check→vision-gate` 2×, `already-correct→vision-gate` 1×, `recommit→vision-gate` 1×.
The committer clicked, its own DOM/next-tick check said selected, and the final screenshot shows
nothing selected. Root cause (see §2): `_BTN_FIND` re-locates the field container **by label-text
substring** (`oa_cdp_action.py:667-678`) — on pages with same-stem sibling questions it binds the
wrong (or no) container, clicks there, and self-verifies *that* container; the real field stays
blank. When vision is alive this is an ESCALATE (completion loss); when vision is dead it is a
**false-green** — the two headline problems are ONE bug.
Examples: `jobs.lever.co/gridware/ffe216c9-…/apply` (018, "I'm local to the SF Bay Area"),
`jobs.ashbyhq.com/sierra/013d54ba-…/application` (105, consent pill),
`jobs.ashbyhq.com/airwallex/6e975468-…/application` (186 — "right to live and work" AND
"If No, do you need a work visa sponsorship" — the same-stem pair, both escalated).

**P2 — S_OTHER no-escape → ESCALATE: 31×** (was 45× pre-fixes; same class).
Signatures: `search-exhausted→S_OTHER→no-escape` 11×, `probe-residue-cleared→S_OTHER→no-escape` 9×,
`occupied-foreign-input→S_OTHER→no-escape` 7×, plus pick-fail leftovers (discord `Gender` filter
`'Non-bi' -> 0 opts` 2×, ramp start-date 1×, octoenergy German salary 1×). A field lands in
S_OTHER with no fill rung and dies silently.
Examples: `job-boards.greenhouse.io/twilio/jobs/8023557` (066, "source of your right to work"),
`jobs.ashbyhq.com/airwallex/330db6f4-…/application` (136, "relatives currently working").

**P3 — NOT-DISCOVERED: ~70 of the 122 missing-required/visually-unanswered fields** (from
`failure_clusters.json`): the completeness judge SEES the question but the fill loop never got a
row. Dominant shapes: Lever `cards[<uuid>]` custom-question groups (gridware linkedin 13×, fampay/
netomi "Select..." 4× each), Greenhouse work-auth/location below-fold (anthropic 4×, twilio 4×).
Discovery is Team A's lane, but the ENGINE owns the closing loop: a vision-flagged unanswered
region must trigger one scoped re-discover+fill pass instead of only flipping the verdict red.
Examples: `jobs.lever.co/gridware/ffe216c9-…/apply`, `job-boards.greenhouse.io/anthropic/jobs/5222180008`.

**P4 — Cross-field bleed guard fires, field dies: 86 rows** (`occupied-foreign-input->escalate`:
79 SKIP + 7 ESCALATE). The guard correctly detects a foreign value squatting in the control
(probe residue / tenant default / earlier bleed) but the escape hatch is death, not repair
(wipe → recommit → verify).
Examples: `jobs.ashbyhq.com/clipboard/14a8d657-…/application` (096, "authorized to work"),
`jobs.ashbyhq.com/clipboard/207a6c4e-…/application` (193).

**P4b — FR-1, false-red via duplicate discovery (VERDICT layer, confirmed live in sweep500b/004):**
`occupied-foreign-input` fired on **165 rows** in sweep500; **127 of them have a same-value DONE
sibling row in the same run** (e.g. two "Phone*" rows, 000–003.json — one control discovered twice
under alias labels; the second row reads the FIRST row's own fill as "foreign" text). Outcomes:
116 SKIP / 42 DONE-after-recovery / 7 ESCALATE; **7 rows landed in `missing_required` across 6
runs** = direct false-red verdict flips. Live proof: sweep500b/004 (anthropic greenhouse) — ledger
escalates "What is your motivation for applying to Anthropic?*" as occupied-foreign while the PNG
shows the same textarea ("Why Anthropic?") fully filled. Root: the bleed guard never asks "is the
occupying text MY OWN mapped value / was this backend_node_id already committed by an earlier row?"
before calling it foreign.

**P4c — FR-2, never-rendered fields scored as engine failures (VERDICT layer, sweep500b/004):**
of the 36 `no-escape->ESCALATE` rows, **14 carry `visual-choice:none` / `implausible-reject`
attempts** — the option search worked correctly on a field that has NO rendered box anywhere on
the page (004: two INTRINSIC_RADIO questions absent from the PNG — hidden/conditional GH section).
The engine behaved; the CLASSIFICATION is wrong: a field whose node never gets a rendered rect
should be `NOT_RENDERED` and excluded from scoreable (like DEAD), not an ESCALATE counted against
the engine. This also shrinks P2's real size: its true no-fill-path core is the remaining 22×.

**P5 — Verify-blind DONE fast paths: 2,428 rows (38% of DONE) with no verify step.**
Not a failure count — an EXPOSURE surface. `native-identity-match` (267) trusts
`select.value` assignment; `S_FILE_GLOBAL` (784) trusts a page-global filename chip
(`[:8]` prefix — the airbnb cover-letter-got-resume false-green lived here);
`already-correct` short-circuits trust `located:grouped` representative nodes that desync
(memory: grouped-locate desync). Every §7-doctrine false-green class (decoupled paint,
`rendered_present`, `value='on'`) lives in this population when the vision gate is dead.
Also: `twin-label-already-done->skip` 663× — mostly-correct dedup, but it keys on normalized
label text, so two DISTINCT same-label questions (two "LinkedIn profile" fields in repeated
sections) silently lose one.

---

## 2. STRING-MATCH AUDIT — every coded text comparison in the commit path

Legend — Risk: how it manufactures a wrong LOCATE/MATCH. Fix: **S** = structure identity
(backend_node_id / already-located card / geometry / ARIA state), **M** = meaning call (LLM/VLM),
**P** = protocol/structured-output cleanup. Exact **normalized full-string equality** used as a
deterministic fast path is identity, not pattern — kept, but its result must still be paint-verified.

### HIGH — wrong-field or wrong-option commits, or false-green enablers

| Where | What it matches | Risk | Replacement |
|---|---|---|---|
| `oa_cdp_action.py:667-678` `_BTN_FIND` | field CONTAINER by label stem `slice(0,40)` + `innerText.includes(stem)` | binds the wrong container on same-stem siblings (airwallex 186) → commit lands on the wrong field, self-verifies there → P1's 40 choice escalates / false-greens when vision dead | **S**: pass the ALREADY-LOCATED card node (S1 owns it, by backend_node_id) into the committer; delete the label re-search. No card → set-of-marks VLM (§3) |
| `oa_cdp_action.py:683` | chrome blocklist regex `/submit\|apply\|upload\|replace\|next\|continue/` + `t.length<=30` | localized tenants (octoenergy German forms in this sweep) sail through; legit long option labels (CC-305 clauses) are excluded | **S**: exclude `type=submit`/form-action buttons outside the card; length caps removed; residual ambiguity → **M** |
| `oa_cdp_action.py:697-706` | option by `norm2(text)===w`, word-boundary prefix regex, then **bidirectional substring** `t.includes(w)\|\|w.includes(t)` | substring both ways: want "No" ⊂ "I do NOt wish…" class of false-picks (the CC-305 bug this code already fought once) | keep exact identity; everything else → **M** (`pick_option` already exists) |
| `oa_cdp_action.py:1165-1166, 1179-1181` (portal listbox) | `nv in option \|\| option in nv` bidirectional substring | "no" ⊂ "norway"-class false-pick in windowed country lists | exact identity, else **M**; virtualized scroll keeps exact-only (correct as is at 1180) |
| `oa_complete.py:1463-1474, 1486-1492` `_committed_for` | vision-flag label vs ledger label by **token overlap ≥ half** | THE cross-label-echo false-green: a flag on field X cleared because similar-worded field Y was committed (mega4 root cause, still live in code) | **S+M**: resolve the flagged label to its on-page card rect (`_RECT_FOR_LABEL_JS` exists), match to the ledger row whose located node sits in that rect; ambiguity → one LLM "same field?" ask |
| `oa_singlepage.py:995` | conditional-field detection by English+French phrase regex (`^if so\|if yes\|…\|si oui`) | the HARD GATE keeps/drops required-field failures by phrase list — a German "falls ja" conditional false-fails the run; an unlisted English phrasing false-passes | **M**: mapper emits `is_conditional` per field in the ONE existing map call (schema addition, zero extra calls) |
| `oa_observe_act.py:727-747` `_SELFID_TOKENS` / `_CONSENT_TOKENS` | EEO/consent fields by English keyword lists | localized/renamed labels miss → blank-policy and consent-check misroute | **M**: same mapper schema addition — `is_eeo`, `is_consent` flags decided by meaning at map time |
| `oa_observe_act.py:638` | delta node is "an option" iff `len<=80` and no `?` | drops REAL long-clause options (disability/veteran clauses run >80 chars) → pick sees a truncated menu | **S**: role/containment (inside menu ancestor = option), text length never a criterion |

### MEDIUM — wrong picks under specific shapes, localization misses

| Where | What it matches | Risk | Replacement |
|---|---|---|---|
| `oa_observe_act.py:161` `_phantom_twin` | placeholder by `startswith("select ")/"choose "` | localized placeholder ("Auswählen") → phantom NOT skipped → stray overwrite returns | **S**: native-mirror twin is a DOM fact (same backend node / `role=combobox` + hidden mirror); decide on node identity |
| `oa_observe_act.py:429-450` `_identity_pick` | ≥4-char word-boundary prefix both ways + initials acronym | unique-hit discipline mitigates, but prefix ("San Francisco" saga) and acronym ("IT") still guess meaning from characters | keep exact-equality rung only; prefix/acronym hits demote to CANDIDATES passed through the existing memoized meaning-confirm (`_chosen_plausible`) |
| `oa_cdp_action.py:1496-1506, 1582-1590` | short-boolean word-boundary prefix (`^Yes\b` + option ≥ value+6 chars) | "Yes" matches "Yes, with an accommodation…" when want was plain Yes — right most of the time, silently wrong on 3-option consent variants | option lists here are ≤ dozens: **M** pick is cheap; keep exact rung |
| `oa_cdp_action.py:711-713 & 744, 803-804` `_active` | selected-state via class regex `(active\|selected\|checked)` | a theme using `is-on`/`highlighted` false-reds the next-tick verify → re-click toggles OFF a correct commit | ARIA state (`aria-checked/pressed/selected`, `data-state`) = **S**, keep; class regex demoted to hint; truth = §3 crop verify |
| `oa_cdp_action.py:776` `_GRID_SCALE_JS` rowMatch | row header vs label bidirectional `includes` + `len>8` | likert row mis-bind on nested/near-duplicate row texts | row = the card's OWN aria-labelledby target (**S**, id-ref not text); col label exact-only, else **M** |
| `oa_observe_act.py:1451`, `oa_cdp_core.py:255`, `oa_complete.py:1247` | uploaded-file chip by `basename[:8].lower()` prefix | 8-char collision across resume/cover on multi-upload forms (airbnb mega4/9 lineage) | **S**: card-scoped chip (already half-done at 1445) + full normalized basename; final truth = crop verify of the card |
| `oa_observe_act.py:1754, 2830` `_OTHER_ESCAPE_RE` | "other/specify/self-describe" by English word regex | German/renamed escape option → S_CASCADE never fires, revealed input stays blank | **S**: the reveal is OBSERVABLE — after commit, delta shows a new empty text input inside the card → cascade. No word list at all |
| `oa_brain.py:113-124` `_MULTI_LABEL_TOKENS` | multi-value fields by English token list | localized "Sprachen"/"Compétences" → MULTI missed → head-token-only commit | **M**: mapper already returns per-field cardinality — honor it; delete the list |
| `oa_brain.py:378` label-leak guard | dom_value vs label by `len>12` prefix startswith | long question whose true ANSWER echoes its opening words → false WRONG → revalue churn | **S**: reject only when the read-back NODE is the label/placeholder node (identity), not when texts resemble |
| `oa_complete.py:1129` `_section_has_entry` | section heading by first-word `includes` | title-based section detection (violates title-ignorant rule); renamed headings → repeater wrongly "filled"/"empty" | **S**: detect entries by structure (saved-card rows / Add-affordance siblings), heading text never consulted |

### LOW / protocol / acceptable

| Where | What | Verdict |
|---|---|---|
| `vision_verify.py:50,58`, `oa_brain.py:747-752` | `'"filled":true' in v` verdict-string parse | **P**: structured output (`output_format=` pydantic) — brittle but self-inflicted format, not DOM text |
| `oa_singlepage.py:1063` `_BLANK` set | VLM `rendered` free-text vs uppercase set | **P**: make the gate prompt return an enum `state: filled\|blank\|offscreen`; delete the set |
| `oa_observe_act.py:87` `_CATCH_ALL_WANTS`, `:2864` `_PRESENT_VALUES`, `oa_complete.py:1091` `+1` phone | classify OUR OWN mapper-emitted values | value-side (we control the vocabulary) — tolerable; cleaner: mapper emits `is_catch_all` flag with the value |
| `oa_observe_act.py:1917,1960,1986` date regexes | parse OUR canonical ISO value format | fine — format parsing of our own data, not matching |
| `oa_observe_act.py:1575-1577` self-label reject | full normalized equality label==chosen | identity, fine |
| `oa_cdp_action.py:1210` react-select `control` class | library-convention structure | fine (documented as convention, has non-matching fallback) |
| `committed_nodes` / `backend_node_id` maps, `_resolve_choice_target` geometry | DOM identity + geometry | the GOOD pattern — this is what "locate by structure" means |
| `runs/newats/triage_failures.py:23-38` semantic buckets | triage tooling keyword buckets | exempt (offline reporting), but don't let its buckets leak into engine decisions |

---

## 3. VLM-FIRST COMMIT/VERIFY DESIGN (the inversion)

Today: DOM commit → DOM read-back → VLM as last-resort aid + one fail-open end-of-run gate.
Inverted principle: **for any control whose painted state is not readable as plain text, the pick
and the verify are VISUAL; DOM is the fast path only where DOM *is* the paint.**

### Where DOM-only remains sufficient (no VLM spend)
- Native `<select>` / `<select multiple>`: `selectedOptions[].text` IS painted truth.
- Plain text / textarea / date inputs: `.value` IS painted truth (wipe-gate already covers).
- File inputs: card-scoped chip read (structural), crop verify only on doubt.
- Exact-normalized-identity option matches on native widgets.

### The visual choice/dropdown commit (replaces `_BTN_FIND` text-search + prefix pickers)
1. **Locate** the question card structurally (existing S1 — unchanged).
2. **Candidates** = option-shaped descendants of the card (intrinsic radios/checkboxes,
   `role=radio|checkbox|option|button`), or the opened menu's owned options (S3). Structure only.
3. **Fast path**: exactly one candidate whose text exact-normalized-equals the value → click it —
   but verify visually (step 5). No prefix, no substring, no acronym.
4. **Set-of-marks pick**: `pick_control_by_marks` (`oa_brain.py:551` — already built on
   browser-use `create_highlighted_screenshot`, marks = `backend_node_id`) over the candidates:
   "which marked control means <value> for question <label>?" → node. ONE VLM call.
5. **Trusted coordinate click**: `cdp_click_xy` at the chosen node's center (already the trusted
   primitive; keeps the React-hotspot lesson).
6. **Painted-delta verify**: crop the card rect (rect infra exists: `node_rect`,
   `_RECT_FOR_LABEL_JS`, `_crop_check`) BEFORE and AFTER the click; one VLM call on the after-crop
   (before-crop attached only when a default was pre-selected): "which option is selected now?"
   Meaning-match against value → CORRECT. BLANK/other → one recommit via the structural rung, then
   ESCALATE with the crop attached (HITL-ready evidence). Ledger: `verify-src:vlm-crop` + rendered text.
7. **Group batch**: one crop verify covers the WHOLE radio/checkbox group (it's one card) — and one
   end-of-card crop can verify several stacked pills at once; verify calls scale with cards, not options.

### Why this kills the §7 false-green classes
- **Decoupled paint** (hidden checkbox `.checked` vs button-driven paint): verify reads pixels of
  the card; the hidden input is never consulted.
- **`rendered_present`**: the question is "which option is SELECTED", never "is the label text
  present" — label-always-in-DOM stops mattering.
- **`value='on'`**: the VLM reports the painted selected LABEL; `input.value` is never read.
- **Grouped-locate desync**: the crop is of the structurally-located card rect; the representative
  node's `.value` is out of the loop entirely. Coordinate click + pixel verify = the pair no DOM
  desync can fool.
- **Vision-dead false-greens**: §4's liveness canary makes a dead VLM produce UNKNOWN (fail-closed
  for the VERDICT, not ESCALATE-spam: rows keep DOM outcome but the run is marked `vision_dead`,
  scored UNVERIFIED — never COMPLETE).

### Budget & latency (measured base: $0.002–0.003/app, VLM = gemini-3.1-flash-lite, low-detail)
- Calls: pick ≤1 + crop-verify ≤1 per choice CARD; typical form has 3–6 choice cards →
  +6–12 calls/app. Raise `GH_VERIFY_MAX_CALLS` 6 → 16 (env, per-page).
- Cost: flash-lite low-detail ≈ $0.0001–0.0003/call → **+$0.001–0.004/app; total ≤ $0.008** —
  ~60× under the $0.50/job budget.
- Latency: 0.8–2.5s/call → +5–25s/app worst case. Mitigations: fast path (3) skips the pick call;
  group-batched verify (7); `OA_VLM_TIMEOUT=12` with resilient fallback already in place.
- The end-of-run vision gate STAYS (defense in depth) but becomes fail-closed-to-UNVERIFIED and
  enum-structured (§2 LOW table), and now sees far fewer blanks because commits were verified at
  commit time.

---

## 4. VLM QUALITY HARNESS ("is the VLM actually good?" — today: unmeasured)

The entire inverted design leans on one unmeasured component. Build `tools/bench_vlm.py`:

**Corpus (auto-labeled, no hand annotation):**
- *Fixtures (ground truth free)*: run the playground with `OA_PAINTED_DUMP=1`; for each of the 125
  fixtures capture (a) pre-fill screenshot → expected BLANK, (b) post-fill screenshot + `data-read`
  painted truth → expected value. ≈250 cases, mechanically regenerated whenever fixtures change.
- *Live hard cases*: sweep500 PNGs where `vision_gate` records exist (59 blank-downgrades = true
  positives to re-confirm) + the mega4 audit's labeled false-green/false-red rows as adversarial
  negatives (e.g. the correctly-selected 'No' radio the VLM once judged match=False).

**Tasks measured** (each = the exact production prompt path through `resilient_vlm`):
1. blank-vs-filled on a field crop; 2. read-the-selected-option (pills/radios);
3. set-of-marks pick given (label, value); 4. offscreen detection; 5. full-form `vision_audit`.

**Metrics & gates:** accuracy per task, p50/p95 latency, timeout rate at load ~1 vs load ≥5 (the
documented sandbox/load trap), $/call. Ship gates: pick ≥95% and blank-detect ≥98% on fixture
cases (they are unambiguous), timeout rate <5% on a rested machine. A configured VLM that misses
the gate is not shippable as the primary — the bench is how we compare flash-lite vs alternatives
(one env var swap, rerun).

**Liveness in production (kill the silent-dead-vision class):**
- Sweep preflight: 30-case bench subset; timeout rate >20% or blank-detect fail → abort with
  VISION-DEAD instead of producing poisoned numbers.
- Per-run canary: one known-answer crop call at page start (a blank region → expect blank); canary
  fail or any VLM timeout during the run → `result.vision_dead=true`; `sweep500_score.py` scores
  such runs UNVERIFIED (excluded from the confidence numerator, never COMPLETE).
- Alert: the runner prints/logs `[VISION-DEAD]` prominently — a silent `visually_unanswered=0` is
  the exact past trap (dead vision reading as vision-confirmed).

---

## 5. FIX ORDER (each: root cause → narrow fix → repro fixture → regression gate)

Order = descending (count × false-green severity). Failed-first loop per §4.4 of the handoff:
fixture → `selfcheck.py <kind>` (winnable) → `trace_one.py <kind>` (~30s) → full ISO 125 comparing
PASSING SETS → live re-verify on the original URL.

**F1. P1 (59×) — visual choice commit + crop verify (§3), retiring `_BTN_FIND` label re-search.**
Root: label-text container binding + class-regex active-state; DOM self-verify of the wrong node.
Fix: committer receives the located card NODE (backend_node_id), candidates from it; set-of-marks
pick; `cdp_click_xy`; crop verify. Fixture: `sibling_stem_pills` — two pill groups whose labels
share a 40-char stem ("Do you require visa sponsorship?" / "If No, do you require visa
sponsorship in the future?"), `data-read` on each; plus existing decoupled-pill fixtures now
verified via crop path. Gate: ISO 125 + new fixtures, verdict-consistency 0/0; live re-run
airwallex 186 + gridware 018 + sierra 105.

**F2. P5 exposure — vision fail-closed + liveness canary (§4).**
Root: gate suppressed on any exception; vision-dead = silent pass. Fix: `vision_dead` flag,
UNVERIFIED scoring, enum-structured gate output, thread a real cheap LLM (not `llm=None`).
Fixture: bench preflight subset + a mocked-timeout unit test (gate must mark, not pass). Gate:
scorer shows UNVERIFIED bucket; a forced-timeout run can never score COMPLETE.

**F3. P2 (31×) — S_OTHER escape rung.**
Root: no fill path → silent ESCALATE. Fix (narrow): S_OTHER gets ONE set-of-marks action —
mark the card's interactive descendants, VLM proposes which to click/type for (label, value),
execute trusted, crop-verify; failure → NEEDS_HUMAN with the crop (routes to HITL per handoff §9.6),
never silent. Fixture: `no_path_widget` (contenteditable/custom composite from twilio 066 skeleton).
Gate: ISO + the 3 live URLs (twilio 066, airwallex 136, discord 499); S_OTHER ESCALATE count in a
20-URL smoke sweep drops ≥50%.

**F4. P4 + FR-1 (165 guard rows, 127 alias-dup, 7 verdict flips) — bleed guard: self-check first, repair second.**
Root: guard detects occupied text then gives up — and never asks whose text it is. Fix, in order
inside the guard: (a) **self-value check** — the occupied text meaning-matches THIS field's own
mapped value → already-correct, verify and DONE (kills sweep500b/004 "Why Anthropic?" false-red);
(b) **alias check** — this row's located `backend_node_id` is in `committed_nodes` (an earlier row
already committed this control) → alias SKIP, excluded from missing_required (identity, not label
text — kills the 127 dup-Phone rows); (c) genuinely foreign → wipe → recommit once → crop verify;
only then escalate. Fixture: `alias_duplicate_field` (one textarea discovered under two labels) +
`prefilled_foreign_default` (bamboohr UK-default skeleton + probe-residue variant). Gate: ISO +
sweep500b/004 + clipboard 096/193 live; occupied-foreign rows in missing_required → 0 without any
cross-field wipe regression (rich2 bleed fixtures stay green).

**F4b. FR-2 (14× measured proxy) — NOT_RENDERED verdict class.**
Root: verdict layer counts a never-painted field as an engine failure. Fix (narrow, S0_GUARD +
scorer): before classify, require a rendered box — `node_rect` non-empty/visible after
scroll-locate exhausts; none → outcome `NOT_RENDERED` (recheck once at end-of-run in case a
conditional reveal painted it later; a reveal whose premise answer we committed is S_CASCADE's job,
not a failure). Scorer excludes NOT_RENDERED from scoreable exactly like DEAD. Fixture:
`hidden_conditional_field` (display:none GH-style question). Gate: ISO + sweep500b/004; ESCALATE
count on box-less nodes → 0; no NOT_RENDERED leakage on fields that are merely below the fold
(scroll-locate fixtures stay green).

**F5. P3 (~70 fields) — vision-flagged → scoped re-discover loop (bridge to Team A).**
Root: fill loop never saw the field; judge sees it and only flips red. Fix (engine side): for each
`visually_unanswered` flag with no ledger row, resolve flag → card rect (`_RECT_FOR_LABEL_JS`),
run discovery scoped to that region, fill the new rows, re-verify — ONE extra pass, capped. Also
fixes the `_committed_for` token-overlap clearing (§2 HIGH) in the same touch: flag↔ledger matching
becomes rect-based identity. Fixture: `undiscovered_below_fold` + lever `cards[uuid]` group skeleton.
Gate: ISO + gridware/fampay/netomi/anthropic URLs; NOT-DISCOVERED cluster count in the next sweep.

Then: re-run the fresh 500-sweep (rested machine, unsandboxed, `OA_VLM_TIMEOUT=12`, preflight bench
green) + screenshot-audit swarm — the numbers only count from there.

---

## Rules honored
No static pattern matching (every fix above replaces text-vs-text with node identity, geometry, or
a meaning call); FILLED≠COMPLETE (crop verify + fail-closed vision + UNVERIFIED bucket); verify
real painted state (before/after crops, never `.checked`-of-hidden or label-presence); scope
narrowly (each fix is one rung/one committer, gated by the ISO passing-SET comparison); failed-first
fast loop (`trace_one.py` per fixture before any full run).
