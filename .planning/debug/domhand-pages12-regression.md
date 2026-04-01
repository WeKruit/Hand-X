---
status: investigating
trigger: "DomHand pages 1-2 regression - fields not being filled that previously worked"
created: 2026-03-31T00:00:00Z
updated: 2026-03-31T00:02:00Z
---

## Current Focus

hypothesis: No page 1-2 fill regression exists in the current log. DomHand filled 13 fields on page 1 and 15 on page 2 with 0 failures. The perceived regression is likely either (a) the government question LLM answer being wrong (Yes instead of No), or (b) a different test run/form not captured in this log.
test: Present findings to user for clarification
expecting: User to confirm whether this log captures the issue, or to point to different evidence
next_action: Present diagnosis to user

## Symptoms

expected: DomHand fills fields on pages 1-2 directly via DOM manipulation (as it did in commits 819017f5 / 1cbdad09)
actual: DomHand is NOT filling fields on pages 1-2; browser-use agent fills them instead (wasting steps)
errors: No errors found in log for pages 1-2
reproduction: Run Hand-X against an Oracle application
started: After commits on snapshot/handx-03-26 branch diverged from 819017f5

## Eliminated

- hypothesis: DomHand is completely broken on pages 1-2
  evidence: Log shows page 1 filled 13/0/3/4 (filled/failed/already/skipped), page 2 filled 15/0/3. Both pages working.
  timestamp: 2026-03-31T00:01:00Z

- hypothesis: Placeholder regex change causes fields to be falsely marked as "already filled"
  evidence: views.py has ZERO diff between 819017f5 and HEAD. _is_effectively_unset_field_value unchanged.
  timestamp: 2026-03-31T00:01:00Z

- hypothesis: _completed_repeaters guard accidentally blocks page-1 fills
  evidence: Guard only fires when params.target_section matches a completed repeater section. Page 1-2 broad fills have no target_section.
  timestamp: 2026-03-31T00:01:00Z

- hypothesis: fill_resolution.py or fill_label_match.py changes broke triage
  evidence: Both files have ZERO diff between 819017f5 and HEAD.
  timestamp: 2026-03-31T00:01:00Z

- hypothesis: _already_correct_field_labels tracking changed field skip logic
  evidence: This is append-only tracking for agent summary text. Does not affect fields_seen or skip logic. Only adds to agent message.
  timestamp: 2026-03-31T00:02:00Z

- hypothesis: Agent summary format change confuses agent into re-filling fields
  evidence: Agent evaluated page 1 as "Success" and page 2 as "Success (partial)" (only partial because Name of Latest Employer was correctly flagged as needing manual fill). Agent did NOT re-fill DomHand-handled fields.
  timestamp: 2026-03-31T00:02:00Z

## Evidence

- timestamp: 2026-03-31T00:01:00Z
  checked: /tmp/handx-gs-apply.log DomHand fill events for ALL pages
  found: Page 1 (section 1): filled=13, failed=0, already=3, skipped=4. Page 2 (section 2): filled=15, failed=0, skipped=3. Page 3 (section 3): filled=13+1+1+1... (education+skills+languages). Page 4 (section 4): filled=3, failed=0.
  implication: DomHand IS filling all pages successfully. No regression visible in this log.

- timestamp: 2026-03-31T00:01:00Z
  checked: Agent manual actions after DomHand on pages 1-2
  found: Agent manually handled 3 things: (1) Typed employer name in search combobox (expected), (2) Called domhand_select for visa N/A which was already filled, (3) Fixed government question from Yes to No via domhand_interact_control.
  implication: Only #3 is a real issue (LLM answered government question wrong). #1 is expected. #2 is agent redundancy, not a DomHand issue.

- timestamp: 2026-03-31T00:01:00Z
  checked: git diff 819017f5..HEAD for 7 key files
  found: Only 4 files changed: domhand_fill.py, fill_executor.py, fill_llm_answers.py, dropdown_match.py. fill_resolution.py, fill_label_match.py, views.py are identical.
  implication: Core triage/matching/view logic is unchanged. Changes are in execution mechanics and LLM prompts.

- timestamp: 2026-03-31T00:02:00Z
  checked: fields_seen gate change (line 3230)
  found: OLD: skip if (key in fields_seen AND has_effective_value AND NOT has_validation_error). NEW: skip if (key in fields_seen). Change prevents round-2 retry of fields that failed in round 1.
  implication: POTENTIAL regression for forms where round-1 fills fail and need retry. But NOT triggered in this log (all round-1 fills succeeded on pages 1-2).

- timestamp: 2026-03-31T00:02:00Z
  checked: SCAN_VISIBLE_OPTIONS_JS viewport filter
  found: New filter: `if (rect.bottom < 0 || rect.top > vh) continue;` skips dropdown options outside viewport.
  implication: POTENTIAL regression for dropdowns with options below viewport fold. Not triggered in this log.

- timestamp: 2026-03-31T00:02:00Z
  checked: Government question LLM answer
  found: LLM answered "Yes" to "Do you hold a government position?" despite new prompt instruction to answer "No". Agent had to fix via domhand_interact_control.
  implication: LLM quality issue, not DomHand mechanics. The fill_llm_answers.py prompt update IS present but LLM ignored it.

## Resolution

root_cause: PENDING USER CLARIFICATION - The log at /tmp/handx-gs-apply.log does NOT show a page 1-2 regression. DomHand filled 13 fields on page 1 and 15 on page 2 with zero failures. Two potential subtle regressions were identified in the code diff but neither is triggered in this log.
fix:
verification:
files_changed: []
