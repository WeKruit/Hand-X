# Feature Landscape: Browser Form Observation Layer

**Domain:** Browser automation observation for ATS job application forms
**Researched:** 2026-04-02
**Overall confidence:** HIGH (grounded in codebase analysis + real ATS platform experience encoded in existing code)

---

## Table Stakes

Features the observation layer must have or automation breaks. Missing any of these causes fill failures, loops, or wrong data.

### 1. Field Discovery

| Feature | Why Expected | Complexity | Dependencies | Notes |
|---------|-------------|------------|--------------|-------|
| Native HTML input discovery (text, email, tel, url, number, date, password, file, hidden) | These are the foundation of every form | Low | None | Already implemented in `field_extractor.py` via `INTERACTIVE_SELECTOR` |
| Native select/textarea discovery | Core form elements | Low | None | Already implemented |
| ARIA role-based widget discovery (combobox, listbox, textbox, checkbox, radio, switch, spinbutton, slider, searchbox) | ATS platforms (especially Workday, Oracle) use custom ARIA widgets instead of native elements | Med | Shadow DOM traversal | Already implemented but needs hardening — `INTERACTIVE_SELECTOR` covers these roles |
| Shadow DOM traversal | Workday uses shadow DOM for many form components; without traversal, fields are invisible | Med | Browser API | Already implemented via `shadow_helpers.py` `allRoots()` traversal |
| Label resolution chain | Without labels, the agent cannot match fields to profile data — label is the semantic key | High | DOM traversal, ARIA spec knowledge | Already implemented: aria-labelledby -> aria-label -> label[for] -> ancestor label/legend -> placeholder -> name -> title. Quality varies by platform |
| Current value reading | Must know what a field already contains to decide whether to fill it | Med | Field type awareness | Already implemented per-type: `.value`, `.checked`, `textContent`, `selectedIndex` |
| Required field detection | Must distinguish required from optional to prioritize and detect submission blockers | Med | Multi-signal analysis | Already implemented via html_required, aria_required, data_required, label_asterisk, label_required_text |
| Visibility detection | Invisible fields must not be filled — filling hidden fields causes validation errors or wrong data | Med | Computed style traversal | Already implemented via `isVisible()` — walks up the tree checking display/visibility/aria-hidden |
| Disabled state detection | Disabled fields must be skipped | Low | None | Already implemented via `el.disabled` + `aria-disabled` |
| Option enumeration for selects/combos | Must know available choices to match profile data to dropdown options | High | Dropdown opening, portal detection, virtualized list handling | Partially implemented in `option_discovery.py` — handles native select, aria-controls, Workday portals, React Select, MUI. Virtualized lists are a known gap |
| Field tagging for re-targeting | After observation, actions need stable selectors to target fields | Low | None | Already implemented via `data-ff-id` tagging |
| Page metadata collection (title, URL, form count, submit button presence) | Needed for page-level decisions: "am I on the right page?", "can I submit?" | Low | None | Already implemented in `_JS_PAGE_METADATA` |

### 2. Semantic Grouping

| Feature | Why Expected | Complexity | Dependencies | Notes |
|---------|-------------|------------|--------------|-------|
| Radio/checkbox sibling grouping | Individual radio buttons are meaningless without their group context — "Yes"/"No" must be associated with their question | Med | Label matching, section matching | Already implemented in `_group_fields()` — groups by same label + same section |
| Button group detection | Yes/No/Maybe button groups are functionally equivalent to radio groups but use `<button>` elements | High | Heuristic parent walking, question text extraction | Already implemented in `_JS_DETECT_BUTTON_GROUPS` — walks up 3 ancestors to find parent with 2-4 buttons, extracts question from preceding siblings |
| Section heading resolution | Fields must be associated with their section ("Personal Information", "Work Experience") for scoped filling | Med | DOM ancestor traversal | Already implemented via `getSection()` — walks up parents looking for h1-h3/legend |
| Question-answer association for DOM siblings | On many ATS platforms (Oracle, Greenhouse), the question text and answer options are DOM siblings, not parent-child. Without sibling association, questions have no labels | High | Heuristic sibling walking, text extraction with widget stripping | Partially implemented for button groups. The general case (question label is a sibling `<p>` or `<div>` before the input container) is a known weakness |

