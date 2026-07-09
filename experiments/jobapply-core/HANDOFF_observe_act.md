# observe_act — COMPLETE Ecosystem Handoff (single file, no prior memory needed)

**Audience:** a fresh Claude session (or engineer) with ZERO context. Read top-to-bottom before touching
anything. This is the whole system: architecture, interface, testing ecosystem (playground + live URL
sweeps), how test URLs are fetched, false-green defense doctrine, profiles, archived/legacy code,
verified progress, and the integration plan with the actual app.

Handoff date: 2026-07-09. All numbers verified from re-run JSON + screenshot audit — no estimates.
Canonical location: `experiments/jobapply-core/HANDOFF_observe_act.md` on `Hand-X` `main`
(merged via PR #40, merge commit `ecc6bd747`).

---

## 0. TL;DR + Progress

- **What:** `observe_act` — a generic web-form filler that fills ANY ATS job application (Greenhouse /
  Lever / Ashby / arbitrary career pages; Workday has a separate driver) without per-site scripting.
  Built on browser-use + raw CDP. ONE LLM call maps profile→field values; a per-field state machine
  commits each field; verification is against REAL painted state (DOM read-back + VLM screenshot).
- **Verified progress (current):**
  - Playground: **125/125 ISO PASS ALL, false-green 0 / false-red 0** (two independent oracles).
  - Fixture winnability guard `selfcheck.py`: **64 PASS / 0 FAIL / 61 SKIP** (SKIP = shapes its simple
    actuator can't drive; covered by the ISO run).
  - Live generic lane: **91.7%** screenshot-audited confidence (411/448; 5 profiles × ~100 fresh live
    Ashby/Greenhouse/Lever jobs). Per-ATS (pre-audit): greenhouse ~95.7 / lever ~92 / ashby ~89.9.
  - **Live 95% NOT yet verified** — needs a fresh sweep on a rested machine (see §5 traps).
  - Workday (separate `wd_one` engine, 22 tenants): fill ~79% [52,92], auth ~64%, e2e ~50%.
  - Cost: ~$0.002–0.003 per application (generic lane, gemini-3-flash mapper).
- **Where:** branch `feat/observe-act-generic` == `main` (both carry everything). Working tree:
  `Hand-X/.claude/worktrees/observe-act-generic/experiments/jobapply-core/`.
- **#1 meta-lesson of the whole build:** diagnose by OBSERVING the live thing (screenshot / one debug
  print / direct CDP), never by trusting artifacts you haven't validated. A full session was lost to 3
  test fixtures whose click handler was a silent no-op — the "engine bug" was a broken test (§7.6).

---

## 1. Architecture — the whole ecosystem

```
                        ┌──────────────────────────────────────────────────────────┐
                        │                    TEST ECOSYSTEM                         │
                        │                                                          │
  URL SOURCING          │  PLAYGROUND (offline, fixtures)   LIVE SWEEP (real ATS)  │
  fetch_fresh_urls.py ──┼─► sweep500.tsv ────────────────────► sweep500_run.py     │
  (public board APIs,   │                                        │ per-URL:        │
   dedup, 5-profile     │  all_fixtures.json (125)               ▼                 │
   round-robin)         │   ├ selfcheck.py  (winnable?)      oa_singlepage.py      │
                        │   ├ run_playground_iso.py (2 oracles)  │                 │
                        │   ├ trace_one.py  (1 fixture, fast)    ▼                 │
                        │   └ map_test.py   (real mapper)    json+png+log+ledger   │
                        │                                        │                 │
                        │                     sweep500_score.py ◄┘                 │
                        │                     triage_failures.py                   │
                        │                     screenshot-audit swarm (Workflow)    │
                        └──────────────────────────────────────────────────────────┘
                                             │ failures → new fixtures → playground
                                             ▼  (the self-improve loop)
┌─────────────────────────────────── THE ENGINE ────────────────────────────────────┐
│ oa_singlepage.py (driver/entry)                                                   │
│   ├ adapter lane: ats_greenhouse / ats_lever / ats_ashby .extract() → schema      │
│   ├ generic lane (--generic): oa_discover.py discovers fields from LIVE DOM       │
│   ├ ats_engine.map_fields() — ONE structured LLM call (label→value, gemini-flash) │
│   └ per field → oa_observe_act.observe_act():                                     │
│        S0_GUARD → S1_LOCATE → S2_CLASSIFY → {S_CHOICE|S_NATIVE|S3_OPEN|S4_SEARCH  │
│        |S_CASCADE} → S_VERIFY   (oa_perception=DOM serialize, oa_cdp_action=      │
│        committers, oa_brain=LLM/VLM, oa_complete=completeness verdict)            │
└───────────────────────────────────────────────────────────────────────────────────┘
                                             │
                            (NOT YET WIRED — see §9 integration plan)
                                             ▼
┌────────────────────────────── THE ACTUAL APP (production) ─────────────────────────┐
│ VALET API (assigns jobs) ◄── callbacks ── ghosthands/ worker (deployed, polls)     │
│   Supabase job_fill_runs ingest + Storage screenshots + dashboard (VALET PR #259)  │
│   GH-Desktop-App owns the user's REAL Chrome → Hand-X attaches via OA_CDP_URL      │
│   ⚠ deployed worker still runs plain browser-use agent (THIN data, no ledger) —    │
│     porting observe_act into it is THE integration work.                           │
└────────────────────────────────────────────────────────────────────────────────────┘
```

**Per-field state machine** (`oa_observe_act.py`):
- **S1_LOCATE** binds field→DOM node/card (`located:grouped` = representative node in a card;
  `located:spatial` = geometry). Grouped-locate can desync — never trust its `.value` for short-circuits.
- **S2_CLASSIFY** = `classify_intrinsic()` (DOM standards: `input[type]`, role) → adapter `kind-hint` →
  label-meaning LLM only as last resort.
- **S_CHOICE** (radios/checkboxes/pills), in order: (1) `cdp_choose_button_by_label` — label-anchored
  button click, verifies NEXT-TICK paint (`choice-label-anchor`); (2) `cdp_choose_option` — structural
  scan of the located container incl. visually-hidden styled inputs (`choice-dom-direct`);
  (3) `_read_choice_group` + set-of-marks visual VLM pick, click by coordinate. Self-verifies on real
  painted selected-state → cannot false-green.
- **S3_OPEN** (custom/native selects): `cdp_choose_react_select` opens + delta-reads the menu; when
  options are painted but DOM-ownership-unbindable → `cdp_pick_option_visually` (ARIA role +
  `[class*=option]` + center coords, proximity-guarded, trusted `cdp_click_xy`). This fix = +3pts live.
- **S4_SEARCH** type-to-filter; **S_CASCADE** for reveals ("Yes" → "please specify" input).
- **S_VERIFY**: DOM read-back primary; VLM screenshot second opinion; verdict CORRECT/WRONG drives
  S_REVALUE retries.

**Mapping** (`ats_engine.py::map_fields`, prompt `_MAP_SYSTEM` ~line 610): ONE structured call via
ChatGoogle `gemini-3-flash-preview` (`GOOGLE_API_KEY` in `.env`), wrapped in `oa_llm.ResilientLLM`
(503/stall → failover gpt-5.4-mini). Consent/agreement questions answered affirmatively **by MEANING**
(any language), never by keyword list.

**Confidence metric:** COMPLETE = `complete` OR (no `missing_required` AND no `visually_unanswered`);
DEAD (page never reached) excluded; **confidence = COMPLETE / scoreable**.

---

## 2. The interface

### 2.1 CLI (the only entry point you need)

```bash
cd experiments/jobapply-core && export OA_NO_SANDBOX=1
.venv/bin/python oa_singlepage.py \
  --url  "https://jobs.ashbyhq.com/…/application"   # required; GH/Lever/Ashby or any career page
  --profile fixtures/rich_profile.json               # required; profile JSON (NO secrets in argv)
  --resume  fixtures/resumes/test_resume.pdf         # optional; file-upload answer
  --json    out.json                                 # optional; full per-field result ledger
  --screenshot out.png                               # optional; end-of-fill PNG
  --generic                                          # force the no-adapter generic lane
  --headed                                           # default headless
```
**FILL-ONLY — it never submits.** Adapter lane auto-picks Greenhouse/Lever/Ashby extractors from the
URL; `--generic` forces live-DOM discovery (the benchmark lane, also what arbitrary career pages use).

### 2.2 Key env vars (full set greppable: `grep -rhoE 'OA_[A-Z_]+' oa_*.py | sort -u`)

| Var | Meaning / recommended |
|---|---|
| `OA_NO_SANDBOX=1` | required in most local envs (Chrome sandbox) |
| `OA_CDP_URL=ws://…` | attach to an ALREADY-RUNNING real Chrome (`--remote-debugging-port`) instead of launching — **the production shape** (Desktop app owns the browser); also the real-fingerprint path that passes device checks |
| `OA_CHROME_PATH` / `OA_STEALTH=1` | real Chrome binary / stealth profile for fingerprint checks |
| `OA_VLM_TIMEOUT=12` | VLM screenshot-call timeout. 5s is too tight → vision silently dies → false-greens |
| `OA_VISION_GATE=0/1` | vision second-opinion gate (playground mocks it off) |
| `OA_COMPLETE_AGENT=0/1` | completeness agent on/off |
| `OA_FIXTURE_VALUES=path.json` | inject label→value (playground bypasses the mapper — mind §4.3) |
| `OA_PAINTED_DUMP=1` | dump each fixture's `data-read` painted truth into result JSON |
| `OA_PROC_CAP_S=120` | hard per-process cap |
| `OA_SCROLL_LOCATE=1` | scroll-locate for forms below long job descriptions (auto-on in generic lane) |
| `OA_VIEWPORT_W/H` | default 1280×900 |
| `OA_PRIMARY` / `OA_BRAIN_MODEL` / `OA_FALLBACK_MODEL` / `OA_OPENAI_*` | model routing overrides |
| `OA_FIELD_TRACE=1`, `OA_CHOICE_DIAG=1`, `OA_GETSTATE_LOG=1` | per-field debug traces |

Secrets: `.env` only (`GOOGLE_API_KEY`, `GH_*`). **Never in argv** (`ps aux` leaks). Never `fly secrets set`
(ATM/Infisical is the source of truth).

### 2.3 Result JSON (the per-field ledger — the data contract)

Top level: `adapter, title, url, fields_total, mapped, status, completeness{complete, missing_required,
not_reached…}, painted[] (fixture oracle), outcomes{DONE/SKIP/ESCALATE/OTHER}, fill_rate, coverage,
cost, secs, results[]`. Each `results[]` row:
`{name, label, type, src, outcome, nature, committed, value, trace[]}` — `trace` is the full state-machine
path (e.g. `S0_GUARD → S1_LOCATE → located:grouped → S2_CLASSIFY → kind-hint:checkbox->CHOICE → S_CHOICE
→ choice-label-anchor:Yes → S_VERIFY → verify-src:dom → verdict:CORRECT`). **Read traces before
theorizing about any failure.** This rich ledger is what the deployed worker LACKS (§9).

---

## 3. Code inventory — active vs archived

### ACTIVE (`experiments/jobapply-core/`)
| File | Role |
|---|---|
| `oa_singlepage.py` | driver/entry — browser launch/attach, nav, orchestration, painted-dump, cleanup |
| `oa_observe_act.py` | the per-field state machine (heart of the engine) |
| `oa_cdp_action.py` | CDP committers: `cdp_choose_button_by_label`, `cdp_choose_option`, `cdp_choose_react_select`, `cdp_pick_option_visually`, `cdp_click_xy`, dual-listbox, datepickers… |
| `oa_perception.py` | DOM serialize (`get_state`), visibility, geometry |
| `oa_discover.py` | generic-lane live-DOM field discovery |
| `oa_brain.py` / `oa_llm.py` | LLM/VLM helpers, ResilientLLM failover |
| `oa_complete.py` | completeness verdict (missing_required / visually_unanswered) |
| `ats_engine.py` | `map_fields` — the ONE mapping call + `_MAP_SYSTEM` prompt |
| `ats_greenhouse.py` / `ats_lever.py` / `ats_ashby.py` | schema extractors (adapter lane) |
| `oa_repeater.py`, `oa_file_locate.py`, `oa_dom_value.py`, `oa_planner.py`, `oa_profiles.py`, `oa_hitl.py`, `oa_proof.py`, `vision_verify.py` | repeaters, file upload, value read-back, page planning, profile access, HITL, proof shots |
| `wd_one.py` + `wd_repeaters.py` + `wd_verify_email.py` + `ats_workday.py` | **Workday driver — a SEPARATE engine** (deterministic wizard walker + agent backstop; account creation, email-verify bail, never submits) |
| `failcap.py`, `l3_promote.py`, `auto_fix.py` | self-improve loop: failure capture + VLM triage, L3 selector-promotion miner |

### ARCHIVED / LEGACY — do not build on these
| What | Status |
|---|---|
| `GHOST-HANDS/` (sibling repo) | **LEGACY repo, replaced by Hand-X.** Do not develop there. |
| `ghosthands/` (in Hand-X) | the DEPLOYED worker — plain browser-use agent + DomHand DOM-first fill. **Zero references to observe_act** — it is the integration TARGET, not current state (§9). |
| `greenhouse_fill.py`, `greenhouse_schema.py`, `jobapply.py`, `jobapply_cloud.py`, `sweep.py` | earlier deterministic-filler generation (branch `feat/deterministic-ats-filler`, merged PR #39). Superseded by the oa_* engine; keep for reference on GH DOM quirks. |
| `scratch_chooser_toy.py`, `scripts/probe_*.py` | one-off live-DOM probes (Twilio react-select, Samsara firstname, Duolingo listbox). Useful as CDP-probe TEMPLATES (§4.5). |
| DomHand (`ghosthands/dom/`) | v2 worker's DOM-first filler — HAS reusable dropdown readers (5 dropdown kinds inventory) |

---

## 4. The testing ecosystem (how we test — all layers)

### 4.1 Layer 1 — Playground (offline fixtures; fast, free, deterministic)
`runs/fixtures/all_fixtures.json` — 125 self-describing fixtures. Each: `kind`, `label`, `html`
(field snippet incl. hostile shapes: react-select portals, decoupled pills, aria-listboxes, MUI/antd/
radix/downshift widgets, localized German, likert grids, dual-listbox, tag inputs…), `profile_value`
(injected), `expected` (what must END UP PAINTED), and `data-read` (a JS expression over `f` = the field
element returning painted ground truth — self-describing oracle, no per-widget dump code).

```bash
export OA_NO_SANDBOX=1   # all from experiments/jobapply-core/
.venv/bin/python runs/fixtures/selfcheck.py            # 0) EVERY fixture winnable? (run FIRST)
.venv/bin/python runs/fixtures/trace_one.py <kind>     # 1) ONE fixture, injected value (~30s) — failed-first loop
.venv/bin/python runs/fixtures/map_test.py <kind>      # 2) ONE fixture through the REAL mapper (no injection)
.venv/bin/python runs/fixtures/run_playground_iso.py   # 3) full 125, each on its own page, TWO oracles
```
Two oracles in ISO: **(a)** each fixture's own `data-read` vs `expected`; **(b)** verdict-consistency —
DOM-wrong-but-engine-COMPLETE = **false-GREEN**, DOM-right-but-engine-incomplete = **false-RED**
(e.g. radio `value='on'` read-back). Green bar = `ISOLATED: PASS ALL` + both zero.

### 4.2 Layer 2 — Live URL sweeps (the real number)
**Getting test URLs** — `runs/newats/fetch_fresh_urls.py`: pulls FRESH live apply URLs from **public ATS
board APIs (no auth)** — Greenhouse `boards-api.greenhouse.io/v1/boards/<slug>/jobs`, Lever
`api.lever.co/v0/postings/<slug>`, Ashby `api.ashbyhq.com/posting-api/job-board/<slug>` — using a
company-slug list (`/tmp/slugs.json`), **dedups against every previously-swept URL** (freshmatrix/gen/
mega/atsx lists + `newats_meta.json`), caps 14/company for diversity, round-robins the 5 profiles.
Output: `fresh500.jsonl` + `sweep500.tsv` (rows: profile, url, ats, company). Run UNSANDBOXED (network).

**Running** — `runs/newats/sweep500_run.py [CONC] [tsv]`: each row → `oa_singlepage --generic` with its
assigned profile; concurrency-capped (default 5); unique user-data-dir per run; **no global pkill**
(self-cleans); per-run `json+png+log` + per-field ledger into `runs/newats/<tsv-stem>/`; incremental
results (crash-safe); one retry on CRASH/NO-RESULT. Smoke-test with a small tsv first.

**Scoring** — `sweep500_score.py` (COMPLETE/scoreable by profile × ATS), `triage_failures.py` (cluster
failures by trace signature → the fix worklist), `final_scorecard.py` (best-verdict across retests).

**Trustworthy number** — NEVER trust raw COMPLETE. Run the **screenshot-audit swarm**: a Workflow that
fans out one adversarial agent per COMPLETE run's PNG ("is every visible required field ACTUALLY
answered? pills = exactly one highlighted; dropdowns ≠ placeholder"), tally false-greens, subtract.
(Last audit: 9/21 flipped-COMPLETE were false-green → 93.8% corrected to 91.7%.)

### 4.3 What each layer CANNOT test (the honest blind-spot map)
- ISO **injects values** (`OA_FIXTURE_VALUES`) → cannot test the mapper → use `map_test.py`.
- ISO **mocks the VLM** → cannot reproduce load-fragile vision false-greens → live only.
- Single-field pages cannot reproduce **emergent locate bugs** (mis-binding to a neighbor field) —
  only full live-DOM complexity shows those.
- Fixtures can be **silently unwinnable** (§7.6) → `selfcheck.py` before scoring, always.
- `fill_rate`'s denominator is blind to undiscovered fields → completeness verdict + screenshot only.

### 4.4 Self-improve loop (failure → fixture → fix → regress)
1. Sweep fails a field → `triage_failures.py` clusters by trace signature (+ `failcap.py` VLM triage).
2. Reproduce the failing DOM shape as a NEW fixture in `all_fixtures.json` (copy the live widget's HTML
   skeleton; give it `data-read` + `expected`). Run `selfcheck.py <kind>` to prove it's winnable.
3. Fix the engine (narrowly — §8). `trace_one.py <kind>` until green (~30s loop).
4. Full ISO 125 as the regression gate. Compare the PASSING SET, not just the count — a fix that flips
   a neighbor red is net-negative → revert.
5. Live-verify on the original failing URL. Then (periodically) fresh sweep + screenshot audit.

### 4.5 CDP debugging (when a widget family fails twice)
Do NOT run another sweep. Open a **direct CDP session on the live page**: launch system Chrome with
`--remote-debugging-port=9222`, connect (`cdp_use` / `OA_CDP_URL`), and probe the exact DOM with the
same primitives the engine uses (see `scripts/probe_*.py` templates). Independently verify any committer
change with raw CDP evaluates — offline selftests hid a `bool(evaluate)` bug for weeks.

---

## 5. Live-sweep operational traps (each cost real time — respect them)

- **UNSANDBOXED only.** Sandbox throttles Gemini/VLM → timeouts → `visually_unanswered=0` is
  vision-DEAD not vision-confirmed → false-greens slip in silently. Also `OA_VLM_TIMEOUT=12`.
- **Load poisons measurement.** Under load ≥5, identical code flip-flops green/red (VLM timeouts).
  Check `uptime` BEFORE trusting any number; kill strays; measure on a rested machine.
- **Zombie browsers:** browser-use leaks ~1.8 system-Chrome processes per job. Kill by AGE
  (`runs/newats/kill_old_oa.py`, >300s) — NEVER blanket-pkill chromium while ANY sweep runs (two
  concurrent sweeps cross-kill each other's browsers and contaminate verdicts). `pgrep` first.
- **One live test at a time.** Toy-test before live. Kill old processes before relaunching.
- **Background shell monitors:** `until grep -qa DONE file; do sleep 30; done` ONLY — python/heredoc
  inside a `while` loop dies instantly. `nohup … &` inside an already-backgrounded command
  double-backgrounds (wrapper "completes" while work continues — poll the output file).

---

## 6. Profiles (test identities)

`experiments/jobapply-core/fixtures/`:
| File | Persona |
|---|---|
| `rich_profile.json` | full US profile (default; sweeps + generic_sweep) |
| `rich_profile2.json` | second rich variant (different answers — catches cross-profile value bleed) |
| `profile_intl.json` | international (visa/sponsorship answers differ) |
| `profile_minimal.json` | sparse profile (tests data-gap SKIP correctness — engine must NOT invent) |
| `profile_veteran.json` | veteran/EEO variations |
| `zoo_profile.json` (in `runs/fixtures/`) | playground profile for fixture runs |

Resume: `fixtures/resumes/test_resume.pdf`. Workday test applicant: Ruiyang Chen, email
`1808111261@qq.com` (NOT rc5663@nyu.edu). Workday cred policy: random email + fixed test password,
**bail on email-verify code, NEVER submit**.

---

## 7. False-green defense doctrine (how we keep the number honest)

1. **FILLED ≠ COMPLETE.** Claim done only with DOM audit + visual second opinion + screenshot.
2. **Two oracles** in the playground (DOM read + verdict-consistency); both must be 0.
3. **Screenshot-audit swarm** over every live COMPLETE before believing a sweep number.
4. **Verify REAL painted state:** a pill's active class next-tick, a radio's `.checked` — never
   option-label-presence (`rendered_present` false-greens radio/button groups: the label is ALWAYS in
   the DOM), never a decoupled hidden checkbox's `.checked` (paint is button-driven).
5. **Radio `value='on'`** → read back the LABEL, not `input.value`.
6. **Fixture winnability** (`selfcheck.py`): a fixture that can never paint its `expected` turns every
   result on it into noise. Tonight's proof: 3 fixtures had `(function(){…},0)` — a comma expression
   that never invokes (author meant `setTimeout(fn,0)`) — and masqueraded as an engine false-green for a
   full session. Fix was 3 fixtures; engine untouched; 125/125 clean after.
7. **Vision liveness:** a vision verdict under VLM-timeout is a DEAD verdict — treat as unknown, not green.
8. **React active-class is NEXT-tick:** split click and verify into separate evaluates or the check
   fires before paint and reads 0.

---

## 8. Engineering principles (violate → regress; each learned the hard way)

1. **No static/coded pattern matching** for locate or match — no string ==, includes, prefix/suffix,
   regex, fixed char/word thresholds. A 5-word prefix is the same sin as a 40-char slice. **Locate by
   STRUCTURE** (located node/card, ARIA roles, containment, proximity, automation-id prefixes), **match
   by MEANING** (LLM/VLM). DOM identity cross-checks OK; label-text-vs-DOM matching never.
2. **Detect sections by STRUCTURE, never heading text** — tenants rename & localize everything.
3. **Prompts: principle + 1 example** — enumerated ignore-lists/word-lists/label-regexes in prompts are
   the same anti-pattern as regex in code.
4. **Observe-first, failed-first, fast-loop.** One debug print / screenshot / direct CDP beats another
   sweep. Rerun ONLY the failed 2–3 first (~30s each); full suite once, as the gate.
5. **Scope narrowly.** Never broadly edit shared fill code — targeted per-field-type bypasses. Parallel
   patch families (agents each seeing one fixture) can fix nothing and break neighbors: full re-run,
   compare PASSING SETS, revert any family netting ≤0.
6. **`page.evaluate` needs a BARE arrow `()=>{}`** — the actor wraps args as `(fn)()`; an IIFE
   double-invokes → silent TypeError → suppressed → silent `[]`. Killed gates for weeks.
7. **After any CDP/action change: live-test on a real page + verify with independent raw CDP.**
8. **Every failure gets an autopsy as "why did generic L1 fail"** — acceptance asserts use VERBATIM live
   strings; both compare sides go through the SAME normalizer.
9. **grouped-locate desync:** `located:grouped` binds a representative whose DOM state can desync from
   the painted control — never trust it for already-correct/verify short-circuits; instrument, don't assume.
10. **Aria-owns scoped dropdown read BEFORE delta-read** (delta grabs page chrome → escalate). 5 dropdown
    kinds each have a scoped reader; DomHand has reusable dropdown code.
11. **Secrets in env vars only; never argv. Laptop protected:** kill zombies by age, one live test at a
    time, check `pgrep` before trusting rerun verdicts.

---

## 9. Integration plan with the actual app (the REAL next milestone)

**Current production path (what runs today):** VALET API assigns jobs → `ghosthands/` worker
(`worker/poller.py` polls, `worker/executor.py` runs) → **plain browser-use agent + DomHand** fills →
`integrations/valet_callback.py` reports → Supabase `job_fill_runs` ingest + Storage screenshots +
dashboard (VALET PR #259, merged). Credentials AES-256-GCM (`integrations/credentials.py`), domain
allowlist enforced pre-navigation.

**The gap:** the deployed worker produces THIN data (browser-use agent transcript) and lower fill
quality; the rich per-field ledger + 91.7% engine exists only in `experiments/jobapply-core`. Zero
references to observe_act inside `ghosthands/`.

**Integration steps (recommended order):**
1. **Extract the engine as a callable:** `oa_singlepage.run_one(url, profile, resume, session) -> result`
   — it's already structured that way internally (the CLI is a thin argparse wrapper). Keep FILL-ONLY;
   submission stays a separate explicit action behind review.
2. **Wire into `worker/executor.py`** as the primary fill path, browser-use generic agent demoted to
   fallback (the tiered model the worker already documents: DOM-first → LLM answers → agent fallback).
3. **Browser ownership:** production shape = Desktop app owns the user's REAL Chrome; worker attaches
   via `OA_CDP_URL` (`BrowserSession(cdp_url=…)` path in `oa_singlepage.py`, already built + the
   fingerprint-passing shape). Env-var it per job.
4. **Ship the ledger:** map `results[]` (per-field trace/outcome/committed) into `job_fill_runs` so the
   dashboard shows per-field truth, not agent prose. The self-improve loop (§4.4) then feeds from
   production failures automatically.
5. **Profile mapping:** VALET user profile → the profile-JSON shape the engine consumes
   (`oa_profiles.py` is the access layer; the 5 test profiles in §6 define the schema by example).
   Cred refs stay OUT of the profile (V2 claim/secrets split: lease-scoped bootstrap via stdin).
6. **Budget/HITL:** cost tracking exists both sides (`worker/cost_tracker.py`, engine `cost` field;
   ~$0.003/app). `oa_hitl.py` + NEEDS_HUMAN outcome → route to the review UI instead of silent ESCALATE.
7. **Verification gate before GA:** rested-machine 500-URL × 5-profile sweep + screenshot audit ≥95%,
   plus the Workday lane decision (separate driver; email-verify needs AgentMail/Gmail integration).

**Hand-X repo rules:** branch from `main`, PR to `main`, commit style `feat|fix(module): …`,
run `ruff check . && ruff format --check .` before committing. CI/CD auto-deploys — never `fly deploy`.

---

## 10. Open work (priority order)

1. **Live 95% verification** — fresh `fetch_fresh_urls` → `sweep500_run` on a RESTED machine (load <3,
   unsandboxed, `OA_VLM_TIMEOUT=12`, vision confirmed live) → screenshot-audit swarm → true number.
   The mapper-consent fix + clean pill path have never been measured live together.
2. **Integration** (§9) — port observe_act into the worker; ship the ledger.
3. **Workday** — separate driver at e2e ~50%; email-verification design (AgentMail / Gmail per-user);
   account-gated tenants; WAF blocks (Tesla).
4. **S_OTHER no-escape cluster** (45× in the 500-sweep) — fields with no fill path escalate; the audited
   lever is a trustworthy verify/audit layer, not more fill paths.
5. **Playground expansion** from every new live failure (§4.4 loop) — with `selfcheck.py` gating each
   new fixture.

---

## 11. Bootstrap prompt for a fresh session (copy-paste)

> Read `experiments/jobapply-core/HANDOFF_observe_act.md` on Hand-X `main` top-to-bottom. It is the
> complete ecosystem handoff for the observe_act generic ATS filler: architecture, CLI/env interface,
> playground + live-sweep testing stack, URL fetching, false-green doctrine, profiles, legacy-code map,
> integration plan with the VALET worker, and open work. Verified state: playground 125/125 clean
> (two oracles), live 91.7% screenshot-audited; live 95% unverified pending a rested-machine sweep.
> Before ANY scoring run: `runs/fixtures/selfcheck.py`. Before ANY diagnosis: read the per-field
> `trace[]` in the result JSON and look at the screenshot. Failed-first, observe-first. No static
> pattern matching — structure + meaning only.
