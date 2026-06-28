# Workday Multi-Page Filler — Handoff (Milestone: baseline reverted 2026-06-28)

**This commit = the VERIFIED baseline.** It fills Workday applications to the **Review** page and stops
(never submits). Reverted here after an unverified refactor caused churn; the refactor is preserved as a
patch (see *Churn* below) for the next session to mine.

## ⭐ KEY FINDING (offline, 2026-06-28) — changes the optimization plan
Captured Intel's live per-step DOM (`fixtures/dom_intel/`, via `GH_DUMP`) and ran the baseline extractor
against it OFFLINE in seconds (`tools/offline_dom_harness.py` — strips `<script>` so React can't mangle
the static DOM). Result: **`_EXTRACT_STEP_JS` already classifies EVERY My Experience field** —
Job Title/Company/Location/From/To/Role Description/currently-work-here, **School (multi_select), Degree
(single_select), Field of Study (multi_select), GPA, Skills (multi_select)**. These are all STANDARD field
types the schema fill ladder ALREADY handles (single_select→`_pick_option`, multi_select→tags, date, text).

**⇒ The repeater fields are NOT special. The whole churn (observe_and_fill, `_SECTION_ROWS_JS`,
`fill_language_proficiencies`, `fill_education_combos`) re-solved a problem the existing extractor + ladder
already solve.** The agent only needs to exist for what the schema path genuinely can't do:
- **"Add Another"** to create rows for profile entries beyond the first (extractor only sees present rows).
- **multi_select / tag commit reliability** (School/Field/Skills — the ~1/3 chip-drop) — fix the tag
  primitive, not a per-section filler.
- **dynamic language proficiency** dropdowns (appear only AFTER a language chip is added — NOT in the
  mount-state DOM; capture a post-add DOM to test them) — single_selects, `_pick_option` once present.

**Recommended optimization (simplest, data-backed):** loop per repeater section — schema-fill the present
rows via the existing ladder → click "Add Another" to reach the profile count → RE-EXTRACT → fill the new
rows → repeat to closure. No new fill primitives; just *re-extract after add*. Verify each step OFFLINE
against `fixtures/dom_intel/` before any live run.

## What works (baseline, proven)
- **Single-page ATS** (Greenhouse / Lever / Ashby): ~98% deterministic, fill-only (shipped in PR #38).
- **Workday multi-page**: reached **Review on 3 tenants** — Intel (7-step), NVIDIA (5-step), Visa (5-step)
  — at **$0.022–0.071/app**, 2 profiles, **never submitted**. Account-create gate, submit-guard,
  back-nav guard, per-step instrumentation, VLM read-back rescue all in place.
- Auth: create-account state machine verified on Intel (throwaway email, no mail infra needed). Tenants
  needing email verification return `needs_verification` (Agent Mail design in AUTH_DESIGN / memory).

## Where the 14–18 min goes (the "stuck" breakdown)
From the instrumented runs (per-step `TIME`/`COST`/`AGENT` table):

| Step | Time | Cost | Driver |
|---|---|---|---|
| 1. Autofill-with-Resume | ~4s | $0 | deterministic |
| 2. My Information | ~50s | $0.0014 | **deterministic** (schema MAP + ladder) — fast, cheap ✅ |
| 3. My Experience (mega-page: experience+education+skills+languages) | **~13–17 min** | **~$0.10** | **AGENT — 95% of all time/cost** |

Inside step 3, the agent time splits into:
1. **Skills** tags → agent fallback (deterministic typeahead commit drops ~1/3 of chips run-to-run).
2. **Languages** → name tags **+ 5 proficiency dropdowns** (Comprehension/Overall/Reading/Speaking/
   Writing). The agent clicks each dropdown with **vision** (~1 min each) = the single biggest sink.
3. **`repair_and_advance`** loops on residual validation it can't clear (observed `Loop detection nudge
   repetition=5`, `spa_transition same=True`) = the **STUCK** part (re-tries same fix until max_steps).

Each agent step ≈ 20–40s (gemini + a vision screenshot). ~30+ steps → the 14–18 min. **The deterministic
path (steps 1–2) is fast and cheap; everything slow is the agent doing what deterministic code should.**

## Known gaps (next session — priority order)
1. **My Experience is agent-driven.** Deterministic fillers exist (rows, tags, combos, lang-prof, dates)
   but partially miss on some tenants → agent fallback → slow/$. Make them catch more so the agent rarely
   fires. *This is the whole ballgame for cost+speed.*
2. **Right architecture (validated direction, NOT yet shipped):** ONE *observe-act* primitive —
   `observe_and_fill`: click a control → OBSERVE what pops (dropdown? text? calendar?) → pick/type. No
   per-field special-casers. A dropdown is a dropdown whether it's School, Degree, Country, or language
   proficiency. Prototype is in the churn patch — **unverified**, reverted because verification was broken.
3. **`repair_and_advance` loop guard.** It re-tries the same validation fix and spins. Add a no-progress
   break + target the specific failing field.
4. **Languages proficiency** = 5 single-selects, the SAME widget + `_pick_option` handler as every other
   dropdown. Should be deterministic; an ordering bug had the agent reach them first.
5. **Verification is too slow.** 14-min end-to-end runs to test a section filler, and `_DBG` prints don't
   surface on early returns. ADD: per-step DOM dump (`GH_DUMP` env, in churn patch) → test the detector
   OFFLINE against saved HTML in seconds. Stop blind live-run debugging.
6. **Visa auth rate-limits** after many same-day account-creates (`AUTH_FAILED` validation/CAPTCHA). Space
   runs out; prefer Intel/NVIDIA for iteration.

## Models (findings from the reverted experiment — apply, don't re-litigate)
- **Agent** (browser_use Agent for repair/escalate): **`gemini-3-flash-preview`** (proven baseline).
  `bu-2-0` LOST the bake-off — it is the **priciest** option ($0.60/M in, $3.50/M out vs flash-lite
  $0.25/$1.50) AND it thrashed (23 calls/18 min/failed). Keep gemini.
- **MAP / deterministic LLM**: `gemini-3.1-flash-lite`. If you set a thinking param, use
  **`thinking_budget=0`** — flash-lite REJECTS `thinking_level="minimal"` (floods warnings, degrades the
  run). This bit us.
- **Cost tracking is accurate**: browser_use reads real `usage_metadata` tokens × LiteLLM pricing; bu-2-0
  priced via `CUSTOM_MODEL_PRICING`. The per-page `$` in reports is real.

## Churn (preserved, unverified — mine selectively)
`.observe-act-churn.patch.bak` (in this dir) / scratchpad `observe-act-churn.patch` — 1481 lines:
`observe_and_fill` unified primitive, generic section detector (`_SECTION_ROWS_JS`), `fill_rows_semantic`,
the `GH_DUMP` per-step HTML dump, the bu-2-0 wiring + pricing audit, `thinking_budget` fix. Apply with
`git apply` from repo root, but **VERIFY each piece against a saved DOM before trusting it.**

## Hard invariants (never violate)
- **NEVER submit.** Stop at Review. Submit-guard enforces it.
- Workday native email/password only — never Google/SSO; never fill the beecatcher honeypot.
- Secrets via env/stdin, never CLI args. CAPTCHA → HITL, never auto-solve. Throwaway emails, no real PII.
