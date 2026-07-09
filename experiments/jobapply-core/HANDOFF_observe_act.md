# observe_act Generic ATS Filler — Handoff (for a fresh session with no prior memory)

**Read this top-to-bottom before touching anything.** It is the distilled memory of the whole build:
what the system is, where every file lives (a lot is gitignored local scratch), the exact commands,
the verified numbers, the hard-won principles, the traps that cost days, and the open work.

Date of handoff: 2026-07-08. Author context: this was built over many sessions; the numbers below are
verified from re-run JSON + screenshot audit, not estimates.

---

## 0. TL;DR

- **What:** a generic web-form filler (`observe_act`) that fills ANY ATS job application (Greenhouse,
  Lever, Ashby, Workday, plus arbitrary company career pages) — not per-ATS scripted. Built on
  browser-use + raw CDP. Maps profile→field values with ONE LLM call, commits each field with a
  per-field state machine, verifies by real painted state.
- **Where the code is:** worktree `…/Hand-X/.claude/worktrees/observe-act-generic/experiments/jobapply-core/`,
  branch `feat/observe-act-generic` (361 commits ahead of `main`). Engine = the `oa_*.py` files.
- **Where the TESTS are:** `runs/fixtures/` — **GITIGNORED** (`experiments/jobapply-core/.gitignore` line
  `runs/`). The entire playground is local scratch, 0 files tracked in git. It will NOT come with a clone.
- **Verified state:** playground **125/125 ISO PASS, 0 false-green/red**; every fixture proven winnable by
  `selfcheck.py`. Live generic-lane sweep: **91.7%** screenshot-audited (5 profiles × ~100 fresh live
  Ashby/Greenhouse/Lever jobs). Live 95% is NOT yet reached/verified — needs a fresh rested-machine sweep.
- **The #1 meta-lesson:** diagnose by OBSERVING the live thing (screenshot / one debug print), not by
  reading code and trusting test artifacts. A whole session was lost to 3 test fixtures I wrote with a
  no-op click handler that could never pass — the "engine bug" was a broken test.

---

## 1. Architecture — the fill pipeline

Per job: navigate → discover fields from live DOM → map values (ONE LLM call) → per-field commit → verify.

**Per-field state machine** (`oa_observe_act.py`, function `observe_act` / `_s_*`):
```
S0_GUARD → S1_LOCATE → S2_CLASSIFY → { S_CHOICE | S_NATIVE | S3_OPEN(select) | S4_SEARCH | S_CASCADE } → S_VERIFY
```
- **S1_LOCATE** binds the field to a DOM node/card. `located:grouped` binds a representative node in a
  card; `located:spatial` binds by geometry. (Grouped-locate can desync — see traps.)
- **S2_CLASSIFY** = `classify_intrinsic()` (DOM standards: input[type], role) then a `kind-hint` from the
  adapter, then a label-meaning LLM as last resort.
- **S_CHOICE** commits radios/checkboxes/pills. Order: (1) `cdp_choose_button_by_label` label-anchored
  button click → verify next-tick paint (`choice-label-anchor`); (2) `cdp_choose_option` structural scan
  (`choice-dom-direct`); (3) `_read_choice_group` + visual VLM pick by coordinate. Self-verifies on REAL
  painted selected-state, so it cannot false-green.
- **S3_OPEN** = custom/native selects. `cdp_choose_react_select` opens the menu; if options are painted
  but DOM-ownership-unbindable, `cdp_pick_option_visually` reads visible option nodes by ARIA role +
  `[class*=option]` WITH center coords, picks by meaning, trusted `cdp_click_xy`. (This +3pts fix is real.)
- **S_VERIFY** = DOM read-back primary + a VLM screenshot second opinion when needed.

**Mapping** (`ats_engine.py map_fields`, `_MAP_SYSTEM` prompt ~line 610): ONE structured LLM call,
label→value, via ChatGoogle `gemini-3-flash` (`GOOGLE_API_KEY` in `.env`), wrapped in `oa_llm.ResilientLLM`
(fails over to gpt-5.4-mini on 503/stall). Consent/agreement questions are answered affirmatively by
MEANING (not keyword) — see principles.

**Confidence metric:** COMPLETE = `complete` OR (no `missing_required` AND no `visually_unanswered`);
DEAD (page never reached) excluded; confidence = COMPLETE / scoreable.

---

## 2. Where everything lives

