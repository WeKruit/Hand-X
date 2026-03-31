---
status: investigating
trigger: "Research why browser-use Gemini agent keeps trying to add skills AFTER domhand_fill_repeaters returns with Skills section is COMPLETE"
created: 2026-03-31T00:00:00Z
updated: 2026-03-31T00:00:01Z
---

## Current Focus

hypothesis: TWO compounding root causes: (1) Cancel button likely fails on Oracle Skills because Oracle's inline skill editor uses different button text or getBoundingClientRect returns zero, and (2) the COMPLETE message is only visible for ONE step via read_state then vanishes, while the open form persists in the visible DOM
test: Code analysis of cancel path + message persistence
expecting: Confirm cancel fails silently (exception swallowed) AND message is ephemeral
next_action: Document findings

## Symptoms

expected: After domhand_fill_repeaters returns "Skills section is COMPLETE", the agent should move on to the next section
actual: Agent calls domhand_fill target_section='Technical Skills' again at Step 23, then keeps trying domhand_select and manual input on Skill Type, appending junk text "TechnicaProgramming LanguageProgramminLanguages"
errors: 3 consecutive taxonomy misses trigger skill_early_termination, but form stays open
reproduction: Run Oracle application with skills section containing items not in Oracle taxonomy
started: Recent — after early termination logic was added

## Eliminated

## Evidence

- timestamp: 2026-03-31T00:00:01Z
  checked: Early termination code path (lines 544-561)
  found: On early termination, code does (1) _CLEAR_COMBOBOX_TEXT_JS, (2) _cdp_click_cancel(page), (3) break. The entire cleanup is wrapped in try/except Exception: pass -- any failure is silently swallowed.
  implication: If cancel fails, code still breaks out of the loop and returns "COMPLETE", but the skill form remains OPEN on the page.

- timestamp: 2026-03-31T00:00:02Z
  checked: _FIND_CANCEL_BUTTON_JS (lines 111-130)
  found: Searches for visible buttons with text exactly equal to 'cancel', 'discard', or 'remove' (case-insensitive). On Oracle HCM skill forms, the cancel/close mechanism may be an icon button (X), or a link-styled element, or the button text may include whitespace or extra text (e.g. "Cancel " with trailing space, or nested span). The getBoundingClientRect can also return zero on Oracle overlay elements (the save button JS already has a fallback for this -- see lines 150-158 -- but _FIND_CANCEL_BUTTON_JS has NO such fallback).
  implication: High probability cancel button is not found OR found but click coordinates are (0,0). Either way, _cdp_click_cancel returns False but exception is swallowed.

- timestamp: 2026-03-31T00:00:03Z
  checked: _cdp_click_cancel (lines 242-255)
  found: If _FIND_CANCEL_BUTTON_JS returns {found: false}, the function simply returns False. If found but coords are 0,0 (Oracle overlay getBoundingClientRect issue), it clicks at (0,0) which hits the top-left corner of the page, not the cancel button. No logging on failure.
  implication: Silent failure. No retry, no fallback, no log entry. The skill form stays open.

- timestamp: 2026-03-31T00:00:04Z
  checked: ActionResult message persistence mechanism
  found: domhand_fill_repeaters returns with include_extracted_content_only_once=True and metadata.tool="domhand_fill_repeaters". This tool is NOT in _READ_STATE_SUPPRESSED_TOOLS (only "domhand_fill" and "domhand_assess_state" are). So the "COMPLETE -- do NOT call domhand_fill" message goes into read_state_description. BUT read_state_description is CLEARED at the start of every step (line 363: self.state.read_state_description = ''). It's only visible for the NEXT step.
  implication: The COMPLETE warning is visible for exactly ONE agent step, then vanishes from context. If the agent doesn't act on it in that single step, the warning is gone forever.

- timestamp: 2026-03-31T00:00:05Z
  checked: domhand_fill_repeaters does NOT set long_term_memory
  found: The ActionResult has no long_term_memory field. With include_extracted_content_only_once=True and no long_term_memory, the message goes ONLY to read_state (ephemeral, 1 step). It does NOT go to the persistent action_results history that the agent sees on every subsequent step.
  implication: This is the critical messaging gap. Compare with line 391-396 in message_manager/service.py: if long_term_memory is set, it goes to action_results (persistent). If not, and include_extracted_content_only_once is True, extracted_content goes ONLY to read_state (ephemeral). The "COMPLETE" message vanishes after 1 step.

- timestamp: 2026-03-31T00:00:06Z
  checked: Diff with old version at 819017f5
  found: The old code at 819017f5 had NO early termination logic at all. No skills_taxonomy_miss_labels, no consecutive_skill_misses, no _MAX_CONSECUTIVE_SKILL_MISSES, no _CLEAR_COMBOBOX_TEXT_JS, no _FIND_CANCEL_BUTTON_JS, no _cdp_click_cancel. It tried all skills and just reported results. The current early termination is new code that was added to optimize, but the cleanup path has these two bugs.
  implication: The early termination feature is correctly detecting when to stop, but its two supporting mechanisms (cancel the form, tell the agent to stop) both fail.

- timestamp: 2026-03-31T00:00:07Z
  checked: What the agent sees after early termination
  found: (1) The skill form is still open with a combobox (Skill field) visible. (2) The "COMPLETE" message appears in <read_state> for one step, then disappears. (3) On the next step, the agent sees the open form with unfilled fields in the browser state, no memory of the COMPLETE instruction, and naturally tries to fill the visible skill form. (4) The junk text "TechnicaProgramming LanguageProgramminLanguages" is the agent typing into the Skill combobox that still has stale text from the failed clear.
  implication: The agent is behaving rationally given what it can see. It has an open form, no persistent instruction not to fill it, so it tries.

## Resolution

root_cause: Two compounding bugs in the skill_early_termination path:

**Bug 1 (DOM): Cancel button click silently fails on Oracle.**
_FIND_CANCEL_BUTTON_JS has no fallback for zero-sized getBoundingClientRect (Oracle overlay issue). The _FIND_SAVE_BUTTON_JS already has this fallback (lines 150-158 with offsetWidth/offsetHeight + scrollIntoView), but _FIND_CANCEL_BUTTON_JS does not. Additionally, Oracle's "Cancel" button may not have exact textContent="cancel" -- it could be inside a nested span or styled differently. The entire cancel path is wrapped in try/except pass with no logging, so failure is invisible. Result: the skill form stays open after early termination.

**Bug 2 (Agent memory): "COMPLETE" message is ephemeral, not persistent.**
domhand_fill_repeaters returns with include_extracted_content_only_once=True but sets NO long_term_memory. Per browser-use's message_manager logic (service.py lines 373-396): if include_extracted_content_only_once=True and no long_term_memory, the extracted_content goes ONLY to read_state_description which is cleared at the start of every step. The "do NOT call domhand_fill on this section again" warning is visible for exactly ONE step, then vanishes. The agent has no persistent memory that skills are complete.

**Combined effect:** The agent sees an open skill form (Bug 1) with no memory of being told to stop (Bug 2), so it rationally re-enters the skill section and types junk into the combobox.

fix: (not applied -- research only)
verification:
files_changed: []
