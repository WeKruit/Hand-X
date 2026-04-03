# Roadmap: Hand-X

## Milestones

- **v1.0 Runtime Profile Contract** - Phases 1-4 (deferred)
- **v1.1 Generic Repeater Pre-fill Detection** - Phases 5-7 (in progress)
- **v1.2 SPA Page Transition Detection** - Phases 8-9 (planned)
- **v1.3 Streamlined Desktop <-> Hand-X Integration** - Phases 10-11 (in progress)
- **v1.4 Production Ship** - Phases 12-14 (in progress)

## Phases

<details>
<summary>v1.0 Runtime Profile Contract (Phases 1-4) - DEFERRED</summary>

### Phase 1: Runtime Contract In VALET
**Goal**: VALET becomes the authoritative producer of hydrated applicant runtime profile data for a selected user/resume context.
**Depends on**: Nothing (first phase)
**Requirements**: PROF-01, PROF-02, PROF-03
**Success Criteria** (what must be TRUE):
  1. A consumer can request one hydrated runtime profile from VALET for a selected user/resume context
  2. The payload includes parsed resume data, global application defaults, resume-specific overrides, and always-use QA answers
  3. The payload contract is documented clearly enough for multiple consumers to adopt safely
**Plans**: 3 plans

Plans:
- [ ] 01-01: Define the runtime-profile schema and ownership boundary
- [ ] 01-02: Implement the VALET endpoint/service that produces the hydrated payload
- [ ] 01-03: Document the contract and add producer-side tests

### Phase 2: Runtime Asset And Credential Delivery
**Goal**: Resume asset selection and auth intent are delivered through the same VALET-owned runtime path instead of local ad hoc reconstruction.
**Depends on**: Phase 1
**Requirements**: RUNT-01, RUNT-02, RUNT-03
**Success Criteria** (what must be TRUE):
  1. Consumers can resolve the selected resume file from the VALET-owned runtime delivery flow
  2. Auth credentials or references can be delivered without leaking secrets through CLI arguments
  3. Existing-account vs create-account intent is carried consistently to consumers
**Plans**: 3 plans

Plans:
- [ ] 02-01: Define resume asset delivery semantics for the runtime contract
- [ ] 02-02: Define credential and auth-intent delivery semantics
- [ ] 02-03: Add producer-side verification for asset and credential delivery

### Phase 3: Consumer Migration In Hand-X And Desktop
**Goal**: Hand-X and Desktop consume the VALET-owned runtime contract instead of maintaining separate merge behavior.
**Depends on**: Phase 2
**Requirements**: CONS-01, CONS-02, CONS-03
**Success Criteria** (what must be TRUE):
  1. Hand-X can run from the VALET-provided runtime payload instead of reconstructing profile data locally
  2. Desktop uses the same payload boundary for local execution handoff
  3. Login-credential overrides for platform testing do not silently alter applicant contact fields
**Plans**: 3 plans

Plans:
- [ ] 03-01: Switch Hand-X local/worker loaders to the VALET-owned runtime contract
- [ ] 03-02: Align Desktop handoff with the same contract boundary
- [ ] 03-03: Remove or quarantine duplicated merge logic in consumers

### Phase 4: Parity Verification And Cutover Safety
**Goal**: Migration is proven safe by parity tests and explicit cutover rules.
**Depends on**: Phase 3
**Requirements**: QUAL-01, QUAL-02, QUAL-03
**Success Criteria** (what must be TRUE):
  1. Automated tests prove merge precedence and runtime payload parity across producer and consumers
  2. Missing required runtime data fails loudly with actionable blockers
  3. Desktop, worker, and local deterministic testing continue to work after cutover
**Plans**: 3 plans

Plans:
- [ ] 04-01: Add end-to-end parity and regression tests
- [ ] 04-02: Add explicit failure-path coverage for missing runtime data
- [ ] 04-03: Finalize cutover notes and remove temporary migration seams

</details>

<details>
<summary>v1.1 Generic Repeater Pre-fill Detection (Phases 5-7) - IN PROGRESS</summary>

