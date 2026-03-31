# Requirements: Hand-X Repeater Pre-fill Detection

**Defined:** 2026-03-31
**Core Value:** A saved applicant identity can be applied accurately, repeatably, and safely across ATS flows without the user re-entering data.

## v1.1 Requirements

Requirements for generic repeater pre-fill detection. Each maps to roadmap phases.

### Observation

- [ ] **OBS-01**: Repeater extracts visible anchor fields per section using `extract_visible_form_fields` before expanding
- [ ] **OBS-02**: Anchor fields are scoped to the target section via `_section_matches_scope`
- [ ] **OBS-03**: Only fields with effective values (`_field_has_effective_value`) are counted as existing entries

### Matching

- [ ] **MATCH-01**: LLM batch matcher compares all profile anchors against all page anchors in one call per section
- [ ] **MATCH-02**: Exact match after `normalize_name` skips LLM (fast path for trivial cases)
- [ ] **MATCH-03**: LLM handles fuzzy entity matching (UCLA = University of California Los Angeles, Google = Alphabet/Google)

### Integration

- [ ] **INT-01**: `_observe_existing_entries` replaces `_COUNT_SAVED_TILES_JS` as primary detection in the repeater loop
- [ ] **INT-02**: `_COUNT_SAVED_TILES_JS` kept as fallback when observation finds zero anchor fields
- [ ] **INT-03**: Only unmatched profile entries are passed to the expand+fill loop

### Testing

- [ ] **TEST-01**: Unit tests validate anchor label matching, profile key extraction, and `ObservationResult` contract
- [ ] **TEST-02**: CI browser tests use toy-workday fixture with JS-simulated pre-fill to test observation + matching
- [ ] **TEST-03**: CI browser tests verify section scoping isolates education from experience fields
- [ ] **TEST-04**: LLM integration tests validate fuzzy matching (abbreviations, variants) — skipped without API key

## v1.0 Requirements (previous milestone)

### Runtime Profile Contract

- [ ] **PROF-01**: VALET exposes one authenticated interface that returns a hydrated runtime profile for a selected user/resume context
- [ ] **PROF-02**: The hydrated payload includes parsed resume data, global application defaults, resume-specific application overrides, and always-use QA answers
- [ ] **PROF-03**: The hydrated payload schema is documented and versioned enough for multiple consumers to rely on it safely

### Resume And Credential Delivery

- [ ] **RUNT-01**: The selected resume file for the run is resolved through the same VALET-owned contract path, not local Hand-X defaults
- [ ] **RUNT-02**: Platform auth credentials can be delivered to consumers without exposing secrets on CLI arguments
- [ ] **RUNT-03**: The runtime contract distinguishes auth intent consistently, including existing-account and create-account flows

## Future Requirements

### Extended Platform Support

- **EXT-01**: Observation detects pre-filled skills shown as chips/badges (not form fields)
- **EXT-02**: Observation reads Greenhouse saved tile text content for matching (beyond CSS counting)

## Out of Scope

| Feature | Reason |
|---------|--------|
| Non-repeater field pre-fill detection | Different problem — `domhand_fill` already handles individual field observation |
| Rewriting `_COUNT_SAVED_TILES_JS` entirely | Kept as fallback — working for Greenhouse/Lever |
| Per-entry LLM matching (one call per profile entry) | Batch matching is more cost-efficient |
| Platform-specific anchor label overrides | Generic label matching sufficient for v1.1 |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| OBS-01 | Phase 5 | Pending |
| OBS-02 | Phase 5 | Pending |
| OBS-03 | Phase 5 | Pending |
| MATCH-01 | Phase 6 | Pending |
| MATCH-02 | Phase 6 | Pending |
| MATCH-03 | Phase 6 | Pending |
| INT-01 | Phase 7 | Pending |
| INT-02 | Phase 7 | Pending |
| INT-03 | Phase 7 | Pending |
| TEST-01 | Phase 5 | Pending |
| TEST-02 | Phase 7 | Pending |
| TEST-03 | Phase 7 | Pending |
| TEST-04 | Phase 6 | Pending |

**Coverage:**
- v1.1 requirements: 13 total
- Mapped to phases: 13
- Unmapped: 0

---
*Requirements defined: 2026-03-31*
*Last updated: 2026-03-31 after roadmap creation*
