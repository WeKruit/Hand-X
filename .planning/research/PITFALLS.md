# Domain Pitfalls: Browser Automation Observation Layer Rebuild

**Domain:** Observation layer for browser automation agent (ATS form filling)
**Researched:** 2026-04-02
**Context:** Brownfield rebuild -- action layer stays, observation is replaced from scratch

---

## Critical Pitfalls

Mistakes that cause rewrites, block progress, or break existing functionality.

---

### C1: Breaking the domhand_fill Contract During Migration

**Severity:** Critical -- will block progress
**Phase:** Should be addressed in Phase 1 (observer contract definition)

**What goes wrong:** The new observation layer produces a different `FormField` shape, different `ff_id` tagging scheme, or different field_type taxonomy than what `fill_executor.py`, `fill_verify.py`, `verification_engine.py`, and the 20+ `fill_browser_scripts.py` JS snippets expect. Every fill strategy in `fill_executor.py` dispatches on `field.field_type` (text, select, radio-group, button-group, checkbox-group, etc.) and reads `field.ff_id` to target elements via `[data-ff-id="ff-0"]` selectors. Changing any of these silently breaks all fill paths.

**Why it happens:** The observation layer is being rebuilt from scratch, but the action layer is explicitly staying. The natural impulse is to design a "cleaner" data model. But `FormField` has 25+ fields, and downstream consumers reference most of them. The `fill_executor.py` alone imports `FormField` and reads `ff_id`, `field_type`, `selector`, `label`, `raw_label`, `name`, `value`, `options`, `choices`, `section`, `visible`, `is_native`, `is_multi_select`, `btn_ids`, `disabled`, `widget_kind`, `component_field_ids`, `has_calendar_trigger`, and `format_hint`.

**Consequences:** Every platform that currently works (Workday, Greenhouse, Lever, Oracle, SmartRecruiters, Phenom) breaks simultaneously. No way to test incrementally. Regression debugging becomes impossible because you can't tell if the bug is in observation or in the contract mismatch.

**Warning signs:**
- New observer produces fields that `fill_executor._fill_single_field` doesn't recognize
- `verification_engine.read_field_actual()` returns empty/wrong for fields the old extractor handles
- `data-ff-id` attributes stop appearing on elements

**Prevention:**
1. Define the observer contract as a strict superset of `FormField` in `ghosthands/dom/views.py` -- new fields are additive only
2. Write an adapter that converts new observation output to the existing `ExtractionResult` shape during migration
3. Run both old and new extractors in parallel (shadow mode) and diff their outputs before switching
4. Keep the `ff_id` tagging scheme (`data-ff-id="ff-N"`) -- the fill executor and all browser scripts depend on it
5. Never change the `field_type` enum values -- `fill_executor` switches on exact strings like "radio-group", "button-group", "checkbox-group"

**Detection:** Automated contract tests: given a known HTML fixture, assert that old extractor and new observer produce fields with identical `ff_id`, `field_type`, `label`, `section`, `options`, `choices` shapes.

---

### C2: Accessibility Tree Doesn't Encode What You Need

**Severity:** Critical -- will cause architectural rework if discovered late
**Phase:** Should be validated in Phase 1 (spike/proof-of-concept)

**What goes wrong:** Teams switch from DOM traversal to the accessibility tree (CDP `Accessibility.getFullAXTree`) expecting it to be a cleaner, more semantic data source. But the a11y tree has systematic gaps:

1. **Custom widgets without ARIA roles:** Workday's `data-uxi-widget-type="selectinput"` is not a standard ARIA role. The a11y tree may expose it as a generic "group" or "text" node, losing the fact that it's a select dropdown. The current `field_extractor.py` handles this explicitly (line 136: `el.getAttribute('data-uxi-widget-type') === 'selectinput'` maps to type "select"). The a11y tree won't.

2. **Button groups have no semantic role:** The current extractor runs a second JS pass (`_JS_DETECT_BUTTON_GROUPS`) that identifies 2-4 sibling buttons as a "button-group" question with choices. The a11y tree sees these as individual buttons -- it has no concept of "these buttons form a multiple-choice question."

3. **Section headings and field grouping:** The a11y tree provides a flat list of nodes with parent-child relationships, but it doesn't encode "this h2 heading is the section title for these 5 form fields below it." The current `getSection()` walks DOM ancestors looking for h1/h2/h3/legend. The a11y tree's hierarchy may not preserve this relationship.

4. **Value readback for custom selects:** Workday's "Select One" buttons show current selection in `textContent`. The a11y tree may or may not expose this as the node's `value` property -- it depends on whether Workday set `aria-label` or `aria-valuenow` correctly.

5. **Shadow DOM cross-root references:** As documented by Nolan Lawson and Alice Boxhall, `aria-labelledby` cannot reference elements across shadow root boundaries. The a11y tree inherits this limitation -- labels that work via visual proximity in the DOM may be absent in the a11y tree.