### Phase 5: Observation + Anchor Detection
**Goal**: The repeater system can observe pre-filled form fields per section and identify which entries already exist on the page before expanding any new rows.
**Depends on**: Nothing (first phase of v1.1; builds on existing `extract_visible_form_fields` in codebase)
**Requirements**: OBS-01, OBS-02, OBS-03, TEST-01
**Success Criteria** (what must be TRUE):
  1. Given a repeater section with pre-filled entries, `_observe_existing_entries` returns an `ObservationResult` containing the anchor field labels and their values
  2. Observation results for education fields do not include experience fields visible elsewhere on the same page, and vice versa
  3. Only fields that have effective non-empty values (not just placeholder text or blank inputs) are counted as existing entries
  4. Unit tests pass for anchor label matching, profile key extraction, and the `ObservationResult` data contract
**Plans**: 2 plans

Plans:
- [ ] 05-01-PLAN.md -- Anchor definitions, ObservationResult dataclass, and _observe_existing_entries function
- [ ] 05-02-PLAN.md -- Unit tests for anchor matching, profile key extraction, and ObservationResult contract

### Phase 6: LLM Batch Matching
**Goal**: Profile entries can be matched against observed page entries using a normalization fast-path for exact matches and a single LLM call per section for fuzzy matches.
**Depends on**: Phase 5
**Requirements**: MATCH-01, MATCH-02, MATCH-03, TEST-04
**Success Criteria** (what must be TRUE):
  1. When a profile entry's anchor value exactly matches a page anchor value after normalization (e.g., "Google" == "google"), the match is resolved without any LLM call
  2. When profile and page anchors differ in form but refer to the same entity (e.g., "UCLA" vs "University of California Los Angeles"), `batch_match_entries_llm` correctly pairs them in a single LLM call per section
  3. The batch matcher returns a clear mapping of which profile entries matched which page entries, and which profile entries are unmatched
  4. LLM integration tests validate fuzzy matching for abbreviations and entity variants, and are skipped gracefully when no API key is available
**Plans**: TBD

Plans:
- [ ] 06-01: TBD
- [ ] 06-02: TBD

### Phase 7: Integration + End-to-End Testing
**Goal**: The repeater loop uses observation-based detection as its primary method, falls back to tile counting when needed, and only expands entries that are not already on the page.
**Depends on**: Phase 6
**Requirements**: INT-01, INT-02, INT-03, TEST-02, TEST-03
**Success Criteria** (what must be TRUE):
  1. When a repeater section has pre-filled entries, the expand+fill loop receives only the unmatched profile entries (matched entries are skipped)
  2. When observation finds zero anchor fields (e.g., Greenhouse saved tiles that are not form fields), the system falls back to `_COUNT_SAVED_TILES_JS` and still produces a correct entry count
  3. A CI browser test using the toy-workday fixture with JS-simulated pre-fill confirms that observation detects the pre-filled entries and the repeater does not duplicate them
  4. A CI browser test confirms that section scoping isolates education observation from experience observation on the same page
**Plans**: TBD

Plans:
- [ ] 07-01: TBD
- [ ] 07-02: TBD

</details>

<details>
<summary>v1.2 SPA Page Transition Detection (Phases 8-9) - PLANNED</summary>

### Phase 8: Fingerprint Collection + Transition Detection
**Goal**: The browser-use agent collects a lightweight DOM fingerprint per step and uses it to detect SPA page transitions where the URL stays the same but the page content changes.
**Depends on**: Nothing (first phase of v1.2; builds on existing `_page_identity()` and `_apply_page_transition_context()` in browser_use)
**Requirements**: FPRINT-01, FPRINT-02, TRANS-01, TRANS-02
**Success Criteria** (what must be TRUE):
  1. After each agent step, `BrowserStateSummary` includes a fingerprint hash derived from page headings, buttons, and form count -- collected via a single JS eval taking less than 10ms
  2. `_page_identity()` returns a different identity when SPA content changes (new headings, different buttons, different form count) even if URL and title remain the same
  3. When page identity changes due to fingerprint delta, the existing PAGE UPDATE system note is injected, stale context is cleared, and forced compaction fires -- identical behavior to URL-based transitions
  4. The fingerprint JS snippet and hash logic live in browser_use core (not ghosthands), so any browser_use consumer benefits from SPA detection
**Plans**: 1 plan

Plans:
- [ ] 08-01-PLAN.md -- Add page_fingerprint field, JS collection in _prepare_context, identity enrichment in _page_identity, unit tests

