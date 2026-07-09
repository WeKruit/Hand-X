# CONVERGED MAIN PLAN — Generic Fill to 100%
### One spine, three sub-plans: [A: Playground-100%](SUBPLAN_A_playground.md) · [B: VLM-First Engine](SUBPLAN_B_engine.md) · [C: Product Interface](SUBPLAN_C_product.md)

Date: 2026-07-09. Evidence base: 464 old-sweep ledgers (8,080 field rows) mined; fresh 500-URL sweep
streaming now (`runs/newats/sweep500b/`); live specimens cross-checked ledger-vs-PNG mid-sweep.

---

## The three root discoveries that set the plan's shape

1. **The verify layer, not the fill layer, is the bottleneck (Team B).** Of 6,387 DONE field rows, only
   **37 were VLM-verified**; 2,428 had NO verify step; the one pixel gate runs `llm=None` and **fails
   OPEN** under `contextlib.suppress`. Dead vision = unchallenged greens. Separately, **one string-match
   bug (`_BTN_FIND` label-substring re-locate) explains 61% of all ESCALATEs** — and the same bug
   produces false-greens when vision is dead. 8 HIGH + 10 MEDIUM string-match violations remain, each
   with file:line and a structure/meaning replacement.
2. **The combination space is finite and 44 cells are uncovered (Team A).** 6 dimensions (Widget ×
   Label-binding × Option-coupling × Page-structure × Language × Dynamic), machine-readable
   `coverage.json`, external closure test = "mining a FRESH sweep's screenshots yields ZERO novel
   cells." Multi-page/Workday mimicry needs no server: flow fixtures are SPA step-swaps.
3. **The product control plane is ~80% built (Team C).** Claim/cancel, HITL open-question pipeline,
   CDP browser handoff, and the `job_fill_runs` ledger schema all EXIST. The real gaps: observe_act
   isn't what the worker runs; no pause verb; no resume; no user-facing usage; HITL answers don't
   persist; the login-cookie profile gets wiped. Chrome 136+ blocks CDP on the default profile → the
   cookie story is a persistent per-user LOGIN profile, not the user's daily browser.

## Converged phase spine (each phase independently gated; A/B/C workstreams interleave)

**Phase 0 — now, continuous:** streaming sweep + `live_watch.py` event wakes (cluster ≥3 / 40-chunk
audit / done) — audit overlaps sweep, no batch-at-end. No engine changes mid-population.

