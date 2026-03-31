# Phase 5: Observation + Anchor Detection - Context

**Gathered:** 2026-03-31
**Status:** Ready for planning
**Mode:** Auto-generated (discuss skipped — infrastructure phase)

<domain>
## Phase Boundary

The repeater system can observe pre-filled form fields per section and identify which entries already exist on the page before expanding any new rows. This uses existing `extract_visible_form_fields`, `_field_has_effective_value`, and `_section_matches_scope` from the codebase.

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion
All implementation choices are at Claude's discretion — pure infrastructure phase. Use ROADMAP phase goal, success criteria, and codebase conventions to guide decisions.

Key constraints from discussion:
- Anchor labels must be fuzzy-matched (normalize_name) against field labels, not exact string match
- Section scoping uses existing `_section_matches_scope` from `fill_label_match.py`
- `_field_has_effective_value` from `fill_executor.py` determines if a field has real content
- `ObservationResult` dataclass returns: existing_count, matched_profile_indices, unmatched_entries, page_anchor_values
- Anchor definitions per section: experience→company/employer, education→school/institution, languages→language, skills→skill, licenses→certification/license

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `extract_visible_form_fields(page)` — `ghosthands/dom/fill_executor.py:268` (delegates to `domhand_fill.py:2499`)
- `_field_has_effective_value(field)` — `ghosthands/dom/fill_executor.py:705`
- `_is_effectively_unset_field_value(value)` — `ghosthands/dom/fill_executor.py:195`
- `_section_matches_scope(section, scope)` — `ghosthands/dom/fill_label_match.py:733`
- `normalize_name(text)` — `ghosthands/actions/views.py`
- `FormField` model — `ghosthands/actions/views.py:9` (has `name`, `current_value`, `section`, `field_type`)

### Established Patterns
- Repeater code lives in `ghosthands/actions/domhand_fill_repeaters.py`
- Profile entries extracted via `_get_entries_for_section(profile_data, canonical, max_entries)`
- Section normalization via `_SECTION_ALIASES` dict
- Profile key mapping via `_PROFILE_KEY_MAP` dict

### Integration Points
- New `_observe_existing_entries()` function goes in `domhand_fill_repeaters.py`
- Called before `_COUNT_SAVED_TILES_JS` (line 414) — replaces it as primary detection
- Anchor definitions as module-level dicts (similar to `_SECTION_ALIASES`, `_PROFILE_KEY_MAP`)

</code_context>

<specifics>
## Specific Ideas

- Anchor label matching: normalize both field label and anchor label, check containment
- Profile anchor extraction: try keys in order (company → employer → organization), take first non-empty
- `ObservationResult` as a dataclass with `existing_count`, `matched_profile_indices`, `unmatched_entries`, `page_anchor_values`

</specifics>

<deferred>
## Deferred Ideas

- LLM matching logic (Phase 6)
- Repeater loop integration (Phase 7)
- Toy fixture testing (Phase 7)

</deferred>