### Phase 9: Validation + Regression Testing
**Goal**: SPA transition detection is proven correct on real ATS flows: Workday transitions trigger domhand_fill, GS Oracle URL-based transitions still work, and conditional field reveals do not cause false positives.
**Depends on**: Phase 8
**Requirements**: VAL-01, VAL-02, VAL-03
**Success Criteria** (what must be TRUE):
  1. On a Workday SPA flow (URL stays the same, content changes between sections), PAGE UPDATE fires and the agent calls domhand_fill on the new page content
  2. On a GS Oracle flow (URL changes between sections), existing PAGE UPDATE behavior is unchanged -- no regression in transition detection or domhand_fill triggering
  3. When a conditional field reveal occurs within a page (e.g., clicking a radio button reveals new fields), the fingerprint does NOT change enough to trigger a false page transition
**Plans**: TBD

Plans:
- [ ] 09-01: TBD
- [ ] 09-02: TBD

</details>

<details>
<summary>v1.3 Streamlined Desktop <-> Hand-X Integration (Phases 10-11) - IN PROGRESS</summary>

### Phase 10: Build Pipeline + Installation
**Goal**: Running `dev-deploy.sh` reliably produces a binary from the project .venv Python 3.12 with all required modules bundled, validates the binary with a smoke test, and installs it where Desktop expects it.
**Depends on**: Nothing (first phase of v1.3; builds on existing dev-deploy.sh and build/hand-x.spec)
**Requirements**: BUILD-01, BUILD-02, BUILD-03, INST-01, INST-02
**Success Criteria** (what must be TRUE):
  1. Running `dev-deploy.sh` on a machine with conda active still uses the project .venv Python 3.12 (not anaconda/system Python) and the build completes without errors
  2. The built binary can import openai, anthropic, google.genai, playwright, aiohttp, and stagehand without ModuleNotFoundError
  3. A smoke test runs automatically after build and before installation, blocking install if any critical import fails
  4. The binary is installed to `~/Library/Application Support/gh-desktop-app/bin/` and a version state JSON is written so Desktop's updater recognizes the dev build
**Plans**: 2 plans

Plans:
- [ ] 10-01-PLAN.md -- Harden venv activation, add Python 3.12 version guard, add critical module import smoke test
- [ ] 10-02-PLAN.md -- Audit PyInstaller spec, run full build pipeline, validate installation and version state

### Phase 11: End-to-End Validation
**Goal**: A fresh binary built from current source is dispatched by Desktop and completes a job without module errors, with correct profile field names and working LLM proxy.
**Depends on**: Phase 10
**Requirements**: E2E-01, E2E-02, E2E-03
**Success Criteria** (what must be TRUE):
  1. Desktop dispatches a job and the Hand-X binary starts, runs the agent loop, and completes without any module import errors
  2. Profile data flowing from Desktop to Hand-X uses the correct field names (authorized_to_work_in_us, needs_visa_sponsorship, citizenship_country, visa_type) with no EMPTY values for renamed/new fields
  3. LLM calls through the VALET proxy succeed from the binary (Gemini calls via proxy return valid responses, no import crashes on error-path fallback)
**Plans**: TBD

Plans:
- [ ] 11-01: TBD
- [ ] 11-02: TBD

</details>

### v1.4 Production Ship (In Progress)

**Milestone Goal:** Promote all staging work to production across VALET, Desktop, and Hand-X -- DB migrations verified safe, unified data contract live on prod, Desktop release cut with latest Hand-X binary, and E2E validated on production infrastructure.

**Phase Numbering:**
- Integer phases (12, 13, 14): Planned milestone work
- Decimal phases (12.1, 12.2): Urgent insertions if needed (marked with INSERTED)

- [ ] **Phase 12: VALET Prod Promotion** - Verify prod DB safety, merge staging to main, deploy to prod, confirm profile endpoint live
- [ ] **Phase 13: Desktop + Hand-X Ship** - Commit Workday gating, rebuild Hand-X binary from current source, install and smoke-test
- [ ] **Phase 14: E2E Validation on Prod** - Launch Desktop on prod, verify profile fields, dispatch job end-to-end on production

## Phase Details

