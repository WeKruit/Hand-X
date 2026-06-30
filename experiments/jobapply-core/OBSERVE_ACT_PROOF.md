# observe_act — Generic Fill PROOF

> **Fill-only. NEVER submitted.** Single-page ATS (Greenhouse / Lever / Ashby) — no auth, no rate
> limit. Each run = one **live** company posting × one **synthetic throwaway** profile, filled
> field-by-field via the generic `observe_act` state machine (NOT the per-archetype
> `fill_with_ladder`). Every Submit button was left visible and untouched (verified by screenshot).

Harness: `oa_proof.py` (live URL gather + matrix runner) + `oa_profiles.py` (10 profiles).
Entry point under test: `oa_singlepage.run_single_page_oa` → `oa_observe_act.observe_act`.

---

## 1. What ran

- **URL gathering (live, public APIs):** Greenhouse `boards-api`, Lever `v0/postings`, Ashby
  `ApiJobBoardWithTeams` GraphQL. Dry-run gathered **60 open engineering postings** balanced across
  the three ATSes (gh 24 / ashby 24 / lever 12) — the harness **supports the full 50×10 sweep**
  (`--companies 50 --profiles 10`); the runs below are the representative batch.
- **Two batches actually executed (24 run-slots), spanning all 3 ATSes and 3 profiles
  (`pyr_backend`, `maya_ml`, `diego_mech`):**
  - `batch2` — concurrency 2, 240 s/job: 18 run-slots (13 completed, 5 Ashby timeouts).
  - `serial` — concurrency 1, 240 s/job: 6 run-slots (4 completed, 2 Ashby timeouts).
- A real throwaway PDF resume (`fixtures/test_resume.pdf`, no PII) was supplied so the file-upload
  archetype was exercised.

> **Companies actually run:** ~6 distinct postings (Anthropic ×2 reqs on Greenhouse, Palantir ×2
> on Lever, Ramp ×3 on Ashby) × 3 profiles. **Profiles run:** 3 of the 10. Ashby consumed most of
> the budget in timeouts (see §4), which capped how many distinct companies completed.

---

## 2. Fill-rate — the headline, stated honestly

`fill_rate` here = **VLM-verified** correct fields ÷ non-skip fields. This is a *verify* metric, and
the proof's single most important finding is that **the VLM verify — not the fill — is the
bottleneck.** Ground truth (screenshots) shows fields filled that the verify scored 0 (see §3).

| ATS | runs | timeouts | VLM-verified filled / fillable | verified fill-rate | cost |
|---|---|---|---|---|---|
| **Greenhouse** | 8 | 0 | 14 / 52 | **27%** (serial alone: **78%**) | $0.0211 |
| **Lever** | 8 | 0 | 9 / 71 | **13%** | $0.0754 |
| **Ashby** | 8 | **7** | 0 / 11 | **0%** (never completed) | $0.0031 |
| **Overall** | 24 | 7 | 23 / 134 | **17% verified** | $0.0996 |

**The verified number badly understates the true fill.** On Greenhouse, the **serial** run hit
**78%** and a screenshot of a "0%" concurrent run shows *every* field correctly filled (First/Last
name, email, phone, the boolean dropdown) — the 0% is the VLM `visual_check` returning UNKNOWN under
load, which the design routes (required free-text UNKNOWN → ESCALATE). Corrected for that, the
**real Greenhouse fill-rate is ~80–100% on clean standard forms.**

Cost on completed runs: **~$0.002–0.007 / form** (Greenhouse cheapest, Lever pricier due to the
search loops). Within the design's per-app envelope; rises with custom-widget search loops as
predicted.

---

## 3. The verify-vs-fill distortion (proven, not asserted)

Run `006` (Greenhouse, Anthropic, `pyr_backend`) scored **fill_rate=0%** — every field
`verdict:UNKNOWN → ESCALATE`. The end screenshot
(`runs/batch2/shots/006_greenhouse_anthropic_pyr_backend.png`) shows the form **fully and correctly
filled**: `First Name=Pyry`, `Last Name=Halonen`, `Email=pyry.halonen.test@example.com`,
`Phone=+1 415 555 0177`, and the required boolean committed. Submit untouched.

So `observe_act` **filled the fields**; the VLM verify under concurrent load **could not confirm
them**, and the state machine (correctly, per its own UNKNOWN→ESCALATE rule for required text)
reported ESCALATE. Fill-rate inversely tracks concurrent VLM pressure: runs 000/001 (machine fresh)
= 80%/75%; the same form later under load = 0%; Lever 014/015 recovered to 38%/55% as parallel load
drained.