**Consequences:** You build the entire observation layer on the a11y tree, then discover it can't observe button groups, can't read Workday select values, and loses section context. Major architectural rework.

**Warning signs:**
- A11y tree returns "generic" or "group" role for elements that should be "combobox" or "listbox"
- Button-group questions appear as unrelated individual buttons
- Section headings are orphaned from their child fields

**Prevention:**
1. **Spike first:** Before committing to any architecture, run `Accessibility.getFullAXTree` on 3 real pages (Workday personal_info, Oracle application_form, Greenhouse single-page) and manually compare what the a11y tree sees vs. what `field_extractor.py` extracts. Document every gap.
2. **Hybrid approach from day one:** Plan for a11y tree as one signal source, not the only one. DOM queries for platform-specific patterns (`data-automation-id`, `data-uxi-widget-type`) must remain available.
3. **Don't discard the `_JS_DETECT_BUTTON_GROUPS` pass** -- it solves a problem the a11y tree fundamentally cannot solve (grouping sibling buttons into a question).

**Detection:** Build a comparison harness that runs both `extract_form_fields()` and the new observer on the same page and flags fields found by one but not the other.

---

### C3: Regression in Platforms That Currently Work

**Severity:** Critical -- will destroy user trust
**Phase:** Must be addressed before any observation code ships (Phase 1-2)

**What goes wrong:** The new observation layer is tested against Workday (the hardest platform) and declared ready. But Greenhouse, Lever, Oracle, SmartRecruiters, and Phenom each have unique quirks that the old extractor handles implicitly through its DOM traversal:

- **Greenhouse:** Uses `[data-question-id]` for custom questions, `#eeoc_fields` for EEO section, iframe-embedded forms
- **Oracle:** Uses `cx-select-pills` for single-choice, `role="grid"` for dropdown lists, `geo-hierarchy-form-element` for cascading address fields, `profile-inline-form` repeater tiles
- **Lever:** Standard HTML but with custom dropdowns that use non-standard class patterns
- **Phenom:** White-labeled domains with `ph-*` CSS classes, `data-ph-id` attributes
- **SmartRecruiters:** `c-spl-select-field` custom components, repeater sections behind "Add Another" buttons

The old extractor's `INTERACTIVE_SELECTOR` (in `shadow_helpers.py`) queries for `input, select, textarea, [role="textbox"], [role="combobox"], [role="listbox"], [role="checkbox"], [role="radio"], [role="switch"], [role="spinbutton"], [role="slider"], [role="searchbox"], [data-uxi-widget-type="selectinput"], [aria-haspopup="listbox"]`. Each of these was added because a specific platform needed it.

**Consequences:** Users applying on Greenhouse or Lever suddenly fail after months of working fine. Trust is destroyed. You can't ship "observation is better for Workday but broken for Greenhouse."

**Warning signs:**
- No end-to-end test suite for any platform
- "We'll test Greenhouse later" mentality
- New observer tested only on Workday pages

**Prevention:**
1. **Platform parity matrix:** Before starting, snapshot 1-2 real pages from each of the 6 platforms. Run old extractor, save the `ExtractionResult` JSON. This becomes the regression baseline.
2. **Gate shipping on parity:** New observer must produce equivalent or better results on ALL 6 platforms before replacing the old one for ANY platform.
3. **Platform-by-platform rollout:** Ship new observer for one platform at a time behind a flag. Old extractor remains the fallback.
4. **Snapshot tests, not live tests:** Save HTML snapshots of real ATS pages. Run both extractors against snapshots. Diff results. This is fast, deterministic, and doesn't require accounts.

**Detection:** CI job that runs both extractors on HTML fixtures and fails if the new observer misses fields that the old one finds.

---

### C4: getFullAXTree Performance on Workday-Scale Pages

**Severity:** Critical -- will cause timeouts and stuck agents
**Phase:** Should be validated in Phase 1 spike

**What goes wrong:** Workday pages can have 1000+ interactive elements across multiple shadow DOM trees. CDP's `Accessibility.getFullAXTree` returns the entire tree synchronously. On a page with 5000+ DOM nodes across shadow roots, this call can take 2-8 seconds. The current `field_extractor.py` uses `page.evaluate()` which runs in the page's JS context and is generally faster (500ms-2s for extraction + tagging). Adding a11y tree fetch on top of DOM extraction doubles the observation time per page.

For multi-page flows (Workday has 5-8 pages), this adds 10-40 seconds of pure observation overhead per application.

**Consequences:** Agent appears hung during observation. User sees no progress. Timeout limits are hit. Cost increases because the agent loop takes longer.