### 3. State Detection

| Feature | Why Expected | Complexity | Dependencies | Notes |
|---------|-------------|------------|--------------|-------|
| Fill verification (post-action readback) | Must confirm a fill action took effect. Without verification, the agent loops on fields it thinks are empty but actually filled | High | Per-type readback, normalization, fuzzy matching | Already implemented in `verification_engine.py` with two-axis model (execution_status x review_status). This is the strongest part of the current system |
| Checkbox/toggle binary state reading | Must distinguish checked from unchecked | Low | None | Already implemented via `_read_binary_state` |
| Select/combobox current selection reading | Must know what's currently selected in dropdowns | Med | Multi-strategy readback (textContent, aria-selected, value) | Already implemented but struggles with custom widgets where value is opaque (Oracle cx-select) |
| Validation error detection | Must detect inline and page-level validation errors to know whether a fill was accepted | Med | Error message DOM patterns, platform-specific selectors | Already implemented in `validation_reader.py` and `ValidationSnapshot` |
| Placeholder value recognition | Must distinguish "Select..." from an actual value | Low | Regex pattern matching | Already implemented via `PLACEHOLDER_RE` pattern |

### 4. Page Understanding

| Feature | Why Expected | Complexity | Dependencies | Notes |
|---------|-------------|------------|--------------|-------|
| Submit/Next button identification | Must know which button advances the form vs. which buttons are actions within sections | Med | Button text heuristics, position heuristics | Partially implemented — `_JS_PAGE_METADATA` checks for common submit texts. Not robust for "Save and Continue" vs "Save" within a repeater |
| Page fingerprinting for SPA transition detection | Multi-step forms often don't change URL. Must detect when page content has changed to re-observe | High | DOM hashing, headings+buttons+forms fingerprint | Already implemented at browser_use level via `page_fingerprint`. Uses headings + buttons + forms as signals |

### 5. Determinism

| Feature | Why Expected | Complexity | Dependencies | Notes |
|---------|-------------|------------|--------------|-------|
| Stable field identity across re-extractions | When re-observing after an action, must correlate new fields to previously seen fields | Med | Fingerprint generation | Already implemented via `field_fingerprint` (hash of type + label + name + section). Breaks when label changes dynamically |
| Idempotent observation (same page -> same output) | Non-deterministic observation causes the agent to oscillate between different interpretations of the same page | High | Deterministic traversal order, stable labeling, timing-independent readback | **This is the core problem.** Current observation is non-deterministic because: (1) textContent of custom widgets includes transient states, (2) DOM order isn't stable for dynamically rendered lists, (3) async-loaded options produce different observations depending on timing |

---

## Differentiators

Features that make observation significantly better than current state. Not strictly required for basic function but address the documented failure modes.

### 1. Semantic Understanding

| Feature | Value Proposition | Complexity | Dependencies | Notes |
|---------|-------------------|------------|--------------|-------|
| Canonical field type mapping | Map structural variations to meaning: Workday's `data-uxi-widget-type="selectinput"` and Greenhouse's React Select and Oracle's cx-select are all "dropdown-with-search". Reduces per-platform branching | High | Platform analysis, pattern library | Current code has scattered type resolution (`role === 'combobox'`, `data-uxi-widget-type`, `aria-haspopup`) but no canonical widget taxonomy |
| Confidence scoring per field | Not all observations are equally reliable. A field with `aria-labelledby` has HIGH confidence; a field with a synthetic label from `name` attribute has LOW confidence. Downstream can use this to decide when to proceed vs. flag for review | Med | Label source tracking (already exists), scoring rubric | `observation_warnings` and `label_sources` already exist. Need a numeric confidence score derived from them |
| Semantic section classification | Map section headings to canonical categories: "Personal Details" / "Basic Information" / "About You" all mean "personal_info". Enables generic logic per section type | Med | Fuzzy string matching, known heading patterns | `_SECTION_ALIASES` in repeater logic does this for repeater sections. Should be generalized to all sections |
| Question intent detection | Determine what a question is asking about (e.g., "Do you require visa sponsorship?" is about work_authorization, regardless of wording variations across ATS platforms) | High | LLM or pattern library | Currently handled by `fill_label_match.py` and `fill_profile_resolver.py` at fill time. Moving intent detection to observation would separate "what is this?" from "how do I fill it?" |

