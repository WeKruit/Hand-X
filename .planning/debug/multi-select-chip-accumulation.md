---
status: awaiting_human_verify
trigger: "_fill_multi_select blindly adds values without checking existing chips, causing stale accumulation across fill rounds"
created: 2026-04-01T00:00:00Z
updated: 2026-04-01T00:00:00Z
---

## Current Focus

hypothesis: CONFIRMED — _fill_multi_select at line 2815 has per-value dedup (lines 2827-2834) but NO pre-loop comparison of existing vs desired, NO removal of stale/wrong chips. It only skips adding a value that already exists as a chip — it never clears chips that should NOT be there.
test: n/a — root cause confirmed by code read
expecting: n/a
next_action: Implement fix — add check-before-act logic at the top of _fill_multi_select, before the per-value loop

## Symptoms

expected: Before filling a multi-select field, _fill_multi_select should: (1) Read existing chips (2) Compare existing vs desired — skip if match, clear wrong first then add correct, add normally if empty (3) For EEO single-answer fields: clear wrong chip and select right one
actual: Stale chip accumulation across fill rounds, wrong values not cleared, unnecessary re-entry of correct values
errors: No errors — function succeeds but produces wrong UI state (multiple chips when only one should be selected)
reproduction: Run domhand_fill on Greenhouse EEO fields. On first fill + verification failure + retry, second chip added instead of replacing first.
started: Always been the behavior — _fill_multi_select designed for true multi-select, doesn't handle single-answer-in-multi-select-widget case

## Eliminated

## Evidence

- timestamp: 2026-04-01T00:01:00Z
  checked: _fill_multi_select function (lines 2815-2879)
  found: Function enters per-value loop immediately. Only dedup is per-value: if val already in existing_tokens, skip it (line 2832). But it NEVER compares the full set of existing chips against the full set of desired values. It NEVER removes chips that are present but unwanted.
  implication: Confirmed root cause. Three specific gaps: (1) no stale chip removal before adding, (2) no early-return when existing chips already match desired set, (3) no mechanism to clear individual wrong chips on Greenhouse react-select.

- timestamp: 2026-04-01T00:02:00Z
  checked: _read_multi_select_selection (line 4311) and its JS (line 3916)
  found: Returns {tokens: [...], count: N, summary: "..."} — reliably reads chip text from react-select and Workday multi-select widgets. Already called inside the per-value loop (line 2824). Can be called BEFORE the loop for initial state.
  implication: Infrastructure to read existing chips already exists and works.

- timestamp: 2026-04-01T00:03:00Z
  checked: _TAG_WORKDAY_CHIP_DELETE_JS (line 3044) and _fill_workday_prompt_search chip removal (lines 3109-3140)
  found: Workday has a working pattern: JS tags the chip's x button with data-dh-chip-delete, then Playwright clicks it with locator. Loop runs up to 3 times to remove multiple chips. Greenhouse react-select uses different DOM — chips have [class*="multi-value"] with [class*="remove"] buttons, or a global clear-indicator button.
  implication: The Workday pattern is a reference but needs adaptation for react-select. For Greenhouse, need a new JS snippet that finds react-select multi-value remove buttons.

- timestamp: 2026-04-01T00:04:00Z
  checked: combobox_toggle.py isClearOrRemove helper (lines 29-33)
  found: There is already detection logic for clear/remove buttons: checks aria-label for 'clear'/'remove' and class for 'clear-indicator'. This is used to AVOID clicking them when opening menus.
  implication: The codebase already knows about these buttons — we just need to intentionally click them when clearing stale chips.

## Resolution

root_cause: _fill_multi_select (line 2815) has per-value dedup but no pre-loop set comparison and no stale chip removal. When fill rounds retry with different LLM answers, old chips remain and new ones stack on top.
fix: Add check-before-act block at top of _fill_multi_select: (1) read existing chips once, (2) compare existing set vs desired set, (3) if match -> early return, (4) if stale chips present -> remove them via per-chip x-button clicks (new JS snippet for react-select) or Workday pattern, (5) then proceed to add only missing values.
verification: Syntax valid. Ruff passes (no new warnings). Existing unit tests unaffected (pre-existing playwright import failures). Logic reviewed: early-return when matching, stale chip removal before add loop, only missing values iterated.
files_changed: [ghosthands/dom/fill_executor.py]