**Warning signs:**
- Observation taking >3 seconds on Workday personal_info page
- CDP connection timeouts during `getFullAXTree`
- Agent step count increasing because more time is spent observing

**Prevention:**
1. **Benchmark first:** Measure `getFullAXTree` on real Workday, Oracle, Greenhouse pages before committing to it as primary data source
2. **Depth-limited fetch:** CDP supports `max_depth` parameter -- use it to fetch only the interactive layer, not the full tree
3. **Cache aggressively:** A11y tree for a given page should be fetched once, not per-field
4. **Lazy enrichment:** Fetch minimal tree first, then enrich specific subtrees (e.g., a dropdown's options) only when needed for fill decisions
5. **Budget:** Set a hard 2-second budget for observation. If the primary method exceeds it, fall back to lightweight extraction.

**Detection:** Add timing instrumentation to observation calls. Alert if p95 exceeds 2 seconds.

---

## High-Severity Pitfalls

Mistakes that cause significant rework or recurring bugs.

---

### H1: Over-Relying on Screenshots for State Detection

**Severity:** High -- will cause cost spirals and latency
**Phase:** Should be constrained in Phase 2 (state detection design)

**What goes wrong:** The DOM can't tell you if a custom widget looks selected (Workday's "Select One" button may still say "Select One" in its textContent even after selection, until a framework-level re-render). The temptation is to take a screenshot after every action and ask a vision model "did this work?" This was explicitly rejected in PROJECT.md: "Strategic screenshots OK, per-action screenshots not."

Per-action screenshots at scale:
- 15-30 fields per page x 5-8 pages = 75-240 screenshots per application
- Each screenshot + vision model call = ~$0.01-0.03 + 2-3 seconds latency
- Total: $0.75-7.20 + 2.5-12 minutes of pure screenshot overhead per application

**Consequences:** Applications take 3-4x longer. Cost per application doubles or triples. Vision models hallucinate on screenshots (they misread dropdown text, confuse selected/unselected states on checkboxes with custom styling).

**Warning signs:**
- Cost per application increasing after observation layer change
- Agent taking screenshots for every field verification
- Vision model disagreeing with DOM readback (which do you trust?)

**Prevention:**
1. **Screenshots per-page only:** Take one screenshot per page load for orientation. Not per field.
2. **DOM readback for state:** The existing `verification_engine.py` works well for text fields, selects, checkboxes, radio groups. Keep it.
3. **Screenshot as escalation:** Only screenshot when DOM readback returns "unreadable" AND the field is required. This targets the 5-10% of fields that genuinely need visual confirmation.
4. **Never trust screenshot over DOM for text values:** If DOM says the field contains "John", the screenshot will also show "John". The screenshot adds nothing. Screenshots are useful only for: (a) widget visual state that DOM can't encode, (b) unexpected dialogs/overlays blocking the form.

**Detection:** Track screenshot count per application. Alert if >10 per application.

---

### H2: Grouping Heuristics That Over-Group or Under-Group

**Severity:** High -- will cause wrong answers and missed fields
**Phase:** Should be addressed in Phase 2 (semantic grouping)

**What goes wrong:** The current grouping logic in `_group_fields()` (field_extractor.py lines 673-744) groups radio/checkbox siblings by matching on `(field_type, label, section)`. This works when the DOM uses one label per question with child radio buttons. It fails when:

**Over-grouping (treating separate questions as one):**
- Two questions in the same section have identical labels (e.g., two "Yes/No" questions about different topics, both with label "Select one"). The grouper merges them into one radio-group with 4 options instead of two groups with 2 each.
- Workday pages where the section heading is the same for multiple question blocks.

**Under-grouping (splitting a question from its options):**
- Oracle `cx-select-pills`: The question text is in a preceding sibling div, and each pill button is a separate element. If the label resolver gives each pill the pill's own text instead of the question text, they won't group.
- Button groups where some buttons have `role="button"` and others are `<button>` elements -- the current grouper only looks at radio/checkbox, and button groups are handled by the separate `_JS_DETECT_BUTTON_GROUPS` pass.
- Greenhouse custom questions where the question text is in a `[data-question-id]` wrapper but the options are standard radio buttons inside a different wrapper.

**Consequences:** Over-grouped: LLM sees "Yes, No, Yes, No" as options and picks the wrong one, or two different questions get the same answer. Under-grouped: Required questions appear as standalone checkboxes, get wrong values, and the application fails validation.

**Warning signs:**
- Fields with label "Select one" or "Choose" appearing in extraction
- Radio-group with >4 choices when the page visually shows a Yes/No question
- Standalone radio buttons that should be part of a group