| Thing | Path (under `experiments/jobapply-core/`) | Tracked? |
|---|---|---|
| Engine | `oa_observe_act.py` (state machine), `oa_cdp_action.py` (CDP committers/JS), `oa_perception.py` (DOM serialize/visibility), `oa_discover.py`, `oa_brain.py` (LLM helpers), `oa_complete.py` (completeness verdict), `ats_engine.py` (`map_fields`), `oa_singlepage.py` (single-page driver/entry), `oa_llm.py` | git-tracked (branch `feat/observe-act-generic`), **but currently UNCOMMITTED in the working tree** (~2049 lines) |
| Playground fixtures | `runs/fixtures/all_fixtures.json` (~125 fixtures) | **GITIGNORED** |
| ISO runner (all fixtures, isolated) | `runs/fixtures/run_playground_iso.py` | **GITIGNORED** |
| Single-fixture trace (injects value) | `runs/fixtures/trace_one.py <kind>` | **GITIGNORED** |
| Mapper-exercising run (NO injected value → real LLM) | `runs/fixtures/map_test.py <kind>` | **GITIGNORED** |
| **Fixture winnability guard (NEW)** | `runs/fixtures/selfcheck.py` | **GITIGNORED** |
| Live sweep tooling | `runs/newats/` (fetch_fresh_urls / sweep500_run / score / triage / kill_old_oa.py) | **GITIGNORED** |
| This handoff | `HANDOFF_observe_act.md` | git-tracked (not under `runs/`) |

> **CONSEQUENCE:** the playground is local scratch. To hand it to a fresh clone you must either force-add
> the key files (`git add -f runs/fixtures/all_fixtures.json runs/fixtures/*.py`) or copy the directory.
> The engine work is uncommitted — commit it or it is lost.

Env: `.env` holds `GOOGLE_API_KEY` (mapper) and `GH_*` vars. Venv: `.venv/bin/python`. Never put secrets
in CLI args (visible via `ps aux`) — env vars / ATP/Infisical only. Never `fly secrets set`.

---

## 3. The playground — commands

All run from `experiments/jobapply-core/` with `export OA_NO_SANDBOX=1`.

```bash
# Full 125-fixture ISO regression (each fixture on its own page; DOM oracle + verdict-consistency oracle):
.venv/bin/python runs/fixtures/run_playground_iso.py
#   → "ISOLATED: PASS ALL"  +  "VERDICT-CONSISTENCY: false-RED N false-GREEN N"

# ONE fixture, value injected (fast, ~30s) — the FAILED-FIRST loop:
.venv/bin/python runs/fixtures/trace_one.py <kind>

# ONE fixture through the REAL mapper (no injected value — the only way to test map_fields):
.venv/bin/python runs/fixtures/map_test.py <kind>

# NEW GUARD — prove every fixture is WINNABLE before trusting any green/red:
.venv/bin/python runs/fixtures/selfcheck.py            # all
.venv/bin/python runs/fixtures/selfcheck.py <kind> ... # subset
#   PASS = reachable; FAIL = fixture reads a JS-painted state it can never produce (dead handler / read
#   mismatch); SKIP = actuator can't reproduce a multi/compound/native shape (not a defect). Exit 1 on FAIL.
```

**Two oracles in the ISO harness:** (1) DOM read of each fixture's own `data-read` vs `expected`;
(2) verdict-consistency — DOM-wrong-yet-engine-COMPLETE = false-GREEN, DOM-right-yet-engine-incomplete
(e.g. the `committed=='on'` radio read-back) = false-RED. Both must be 0.

**Playground blind spots (criticize them, they are real):**
- ISO **injects** field values (`OA_FIXTURE_VALUES`) → it structurally CANNOT test the mapper. Use `map_test.py`.
- ISO **mocks the VLM** → it cannot reproduce load-fragile vision false-greens.
- Single-field pages cannot reproduce EMERGENT locate bugs (a field mis-binding to a neighbour) — those
  only appear in full live-DOM complexity.
- **Fixtures can be silently broken** (see §9) → run `selfcheck.py` first, always.

---

## 4. The live sweep — infra, commands, TRAPS

Tooling in `runs/newats/`: `fetch_fresh_urls` (pull fresh live apply URLs), `sweep500_run` (N URLs × 5
profiles), `score`, `triage`. Profiles: `rich`, `rich2`, `intl`, `minimal`, `veteran` (fixtures/profile_*.json).

