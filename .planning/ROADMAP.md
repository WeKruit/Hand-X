# Roadmap: Hand-X

## Milestones

- **v1.0 Runtime Profile Contract** - Phases 1-4 (deferred)
- **v1.1 Generic Repeater Pre-fill Detection** - Phases 5-7 (in progress)

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

### v1.1 Generic Repeater Pre-fill Detection (In Progress)

**Milestone Goal:** Replace platform-specific repeater entry counting with generic field observation + LLM matching so any ATS pre-fill is detected without per-platform selectors.

**Phase Numbering:**
- Integer phases (5, 6, 7): Planned milestone work
- Decimal phases (5.1, 5.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 5: Observation + Anchor Detection** - Extract visible form fields per repeater section and identify existing entries by anchor values
- [ ] **Phase 6: LLM Batch Matching** - Match profile entries against observed page entries using normalization fast-path and LLM fuzzy matching
- [ ] **Phase 7: Integration + End-to-End Testing** - Wire observation into the repeater loop, keep tile-count fallback, and prove correctness with fixture tests

## Phase Details

### Phase 5: Observation + Anchor Detection
**Goal**: The repeater system can observe pre-filled form fields per section and identify which entries already exist on the page before expanding any new rows.
**Depends on**: Nothing (first phase of v1.1; builds on existing `extract_visible_form_fields` in codebase)
**Requirements**: OBS-01, OBS-02, OBS-03, TEST-01
**Success Criteria** (what must be TRUE):
  1. Given a repeater section with pre-filled entries, `_observe_existing_entries` returns an `ObservationResult` containing the anchor field labels and their values
  2. Observation results for education fields do not include experience fields visible elsewhere on the same page, and vice versa
  3. Only fields that have effective non-empty values (not just placeholder text or blank inputs) are counted as existing entries
  4. Unit tests pass for anchor label matching, profile key extraction, and the `ObservationResult` data contract
**Plans**: TBD

Plans:
- [ ] 05-01: TBD
- [ ] 05-02: TBD

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

## Progress

**Execution Order:**
Phases execute in numeric order: 5 -> 6 -> 7

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Runtime Contract In VALET | v1.0 | 0/3 | Deferred | - |
| 2. Runtime Asset And Credential Delivery | v1.0 | 0/3 | Deferred | - |
| 3. Consumer Migration In Hand-X And Desktop | v1.0 | 0/3 | Deferred | - |
| 4. Parity Verification And Cutover Safety | v1.0 | 0/3 | Deferred | - |
| 5. Observation + Anchor Detection | v1.1 | 0/2 | Not started | - |
| 6. LLM Batch Matching | v1.1 | 0/2 | Not started | - |
| 7. Integration + End-to-End Testing | v1.1 | 0/2 | Not started | - |