**Prevention:**
1. **Position-based grouping as secondary signal:** If two radio buttons have the same label AND are within 50px vertically, they're likely the same group. If they're 500px apart, they're probably different questions.
2. **Question-answer proximity model:** For each interactive element, walk up the DOM to find the nearest text node that looks like a question (ends with "?", contains "please select", etc.). Group by question, not by label.
3. **Never group across section boundaries:** If two fields have different `section` values, they should never be in the same group, even if labels match.
4. **Test with Oracle cx-select-pills specifically:** This is the hardest grouping case -- pills as buttons, question text as a sibling, no wrapping fieldset.

**Detection:** Log group sizes. Flag any group with >6 choices (suspicious) or groups where choices are identical (definite over-grouping).

---

### H3: Dynamic IDs and Stale Element References

**Severity:** High -- will cause intermittent failures
**Phase:** Should be addressed in Phase 1 (element identification strategy)

**What goes wrong:** The current system assigns `data-ff-id` tags (`ff-0`, `ff-1`, ...) during extraction by mutating the DOM. After SPA navigation or framework re-render (React, Angular), the DOM is replaced and all `data-ff-id` attributes vanish. The `ensure_helpers` function in `shadow_helpers.py` detects when `window.__ff` is gone and re-injects, but it can't restore the ff-id tags because the elements are new.

The `field_fingerprint` (MD5 of `field_type|label|name|section`) was added as a stable identity, but it's not used by the fill executor -- `fill_executor.py` exclusively uses `ff_id` to target elements via `[data-ff-id="ff-0"]` selectors.

**Specific scenarios where this breaks:**
- Workday SPA: clicking "Save and Continue" replaces the entire page content within the same URL
- React re-renders: Greenhouse's React form may unmount/remount components, destroying ff-id tags
- Oracle's stepper navigation: page content changes but URL stays the same
- Conditional fields: answering "Yes" to a question reveals new fields, which triggers a partial re-render that may destroy ff-ids on existing fields

**Consequences:** Fill executor targets `[data-ff-id="ff-3"]` but that element no longer exists. The fill silently fails or targets the wrong element (if ff-3 was reassigned to a different element after re-extraction).

**Warning signs:**
- `page.evaluate('[data-ff-id="ff-N"]')` returning null
- Fields that were extracted successfully but can't be filled
- ff_id counter resetting unexpectedly (the `_prevNextId` mechanism in shadow_helpers.py tries to prevent this but can fail)

**Prevention:**
1. **Re-extract after any page mutation:** If a fill action triggers a visible DOM change, re-extract before filling the next field. The observation layer should detect DOM changes (MutationObserver or fingerprint comparison) and invalidate cached extraction.
2. **Use field_fingerprint for identity, ff_id for targeting:** Match fields across extractions using fingerprint. Re-tag with ff_id after each extraction. Never assume ff_ids persist across extractions.
3. **Defensive targeting:** Before using ff_id to fill, verify the element still exists and its label matches what was extracted. If not, re-extract and re-resolve.
4. **Stable selectors as backup:** For platform-specific fields (Workday `data-automation-id`, Greenhouse `data-question-id`), store the platform selector alongside ff_id. Fall back to it if ff_id is stale.

**Detection:** Add pre-fill check: `element_exists(ff_id)`. If false, log and re-extract.

---

### H4: MutationObserver Flooding and Noise

**Severity:** High -- will cause performance degradation
**Phase:** Should be addressed in Phase 2 (state change detection)

**What goes wrong:** Installing a `MutationObserver` with `subtree: true, childList: true, attributes: true` on the document body of a Workday page produces hundreds of mutations per second during normal operation. Workday's framework continuously updates `aria-*` attributes, repositions tooltips, manages focus rings, and animates transitions. Oracle's framework similarly generates constant DOM churn.

The naive approach: "observe all mutations and re-extract when something changes" causes re-extraction on every keystroke, every focus change, every tooltip appearance.

**Consequences:** The observer fires so often that it becomes the bottleneck. CPU usage spikes. The agent can never get a stable observation because the page is always "changing." Debouncing helps but introduces its own timing problems (what if you debounce for 500ms but the important change happened 200ms ago and has already been overwritten?).

**Warning signs:**
- Console logs showing hundreds of mutation callbacks per second
- Observation results that change between consecutive extractions with no user action
- CPU usage spiking when MutationObserver is active

