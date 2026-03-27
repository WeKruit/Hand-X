# DomHand RPA Verification — Deep Research

**Question:** How should DomHand verify fills deterministically, without LLM, like a proper RPA tool? And how does the v3 staging branch already solve this?

---

## TL;DR

v3 already solved this. `VerificationEngine.ts` = pure DOM readback + normalize + fuzzy compare after every fill. No LLM, no screenshots, no guessing. Hand-X has all the DOM read primitives to do the same thing but buries the result in a 0.55 confidence float that silently becomes `success=True`. Fix: port v3's review contract, make it the single source of truth, surface it to the agent as structured JSON.

---

## v3 GHOST-HANDS: How it actually works

### The Pipeline (DOMHand.ts)

```
observe()  →  process()  →  execute()  →  review()
PageScanner   FieldMatcher   DOMActionExecutor  VerificationEngine
(scan DOM)    (match fields)  (fill via CDP)     (read back, compare)
```

All 4 phases: **costPerAction = 0, requiresLLM = false.**

### VerificationEngine.verify(field, expectedValue)

For each field that was executed:

1. **Read actual value from DOM** via `page.evaluate()`:
   - `input/textarea` → `.value`
   - `select` → selected option `.text` (not `.value`)
   - `custom_dropdown` → `.textContent` of wrapper, filter placeholders
   - `radio` → find `[name="..."][checked]`, extract label text
   - `aria_radio` → `[role="radio"][aria-checked="true"]`
   - `checkbox` → `"checked"` or `""`
   - `contenteditable` → `.textContent`
   - Default fallback → `.value || .textContent`

2. **Normalize both expected and actual**:
   - Trim whitespace, lowercase, collapse multiple spaces
   - Phone-specific: strip to digits only (`/[^0-9]/g`)
   - Date-specific: extract digits only (remove `/`, `-`, `.`)

3. **Fuzzy match**:
   - Exact match on normalized strings
   - Checkbox: truthy set (`true|yes|1|checked|on`) vs `"checked"`
   - Phone: compare last 7 digits (if both ≥ 7 digits)
   - Date: compare digit-only versions
   - Select/custom_dropdown: contains OR startsWith
   - Radio: contains match
   - General fallback: substring matching

4. **Return `VerificationResult`**:
   ```typescript
   { passed: boolean, actual: string, reason: string }
   ```

### ReviewResult (per field)

```typescript
{
  verified: boolean,     // did DOM readback match expected?
  field: FormField,      // which field
  expected: string,      // what we tried to set
  actual: string,        // what DOM says now
  reason: string,        // why passed/failed
  reviewedBy: 'dom'      // always dom for this layer
}
```

**If execution failed**: `verified: false, actual: '', reason: 'Execution failed'`
**If execution succeeded**: run VerificationEngine, report actual result.

**NEVER `success=true` without a verification outcome.**

### What v3 does NOT do

- No shadow DOM traversal (PageScanner uses `querySelectorAll` only)
- No before/after snapshot diff
- No LLM for verification (only for answer synthesis in the match layer)
- No screenshot-based verification
- No confidence scores — just `verified: true/false`

---

## Hand-X: What we already have (and what's broken)

### DOM Read Paths (already implemented)

| What | JS Script | Capability |
|------|-----------|------------|
| Text/email/tel/etc. | `_READ_FIELD_VALUE_JS` | `.value` + visible text |
| Native select | `_READ_FIELD_VALUE_JS` | `.value` + option text |
| Custom dropdown | `_READ_FIELD_VALUE_JS` + token scan | Wrapper text + tokens |
| Radio/button group | `_READ_GROUP_SELECTION_JS` | Selected item text |
| Checkbox/toggle | `_READ_BINARY_STATE_JS` | `.checked` property |
| Multi-select | `_READ_MULTI_SELECT_SELECTION_JS` | Token elements |
| Grouped date | Per-component reads | M/D/Y separately |
| Validation error | `_HAS_FIELD_VALIDATION_ERROR_JS` | `.error`, `.invalid`, `aria-invalid` |
| Field extraction | `_EXTRACT_FIELDS_JS` | All visible fields via `__ff` |
| Field snapshot | `_visible_field_id_snapshot()` | Set of visible field IDs |

**Hand-X actually has MORE DOM read capability than v3** — it uses `__ff.closestCrossRoot()` to pierce shadow DOM boundaries, which v3 PageScanner does not do.