### 2. Advanced Grouping

| Feature | Value Proposition | Complexity | Dependencies | Notes |
|---------|-------------------|------------|--------------|-------|
| Generic repeater section detection | Detect add/remove/edit patterns for any repeatable section without platform-specific selectors. Currently uses `_COUNT_SAVED_TILES_JS` for Greenhouse tiles and heading patterns for Workday | High | Add button detection, container boundary detection, entry counting heuristics | `domhand_fill_repeaters.py` has platform-specific counting (Greenhouse tiles, Workday numbered headings). Generic detection would use structural patterns: "a container with N similar sub-containers, each with similar fields, plus an Add button" |
| Nested group handling | A radio group inside a repeater entry inside a conditional reveal. Current flat field list loses this hierarchy | High | Tree-structured observation model | Current `FormField` is flat with only a `section` string. No parent-child relationships between fields |
| Multi-select pill detection | Detecting selected items in multi-select widgets (Oracle pill groups, Greenhouse tag inputs) — what's already selected? | Med | Platform-specific DOM patterns | Partially implemented in `fill_verify.py` via `_uses_multi_select_observation`. Should be promoted to observation layer |
| Date component grouping | Workday/Oracle often use 3 separate inputs (month/day/year) for one date field. These must be grouped and treated as one logical field | Med | Sibling analysis, label association | Already partially handled via `component_field_ids` and `has_calendar_trigger` on FormField. Needs more robust detection |

### 3. Dynamic Content Handling

| Feature | Value Proposition | Complexity | Dependencies | Notes |
|---------|-------------------|------------|--------------|-------|
| Conditional reveal detection (post-action rescan) | After selecting "Yes" on "Do you require sponsorship?", new fields appear. Must detect and observe them | Med | Known-field-ID tracking, delta extraction | Already implemented as `_rescan_for_conditional_fields` in `domhand_fill.py`. Works but is reactive (rescan after every fill). A proactive approach would use MutationObserver to detect DOM additions |
| Async-loaded option waiting | Combobox options load asynchronously after typing. Observation must wait for options to stabilize before reporting | Med | Polling with timeout, stability detection | Currently handled ad-hoc in `dropdown_fill.py`. Should be formalized: "wait until option count stabilizes for N ms" |
| Loading state detection | Detect spinners, skeleton screens, "Loading..." text to know when the page is not yet ready for observation | Med | CSS animation detection, common spinner patterns | Not currently implemented. Observation can run while content is still loading, producing incomplete results |
| DOM stability waiting | After any interaction (click, type, select), wait for the DOM to stabilize before re-observing. Prevents reading transient states | Med | RequestAnimationFrame + MutationObserver debounce | Not systematically implemented. Current code uses `asyncio.sleep()` with hardcoded delays (0.3s, 0.5s, 0.8s) |

### 4. Cross-Platform Generality

| Feature | Value Proposition | Complexity | Dependencies | Notes |
|---------|-------------------|------------|--------------|-------|
| Platform detection heuristic | Detect which ATS platform we're on (Workday, Greenhouse, Lever, Oracle, SmartRecruiters, Phenom) to activate platform-specific observation tweaks | Low | URL pattern + DOM signature matching | Partially exists in `ghosthands/platforms/smartrecruiters.py`. Should be centralized |
| Widget fingerprint library | Known widget patterns mapped to canonical types. "I see `[data-automation-id='formField']` with a `[role='combobox']` child -> this is a Workday searchable dropdown" | Med | Platform analysis, pattern documentation | Scattered across field_extractor.py. Should be extracted into a declarative pattern registry |
| Fallback observation for unknown widgets | When the observer encounters a widget it doesn't recognize, produce a best-effort observation with LOW confidence rather than silently skip it | Med | Graceful degradation, warning system | Current code skips unknown widgets. `observation_warnings` exists but isn't used for truly unknown patterns |

