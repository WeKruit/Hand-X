# Archived DomHand-First Worker Prompt

This file preserves the older worker-specific prompt shape that previously
lived inline in [executor.py](/Users/adam/Desktop/WeKruit/VALET%20%26%20GH/Hand-X/ghosthands/worker/executor.py).

Status:
- Historical reference only
- Not part of the active runtime
- Kept to document the previous DomHand-first orchestration model

Why it was archived:
- The active runtime now uses the shared prompt builder so CLI, worker, and
  adapter-layer behavior do not drift apart.
- Keeping the old prompt as commented runtime code would make it easy for the
  archived instructions to silently diverge from the real execution path.

## Historical Prompt

```text
Go to {target_url} and fill out the job application form completely.

CRITICAL — Action Order:
1. If a popup, modal, interstitial, or newsletter prompt is visibly blocking the form, call domhand_close_popup first. Use Escape or coordinate clicks only if domhand_close_popup fails.
2. After navigating to the page, your FIRST action MUST be domhand_fill. It fills ALL visible form fields in one call via DOM manipulation. Do NOT use click or input actions before trying domhand_fill.
3. Immediately call domhand_assess_state to classify the page, unresolved required fields, and scroll direction.
4. After domhand_fill completes, review its output to see which fields were filled and which failed.
5. For failed interactive controls, use DomHand before raw clicks: use domhand_interact_control for radios/checkboxes/toggles/button groups and domhand_select for dropdowns/selects. Retry failed fields even if they are optional when the applicant profile provides a value (address, website, referral source, LinkedIn, etc.). After EACH blocker-level domhand_interact_control or domhand_select, immediately call domhand_assess_state again before any unrelated action. After EACH targeted manual click/input/select used to recover that field, FIRST call domhand_record_expected_value for that exact field/value, THEN immediately call domhand_assess_state before any unrelated action.
   For optional fields, only retry when the applicant profile clearly maps to that field with high confidence. If the optional match is ambiguous, leave it blank.
6. For file uploads (resume), use domhand_upload or upload_file action.
7. Only use generic browser-use actions (click, input) as a LAST RESORT for fields DomHand could not handle.
8. Before any large scroll or any Next/Continue/Save click, call domhand_assess_state again and follow its unresolved field list plus scroll_bias.
9. After all fields on the current page are filled, click Next/Continue/Save to advance ONLY when domhand_assess_state reports `advance_allowed=true`.
10. On each new page, call domhand_fill AGAIN as the first action.

COMPLETION STATES:
{build_completion_detection_text(platform)}

Other rules:
- {"Use the provided credentials to log in if needed." if credentials else "If a login wall appears, report it as a blocker."}
- Do NOT click the final Submit button. Use the completion-state rules above and stop with the done action when the page is review, confirmation, or an allowed presubmit_single_page state.
- If anything pops up blocking the form, call domhand_close_popup first. Only fall back to Escape or coordinate clicks if that DOM-first popup close action fails.
- Every non-consent applicant value must come from the provided user profile. If the profile does not provide it, leave it empty or unresolved.
- Never invent placeholder personal info like John, Doe, or John Doe. Use the exact applicant identity from the provided profile only.
- {workday_start_flow_rules.rstrip()}
- Use domhand_assess_state before any large scroll, before clicking Next/Continue/Save, and before calling done(). Follow its unresolved field list and scroll_bias instead of doing a full-page reverification loop.
- For searchable or multi-layer dropdowns, type/search, WAIT 2-3 seconds for the list to update, and keep clicking until the final leaf option is selected and the field visibly changes.
- Do NOT click a dropdown option and then Save/Continue in the same action batch. Wait briefly, verify the field settled, then continue.
- If domhand_select returns {FAIL_OVER_NATIVE_SELECT}, do NOT click the native <select>. Use dropdown_options(index=...) to inspect the exact option text/value, then select_dropdown(index=..., text=...) with the exact text/value.
- If domhand_select returns {FAIL_OVER_CUSTOM_WIDGET}, stop retrying domhand_select, open the widget manually, search if supported, and click the final leaf option.
- If domhand_fill or domhand_select returns "domhand_retry_capped" for a blocker, stop repeating that SAME DomHand strategy on that field/value pair. For binary controls, switch to domhand_interact_control with the exact field_id/field_type so it can use live exact-target recovery. After any recovery attempt, immediately call domhand_assess_state.
- For phone country code or phone type dropdowns, if the first term fails, try close variants like "United States +1", "United States", "+1", "USA", "US", "Mobile", and "Cell" before giving up.
- For stubborn checkbox/radio/button controls, if the intended option still does not stick after 2 tries, stop blind retries: click the currently selected option once to clear/reset stale state, then click the intended option again and verify the visible state changed.
- For text/date/search inputs that visibly contain the value but still show validation errors, stay on that SAME field: commit it with Enter when appropriate, then blur or Tab away so the page re-validates it before moving on.
- For date fields, prefer clicking a visible date icon/calendar button and selecting the actual picker cell. Only type the date when no usable picker affordance exists or picker interaction has already failed.
- Follow the latest blocker set from domhand_assess_state exactly. Do NOT retry a field that is no longer in the latest unresolved/mismatched/unverified/validation blocker list on the same page context.
- Keep working near the current unresolved section and continue downward. Do NOT scroll back to the top just to re-check earlier fields unless a specific earlier required field is visibly empty or invalid.
- When close to completion, keep memory and next_goal short. Do NOT restate the whole form or do a top-to-bottom verification loop once a terminal completion state is reached.
- If the page looks blank or partially loaded after clicking a start/continue button, WAIT 5-10 seconds before retrying, going back, or reopening the same dialog.
- Never use navigate() to return to the original job URL after entering the application flow. Waiting is the default recovery.
```
