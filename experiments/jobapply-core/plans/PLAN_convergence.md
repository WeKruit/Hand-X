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

## Immediate next actions (this week)
1. Finish current sweep + streaming audit → honest baseline number on the NEW population.
2. Phase 1 verdict fixes + their 3 fixtures (small diffs, verdict-layer, fixture-gated).
3. C-M1 engine-as-callable + ledger→job_fill_runs (parallel, Hand-X only).
4. Then Phase 2 F1 (`_BTN_FIND` retirement) behind its fixtures + passing-set gate.