**Phase 1 — Trustworthy verdict (B-F2, B-F4, B-F4b + A's verdict fixtures). Highest leverage, lowest
blast radius; all verdict-layer:**
- Vision **fail-closed** + per-run canary: vision-dead ⇒ `UNVERIFIED`, never COMPLETE (kills the
  unchallenged-green mechanism).
- Bleed-guard self-value + alias-node check (same `backend_node_id` / same mapped value ⇒ not
  "foreign"), then repair instead of kill: un-false-reds 127 alias rows + 7 direct flips.
- `NOT_RENDERED` verdict class (node never had a rendered box ⇒ excluded from scoreable like DEAD): 14×.
- Fixtures first (TDD): `alias_label_duplicate_field`, `hidden_required_section`, `prefilled_foreign_default`.
- Gate: selfcheck PASS on new fixtures; ISO passing-SET unchanged; the two live sweep500b specimens
  (004 anthropic) reclassify correctly.

**Phase 2 — VLM-first commit (B-F1, B-F3, B-F5 + A-M1 fixtures):**
- Retire `_BTN_FIND` (the 59× / 61% cluster): structural candidates → set-of-marks VLM pick → trusted
  coordinate click → before/after **card-crop** verify. DOM-only stays for native selects/text/date.
- S_OTHER escape rung (set-of-marks or NEEDS_HUMAN — no more no-escape→ESCALATE, 31×).
- Vision-flag → scoped re-discover loop (~70 undiscovered fields; replaces `_committed_for`
  token-overlap with rect identity).
- Cost ceiling: +$0.001–0.004/app (≤$0.008 total, 60× under budget).
- Gate: reproducing fixtures green; ISO passing-SET compare; live spot-checks on the original failing URLs.

**Phase 3 — Playground to 100% (A-M1…M6):**
- M1: 54 gap+weird fixtures (incl. B's reproducers) → M2 composed pages + bleed oracle → M3 flow lane
  (10 wizards; forges the engine's `advance()` primitive Workday needs) → M4 real-mapper nightly lane →
  M5 VLM fixture miner (novelty-gated, selfcheck-PASS mandatory; `data-actuate` drives 61 SKIPs → 0) →
  M6 one-command green bar + **closure test** on a fresh sweep.
- "100%" = 6 simultaneous conditions (selfcheck 0 FAIL/0 unproven; ISO ALL + both-oracle zeros;
  composed 0 bleed; flows clean gates; map-lane green; 3× identical passing SET).

**Phase 4 — Product wiring (C-M1…M6; M1 can start in parallel with Phase 1, Hand-X-only):**
- M1 `run_one()` engine-as-callable + ship the per-field ledger into `job_fill_runs` → M2 pause/resume
  (control check per FIELD boundary; checkpoint = ledger; resume = idempotent rerun, painted-state
  self-skip) → M3 HITL e2e (existing openQuestion pipeline + screenshot crop; answers persist to
  qa-bank + session profile) → M4 usage API/UI → M5 persistent login-profile browser (localhost-bound
  CDP, cookies never leave machine, survives pool wipes) → M6 cloud secrets bootstrap (lease-scoped, stdin).

**Scoring discipline (always):** numbers only from a rested machine (load <3) with green VLM preflight +
streaming screenshot-audit. FILLED ≠ COMPLETE. The metric change (NOT_RENDERED excluded, UNVERIFIED
introduced) lands in Phase 1 — after that, confidence is measured against an honest denominator.

## Cross-team dependency spine
- C-M1 (callable + ledger) ← foundation for product AND for production self-improve feed. No deps.
- B fixes gate on A's reproducing fixtures (fixture-first). A's miner consumes sweep PNGs; B's VLM
  bench shares the same auto-labeled fixture screenshots (one harness, two consumers).
- A's flow lane produces `advance()` → unblocks Workday e2e → C's multi-page product lane.
- Phase 1+2 MUST land before the next scoring sweep; Phase 3/4 proceed in parallel after.

## LIVE VALIDATION — streaming-audit chunk 1 (added mid-sweep, 2026-07-09)

43 COMPLETE runs audited in 101s while the sweep kept running (the new overlapped pipeline):
**39 real / 4 false-green (9.3%)**. The 4 false-greens land EXACTLY on plan targets and add one new class:

1. **Unsolved CAPTCHA / stuck overlay occluding the form, still marked COMPLETE** (gridware 018, ro 026,
   mytos 023 — 3 of 4). Validates **Phase-1 F2 (vision fail-closed)**: the end-of-run pixel gate runs
   `llm=None` and fails open, so nothing SAW the overlay. Fail-closed + canary would have scored all
   three UNVERIFIED/NEEDS_HUMAN. (The engine even logged `blocker: captcha` — the verdict layer ignored
   its own blocker flag: additional trivial gate.)
2. **NEW CLASS — VLM verify at the wrong MOMENT** (artie 045 Location): trace shows
   `filter-lost-chosen → fall through` (option never committed) yet `verify-src:vlm → verdict:CORRECT` —
   the VLM verified typed-but-uncommitted text; the widget wiped it on blur; final PNG shows the
   placeholder. **Verify must observe the SETTLED painted state (after blur + menu close), not the
   mid-interaction state.** Amends B-F1's crop-verify spec: settle-then-verify + end-of-run gate remains
   the backstop. Fixture: `geocomplete_wipe_on_blur` (type→menu→lose→blur-wipe).

Streaming doctrine benefit demonstrated: defects known ~4 hours before the sweep finishes; fixes and
fixtures can be prepared before the retry pass.

**Cluster wake #1 (62/500 swept): `verify-src:dom → CORRECT → vision-gate:blank-on-screen` ×6.**
Commits landed on a DOM control that does NOT drive paint (`aria-core-commit`/`native-identity-match`
set the hidden select; DOM read-back reads the SAME dead control → CORRECT; the vision gate — alive on
these runs — caught the blank and honestly ESCALATEd). 4 of 6 are ONE family: classic
`boards.greenhouse.io` education-section comboboxes (School*/Degree*/Discipline*/End-month*, anduril
062); plus airtable work-auth combobox (061), netomi Lever timezone native-select (024), ro pronouns
checkbox (026). Direct live proof of **P1/F1's thesis**: commit must be the USER-visible action + verify
the SETTLED painted state; DOM-echo verify of the committed control is circular. New fixture:
`gh_classic_education_selects` (hidden `<select>` + custom rendered widget; commit-on-hidden must be
caught). Also proves F2 again — these 6 are honest ESCALATEs only BECAUSE vision was alive; under load
the same rows ship as false-greens.

**Streaming-audit chunk 2 (40 runs, 87s): 30 real / 10 flagged — decomposed honestly:**
3 TRUE fill false-greens: 1password 087 (Location 'Start typing…' + work-auth pill both-grey — engine
claimed both), arq 101 (**cross-field bleed PAINTED**: visa-support question contains
'New York City, New York, United States' — first bleed specimen visible ON SCREEN, upgrades FR-1/N1
from false-red to false-GREEN producer), artie 102 (Location placeholder — geocomplete-wipe now 3×:
045/102/087; the highest-frequency fill bug of this sweep). 3 CAPTCHA-blocked runs marked COMPLETE
(073/074/076 drag-puzzles) — verdict must be NEEDS_HUMAN. 4 cookie-banner rows: auditors verified the
fills ARE real (over-strict audit rule on my part) — real gap = **banner dismissal** (a banner can
block submit; dismiss before verify/finish; fixture `cookie_banner_blocks_submit`). Also arq's Submit
was DISABLED while engine said COMPLETE — completeness should check the submit control's enabled state
(free structural signal; fixture `submit_disabled_incomplete`).

Combined streaming tally at ~160/500 (3 chunks, 123 audited): **108 truly complete (87.8% of
claimed-COMPLETE)**. Deductions decompose into exactly two levers:
- **9 captcha/stuck-overlay runs marked COMPLETE (7.3% of audited)** — THE dominant false-green source,
  bigger than all fill bugs combined; one Phase-1 verdict fix (honor the engine's own `blocker` flag +
  overlay detection → NEEDS_HUMAN) recovers all 9.
- **6 true fill false-greens**: Location-geocomplete placeholder ×4 (artie 045/102/157 + 1password 087 —
  top fill bug; = the verify-timing class), consent-pill both-grey ×1 (airwallex 149 AI-Policy; also
  Middle Name='+1' country-code bleed painted + submit disabled), cross-field bleed painted ×1 (arq 101).
Chunk-3 audit prompt refined (cookie banners noted, not auto-failed) — chunk-2's 4 banner rows recounted
as real. Streaming audits: 3 chunks × ~90s each, zero added wall-clock to the sweep.

**Cluster wake #2 (67/500): `S_OTHER no-escape→ESCALATE` 2→7.** Decomposition:
anthropic ×2 = FR-2 (known). flexport ×2 + twilio ×2 = **duplicate-discovery pairs** (one control
discovered as combobox AND text row; both escalate — failures double-counted; FR-1's
dedup-by-backend_node_id also fixes the denominator). Plus TWO NEW bugs for B's fix order:
**(N1) S4_SEARCH cross-field bleed** — twilio Location value='New York' but the search TYPED
'New York University' (another field's answer used as filter text); fixture `search_filter_bleed`.
**(N2) found-but-not-committed** — `search 'Other' → 1 opts > search-exhausted`: the option was found
and never clicked; fixture `search_single_option_commit`. Also noted: airtable 061 ends
`ledger-reconciled:value-on-screen` (value IS painted post-escalate) — scoring must count
reconciled rows as filled, else self-inflicted false-reds.

**Cluster wake #3 (109/500): `scroll-locate×5:miss → no-control` — the BIGGEST lost-field family.**
~28 rows across ~15 runs; ~18 are **Ashby/Lever EEO-demographic radios** (`systemfield_eeoc_race`,
Veteran, Disability, gender/ethnicity — openai/replit/aleph/vanta ×2 each, 1password, ro, mashgin):
the voluntary self-ID section sits behind a collapsed disclosure / lazy region that scrolling never
reveals — values were correctly MAPPED, locate can't REACH the controls. One structural fix (detect +
expand disclosure affordances before locate-exhaust; title-ignorant, structure-only) recovers dozens of
fields. Fixture: `collapsed_selfid_disclosure`. Remainder: agilityrobotics custom-domain GH-embed
iframe ×5 (reach class, joins coreweave), replit 112 First/Last-name no-control anomaly (probe
separately). Slots into B-F5 (scoped re-discover) + A's flow/disclosure lane.

## Immediate next actions (this week)
1. Finish current sweep + streaming audit → honest baseline number on the NEW population.
2. Phase 1 verdict fixes + their 3 fixtures (small diffs, verdict-layer, fixture-gated).
3. C-M1 engine-as-callable + ledger→job_fill_runs (parallel, Hand-X only).
4. Then Phase 2 F1 (`_BTN_FIND` retirement) behind its fixtures + passing-set gate.