**HARD traps (each cost real time):**
- **Run sweeps UNSANDBOXED.** A sandbox throttles the Gemini/VLM calls → VLM times out → the
  `visually_unanswered=0` "vision-confirmed" guard is actually vision-DEAD → false-greens slip through.
  Also set `OA_VLM_TIMEOUT=12` (5s too tight for screenshot calls).
- **Machine load poisons measurement.** Under load 5-10 the VLM times out and identical code flip-flops
  green/red across runs. Check `uptime`; kill stray processes; never trust a green measured under load.
- **browser-use leaks ~1.8 system-Chrome zombies per job.** Kill by process-age (`kill_old_oa.py`,
  age>300s) — NEVER blanket-kill chromium mid-run (two concurrent sweeps cross-kill each other's browsers
  and contaminate verdicts). `pgrep` before trusting a rerun.
- **Get the number by SCREENSHOT-AUDIT, not by re-sweeping.** A 21-agent parallel workflow that Reads
  each COMPLETE run's screenshot and adversarially checks every required field is the trustworthy way to
  a number without re-contaminating via a fresh load-heavy sweep. (This found 9/21 = 43% of flipped-
  COMPLETE runs were false-greens → corrected 93.8%→91.7%.)

---

## 5. Verified results

- **Playground: 125/125 ISO PASS ALL, false-RED 0, false-GREEN 0.**
- **`selfcheck.py` winnability guard: 64 PASS / 0 FAIL / 61 SKIP.** 0 FAIL = no fixture the guard can
  actuate is broken. The 61 SKIP are multi-select / portal / type-search / switch widgets the guard's
  simple actuator can't drive (honest "can't test this shape", NOT defects) — those are covered by the
  ISO run, which the real engine passes. A FAIL means: the guard clicked the exact `expected` option and
  the JS-painted state still didn't appear (the dead-handler class that bit us in §9). Run it before any
  scoring run.
- **Live generic lane: 91.7%** screenshot-audited (411/448; 5 profiles × ~100 fresh live jobs). By ATS
  (pre-audit): greenhouse ~95.7 / lever ~92 / ashby ~89.9. This is an UPPER bound on the un-audited
  kept-COMPLETE runs; treat 91.7% as the honest live number.
- **Real shipped engine wins:** react-select dropdown vision-fallback (`cdp_pick_option_visually`, +3pts,
  88.9→91.9); meaning-based mapper consent answering; structural choice commit (`choice-label-anchor` /
  `cdp_choose_option`) that verifies real paint.
- **NOT claimed:** live 95%. Playground-clean ≠ live-95%. Requires a fresh rested-machine sweep to verify.

---

## 6. Principles (the hard-won rules — violate these and you regress)

1. **No static / coded pattern matching for locate or match.** Any match via string ==, includes,
   startsWith/prefix, regex, or fixed char/word thresholds is unreliable — swapping a 40-char slice for a
   5-word prefix is the SAME anti-pattern. **Locate by STRUCTURE** (the already-located node/card, ARIA
   roles, containment, proximity, automation-id/data-fkit-id prefixes) and **match by MEANING** (LLM/VLM).
   DOM identity cross-checks are OK; label-text-vs-DOM matching is not.
2. **Detect sections/fields by STRUCTURE, never by heading/title text.** Tenants rename & localize.
3. **Prompts state a PRINCIPLE + 1 example, not enumerated word/label/regex lists** (same anti-pattern).
4. **FILLED ≠ COMPLETE. Never overclaim.** Report done only with DOM audit + visual second opinion +
   screenshot. `fill_rate`'s denominator is blind to undiscovered fields.
5. **Never trust a green you have not SEEN.** `visually_unanswered=0` under load is vision-dead, not
   confirmed. Screenshot-audit greens.
6. **Verify by REAL painted selected-state**, not by a node's `.checked`/value text. `rendered_present` /
   option-label-in-DOM false-greens radio/button groups (the label is always present). A decoupled hidden
   checkbox can read `.checked=true` while the visible pill is blank.
7. **Observe-first, failed-first, fast-loop.** When something fails twice, open a direct CDP session / add
   ONE debug print on the LIVE page — do not run another slow sweep or read more code. Rerun ONLY the
   failed 2-3 fixtures first (~30s); full suite only as the final gate.
8. **Validate every test fixture is WINNABLE before trusting it** (`selfcheck.py`). A broken test masquerades
   as an engine bug.