**Conclusion:** the proof metric conflates "filled" with "verified." The fill engine on standard
inputs is strong; the **verify path is the weak link** and the throughput limiter.

---

## 4. Failure taxonomy (ESCALATE fields across both batches)

| # | failure bucket | what it is | generic or env? |
|---|---|---|---|
| **57** | **locate-failed** (`S1_LOCATE: no-control`) | Lever custom "cards" (screening Q widgets), EEO `single_select`s, and the big "Language Skills (check all)" checkbox list are **not matched by visible-label ranking**. `_locate_by_label` finds no control. | **Real generic gap** |
| **48** | **verify-UNKNOWN** | Field typed, but `visual_check` returns UNKNOWN (capped/slow/failing under load) → required free-text routed to ESCALATE. Screenshot-proven often actually filled. | **Env / verify-path** |
| **6** | **exception-timeout** | `EXC:TimeoutError` inside fill on Ashby react-select under load (browser page not ready). | **Env (Ashby slowness)** |

Other observed (correct-by-design) terminals, not failures:
- `org` SEARCH on Lever → `'Aperture' → 0 opts`, variant retry, then `TERMINATE:deadline`/SKIP. The
  typeahead loop **works**; the company simply isn't in Lever's org autocomplete, so it correctly
  **refused to mis-fill** rather than picking a wrong option.
- `boolean` sensitive (Ashby) → `S_OTHER_GUARD: sensitive->ESCALATE` — the demographic/legal
  no-silent-Other guard firing as designed.

### Widget shapes that still fail
0. **Lever drag-drop resume widget leaves a blocking overlay (root cause of Lever's empty text
   fields).** Screenshots `runs/serial/shots/002,003_lever_*` show a "Drag the file…" upload
   overlay (teal box) **open and covering the top-right of the form** after the resume step — so
   the subsequent Full-name / Email / Phone trusted-types land on/behind the overlay and the fields
   read **empty**. The Lever 0% is largely this overlay-occlusion, not just locate failure. Fix:
   dismiss/blur the upload overlay (Escape / click-away) before filling the rest, or fill text
   fields **before** touching the resume control on Lever.
1. **Lever "cards" / custom screening-question widgets** — id-keyed, no visible `<label>` the
   ranker can bind to → `no-control`. The biggest *structural* gap.
2. **EEO `single_select` dropdowns on Lever** — same locate miss.
3. **Long checkbox groups ("Language Skills, check all that apply", ~40 options)** — not located /
   not entered.
4. **Ashby react-select-heavy forms** — each field's settle×VLM is slow enough that an 8–13-field
   form blows the 240 s budget (7 of 8 Ashby runs timed out, even serial).

### Widget shapes that WORK
- Greenhouse standard `input[type=text]` (name/email/phone), the boolean "I understand…" dropdown
  (S3_OPEN → S_CLOSED_LIST → cascade), and CDP resume upload on a locatable `input[type=file]`.

---

## 5. Honest generic-ness verdict

**Partly generic, with one clear structural gap and one environment limiter — NOT yet "generic +
correct" across all three ATSes.**

- ✅ **Greenhouse: genuinely generic and correct.** ~80–100% real fill on clean standard forms at
  ~$0.002, no per-tenant code. This is the bar, met on one family.
- ⚠️ **Lever: generic engine, but the locate layer can't reach Lever's custom-card / EEO / big-
  checkbox widgets.** Standard text fields fill but the form is dominated by widgets `_locate_by_label`
  doesn't bind. This is a **real BUILD gap**, not an environment artifact.
- ❌ **Ashby: blocked on latency, not correctness.** The per-field settle+VLM cost makes a full
  react-select form exceed budget (7/8 timeouts). Needs a faster verify path before it can be judged.
- 🔬 **The fill-rate metric understated everything** because the **VLM verify is the throughput
  bottleneck**: under any concurrency it saturates and returns UNKNOWN on already-filled fields. The
  generic-ness of the *fill* is better than the 17% verified headline; the generic-ness of the
  *verify* is the actual blocker.

**Fill-only and safety held on every run:** `observe_act` never clicked an advance/submit control;
every screenshot shows the Submit button present and untouched; the sensitive-field guard fired;
the search loop refused to mis-fill an absent autocomplete value.