### The Fuzzy Matcher (already implemented)

`_field_value_matches_expected()` in fill_executor.py delegates to `selection_matches_desired()` in dropdown_verify.py. It already does:
- Case-insensitive comparison
- Whitespace normalization
- Substring matching for selects
- Synonym group matching

Additionally, `assess_state` has `_semantic_text_values_match()` which does:
- Number extraction and comparison
- Token overlap matching
- Range comparison for salary/numeric fields

### What's BROKEN (the 8 silent failures)

1. **fill_verify.py:456** — Executor success + readback timeout → `confidence=0.55, success=True`
2. **fill_executor.py:419** — `_read_field_value` exception → returns `""` silently
3. **fill_executor.py:497** — `_read_binary_state` parse error → returns `None`
4. **fill_executor.py:512** — `_get_binary_click_target` exception → returns `{found: false}`
5. **fill_executor.py:460** — `_read_group_selection` parse error → returns `""`
6. **fill_executor.py:3078** — `_read_multi_select_selection` exception → returns `{tokens: []}`
7. **fill_verify.py:272** — Combobox collapse exception suppressed
8. **domhand_fill.py:638** — Reconciliation skips if `_field_has_effective_value()` is False

### Why DomHand feels "noisy" (root causes)

1. **Lies about success.** `confidence=0.55` still returns `success=True` — the agent thinks fields are filled when they might not be.
2. **Two separate verification systems.** `fill_verify` and `domhand_assess_state` have independent matching logic that can disagree.
3. **Talks in prose.** Agent gets "DomHand filled 5 fields" text, not structured field-level truth.
4. **LLM on the verification path.** `llm_verify_field_value()` takes screenshots for verification — non-deterministic, expensive, wrong layer.
5. **Stagehand on the verification path.** Vision-based escalation for what should be a DOM read operation.
6. **Silent read failures.** 8 code paths where DOM read exceptions return empty/null silently.

---

## Deterministic Verification Techniques (research)

### Shadow DOM Observation

**Open Shadow DOM** (most ATS components):
- `element.shadowRoot` returns the shadow tree — fully queryable
- Playwright `page.evaluate()` can traverse: `el.shadowRoot.querySelector(sel)`
- CDP `DOM.describeNode` with `pierce: true` penetrates all shadow roots
- **Hand-X `__ff.closestCrossRoot()` already handles this**

**Closed Shadow DOM** (rare in ATS):
- `element.shadowRoot` returns `null`
- CDP `DOM.describeNode` with `pierce: true` STILL works (Chrome DevTools bypass)
- Playwright `page.$('>>> selector')` pierces both open and closed
- Workaround: read computed styles or accessibility tree

**Conclusion:** Shadow DOM is NOT a barrier to deterministic verification. CDP pierces everything.

### Accessibility Tree as Verification Source

Chrome's accessibility tree (via CDP `Accessibility.getFullAXTree`) provides:
- Element role, name, value, description
- Penetrates shadow DOM
- Represents what screen readers see = what the user sees
- Available via Playwright's `page.accessibility.snapshot()`

**Pros:** Deterministic, penetrates shadow DOM, reflects visual state
**Cons:** Slower than direct DOM queries, not all ATS components have good ARIA attributes

**Verdict:** Good supplementary signal for opaque widgets, not primary path.

### Before/After Snapshot Comparison

The user's proposed approach:

```
BEFORE = snapshot all interactive element states
         → { field_id: { value, checked, selected, error } }

FILL = execute DomHand fills

AFTER = snapshot all interactive element states
        → { field_id: { value, checked, selected, error } }

DIFF = for each field:
       - before[field] == after[field] → "no change" (fill may have failed)
       - before[field] != after[field] AND after matches expected → "verified"
       - before[field] != after[field] AND after != expected → "changed but wrong"
       - after[field] == expected AND before[field] == expected → "already correct"
       - after[field] is empty/opaque → "unreadable"
```

**This is strictly more powerful than v3's approach** because:
- v3 only checks "does current value match expected" (post-fill)
- Before/after also detects: "field changed but to wrong value", "field was already correct", "field reverted"

**Implementation cost:** LOW — Hand-X already has all the read primitives. Just need to call them before AND after the fill batch.

### MutationObserver Approach

Install a DOM MutationObserver before fills to capture all mutations:

**Pros:** Catches every change including async widget updates, no polling needed
**Cons:** Extremely noisy (CSS class changes, attribute mutations), hard to filter to "value changes only", doesn't capture what the value changed TO (only that it changed)

**Verdict:** Not recommended. Before/after snapshot is simpler and gives you the actual values.

---

## Parity Table: v3 TS → Hand-X Python

| v3 Module | v3 Responsibility | Hand-X Equivalent | Gap |
|-----------|-------------------|-------------------|-----|
| `PageScanner.ts` | Scan DOM, discover fields, labels, types | `_EXTRACT_FIELDS_JS` + `extract_visible_form_fields()` | Hand-X MORE capable (shadow DOM via `__ff`) |
| `FieldMatcher.ts` | Match fields to profile values (7 strategies) | `fill_label_match.py` + LLM batch answers | Hand-X uses LLM for unmatched fields (correct for answer synthesis) |
| `DOMActionExecutor.ts` | Fill via CDP DOM injection (tier 0) | `fill_executor.py` | Functionally equivalent |
| `VerificationEngine.ts` | DOM readback + normalize + fuzzy | `fill_verify.py` + `fill_executor.py` reads | **GAP: result not surfaced as structured truth** |
| `types.ts ReviewResult` | `{ verified, expected, actual, reason }` | `FillFieldResult.fill_confidence` (float) | **GAP: boolean/float, not structured** |
| Layer abstraction | `observe → process → execute → review` | Monolithic `domhand_fill()` | **GAP: not separated, but functional** |

### Key Normalization Parity

| Rule | v3 TS | Hand-X Python | Parity? |
|------|-------|---------------|---------|
| Trim + lowercase | Yes | Yes (via `selection_matches_desired`) | OK |
| Phone → last 7 digits | Yes | Partial (format matching in assess) | **Port needed** |
| Date → digits only | Yes | No | **Port needed** |
| Select → contains/startsWith | Yes | Yes (substring match) | OK |
| Checkbox → truthy set | Yes (`true\|yes\|1\|checked\|on`) | Yes (`_is_explicit_false` inverse) | OK |
| Radio → contains | Yes | Yes (group selection match) | OK |
| Collapse multiple spaces | Yes | Partial | **Port needed** |

---

## Recommended Architecture

### Principle: DomHand = Pure RPA Tool

```
DomHand is a TOOL, not a decision-maker.

It does ONE thing:
  extract fields → match to profile → fill via DOM → verify via DOM → report truth

It NEVER:
  - Tells the agent what to do next
  - Uses LLM for verification (only for answer synthesis)
  - Uses screenshots for anything
  - Reports success without verification outcome
  - Silently swallows read failures
```

### Verification Engine (new: `ghosthands/dom/verification_engine.py`)

Port v3 VerificationEngine with Hand-X's superior read primitives:

```python
class FieldReviewResult:
    """One field's verification outcome."""
    field_id: str
    label: str
    execution_status: Literal['executed', 'already_correct', 'execution_failed', 'not_attempted', 'retry_capped']
    review_status: Literal['verified', 'mismatch', 'unreadable', 'not_applicable']
    expected_summary: str   # type + length, NOT raw PII
    actual_read: str        # what DOM says now (truncated, no PII for agent)
    reason: str             # why verified/mismatch/unreadable
    field_type: str         # for agent to know what kind of field
    required: bool

class PageReviewSummary:
    """Page-level rollup."""
    verified_count: int
    mismatch_count: int
    unreadable_count: int
    already_correct_count: int
    execution_failed_count: int
    total_attempted: int
    fields: list[FieldReviewResult]  # capped to top-N non-verified
```

### Agent Visibility Contract

```python
# In domhand_fill return:
ActionResult(
    extracted_content=f"DomHand: {review.verified_count} verified, {review.mismatch_count} mismatch, {review.unreadable_count} unreadable of {review.total_attempted} fields.",
    long_term_memory=_build_capped_review_json(review, max_chars=1500),
    metadata={
        "domhand_fill_review": review.model_dump(),  # full detail for tooling
        ...
    }
)
```

The `long_term_memory` JSON (capped at 1500 chars):
- Always: totals object (~100 chars)
- Then: per-field rows for `mismatch` and `unreadable` only (sorted by required first)
- Field names truncated to 40 chars
- `actual_read` truncated to 30 chars
- NO raw `value_set` (PII) — use `expected_summary` like "text(12)" or "phone(10)"
- `verified` fields omitted from per-field detail (just counted)