### 5. Observation Quality

| Feature | Value Proposition | Complexity | Dependencies | Notes |
|---------|-------------------|------------|--------------|-------|
| Observation diff (what changed since last observation) | After an action, report only what changed — "field X went from empty to 'John'" — rather than re-dumping all fields. Reduces LLM context window consumption | Med | Previous observation caching, field fingerprint matching | Not implemented. Every observation dumps all fields. This is a major contributor to "overwhelming the LLM with too much raw DOM context" noted in PROJECT.md |
| Observation staleness detection | Detect when an observation is stale (page has changed since last observation but observer hasn't re-run) | Med | Page fingerprint comparison | Page fingerprint exists but isn't used to invalidate cached observations |
| Field completeness scoring | Per-page score: "17/20 fields filled, 2 required fields empty, 1 validation error" — gives agent a clear picture of page state without reading every field | Low | Aggregate existing per-field data | Components exist (verification counts) but not exposed as a first-class observation output |

---

## Anti-Features

Things the observation layer should deliberately NOT do.

| Anti-Feature | Why Avoid | What to Do Instead |
|--------------|-----------|-------------------|
| Per-action screenshots for observation | Screenshots are expensive (token cost + latency). PROJECT.md explicitly states: "Strategic screenshots OK, per-action screenshots not." Using screenshots as the primary observation mechanism (Skyvern approach) would blow through LLM budget | Use DOM-first observation. Reserve screenshots for per-page/per-section anchoring when DOM observation has LOW confidence |
| Full DOM serialization sent to LLM | Sending the raw DOM tree to the LLM causes hallucinations (documented failure mode). The DOM is noisy — style attributes, event handlers, decorative elements are irrelevant to form understanding | Extract structured field metadata (FormField model) and send only that. The LLM should see labels, types, values, options — not HTML |
| Platform-specific observation code paths | Writing separate extractors for Workday vs. Greenhouse vs. Oracle creates an N-platform maintenance burden. Every new ATS requires new code | Build generic patterns with a thin platform detection layer for tweaks. The widget fingerprint library approach: patterns are data, not code |
| LLM-based field type classification | Using an LLM to determine "is this a dropdown or a text input?" is slow and non-deterministic. Field type resolution must be instant and consistent | Use deterministic DOM analysis: ARIA roles, HTML element types, widget attribute patterns. LLM should only be used for semantic questions ("what is this field asking about?"), never structural questions ("what type of widget is this?") |
| Observing inside open dropdown portals as form fields | Dropdown options rendered in portals (Workday's `activeListContainer`, React Select menus) are not form fields. If they're observed as fields, the agent tries to "fill" them | Skip elements inside known portal containers during field discovery. This is already implemented in `shouldSkip()` but must remain vigilant as new portal patterns appear |
| Tracking mouse position or viewport scroll state | Observable but useless for form understanding. Adds noise to observation without aiding fill decisions | Track only DOM state: field values, visibility, section, page identity |
| Attempting to observe fields in iframes from different origins | Cross-origin iframes are security-sandboxed. Attempting to traverse them will fail silently or throw errors | Detect cross-origin iframes, log a warning, and report them as "inaccessible_iframe" zones. Same-origin iframes should be traversed |
| Real-time continuous observation (MutationObserver always-on) | Always-on DOM watching creates performance overhead and generates noise from irrelevant mutations (animations, counters, third-party scripts) | Use point-in-time observation triggered by the agent at decision points. MutationObserver should only be used for targeted monitoring: "watch this specific container for child additions after I click this button" |

---

## Feature Dependencies

```
Label Resolution -> Field Discovery (fields need labels)
Shadow DOM Traversal -> Field Discovery (custom widgets live in shadow DOM)
Field Discovery -> Semantic Grouping (can't group what you haven't found)
Field Discovery -> State Detection (can't read state of unknown fields)
Field Tagging -> Fill Verification (verification needs stable selectors)
Section Heading Resolution -> Repeater Detection (repeaters are section-scoped)
Radio/Checkbox Grouping -> Button Group Detection (same pattern, different elements)
Visibility Detection -> Field Discovery (invisible fields must be filtered)
Page Fingerprinting -> SPA Transition Detection -> Re-observation trigger
Field Discovery + State Detection -> Observation Diff (need both current and previous)
Platform Detection -> Widget Fingerprint Library (patterns are platform-specific)
Confidence Scoring -> Observation Warnings (warnings feed confidence)
```

---

## MVP Observation Features (v2.0 Core)

Prioritize these features for the observation rebuild. Ordered by dependency chain and impact.

### Must Ship

1. **Deterministic field discovery with stable ordering** — The core problem. Same page must produce the same field list every time. Requires sorting fields by DOM position (document order), not by discovery order.

2. **Improved label resolution with confidence tracking** — Extend label chain to handle sibling-based labels (question text in preceding DOM element). Assign numeric confidence per field based on label source quality.

3. **Canonical widget taxonomy** — Replace scattered type resolution with a declarative pattern registry. Input: DOM attributes. Output: canonical type + confidence. Handles Workday selectinput, Oracle cx-select, React Select, native HTML, etc.

4. **Structured observation output with diff support** — Output a page-level observation object that includes field list, page identity, completeness score, and observation metadata. Support diffing against previous observation to produce "what changed" for the LLM.

5. **DOM stability waiting** — Before any observation, wait for DOM to stabilize (no mutations for 200ms after last change). Replaces hardcoded `asyncio.sleep()` calls.

6. **Loading state detection** — Detect and wait through loading states before observing. Prevents incomplete observations.

### Defer to v2.1+

- Generic repeater detection (complex, current platform-specific approach works)
- Nested group hierarchy (requires model change, current flat list is functional)
- Question intent detection (valuable but LLM-dependent, can be added after core observation is solid)
- Proactive MutationObserver for conditional reveals (reactive rescan works, just slower)

---

## Cross-Platform Variation Analysis

Based on the existing codebase's platform-specific logic, here is what varies across ATS platforms and what observation must handle.

### What Varies (observation must be generic across these)

| Dimension | Workday | Greenhouse/Lever | Oracle HCM | SmartRecruiters | Phenom |
|-----------|---------|-------------------|------------|-----------------|--------|
| **Widget system** | Custom `data-uxi-widget-type`, `data-automation-id` | React components, standard HTML with CSS classes | JET custom elements (`oj-*`), `display:contents` | Standard HTML + React | React + custom |
| **Select implementation** | `activeListContainer` portal, virtualized lists, hierarchical with chevrons | React Select, standard `<select>` | cx-select custom elements, opaque values | Standard `<select>`, custom dropdowns | React Select variants |
| **Shadow DOM usage** | Yes (inner widgets) | Minimal | Yes (JET components) | Minimal | Minimal |
| **Label mechanism** | `data-automation-id="fieldLabel"`, `aria-labelledby` pointing to label elements | `label[for]`, direct label association | `aria-labelledby`, sometimes missing labels | `label[for]`, ARIA | `label[for]`, ARIA |
| **Section structure** | `data-automation-id="sectionHeader"`, heading elements | CSS class-based sections, `fieldset` | heading elements, custom section containers | heading elements | heading elements |
| **Repeater pattern** | Numbered headings ("Education 1"), inline forms | Saved tile badges (`profile-inline-form`), tile-based UI | Section-specific "Add" buttons | Similar to Greenhouse | Similar to Greenhouse |
| **Date input** | Segmented (month/day/year separate inputs) | Usually single date picker | Segmented or calendar widget | Single date picker | Single date picker |
| **Required indicator** | `aria-required`, `data-required`, label asterisk | `aria-required`, label asterisk | `aria-required`, label text | HTML `required`, label asterisk | `aria-required` |
| **Validation display** | Inline errors with `data-automation-id`, summary banners | Inline `.error` class elements | Inline error messages | Inline errors | Inline errors |
| **Button identification** | `data-automation-id="bottom-navigation-next-button"` | Button text heuristics | Button text, sometimes custom elements with `display:contents` | Button text | Button text |

### What's Consistent (build generic logic around these)

| Pattern | Consistency | Reliability |
|---------|-------------|-------------|
| ARIA roles for interactive elements | High — all modern ATS platforms use ARIA | HIGH confidence |
| `aria-required="true"` for required fields | High — WAI-ARIA standard | HIGH confidence |
| `aria-labelledby` / `aria-label` for labels | Medium — present but quality varies | MEDIUM confidence |
| Heading elements (h1-h4) for section structure | High — semantic HTML pattern | HIGH confidence |
| Button text for submit/next identification | High — always present, just varies in wording | MEDIUM confidence (fuzzy matching needed) |
| `role="option"` inside `role="listbox"` for dropdown options | High — ARIA pattern | HIGH confidence |
| `aria-selected="true"` for selected options | Medium — present but not always on the right element | MEDIUM confidence |
| `aria-hidden="true"` for invisible elements | Medium — used but not universally | MEDIUM confidence |
| CSS `display:none` / `visibility:hidden` for hidden elements | High — standard CSS | HIGH confidence |
| `placeholder` attribute for hint text | High — standard HTML | HIGH confidence |

---

## Observation Warnings Taxonomy

Warnings the observer should attach to fields for downstream decision-making.

### Critical (downstream should flag for human review)

| Warning | Meaning | When |
|---------|---------|------|
| `no_label_resolved` | Field has no accessible name — agent cannot match to profile | Label chain exhausted with no result |
| `opaque_value` | Current value is an internal widget ID, not human-readable | Value matches UUID/hex pattern |
| `ambiguous_type` | Widget type could not be confidently determined | Multiple conflicting signals (e.g., role="textbox" but has options) |

### Important (downstream should use cautiously)

| Warning | Meaning | When |
|---------|---------|------|
| `synthetic_label` | Label derived from name/data-attribute, not from accessible name chain | Only name attribute or automation-id available |
| `label_from_placeholder` | Label is the placeholder text, which may disappear after filling | Only label source is placeholder |
| `stale_options` | Options were read from inline DOM, but dropdown may have more options that load on open | Options present in DOM before interaction |
| `sibling_label_heuristic` | Label was resolved by walking siblings, not standard label association | Sibling text extraction heuristic used |

### Informational (useful for debugging, not actionable)

| Warning | Meaning | When |
|---------|---------|------|
| `disabled` | Field is disabled (may become enabled after another field is filled) | `disabled` or `aria-disabled="true"` |
| `native_select_no_options` | Native select element has no non-empty options | Empty select (may load options dynamically) |
| `group_no_choices` | Radio/checkbox group detected but no choice labels found | Group structure found but labels missing |
| `deep_shadow_dom` | Field is nested 2+ shadow DOM levels deep | Multiple shadow root traversals needed |

---

## Sources

- Codebase analysis: `ghosthands/dom/field_extractor.py`, `views.py`, `verification_engine.py`, `shadow_helpers.py`, `option_discovery.py`, `label_resolver.py`, `domhand_fill_repeaters.py`
- [WAI-ARIA Roles - MDN Web Docs](https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Reference/Roles) — comprehensive ARIA role reference
- [Workday Canvas Form Field component](https://canvas.workday.com/components/inputs/form-field) — Workday's design system for form widgets
- [How Skyvern Reads and Understands the Web](https://www.skyvern.com/blog/how-skyvern-reads-and-understands-the-web/) — competitor approach (vision-first, not DOM-first)
- [MutationObserver - MDN Web Docs](https://developer.mozilla.org/en-US/docs/Web/API/MutationObserver) — DOM change detection API
- [Accessibility Tree - Chrome DevTools](https://developer.chrome.com/blog/full-accessibility-tree) — browser accessibility tree internals
- [browser-use/browser-use issue #3972](https://github.com/browser-use/browser-use/issues/3972) — page load waiting problems in browser-use
- PROJECT.md key decisions: "Strategic screenshots OK, per-action screenshots not", "Action layer stays, observation is replaced"