---

## 6. Top 3 fixes for the next iteration

1. **Make the VLM verify cheap, concurrent-safe, and non-fatal — stop conflating "unverified" with
   "unfilled."** For plain text/email/url/tel fields, **read the DOM `.value` back (free, instant)
   as the primary verdict** and only fall to VLM when the DOM read is empty/ambiguous. That alone
   converts the bulk of the 48 `verify-UNKNOWN` ESCALATEs into DONE and removes the load coupling.
   Then cap/serialize VLM calls behind a small global rate-limiter so concurrency doesn't starve it.

2. **Fix Lever's blocking upload overlay + the `locate-failed` gap (together these are the Lever
   blocker).** (a) On Lever, dismiss the drag-drop resume overlay (Escape / click-away) after the
   upload, or fill all text fields **before** touching the resume control — the overlay currently
   occludes Full-name/Email/Phone and leaves them empty (see §4 item 0). (b) Extend
   `_locate_by_label` to bind to a preceding question/heading text block when no `<label>` exists
   (Lever cards) and to group containers for checkbox/radio sets so a "Language Skills (check all)"
   list is located as one multi-field (the 57-field locate gap).

3. **Cut Ashby per-field latency so a full react-select form fits the budget.** Tighten the
   `_settle` deadline on Ashby's predictable react-select (it pre-highlights fast), batch the
   field-scoped delta reads, and prefer DOM-value read-back over a VLM call per field (ties to fix
   #1). Target: a 12-field Ashby form under ~120 s so it completes instead of timing out.

---

## 7. Known harness limitation

When an Ashby run hits the per-job timeout, the underlying headless browser occasionally **hangs in
teardown**, which blocks the `asyncio.as_completed` loop from returning and the final
`records.json` / aggregate write. **No data is lost** — every completed run's full per-field result
is written incrementally to `runs/<batch>/perfield/NNN.json` and every screenshot to
`runs/<batch>/shots/`, from which the tables above were computed. Fix for the scaled run: wrap each
job's `run_single_page_oa` in its own `session.kill()` watchdog / process isolation so one stuck
browser can't stall the sweep.

---

## 8. Artifacts

- Harness: `oa_proof.py`, `oa_profiles.py` (both ruff-clean, import-clean under `.venv`).
- Results: `runs/batch2/perfield/*.json`, `runs/serial/perfield/*.json` (full per-field traces),
  `runs/batch2/shots/*.png`, `runs/serial/shots/*.png` (end-of-fill screenshots, Submit untouched),
  `runs/batch2/companies.json` (the gathered live URL set).
- Ground-truth screenshot proving fill≠verify: `runs/batch2/shots/006_greenhouse_anthropic_pyr_backend.png`.

---

## Verify-Oracle Fix — serial + concurrent results

**The fix.** Verify no longer uses the VLM as the sole oracle. `observe_act._verify_field` →
`oa_brain.verify` now reads the located control's **live DOM value first** (`oa_dom_value.read_dom_value`,
generic standard-DOM via CDP — no renameable attributes, no per-ATS branching) and matches it against the
wanted value **LLM-only** (`wd_repeaters._llm_pick`, with a deterministic full-normalized-string-identity
short-circuit — not substring/regex). The VLM is demoted to a per-FIELD-budgeted AID
(`FIELD_VLM_CAP=2`) consulted **only** when the DOM read is empty/ambiguous (visual-only widgets). The
per-page VLM cap is lifted to a high backstop at run start so it can never pre-empt the per-field budget
and starve field 7+ into the old capped→UNKNOWN→ESCALATE false-failure. Every verify call records a
`verify-src:dom|vlm` trace tag, so the dom-vs-vlm split is auditable per field.

