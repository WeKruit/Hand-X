# Roadmap: Hand-X

## Overview

This roadmap focuses on removing runtime-profile drift across VALET, GH Desktop, and Hand-X. The work starts by making VALET the only owner of hydrated applicant runtime data, then migrates Hand-X and its surrounding execution paths to consume that contract, and ends with regression proof that the unified path preserves current automation behavior.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Runtime Contract In VALET** - Define and expose the single hydrated runtime-profile interface
- [ ] **Phase 2: Runtime Asset And Credential Delivery** - Fold selected resume and auth intent into the same runtime delivery path
- [ ] **Phase 3: Consumer Migration In Hand-X And Desktop** - Switch consumers to the VALET-owned contract and remove duplicated merge logic
- [ ] **Phase 4: Parity Verification And Cutover Safety** - Prove parity with automated tests and finalize the migration boundary

## Phase Details

### Phase 1: Runtime Contract In VALET
**Goal**: VALET becomes the authoritative producer of hydrated applicant runtime profile data for a selected user/resume context.
**Depends on**: Nothing (first phase)
**Requirements**: [PROF-01, PROF-02, PROF-03]
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
**Requirements**: [RUNT-01, RUNT-02, RUNT-03]
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
**Requirements**: [CONS-01, CONS-02, CONS-03]
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
**Requirements**: [QUAL-01, QUAL-02, QUAL-03]
**Success Criteria** (what must be TRUE):
  1. Automated tests prove merge precedence and runtime payload parity across producer and consumers
  2. Missing required runtime data fails loudly with actionable blockers
  3. Desktop, worker, and local deterministic testing continue to work after cutover
**Plans**: 3 plans

Plans:
- [ ] 04-01: Add end-to-end parity and regression tests
- [ ] 04-02: Add explicit failure-path coverage for missing runtime data
- [ ] 04-03: Finalize cutover notes and remove temporary migration seams

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Runtime Contract In VALET | 0/3 | Not started | - |
| 2. Runtime Asset And Credential Delivery | 0/3 | Not started | - |
| 3. Consumer Migration In Hand-X And Desktop | 0/3 | Not started | - |
| 4. Parity Verification And Cutover Safety | 0/3 | Not started | - |