### Before/After Enhancement (beyond v3)

```python
async def snapshot_field_states(page, field_ids: set[str]) -> dict[str, FieldState]:
    """Capture current state of target fields. Zero LLM."""
    # Uses existing _read_field_value_for_field, _read_binary_state, etc.
    ...

# In domhand_fill:
before = await snapshot_field_states(page, target_field_ids)
# ... execute fills ...
after = await snapshot_field_states(page, target_field_ids)

# Diff provides additional signal:
for fid in target_field_ids:
    b, a = before.get(fid), after.get(fid)
    if b and a and b.value == a.value and a.value != expected:
        # Fill had no effect — definitely failed
        review_status = 'execution_failed'  # stronger than just 'mismatch'
```

---

## Implementation Plan (revised based on all reviews)

### Phase 0 — Unified VerificationEngine (pull forward from Phase 3)

All 3 reviewers agreed: `domhand_fill` and `domhand_assess_state` must share verification logic.

1. Create `ghosthands/dom/verification_engine.py` with:
   - `verify_field(page, field, expected) -> FieldReviewResult`
   - `verify_batch(page, fields_with_expected) -> PageReviewSummary`
   - Port v3 normalization rules (phone last-7, date digits-only, collapse spaces)
   - Use existing Hand-X read primitives (not new JS)
   - Add Oracle-specific `readback_opaque` status for known-empty-read widgets
2. Both `domhand_fill` and `domhand_assess_state` call this engine.
3. Tests: golden string parity with v3 fuzzyMatch cases.

### Phase 1 — Agent Visibility (structured review in long_term_memory)

1. Thread `FieldReviewResult` from VerificationEngine into `FillFieldResult`:
   - Add `dom_read_actual`, `review_status`, `execution_status` fields
   - Remove `fill_confidence` float (replaced by structured status)
2. Build `PageReviewSummary` in domhand_fill reconciliation.
3. Inject capped JSON (1500 chars) into `long_term_memory`.
4. Update agent prompts: "trust domhand_fill review for filled/verified status."
5. Fix the 0.55 path: `execution_status=executed, review_status=unreadable` (never `success=True` without verification).

### Phase 2 — Deterministic Normalization Expansion

Before any LLM equivalence:
1. Country name/code normalization ("USA" / "US" / "United States")
2. Phone format normalization ("+1 (555) 123-4567" vs "5551234567") — port v3 last-7-digits
3. Date format normalization ("03/26/2026" vs "March 26, 2026") — port v3 digits-only
4. State/province abbreviation expansion
5. Port remaining v3 fuzzyMatch cases not in current `_field_value_matches_expected`

### Phase 2b — Text-Only LLM Equivalence (only if Phase 2 doesn't cover enough)

- New module `ghosthands/dom/semantic_equivalence.py` (NOT in fill_llm_escalation.py)
- Text-only, no screenshots
- Only invoked when: readback non-empty, all deterministic rules failed, field is text/select
- Restricted to display-string comparisons — NOT for identity, numeric, date, address, contact fields

### Phase 3 — Before/After Snapshot (optional enhancement)

- `snapshot_field_states()` before and after fill batch
- Diff provides stronger signal than post-only verification
- Detects "fill had no effect" vs "fill changed to wrong value"

### Phase 4 — Tests

- Unit: review JSON shape, review_status values, PII redaction
- Unit: verification_engine normalization parity with v3 golden strings
- Unit: semantic equivalence only called when deterministic fails + actual non-empty
- Integration: Oracle page fixture → verify `readback_opaque` for cx-select

---

## References

- `GHOST-HANDS/staging/packages/ghosthands/src/engine/v3/VerificationEngine.ts` — the reference implementation
- `GHOST-HANDS/staging/packages/ghosthands/src/engine/v3/DOMHand.ts` — the pipeline contract
- `GHOST-HANDS/staging/packages/ghosthands/src/engine/v3/types.ts` — ReviewResult type
- `ghosthands/dom/fill_verify.py` — current Hand-X verification (has the 0.55 bug)
- `ghosthands/dom/fill_executor.py` — DOM read primitives (complete, usable)
- `ghosthands/actions/domhand_fill.py` — monolithic fill + reconciliation
- `ghosthands/actions/domhand_assess_state.py` — separate verification (must unify)
- `.planning/REVIEWS-domhand-v3-parity.md` — 3-reviewer cross-AI review findings