**Offline gate (all green, $0, no browser):** `oa_dom_value` 8/8 · `oa_brain` 28/28 (incl.
"DOM exact-normal match → CORRECT, 0 VLM + 0 LLM" and "DOM read-back mismatch → WRONG, 0 VLM") ·
`oa_observe_act` 24/24 (incl. "DOM-first text DONE (no VLM)" and "per-FIELD VLM budget caps at
≤FIELD_VLM_CAP") · `oa_proof` 10/10 · `ruff check` clean.

### Serial (1-proc-per-run, concurrency=1) — the prior passed proof
Greenhouse **93%** (13/14 non-skip DONE) · Lever **50%** (7/14) · Ashby **88%** (7/8). Every verdict
from **DOM read-back, ZERO VLM** (the 2 Ashby file fields took the INTRINSIC_FILE fast-path). GH 50.6s,
Lever 79.7s, Ashby 45.4s — all under the 240s/job budget.

### Concurrent (subprocess-per-job, `--concurrency 4`, 240s/job, fill-only, with resume)
`runs/oa_proof_concurrent/` — 12-job batch (6 companies × 2 profiles), 4 OS subprocesses at a time
(each its OWN interpreter → its OWN verify globals). Total wall-clock **≈ 6m 40s** for the 12-job sweep.

| ATS | runs | completed | timed out | fill-rate (concurrency=4) | serial fill-rate | verify src |
|---|---|---|---|---|---|---|
| **Greenhouse** | 4 | 4 | 0 | **100%** (20/20) | 93% | dom=20 vlm=0 |
| **Lever** | 4 | 4 | 0 | **50%** (28/56) | 50% | dom=28 vlm=0 |
| **Ashby** | 4 | 0 | **4** | **n/a — all 4 hit the 240s wall** | 88% | (no fields reached verify) |
| **Overall** | 12 | 8 | 4 | 63% (48/76) | — | **dom=48 vlm=0** |

- **Completed vs timed out:** 8/12 completed, 4/12 timed out — **all 4 timeouts were Ashby**, GH + Lever
  100% completion.
- **Comparable to serial — no collapse.** GH 100% (≥ serial 93%) and Lever 50% (= serial 50%) under
  concurrency=4. This is the headline: in the OLD in-process harness the same forms collapsed to
  **0–17%** because 4 coroutines shared one module-global VLM counter; with subprocess-per-job they are
  **identical to serial**. The metric holds because the verify state is per-process.
- **DOM verify holds under parallelism:** **every one of the 48 filled fields verified `verify-src:dom`
  `verdict:CORRECT` — ZERO VLM calls across the entire concurrent batch.** Not one field was
  ESCALATED by a capped→UNKNOWN false-failure. The verify-oracle fix is concurrency-proof because the
  free DOM read needs no shared, capped resource.
- **Fill-only confirmed on every screenshot.** All 8 completed runs left the Submit button present and
  untouched (e.g. `shots/000_greenhouse_anthropic_pyr_backend.png`: all 5 fields filled, dark
  "Submit application" button bottom-right, never clicked). The 4 killed Ashby children never reached a
  submit control either — `observe_act` has no submit path by construction.

### BLUNT verdict
**Yes — concurrency now holds because state is per-process.** The metric no longer collapses under
parallelism: GH and Lever return **identical numbers serial vs concurrency-4**, with **100% DOM-sourced
verify and zero VLM**. The old 0–17% concurrent collapse was purely the in-process shared-counter race;
subprocess-per-job eliminates it. The verify-oracle fix is the load-bearing change — a text/email/phone/
url/date/Yes-No value is read straight from the DOM, which costs nothing and needs no capped shared
resource, so 4 parallel jobs cannot starve each other.

**What still collapses, and exactly why — it is NOT the verify oracle, NOT a state race:** all 4 Ashby
jobs hit the 240s wall. Proven by re-running the exact failing job (`ramp / diego_mech`, idx 004)
**ALONE** in its own process: it **completed** at **56% (5/9), fill wall-clock 157.5s, total 233s,
verify-src dom=3 vlm=0** — i.e. it *barely* fits the 240s budget even with zero contention (Ashby's
react-select per-field settle is intrinsically slow). Under concurrency=4, four headless Chromium engines
contend for CPU/IO on one machine and that ~157s fill + nav/extract/map/screenshot overhead is pushed
past 240s → the per-job kill watchdog fires. So the Ashby failure is a **latency/contention budget
overflow, not a verify or isolation bug**: when it *does* run to completion it verifies the same way
(all DOM, no VLM) as serial. The fix needed is on Ashby's per-field settle latency (DESIGN §6 top-3 fix
#3: tighten the react-select `_settle`, batch field-scoped delta reads), or a higher per-job timeout /
lower concurrency for Ashby — not the verify path.

**Artifacts:** `runs/oa_proof_concurrent/records.json` (aggregate + per-run), `perfield/*.json` (full
per-field traces with `verify-src` tags), `shots/*.png` (8 end-of-fill, Submit untouched),
`CONCURRENT_REPORT.md`; solo-Ashby control: `runs/oa_ashby_solo/ashby_solo.json` (+ `.png`).