9. **Scope changes narrowly.** Never broadly edit shared fill code; per-field-type bypasses. Parallel
   "diagnostic patch families" can fix nothing AND break a neighbour (each agent sees only its fixture) —
   ALWAYS full-re-run + compare the PASSING set; revert/narrow any family netting ≤0.
10. **`page.evaluate` wraps its arg as `(fn)()` → pass a BARE arrow `()=>{}`, never an IIFE `(()=>{})()`**
    (double-invokes → silent TypeError → suppressed → silent `[]`). This killed gates for weeks.
11. **After ANY CDP/browser-action change, live-test on a real page + verify with independent raw CDP.**
    Offline selftests hide bugs (a `bool(evaluate)` bug hid for weeks).
12. **Protect the laptop.** Kill zombies by age, not blanket. Never launch multiple live tests at once.
    Secrets in env vars only.

---

## 7. Known traps / gotchas (concrete)

- **grouped-locate desync:** `located:grouped` binds a REPRESENTATIVE node whose DOM `.value`/`.checked`
  can desync from the painted control. Don't trust it for already-correct/verify short-circuits.
- **decoupled pill:** Ashby/consent Yes-No = `<button class=_option_…>` pills + a hidden decoupled
  `<input type=checkbox aria-hidden>`. Clicking the input sets `.checked` but paint is button-driven.
  The engine correctly clicks the visible button (`choice-label-anchor`) and verifies the button's paint.
- **bare-arrow evaluate** (principle 10).
- **backgrounded Bash monitors with python/heredoc inside a `while` loop die instantly** (exit 1, empty).
  Use a plain `until grep -qa DONE file; do sleep N; done`; do merge/score AFTER the loop.
- **`nohup … &` inside a run_in_background Bash double-backgrounds** — the wrapper exits 0 immediately while
  the real process keeps running detached. Poll the output file / `pgrep`, don't trust the "completed".
- **radio value='on' read-back:** a radio whose `value='on'` must read back its LABEL, not `input.value`.
- **5 kinds of dropdown** each need a scoped reader (lever/greenhouse/workday/DomHand/aria); aria-owns
  scoped read must run BEFORE the delta read (delta grabs page chrome → escalate). DomHand HAS reusable
  dropdown code.

---

## 8. Open work / next steps

- **Live 95%:** run a CLEAN sweep on a RESTED machine (low load, `OA_VLM_TIMEOUT=12`, unsandboxed, vision
  confirmed running) → screenshot-audit → get the true post-mapper-fix live number. The mapper consent fix
  + confirmed-clean pill commit MAY lift it above 91.7%, but that is UNVERIFIED.
- **Workday:** account-gated + email-verification. Existing `wd_one`/`run_wizard` driver is a SEPARATE
  engine from observe_act. Cred policy: random email + a fixed test password, bail on verify-code, NEVER
  submit. Fill ~79%, auth ~64%, e2e ~50% across 22 tenants (last measured).
- **Self-improve loop:** worker→VALET Supabase `job_fill_runs` auto-ingest + Storage screenshots +
  dashboard (PR #259 merged). GAP: the deployed worker uses browser-use (THIN data); the rich per-field
  ledger only exists in the observe_act engine (not shipped to the worker).
- **The real lever** (from the 500-sweep audit) is a TRUSTWORTHY verify/audit layer, NOT more fill paths.

---

## 9. Tonight's episode — the broken-fixture lesson (why the loop matters)

For a full session I chased an "unfixed decoupled-pill engine false-green." It did not exist. **3 fixtures
I wrote** (`consent_pill_agreement`, `localized_yesno_pill`, `yesno_pill_below_long_paragraph`) had a click
handler `function(){(function(){…is-active…},0);}` — the inner `(function(){…},0)` is a COMMA EXPRESSION;
the function is never invoked (author meant `setTimeout(fn,0)`). The pill's `.is-active` was never set, so
the fixture could NEVER paint. Every "false-green" was the fixture failing to paint; every "pass" was noise.

Proof: `node -e` on the handler shape → `is-active added? false`; a "passing" pill fixture re-run →
`painted: BLANK`. ONE debug print showed `ctx.node='button' vis=True` — locate had bound the visible pill,
not a decoy — demolishing the theory in one run. Fix = restore `setTimeout` in 3 fixtures; **engine
untouched** (`git diff` engine = empty) → all pills paint, 125/125 clean. Guard added: `selfcheck.py`.

The lesson is principle #7 and #8: I diagnosed from unverified artifacts on a slow loop and read code to
theorize instead of observing. Don't.
