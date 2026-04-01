# Requirements: Hand-X

**Defined:** 2026-04-01
**Core Value:** A saved applicant identity can be applied accurately, repeatably, and safely across ATS flows without the user re-entering data.

## v1.3 Requirements — Streamlined Desktop ↔ Hand-X Integration

### Build Pipeline

- [ ] **BUILD-01**: dev-deploy.sh builds from project .venv Python 3.12 regardless of conda/system Python state
- [ ] **BUILD-02**: PyInstaller spec bundles all required modules (openai, anthropic, google-genai, playwright, aiohttp, stagehand)
- [ ] **BUILD-03**: Smoke test validates critical module imports in built binary before installing

### Binary Installation

- [ ] **INST-01**: Binary installed to `~/Library/Application Support/gh-desktop-app/bin/` as primary path
- [ ] **INST-02**: Version state JSON written so Desktop's updater recognizes the dev build

### End-to-End Validation

- [ ] **E2E-01**: Desktop dispatches a job and Hand-X binary starts without module import errors
- [ ] **E2E-02**: Profile data flows Desktop→Hand-X with correct field names (no EMPTY values for renamed fields)
- [ ] **E2E-03**: LLM proxy works through binary (Gemini calls succeed via VALET proxy)

## v1.2 Requirements — SPA Page Transition Detection (planned)

<details>
<summary>Planned requirements</summary>

### Fingerprint Collection

- [ ] **FPRINT-01**: Browser-use collects a lightweight page fingerprint (headings, buttons, form count) per step as part of browser state extraction
- [ ] **FPRINT-02**: Fingerprint is stored on `BrowserStateSummary` and available to `_page_identity()` without additional browser calls

### Transition Detection

- [ ] **TRANS-01**: `_page_identity()` includes fingerprint hash so SPA content changes (same URL, different content) produce a different identity
- [ ] **TRANS-02**: When page identity changes due to fingerprint, the existing PAGE UPDATE note + stale context clearing + forced compaction fires — same behavior as URL-based transitions

### Validation

- [ ] **VAL-01**: On Workday SPA (URL stays same, content changes), PAGE UPDATE fires and agent calls domhand_fill on the new page
- [ ] **VAL-02**: On GS Oracle (URL changes between sections), existing behavior still works — no regression
- [ ] **VAL-03**: Conditional field reveals within a page (clicking radio → new fields appear) do NOT trigger a false page transition

</details>

## v1.1 Requirements — Repeater Pre-fill Detection (shipped)

<details>
<summary>Completed requirements</summary>

### Observation
- [x] **OBS-01**: Repeater extracts visible anchor fields per section using `extract_visible_form_fields` before expanding
- [x] **OBS-02**: Anchor fields are scoped to the target section via `_section_matches_scope`
- [x] **OBS-03**: Only fields with effective values (`_field_has_effective_value`) are counted as existing entries

### Matching
- [x] **MATCH-01**: LLM batch matcher compares all profile anchors against all page anchors in one call per section
- [x] **MATCH-02**: Exact match after `normalize_name` skips LLM (fast path for trivial cases)
- [x] **MATCH-03**: LLM handles fuzzy entity matching

### Integration
- [x] **INT-01**: `_observe_existing_entries` replaces `_COUNT_SAVED_TILES_JS` as primary detection
- [x] **INT-02**: `_COUNT_SAVED_TILES_JS` kept as fallback when observation finds zero anchor fields
- [x] **INT-03**: Only unmatched profile entries are passed to the expand+fill loop

### Testing
- [x] **TEST-01**: Unit tests validate anchor label matching, profile key extraction, and `ObservationResult` contract
- [x] **TEST-02**: CI browser tests use toy-workday fixture with JS-simulated pre-fill
- [x] **TEST-03**: CI browser tests verify section scoping isolates education from experience
- [x] **TEST-04**: LLM integration tests validate fuzzy matching — skipped without API key

</details>

## Future Requirements

- .dmg packaging for Mac app distribution (after Desktop integration is stable)
- CI/CD automated binary builds via GitHub Actions
- Cross-platform binary testing (Windows/Linux)
- Programmatic domhand_fill execution on page transitions (deferred)
- Observation detects pre-filled skills shown as chips/badges (not form fields)

## Out of Scope

| Feature | Reason |
|---------|--------|
| .dmg packaging | Separate milestone — need stable integration first |
| CI automated builds | Manual dev-deploy.sh is sufficient for now |
| Windows/Linux binaries | macOS-only development environment |
| LLM provider architecture changes | Just bundle existing providers correctly |
| Hook-based ActionResult injection | Tried in v1.1, created noise |
| Per-action middleware | Too aggressive — intercepts every tool call |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| BUILD-01 | TBD | Pending |
| BUILD-02 | TBD | Pending |
| BUILD-03 | TBD | Pending |
| INST-01 | TBD | Pending |
| INST-02 | TBD | Pending |
| E2E-01 | TBD | Pending |
| E2E-02 | TBD | Pending |
| E2E-03 | TBD | Pending |

**Coverage:**
- v1.3 requirements: 8 total
- Mapped to phases: 0 (pending roadmap)
- Unmapped: 8

---
*Requirements defined: 2026-04-01*
*Last updated: 2026-04-01 after milestone v1.3 start*
