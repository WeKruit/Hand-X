# Phase 8: Fingerprint Collection + Transition Detection - Context

**Gathered:** 2026-03-31
**Status:** Ready for planning
**Source:** Discussion (code analysis + log review + approach agreement)

<domain>
## Phase Boundary

Add lightweight DOM fingerprinting to browser_use's per-step browser state extraction so that `_page_identity()` detects SPA page transitions (same URL, different content) and triggers the existing PAGE UPDATE + compaction mechanism.

</domain>

<decisions>
## Implementation Decisions

### Fingerprint Collection
- Collect fingerprint via a single lightweight JS eval per step (~3-5ms)
- Fingerprint captures: headings (h1, h2, h3, [role="heading"]), buttons, form count
- Reference implementation: `_PAGE_FINGERPRINT_JS` in `ghosthands/actions/domhand_click_button.py:419-447`
- Fingerprint JS uses plain `document.querySelectorAll` (not `window.__ff`) since headings are in main document even on shadow DOM sites
- Store fingerprint as a string field on `BrowserStateSummary`

### Identity Enrichment
- `_page_identity()` at `browser_use/agent/message_manager/service.py:193-206` includes fingerprint in identity string
- When fingerprint changes between steps → identity changes → `_apply_page_transition_context()` fires
- This triggers: PAGE UPDATE note + stale read_state clearing + forced compaction (lines 225-240)
- No new mechanism — same behavior as URL-based transitions

### Claude's Discretion
- Exact fingerprint hashing strategy (could be JSON string comparison or hash)
- Whether to bucket/normalize any fingerprint components to avoid minor churn
- Error handling for fingerprint JS eval failures (should degrade gracefully — identity falls back to URL+title+element_count)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Browser State Extraction
- `browser_use/browser/views.py:89-111` — `BrowserStateSummary` dataclass (add fingerprint field here)
- `browser_use/browser/session.py:1535-1571` — `get_browser_state_summary()` (fingerprint collected here or in service.py)
- `browser_use/agent/service.py:1324-1374` — `_prepare_context()` (calls get_browser_state_summary then prepare_step_state)

### Page Transition Detection
- `browser_use/agent/message_manager/service.py:193-206` — `_page_identity()` (ENRICH THIS)
- `browser_use/agent/message_manager/service.py:208-217` — `_build_page_transition_note()` (already exists, no changes needed)
- `browser_use/agent/message_manager/service.py:225-240` — `_apply_page_transition_context()` (already exists, no changes needed)

### Reference Fingerprint
- `ghosthands/actions/domhand_click_button.py:419-447` — `_PAGE_FINGERPRINT_JS` (reference for fingerprint structure, DO NOT modify)

### Existing SPA Detection (context only)
- `browser_use/agent/service.py:1470-1486` — field_ids overlap check for same-page guard (separate mechanism, do not touch)

</canonical_refs>

<specifics>
## Specific Ideas

- The fingerprint JS should be a simplified version of `_PAGE_FINGERPRINT_JS`:
  - Capture headings: `document.querySelectorAll('h1, h2, h3, [role="heading"]')` — get text content, limit to first 6
  - Capture buttons: `document.querySelectorAll('button, [role="button"], input[type="submit"]')` — get text, limit to first 8
  - Capture form count: `document.querySelectorAll('form').length`
  - Return as JSON string
- For `_page_identity()`, append the fingerprint string (or a hash of it) to the existing `title\nurl\nelem_bucket` format
- Error handling: if JS eval fails, return empty string → identity falls back to existing behavior

</specifics>

<deferred>
## Deferred Ideas

- Propagating `domhand_click_button`'s `state_changed` as additional signal (Option A from discussion — deferred, B is sufficient)
- Using `_visible_field_id_snapshot()` for transition detection (false positive risk from conditional fields)
- Programmatic domhand_fill execution on transitions (rejected approach — too aggressive)

</deferred>

---

*Phase: 08-fingerprint-collection-transition-detection*
*Context gathered: 2026-03-31 via discussion*
