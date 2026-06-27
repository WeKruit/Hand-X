# Deterministic ATS sweep — 115 live engineering jobs (fill-only)

Run: `sweep.py --urls runs/job_urls.json --profile fixtures/rich_profile.json --resume … `
(allow_escalation=**off** = the proof mode: ladder caps at L2, FAIL = pure deterministic gap).
Demo profile "Jordan Avery". **Nothing submitted.** Screenshots: `runs/sweep_full_ss/NNN.png`.

## Reliability + cost
- **115/115 jobs FILLED** — 0 blocked, 0 errors, 0 timeouts. Adapters + harness robust.
- **$0.299 total, $0.0026/job** avg (escalation off). ~45× cheaper than the browser-use agent
  path (~$0.11–0.15/job). Some Lever jobs cost **$0.0000** (no select/open-ended → no LLM map call).

## Coverage (success = all fields entered, incl. selects)
| ATS | jobs | FILLED | full-cov (FAIL=0) | cov% | avg FAIL/job | field-level filled | avg $ |
|-----|-----:|-------:|------------------:|-----:|-------------:|-------------------:|------:|
| Ashby | 38 | 38 | 13 | **34%** | 1.0 | 91% | 0.0024 |
| Greenhouse | 40 | 40 | 0 | **0%** | 3.0 | 83% | 0.0025 |
| Lever | 37 | 37 | 0 | **0%** | 1.0 | 91% | 0.0030 |
| **TOTAL** | **115** | **115** | **13** | **11%** | — | ~88% | **0.0026** |

83–91% of *fields* fill, but full-coverage (every field) is only 11% of jobs — nearly every job
has 1–3 unfilled fields.

## The gaps are concentrated (NOT the custom questions)
| FAIL field | count | widget |
|---|---:|---|
| resume / cover_letter | 79 | **file upload** |
| location / _systemfield_location | 61 | **geocomplete typeahead** |
| cover_letter_text | 37 | Lever cover-letter textarea |
| custom `question_*` | **3** | (essentially never fail) |

The LLM map + select/checkbox/standard-text handling is **solid** (custom questions fail ~0%).
The deterministic gaps are exactly the handoff's known-hard widgets: **file upload** (resume
uploads fine on Ashby/Lever but FAILs on all 40 Greenhouse jobs at L1/L2) and **location
geocomplete**. These are why full-coverage is low — not the questions.

## Param comparison (escalation off vs on)
- **off**: $0.0026/job, gaps stay unfilled → 11% full-coverage. (this sweep)
- **on**: ~$0.07/job (smoke) — the L3 single-field agent fills resume/cover/location, but **27×
  the cost** for the last ~3 fields.

## Highest-leverage next work
Add **deterministic routines** for the 2–3 gap widgets (Greenhouse resume upload, location
geocomplete, Lever cover-letter) → drives full-coverage toward 100% while holding ~$0.002/job —
the handoff's "add a routine THERE, drive escalation to zero" path. Do NOT default escalation on.

Note: Greenhouse URL set was affirm-skewed (~30/40), so its template diversity is limited.