### Phase 12: VALET Prod Promotion
**Goal**: VALET staging (289 commits including unified profile endpoint, DB migrations, resume parser) is safely promoted to production with verified DB compatibility and a recorded rollback path.
**Depends on**: Phase 11 (v1.3 must be stable -- binary pipeline works, Desktop-Hand-X integration proven on staging)
**Requirements**: VALET-01, VALET-02, VALET-03, VALET-04
**Success Criteria** (what must be TRUE):
  1. Prod DB `languages` column contains only valid JSON values (no bare text that would break the varchar-to-jsonb ALTER) -- verified by query before migration runs
  2. Staging branch is merged to main via PR and the CD pipeline completes deployment to prod without errors
  3. Rollback SHA (81ce921) is recorded and the operator can revert prod to that commit if critical issues surface post-deploy
  4. `GET /api/v1/local-workers/profile` returns a 200 with the full hydrated profile payload on the production VALET instance
**Plans**: TBD

Plans:
- [ ] 12-01: TBD
- [ ] 12-02: TBD

### Phase 13: Desktop + Hand-X Ship
**Goal**: Desktop has Workday role-gating merged to main, and Hand-X binary is rebuilt from current source with all recent fixes (field renames, EEO aliases, timeout increases) bundled and installed to the Desktop path.
**Depends on**: Nothing for DESK-01 and HANDX-01 (independent of VALET deploy); HANDX-02 depends on HANDX-01
**Requirements**: DESK-01, HANDX-01, HANDX-02
**Success Criteria** (what must be TRUE):
  1. Workday role-gating changes are committed on GH-Desktop-App main branch via a merged PR
  2. Hand-X binary is built from current main source and all critical imports succeed (openai, anthropic, google.genai, playwright, aiohttp)
  3. Binary is installed to `~/Library/Application Support/gh-desktop-app/bin/` and passes the import smoke test at the installed location
**Plans**: TBD

Plans:
- [ ] 13-01: TBD
- [ ] 13-02: TBD

### Phase 14: E2E Validation on Prod
**Goal**: The full stack -- VALET prod, Desktop from main, Hand-X binary -- works end-to-end: profile saves without errors, bridge summary shows all fields populated, and a dispatched job completes an application on production.
**Depends on**: Phase 12 (VALET on prod), Phase 13 (Desktop + binary ready)
**Requirements**: DESK-02, DESK-03, E2E-01, E2E-02
**Success Criteria** (what must be TRUE):
  1. Desktop launched from main can save a profile without HTTP 500 errors (profile API on prod responds correctly)
  2. Desktop dispatches a job to the Hand-X binary and the binary starts without errors (no module import failures, no stale field names)
  3. Bridge summary log shows all profile fields populated -- including sexual_orientation, education start/end dates, and EEO fields -- with no EMPTY or missing values
  4. A Desktop-dispatched job completes an application end-to-end on production infrastructure (VALET prod + Hand-X binary + real ATS)

## Progress

**Execution Order:**
Phases execute in numeric order: 12 -> 13 -> 14
(Phase 12 and Phase 13 can partially overlap -- DESK-01 and HANDX-01 are independent of VALET deploy)

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Runtime Contract In VALET | v1.0 | 0/3 | Deferred | - |
| 2. Runtime Asset And Credential Delivery | v1.0 | 0/3 | Deferred | - |
| 3. Consumer Migration In Hand-X And Desktop | v1.0 | 0/3 | Deferred | - |
| 4. Parity Verification And Cutover Safety | v1.0 | 0/3 | Deferred | - |
| 5. Observation + Anchor Detection | v1.1 | 0/2 | Planned | - |
| 6. LLM Batch Matching | v1.1 | 0/2 | Not started | - |
| 7. Integration + End-to-End Testing | v1.1 | 0/2 | Not started | - |
| 8. Fingerprint Collection + Transition Detection | v1.2 | 0/1 | Planned | - |
| 9. Validation + Regression Testing | v1.2 | 0/2 | Not started | - |
| 10. Build Pipeline + Installation | v1.3 | 0/2 | Planning | - |
| 11. End-to-End Validation | v1.3 | 0/2 | Not started | - |
| 12. VALET Prod Promotion | v1.4 | 0/2 | Not started | - |
| 13. Desktop + Hand-X Ship | v1.4 | 0/2 | Not started | - |
| 14. E2E Validation on Prod | v1.4 | 0/2 | Not started | - |