**Prevention:**
1. **Don't observe everything:** Only observe mutations on interactive elements (inputs, selects, buttons with ARIA roles). Ignore mutations on tooltip containers, animation wrappers, focus ring elements.
2. **Attribute whitelist:** Only care about `value`, `checked`, `aria-selected`, `aria-checked`, `aria-expanded`, `textContent` changes on interactive elements. Ignore `aria-activedescendant`, `aria-owns`, `style`, `class` changes.
3. **Debounce with settle detection:** After the last mutation, wait for 300ms of silence before considering the DOM "settled." If silence never comes (Workday keeps updating), force a read after 1 second max.
4. **Structural vs. value mutations:** Distinguish between structural changes (new elements added = need re-extraction) and value changes (existing element's value changed = just update the field state). Only re-extract on structural changes.
5. **Consider polling instead:** A simple 500ms poll that reads specific fields' values may be more reliable than MutationObserver for state tracking.

**Detection:** Count mutations per second. If >50/s sustained, the observer configuration is too broad.

---

### H5: LLM Hallucination from Oversized DOM Context

**Severity:** High -- will cause wrong field values
**Phase:** Should be addressed in Phase 2 (context construction)

**What goes wrong:** This is the #1 documented problem with the current system (PROJECT.md: "overwhelms the LLM (Gemini 3.0 Flash) with too much raw DOM context, causing hallucinations and wrong field values"). The new observation layer must solve this, not reproduce it.

Specific failure modes:
- **Similar labels:** A page has "Phone Number", "Mobile Phone Number", and "Home Phone Number". The LLM context includes all three with their ff_ids. The LLM picks the wrong one. This happens because the LLM sees a flat list of (ff_id, label, value) tuples and doesn't have spatial/visual context about which field it's looking at.
- **Too many options:** A Workday country dropdown has 200+ options. Including all of them in context wastes tokens and the LLM may pick a wrong but similar-sounding country.
- **Repeater confusion:** Oracle's experience page has 3 work experience entries, each with identical field labels (Company, Title, Start Date, End Date). The LLM can't tell which entry is which.
- **Cross-section bleed:** Fields from "Personal Info" section bleeding into "Application Questions" section in the context window.

**Consequences:** LLM fills wrong field. User gets someone else's phone number in the mobile field. Wrong country selected. Second work experience overwritten with first experience's data.

**Warning signs:**
- LLM choosing ff_id that doesn't match the intended field
- Correct value but wrong field (phone number in mobile field instead of home phone)
- Dropdown selections that are "close but wrong" (selecting "Philippines" instead of "United States")

**Prevention:**
1. **Section-scoped context:** Only send fields from the current section to the LLM. The observation layer should partition fields by section.
2. **Viewport-scoped context:** Only send fields that are visible in the current viewport. Hidden fields (below scroll, in collapsed sections) should be excluded.
3. **Prune dropdown options:** For dropdowns with >20 options, don't send the full list. Send the top 5-10 fuzzy matches against the expected value. The current `dropdown_match.py` already does this for fills -- apply the same principle to observation context.
4. **Disambiguate repeaters:** Each repeater entry should include its index ("Work Experience 1 of 3") and enough context to distinguish it from siblings.
5. **Budget tokens explicitly:** Observation context should never exceed 2000 tokens for a single page. If it does, the observation layer is sending too much.

**Detection:** Count tokens in observation context sent to LLM. Alert if >2000 per page.

---

### H6: Stale State After SPA Navigation

**Severity:** High -- will cause agent loops
**Phase:** Should be addressed in Phase 2 (state management)

**What goes wrong:** This is the documented failure mode from PROJECT.md: "the agent looping on already-completed actions because it can't observe that a selection was made." Specific scenarios:

1. **Workday page transitions:** After clicking "Save and Continue", Workday replaces page content but the URL may not change (or changes only the hash). The old observation data is stale. If the agent doesn't re-observe, it thinks fields from the previous page still need filling.

2. **Conditional field reveals:** On Oracle, selecting "Yes" for "Do you require sponsorship?" reveals additional fields (visa type, dates). The observation layer still has the old field list without these new fields. The agent doesn't know they exist.

3. **Dropdown selection confirmation:** On Workday, clicking a dropdown option triggers an async update. The `textContent` of the dropdown button changes from "Select One" to the selected value, but this happens 200-500ms after the click. If the observer reads immediately, it sees "Select One" and thinks the selection failed.

4. **Post-fill validation:** After filling a field and clicking away, server-side validation may revert the value or show an error. The observer cached the "filled" state but the DOM now shows an error state.

**Consequences:** Agent retypes into already-filled fields (causing "WuWuWu" duplication on Workday -- documented in guardrails). Agent skips newly revealed conditional fields. Agent loops retrying a dropdown that was actually selected successfully.

**Warning signs:**
- Agent attempting to fill fields that already have correct values
- Newly revealed conditional fields being skipped
- Fill verification returning "unreadable" immediately after fill

**Prevention:**
1. **Mandatory re-observation after navigation:** Any page transition (URL change, hash change, or DOM fingerprint change) must trigger full re-observation before the next action.
2. **Settle time after actions:** After any fill action, wait for DOM to settle (no mutations for 300ms or max 1 second) before reading state.
3. **Per-field state versioning:** Each field's observed state should have a timestamp. If the timestamp is >2 seconds old and the agent is about to act on it, re-observe that specific field first.
4. **The verification engine already handles this well:** `verification_engine.py` reads field values deterministically after fills. The new observation layer should integrate with it, not replace it.

**Detection:** Track time since last observation per field. Warn if acting on observation data >3 seconds old.

---

## Moderate Pitfalls

Mistakes that cause persistent bugs and wasted debugging time.

---

### M1: iframe Content Isolation

**Severity:** Moderate -- will cause missed fields on specific platforms
**Phase:** Should be addressed in Phase 2

**What goes wrong:** Greenhouse "Easy Apply" overlay is often rendered in an iframe. The main document's accessibility tree does not include iframe content -- you need a separate CDP call per iframe. The current `shadow_helpers.py` traverses shadow DOMs but does not traverse cross-origin iframes.

Greenhouse's `boards.greenhouse.io` form may embed in the employer's career site via iframe. Oracle can have nested iframes for cascading address selectors. Phenom's white-label sites sometimes embed application forms in iframes.

**Prevention:**
1. Detect iframes during observation using `DomService.max_iframes` / `max_iframe_depth` settings already in `browser_use/dom/service.py`
2. For each iframe, run a separate extraction pass
3. Merge iframe fields into the main field list with a scope prefix (e.g., `iframe:0:ff-5`)
4. The fill executor needs to know which execution context (main page vs iframe) to target

**Detection:** Log iframe count per page. If >0 and no iframe fields extracted, something is wrong.

---

### M2: Screenshot Timing vs. Async Operations

**Severity:** Moderate -- will cause misleading observation data
**Phase:** Should be addressed in Phase 2

**What goes wrong:** Taking a screenshot before an async operation completes produces a misleading image. Scenarios:
- Screenshot taken while a dropdown is still animating open -- vision model can't read the options
- Screenshot taken during page transition -- shows a loading spinner or blank page
- Screenshot taken before server-side validation returns -- field appears valid but will show error 500ms later
- Workday's "Select One" button updates its text asynchronously -- screenshot shows old text

**Prevention:**
1. Always wait for network idle + DOM settle before screenshotting
2. Use `page.waitForLoadState('networkidle')` before screenshot, with a 3-second timeout
3. Never screenshot during a fill action -- screenshot before the first fill and after the last fill on a page
4. If using screenshots for verification, wait at least 500ms after the last DOM mutation

**Detection:** Compare screenshot timestamp to last DOM mutation timestamp. If screenshot was taken within 200ms of a mutation, flag as potentially stale.

---

### M3: Workday Segmented Date Fields as Grouped Widgets

**Severity:** Moderate -- will cause date entry failures
**Phase:** Should be addressed in Phase 2 (widget detection)

**What goes wrong:** Workday dates use three separate `<input>` elements (MM, DD, YYYY) with `data-automation-id` selectors like `dateSectionMonth`, `dateSectionDay`, `dateSectionYear`. The current system has `widget_kind`, `component_field_ids`, `has_calendar_trigger`, and `format_hint` on FormField specifically for this. The a11y tree sees three separate text inputs with no semantic connection.

If the new observer treats these as three independent fields, the fill executor will try to fill each separately. But Workday's date input requires clicking the MM segment and typing all 8 digits continuously (documented in guardrails: "type the full date as continuous digits with NO slashes, e.g. '01152026'"). The segments auto-advance.

**Prevention:**
1. Preserve the `widget_kind: "grouped_date"` detection in the new observer
2. Use `data-automation-id` patterns to detect segmented dates: `dateSectionMonth` + `dateSectionDay` + `dateSectionYear` within the same parent
3. Expose `component_field_ids` so the fill executor knows which sub-fields belong to the group
4. Test explicitly on Workday date fields -- this is the most common Workday fill failure

**Detection:** If three text fields with date-related labels appear consecutively in the same section on a Workday page, they should be grouped. If they're not, the grouper missed them.

---

### M4: Oracle cx-select Opaque Values

**Severity:** Moderate -- will cause verification false negatives
**Phase:** Should be addressed in Phase 2

**What goes wrong:** Oracle's `cx-select` custom component stores the selected value as an internal UUID or numeric ID, not the display text. When `verification_engine.py` reads the field value, it gets something like `300000123456789` instead of "United States". The `is_value_opaque()` check in `verification_engine.py` already handles this (returns "unreadable"), but the new observer might try to use the a11y tree's `value` property for cx-select, which would also be opaque.

**Prevention:**
1. For Oracle cx-select fields, read the visual display text (the pill label or the input's displayed text), not the underlying value
2. The `verification_engine.py` pattern is correct -- treat opaque values as "unreadable" and fall back to visual confirmation
3. Don't change the verification engine's behavior for these fields

**Detection:** If an Oracle field's observed value matches `_OPAQUE_VALUE_RE` (hex IDs, UUIDs), the observer is reading the wrong attribute.

---

### M5: Custom Widget State in JavaScript Variables

**Severity:** Moderate -- will cause missed state changes
**Phase:** Should be addressed in Phase 2 (state detection)

**What goes wrong:** Some ATS platforms store widget state in JavaScript closures or framework state (React state, Angular scope) rather than DOM attributes. Examples:
- React Select: The selected value is in React's fiber tree, not always reflected in a DOM attribute
- Oracle's cascade selects: The parent-child relationship (Country > State > City) is managed in JS
- Workday's multi-select: Selected items are stored in a JS array; the DOM shows pills, but removing a pill updates the JS array first, DOM second

The DOM may show the correct state eventually (after React re-renders), but there's a window where JS state and DOM state disagree.

**Prevention:**
1. **Always read DOM state, never JS state.** JS state is framework-specific, version-dependent, and fragile. DOM is the canonical rendered output.
2. Accept that there will be a 100-500ms delay between action and DOM reflecting the state change
3. Use the existing settle-time approach: wait for DOM to stabilize before reading
4. For React components specifically: React batches state updates and re-renders asynchronously. After interacting with a React component, wait for `requestAnimationFrame` to complete before reading DOM.

**Detection:** If verification reads a field immediately after fill and gets "unreadable", but reading 500ms later gets the correct value, you have a timing issue, not an observation gap.

---

### M6: Testing Observation Changes Without E2E Infrastructure

**Severity:** Moderate -- will cause slow iteration and hidden regressions
**Phase:** Should be addressed in Phase 1 (test infrastructure)

**What goes wrong:** The current codebase has unit tests for specific DOM modules (`test_dropdown_match.py`, `test_dropdown_verify.py`, `test_fill_executor_platform.py`, etc.) but no end-to-end observation tests that run against real or saved ATS pages. Without these, every observation change requires manual testing on live ATS sites, which requires accounts, takes 5-10 minutes per platform, and is unreliable.

**Prevention:**
1. **Save HTML snapshots of real ATS pages:** Use Playwright to save `.mhtml` or `.html` snapshots of real Workday, Greenhouse, Oracle, Lever, SmartRecruiters, and Phenom pages. Store in `tests/fixtures/pages/`.
2. **Snapshot-based extraction tests:** Load saved HTML in Playwright, run old extractor, save expected output. New observer must produce equivalent output.
3. **Golden file tests:** Each fixture page has a `.expected.json` with the expected `ExtractionResult`. CI runs both extractors and diffs against golden files.
4. **Start collecting fixtures NOW, before writing any observation code.** Fixtures are the most valuable testing asset for this rebuild.

**Detection:** Track fixture coverage: how many platforms have at least one fixture page? How many page types per platform? Target: at least 2 fixtures per platform, covering the hardest page type.

---

### M7: Vision Model Disagreeing with DOM Data

**Severity:** Moderate -- will cause inconsistent behavior
**Phase:** Should be addressed in Phase 2 (conflict resolution strategy)

**What goes wrong:** When using a hybrid approach (DOM extraction + strategic screenshots), the vision model may say "the country field shows United States" while the DOM readback says the field value is empty (because the DOM hasn't updated yet, or the value is in a custom widget). Which source of truth wins?

If you always trust the screenshot: You'll skip re-filling fields that actually need it (screenshot shows old cached visual).
If you always trust the DOM: You'll retry fields that are actually filled (DOM readback is opaque/delayed).

**Prevention:**
1. **DOM is primary, screenshot is tiebreaker.** If DOM says "verified", trust it. If DOM says "unreadable", screenshot can confirm or deny.
2. **Never let screenshot override a DOM "mismatch" verdict.** If DOM says the value is "Canada" but you expected "United States", the screenshot can't resolve this -- the fill actually went wrong.
3. **Screenshot only resolves "unreadable" verdicts.** The three-way logic: DOM verified -> done. DOM mismatch -> retry. DOM unreadable -> screenshot check.
4. Document this hierarchy explicitly so it doesn't drift.

**Detection:** Track disagreement rate between DOM and screenshot verdicts. If >20%, something is systematically wrong with one data source.

---

## Minor Pitfalls

Issues that cause friction but have straightforward fixes.

---

### m1: Viewport Size Affecting Observation Results

**Severity:** Minor
**Phase:** Phase 2

**What goes wrong:** Elements below the fold are rendered but may have different computed styles (lazy-loaded images, intersection observer-triggered content). The `DomService` has a `viewport_threshold` parameter (default 1000px) that filters elements beyond this distance. Different viewport sizes produce different field lists.

**Prevention:** Always use a consistent viewport size (1280x1024 is standard for ATS testing). Document it. Don't rely on viewport-dependent observation for determinism.

---

### m2: Placeholder vs. Value Confusion

**Severity:** Minor
**Phase:** Phase 2

**What goes wrong:** The current `PLACEHOLDER_RE_SOURCE` pattern in `shadow_helpers.py` matches common placeholder texts ("Select...", "Choose one", "Start typing"). But some ATS platforms use actual valid values that look like placeholders (e.g., a field pre-filled with "Select One" as a real value, or a field where "None" is a valid selection but looks like a placeholder).

**Prevention:** The current pattern is well-tuned. Keep it. When the new observer encounters a value that matches the placeholder pattern, always check whether the field has `aria-selected="true"` or another confirmation of selection before treating it as empty.

---

### m3: Animation and Transition Interference

**Severity:** Minor
**Phase:** Phase 2

**What goes wrong:** Workday and Oracle use CSS transitions for field reveals, dropdown animations, and page transitions. During a 300ms transition, the element exists in the DOM but may have `opacity: 0` or `height: 0`, causing the visibility check to fail. The current `isVisible()` in `shadow_helpers.py` checks `display`, `visibility`, and `aria-hidden` but not `opacity` or transitioning state.

**Prevention:** Add opacity check to visibility detection. Treat `opacity < 0.1` as hidden. Wait for transitions to complete before extracting (use `transitionend` event or a fixed 400ms delay after detecting a transition).

---

## Phase-Specific Warnings

| Phase | Likely Pitfall | Severity | Mitigation |
|-------|---------------|----------|------------|
| Phase 1: Contract & Spike | C1: Breaking domhand_fill contract | Critical | Define adapter layer, keep FormField shape |
| Phase 1: Contract & Spike | C2: A11y tree gaps discovered late | Critical | Run spike on 3 real platforms before committing |
| Phase 1: Contract & Spike | C4: Performance of getFullAXTree | Critical | Benchmark on Workday before architecture decisions |
| Phase 1: Contract & Spike | M6: No test infrastructure | Moderate | Collect HTML fixtures before writing code |
| Phase 2: Core Observer | H2: Grouping heuristics wrong | High | Test with Oracle pills and Workday button groups |
| Phase 2: Core Observer | H4: MutationObserver flooding | High | Whitelist attributes, debounce with settle |
| Phase 2: Core Observer | H5: LLM context too large | High | Section-scope, viewport-scope, token budget |
| Phase 2: Core Observer | H6: Stale state after navigation | High | Mandatory re-observation after DOM fingerprint change |
| Phase 2: Core Observer | M3: Workday segmented dates | Moderate | Preserve widget_kind detection |
| Phase 3: Platform Parity | C3: Regression on working platforms | Critical | Parity matrix, platform-by-platform rollout |
| Phase 3: Platform Parity | M1: iframe content missed | Moderate | Per-iframe extraction pass |
| Phase 3: Platform Parity | M4: Oracle opaque values | Moderate | Read display text, not underlying value |
| Phase 4: Integration | H1: Screenshot cost spiral | High | Per-page only, escalation-based |
| Phase 4: Integration | H3: Dynamic IDs stale | High | Fingerprint for identity, ff_id for targeting |

---

## Sources

- Codebase analysis: `ghosthands/dom/field_extractor.py`, `shadow_helpers.py`, `label_resolver.py`, `option_discovery.py`, `verification_engine.py`, `fill_executor.py`, `fill_verify.py`, `validation_reader.py`
- Platform configs: `ghosthands/platforms/workday.py`, `greenhouse.py`, `lever.py`, `oracle.py`, `smartrecruiters.py`, `phenom.py`
- Project context: `.planning/PROJECT.md`
- [Chrome DevTools Protocol - Accessibility domain](https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/)
- [Shadow DOM and accessibility: the trouble with ARIA (Nolan Lawson)](https://nolanlawson.com/2022/11/28/shadow-dom-and-accessibility-the-trouble-with-aria/)
- [How Shadow DOM and accessibility are in conflict (Alice Boxhall, Igalia)](https://alice.pages.igalia.com/blog/how-shadow-dom-and-accessibility-are-in-conflict/)
- [Solving Cross-root ARIA Issues in Shadow DOM (Igalia)](https://blogs.igalia.com/mrego/solving-cross-root-aria-issues-in-shadow-dom/)
- [Context Window Limits: Why Your LLM Still Hallucinates](https://pr-peri.github.io/llm/2026/02/13/why-hallucination-happens.html)
- [Browser AI Automation with LLM Agents (deepsense.ai)](https://deepsense.ai/blog/browser-ai-automation-can-llms-really-handle-the-mundane-from-lunch-orders-to-complex-workflows/)
- [Not all automated testing tools support Shadow DOM (Manuel Matuzovic)](https://matuzo.at/blog/2024/automated-testing-tools-and-web-components)
- [MutationObserver - MDN Web Docs](https://developer.mozilla.org/en-US/docs/Web/API/MutationObserver)
