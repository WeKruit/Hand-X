# EVAL & BOUNDARY DEFINITION — what "we can fill it" actually means

Written 2026-07-03 after the user (correctly) had zero confidence in the fill numbers.
The problem was the METRIC, not just the fills. This pins it down.

## The three metrics — only ONE is trustworthy

| metric | formula | verdict |
|---|---|---|
| `fill_rate` | filled / **discovered** | **LIES.** Ignores sections discovery never saw. DEAD — do not report. |
| `coverage` | filled / **planner_expected** | **NOISY.** Planner denominator swings (bamboohr expected 10, rippling 24→35 on the same form); `filled` can exceed `expected` → capped at 1.0. Directional only, NEVER a completion claim. |
| **`complete`** | every REQUIRED field answered, confirmed by **DOM audit AND visual** | **THE metric.** Binary per form. This is "could a human hit Submit with no error." |

**Headline number = COMPLETE-RATE** = fraction of reached forms where `complete == True`.

## The honest baseline (measured, 7 platforms, n=14)

| platform | coverage (noisy) | **COMPLETE (real)** | what's missing |
|---|---|---|---|
| comeet | 85% | **2/2 ✅** | — |
| bamboohr | 100% | 0/2 | Resume not uploaded; "Who referred you" |
| hibob | 93% | 0/2 | Country dropdown; Experience Start date |
| teamtailor | 100% | 0/2 | "Nom*" (localized required field) |
| breezy | 51% | 0/2 | Address; 2nd experience row |
| workable | 59% | 0/2 | react-select ratings; screening |
| rippling | 22% | 0/2 | Experience section barely fills |
| **TOTAL** | 66% | **2/14 = 14%** | |

**We can COMPLETELY fill 14% of forms today.** Coverage 66% was the lie that gave false comfort.

## BOUNDARY — the definition of COMPLETE (fill-only, never submit)

A form is COMPLETE iff a human opening it would find NOTHING required left to do:
1. every required flat field has a value (name/email/phone/location/…)
2. the resume/CV is uploaded
3. every required screening question answered (work-auth, eligibility, …)
4. every required rating/dropdown has a selection
5. every repeater section the profile applies to has its entries (Experience, Education)
6. localized required fields count too (Nom*, Requis, *)

Verified by: DOM audit (required inputs + selects + radio/checkbox groups) + a full-page VLM
"any required field still empty?" + a screenshot on file. NOT a coverage %. Binary.

## What "we can do every ATS generically" means (the boundary of the CLAIM)

- IN SCOPE (must be COMPLETE-able, no account): the no-auth Tier-1 ATSs. Target: complete-rate 90%+.
- OUT (separate tracks): account-gated (Workday/avature auth) → HITL/credential; anti-bot
  (SmartRecruiters slider) → HITL; dead postings → correct decline.
- WORKDAY HONEST NUMBER (audited 2026-07-03): 6/22 tenants (27%) genuinely fill the multi-page
  wizard to Review (5-7 steps each, review screenshots verified). The earlier "11/22" was inflated
  by an is_review bug — a page with NO progressBar defaulted to index=1,total=1 and any sign-in
  wall counted as Review (signature: steps=1, cost=$0.0000). Fixed (ats_workday, total>1 required).
  CLAIM GATE: any REACHED/FILLED claim needs steps>=2 AND cost>0 AND an eyeballed review screenshot.
- The generic engine has ONE path (planner → discover DOM+VLM → map → observe_act → audit),
  zero per-ATS code. A new ATS's failure lands in observe / action / commit and is fixed with a
  GENERIC mechanism (so it converges across platforms, not per-patch).

## The gap right now = the LAST required field, per form

Each form fills MOST fields but misses 1-2 REQUIRED ones. Because COMPLETE needs ALL, one miss
= not complete. The recurring misses, ranked:
1. **Resume upload** (bamboohr, hibob "Add file") — the file affordance not fired
2. **Country / location dropdowns** (hibob) — searchable select not committed
3. **Date format** in repeater rows (hibob Experience start date: dd-mm-yyyy)
4. **react-select ratings** (workable) — commit lands where DOM read-back can't see
5. **2nd/3rd repeater rows** (breezy, rippling) — Add-after-save re-locate
6. **localized required** (teamtailor Nom*) — map/audit must handle non-English labels

Drive COMPLETE-RATE (not coverage) to 90% by closing these 6 recurring misses generically.

## Progress log

2026-07-03 (commit a8b4ca124): workable oracle form missing 7 -> 0, complete:True, screenshot-
verified (runs/newats/wk18.png). Generic mechanisms landed: TIER-0 dom-ref locate (identity
before similarity), identity-rescroll (off-viewport ref beats a weak/structure bind),
cdp_choose_aria_option (ARIA combobox family: self-open + aria-owns listbox + innerText option
click), cdp_choose_option per-id aria-labelledby matching + identity-scoped group by name,
ALREADY-CORRECT pre-check on CHOICE/SELECT lanes (a prefilled-correct widget is never touched —
touching flipped +1 to +44). Misses #4 (ratings) closed; #2 (country/select commit) mechanism
now exists — re-measure pending.
