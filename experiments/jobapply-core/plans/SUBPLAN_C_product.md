# SUBPLAN C — Product Application-Process Interface

Team C (Product Interface). Date: 2026-07-09. Scope: start / pause / check-usage / HITL / resume /
browser-attach-with-user-cookies for the job-application product. Fill-only; submission stays behind
explicit user review. All paths cited are real files read during inventory.

**Headline finding:** the handoff §9 undersells what exists. The desktop lane (VALET
local-workers ⇄ GH-Desktop-App ⇄ `ghosthands/cli.py`) ALREADY has claim/lease, cancel,
open-question HITL with per-field payloads, awaiting-review, CDP browser handoff, and cost
callbacks. The genuinely missing pieces are: (1) the observe_act engine isn't the thing running,
(2) a user-initiated PAUSE verb, (3) a user-facing usage read API (only an admin dashboard exists),
(4) persisting HITL answers back into the profile, (5) a deliberate cookie-profile policy for the
automation browser. This sub-plan is therefore mostly *wiring*, not new systems.

---

## 1. INVENTORY — what exists vs missing, per capability

Legend: HX = `Hand-X/.claude/worktrees/observe-act-generic/`, V = `VALET/`, D = `GH-Desktop-App/`.

### 1.1 Start application
EXISTS — two lanes:
- **Cloud worker lane:** `HX ghosthands/worker/poller.py` polls `gh_automation_jobs`
  (status `pending|queued` → claim → `running`, heartbeats, graceful release);
  `HX ghosthands/worker/executor.py::execute_job` orchestrates (resume load, credential decrypt
  via `HX ghosthands/integrations/credentials.py` AES-256-GCM, domain allowlist via
  `validate_domain()` before navigation, VALET `report_running`).
- **Desktop lane:** `V apps/api/src/modules/local-workers/local-worker.routes.ts` —
  `POST /api/v1/local-workers/jobs/submit`, `POST /claim` (lease), heartbeat, events;
  `D src/main/localWorkerManager.ts` / `localWorkerHost.ts` claim + spawn
  `HX ghosthands/cli.py` as a subprocess speaking JSONL over stdin/stdout
  (`HX ghosthands/bridge/protocol.py`).
MISSING — nothing structural. Gap is only that the thing started is the plain browser-use agent
(`executor.py` line ~237 `TODO: integration point`, `_run_agent` → `agent/factory.run_job_agent`),
not the observe_act engine (`HX experiments/jobapply-core/oa_singlepage.py`). Zero observe_act
references inside `ghosthands/`.

### 1.2 Pause / resume / cancel
EXISTS:
- Cancel: DB status flip + Postgres NOTIFY (`HX ghosthands/worker/hitl.py::check_for_signals`,
  `integrations/database.py::send_signal/listen_for_signals`, channel `gh_job_signal_{id}`);
  desktop stdin `{"type":"cancel"}` (`bridge/protocol.py::listen_for_cancel`);
  VALET `POST /jobs/:jobId/cancel` + `/release`.
- Blocker-driven pause: `worker/hitl.py::pause_job` → status `needs_human` → VALET callback
  (`integrations/valet_callback.py::report_needs_human` with screenshot_url/page_url) →
  `wait_for_resume` (LISTEN/NOTIFY, poll fallback, 300 s default).
- Review hold: `bridge/protocol.py::wait_for_review_command` (24 h window,
  `complete_review`/`cancel`), VALET `POST /jobs/:jobId/awaiting-review`,
  `D src/main/reviewBrowserCoordinator.ts`.
MISSING:
- **User-initiated PAUSE** — no pause verb anywhere (only cancel and engine-raised blockers).
- **Resume-after-pause in the cloud executor** — explicitly stubbed:
  `executor.py` logs `hitl.resume_not_implemented — returning original result` (~line 592).
- Per-field checkpointing (the engine's ledger makes this nearly free — §2).

### 1.3 Check usage
EXISTS:
- Per-job budget + token/cost tracking: `HX ghosthands/worker/cost_tracker.py` (CostTracker,
  BudgetExceededError, presets), `ghosthands/cost_summary.py`; completion callback carries
  `cost{total_cost_usd, action_count, total_tokens}` (`valet_callback.py::report_completion`).
- Engine emits `cost`, `secs`, `fill_rate`, per-field `results[]` ledger in its result JSON
  (HANDOFF §2.3).
- Storage + dashboard: `V packages/db/src/schema/job-fill-runs.ts` — `job_fill_runs` already has
  `costUsd, latencyS, fieldsFilled/Escalated/Total, committedData jsonb (per-field trace!),
  escalations, screenshotPath`; admin routes `V apps/api/src/modules/job-fill-runs/
  job-fill-run.admin-routes.ts` (VALET PR #259, merged).
MISSING:
- A **user-facing** read API + UI ("my usage this month") — current routes are admin-only.
- The deployed worker emits THIN browser-use data, so `committedData` is empty in production;
  the rich ledger only exists in `experiments/jobapply-core` (memory: selfimprove_loop_closed).

### 1.4 HITL (user supplies missing info → continue)
EXISTS — surprisingly complete on the desktop lane:
- VALET: `POST /jobs/:jobId/open-question` with `openQuestionItemSchema`
  `{question_key?, field_label, field_id?, field_type?, question_text?, section?, page_url?,
  form_context?, source?, options[{value,text}]≤100}` plus batch-level
  `{questionBatchId, message, timeoutSeconds, pageUrl, cdpUrl, pausedAt, questions[≤100]}`;
  `POST /jobs/:jobId/resume-open-question` (`local-worker.routes.ts` lines ~1034–1100).
- Desktop relays answers: `D src/main/localWorkerManager.ts:1761` sends
  `{type:'answer_field', leaseId, fieldLabel, fieldId, answer}` over stdin; `runHandX.ts:3347`.
- Hand-X consumes: `bridge/protocol.py::put_field_answer/get_field_answer` (per-field-id answer
  queue with cancel-wake), `listen_for_cancel` handles `answer_field`/`skip_field`;
  `ghosthands/cli.py::_collect_open_question_issues_from_browser` + `_OpenQuestionIssue`
  builds the question batch today.
- Engine hook: `HX experiments/jobapply-core/oa_hitl.py::wait_for_unblock` (GH_HITL=1 opt-in,
  polls `still_blocked(page)`, file/console surfacing) — the in-browser blocker half
  (CAPTCHA/login user clears it themselves). NEEDS_HUMAN outcome exists in the engine.
MISSING:
- observe_act does not *generate* open-question payloads — its no-answer path is
  SKIP/ESCALATE. Need: NEEDS_HUMAN rows → openQuestionItem mapping + screenshot crop.
- **Answer persistence**: answers are consumed in-memory only. `integrations/database.py::
  load_answer_bank` exists (read side); nothing writes accepted HITL answers back
  (VALET has a `qa-bank` module — write path goes there).
- Web-app notification surface for open questions (desktop push exists via the Desktop app;
  VALET web UI for answering needs confirmation/wiring).

### 1.5 Browser layer with the user's real cookies
EXISTS:
- Engine: `OA_CDP_URL` attach (`oa_singlepage.py`, `BrowserSession(cdp_url=…)`), plus
  `OA_CHROME_PATH`/`OA_STEALTH` real-fingerprint path (HANDOFF §2.2).
- Worker CLI: `--cdp-url` / `GH_CDP_URL` env → `BrowserSession(browser_profile=BrowserProfile(
  keep_alive=True, allowed_domains=…), cdp_url=…)` (`ghosthands/cli.py` ~2037–2058), emits
  `browser_ready(cdp_url)`; `detach_keep_alive()` on cleanup so the user's browser survives us.
- Desktop: `D src/main/reviewBrowserCoordinator.ts` launches Chromium with
  `--remote-debugging-port=<port> --remote-debugging-address=127.0.0.1 --user-data-dir=<slot dir>`
  (localhost-bound already); `slotBrowserPool.ts` = pooled persistent per-slot profiles with
  single-owner mutex + stale-profile cleanup; `localWorkerHost.ts` hands the ws URL to Hand-X.
MISSING / DECISION NEEDED:
- Today's browser is a **managed Chromium slot profile**, not the user's daily Chrome. Chrome 136+
  refuses `--remote-debugging-port` on the *default* user-data-dir, so literally attaching to the
  user's everyday Chrome profile is off the table. The gap is a **persistent, per-user login
  profile** the pool must never wipe (slotBrowserPool currently `rmSync`s stale dirs) — the user
  logs into LinkedIn/Workday/etc. once in "their" automation browser and cookies persist locally
  forever after. Functionally equals "their real logged-in browser"; cookies never leave the machine.
- Cloud fallback (no desktop running): headless + credential vault. The V2 claim/secrets split is
  partially built — `GET /api/v1/local-workers/credentials/platforms/:credentialId/secret` and
  `/credentials/lookup` exist; the full lease-scoped bootstrap
  (`POST /jobs/:jobId/secrets` → stdin `{"type":"bootstrap"}`) is designed but not implemented
  (memory: project_v2_claim_secrets_split; no `secrets` route in local-worker.routes.ts).

---

## 2. CONTROL API — session state machine + commands

### 2.1 State machine (lives in the existing `gh_automation_jobs.status` column — no new store)
Existing statuses: `pending|queued → running → needs_human|completed|failed|cancelled`.
Add two values: `paused`, `awaiting_review` (the desktop broker already models awaiting-review;
mirror it in the GH jobs table for the cloud lane).

```
CREATED(pending|queued) → RUNNING(running) → DONE(completed) | FAILED(failed|cancelled)
        RUNNING ⇄ PAUSED(paused)              [user pause/resume]
        RUNNING ⇄ WAITING_HUMAN(needs_human)  [engine blocker / open question]
        RUNNING → REVIEW(awaiting_review) → DONE   [fill finished; user reviews & submits]
```
"RESUMED" is an event (`write_job_event('resumed')` already exists in `hitl.py`), not a state.

### 2.2 Commands — reuse the two existing command channels, add one verb
- **Cloud lane:** commands are status flips + `db.send_signal(job_id, {"action": …})`
  (`integrations/database.py:551`, NOTIFY channel already consumed by `hitl.wait_for_resume`).
  VALET API surface (VALET repo, `modules/ghosthands/`):
  `POST /api/v1/tasks/:id/pause | /resume | /cancel`, `GET /api/v1/tasks/:id/status`.
  pause = `UPDATE status='paused'` + NOTIFY `{"action":"pause"}`;
  resume = `status='running'` + NOTIFY `{"action":"resume","answers?":{…}}`.
- **Desktop lane:** stdin JSONL (`bridge/protocol.py`). Add `{"type":"pause"}` /
  `{"type":"resume"}` beside the existing `cancel`/`answer_field`/`skip_field`/`complete_review`.
  Desktop buttons → `localWorkerManager.safeSend` exactly like answer_field today.

### 2.3 How pause interrupts a mid-fill run safely — per-field boundary
The observe_act loop is already per-field (`oa_singlepage.py` iterates fields →
`oa_observe_act.observe_act()` per field). One engine change:

```python
# oa_singlepage.run_one(..., control: Callable[[], str] | None = None)
for field in fields:
    if control and (cmd := control()) in ("pause", "cancel"):
        result["status"] = "paused" if cmd == "pause" else "cancelled"
        break            # never interrupts inside a field commit
    ... observe_act(field) ...
```
`control()` is non-blocking: cloud = reads a flag set by the NOTIFY listener; desktop = reads a
flag set by `listen_for_cancel` (extended for `pause`). The **checkpoint is the ledger itself** —
`results[]` rows already carry `{name, label, outcome, committed, value, trace}`; persist the
partial result JSON to `job_fill_runs` (status `paused`) on pause. No new checkpoint format.

**Resume = rerun, not replay.** The engine is idempotent per field: S0_GUARD/already-correct
detection skips fields whose painted state already matches, and re-committing an identical value
is harmless. So resume simply calls `run_one` again on the same page (browser kept alive via
`keep_alive=True`/`detach_keep_alive`; cloud keeps the BrowserSession open while `paused`, with a
TTL — default 30 min, after which we close the browser and resume means fresh navigation + rerun).
Do NOT trust the paused ledger to skip fields (grouped-locate desync — memory
feedback_grouped_locate_desyncs); re-verifying each field's painted state IS the safe skip.

### 2.4 Message/DB shape
No new tables. `gh_automation_jobs.status` + `write_job_event` rows are the audit trail;
NOTIFY payload `{"action": "pause"|"resume"|"cancel", "data": {...answers}}` (shape already
half-defined in `worker/hitl.py` docstring). Partial/final ledgers → `job_fill_runs.committedData`.

---

## 3. HITL FLOW — NEEDS_HUMAN → notify → answer → resume at the same field

### 3.1 Flow (both lanes share the payload; transport differs)
1. Engine: a field maps to no profile value / hits a verify code / ambiguous question →
   outcome `NEEDS_HUMAN` (instead of silent ESCALATE — HANDOFF §9 step 6). Collect ALL
   needs-human fields for the page into ONE batch (don't ping the user per field), then pause at
   the field boundary.
2. Worker: build the open-question batch (schema below) with a screenshot crop per field —
   the engine already has the field bbox from `oa_perception.py` geometry; crop the page PNG.
3. Notify:
   - Desktop lane: `ghosthands/cli.py` already emits open-question batches → Desktop →
     `POST /api/v1/local-workers/jobs/:jobId/open-question` → VALET; Desktop shows native push.
     REUSE `_collect_open_question_issues_from_browser`'s emission path, sourcing items from the
     engine ledger instead of DOM scraping.
   - Cloud lane: `hitl.pause_job(...)` + `valet.report_needs_human(...)` — extend the existing
     `interaction` payload to carry `questions[]` (same item schema). VALET web UI renders it;
     web push notification via VALET's existing `notifications` module (`V apps/api/src/modules/
     notifications/`).
4. User answers in VALET web or Desktop → answer travels back:
   desktop: `{type:"answer_field", field_id, answer}` stdin (EXISTS);
   cloud: `POST /api/v1/tasks/:id/resume` body `{answers: {field_id: value}}` → NOTIFY
   `{"action":"resume","data":{"answers":{…}}}` (consumed by `hitl.wait_for_resume`, EXISTS).
5. Merge: answers overlay the in-memory profile for this session (`oa_profiles.py` access layer)
   AND persist: VALET writes accepted answers to its `qa-bank` module keyed by
   `(user_id, question_key)` so the next application never asks again. `database.py::
   load_answer_bank` already reads this — close the loop with the write side in VALET.
6. Resume at the same field: rerun `run_one` (§2.3) — already-filled fields self-skip via painted
   verify; the answered fields now have values from the merged profile and fill normally.

### 3.2 Payload schema (align to VALET's EXISTING `openQuestionItemSchema` — don't invent a rival)
```jsonc
// batch (engine/worker → VALET POST /jobs/:jobId/open-question, or interaction.questions[])
{
  "questionBatchId": "uuid",
  "jobId": "…", "leaseId": "…",           // leaseId desktop lane only
  "pageUrl": "https://…", "pausedAt": "ISO8601",
  "timeoutSeconds": 86400,
  "message": "3 questions need your input to continue",
  "questions": [{
    "question_key": "gh:{ats}:{normalized_label_hash}",   // dedupe/answer-bank key
    "field_id": "results[i].name",        // engine ledger name — resume join key
    "field_label": "Are you legally authorized to work in the US?",
    "field_type": "radio|text|select|file|verify_code",
    "question_text": "…verbatim on-page text…",
    "section": "Voluntary Disclosures",
    "page_url": "…", "form_context": "±1 sibling label for disambiguation",
    "source": "observe_act",
    "options": [{"value":"yes","text":"Yes"},{"value":"no","text":"No"}],   // from the ledger's read options
    "screenshot_crop_url": "storage://…"  // NEW optional field; add to openQuestionItemSchema
  }]
}
// answer (user → engine)
{ "type": "answer_field", "field_id": "…", "answer": "Yes" }   // stdin, EXISTS
{ "answers": {"field_id": "Yes"}, "persist": true }            // cloud resume body
```
One VALET schema change: optional `screenshot_crop_url` on `openQuestionItemSchema`.
Crops upload to the same Supabase Storage bucket PR #259 uses for run screenshots.

---

## 4. USAGE / "check usage"

Engine already emits everything (`cost`, `secs`, `results[]`); `job_fill_runs` already stores it
(`costUsd, latencyS, fieldsFilled/Total, committedData`). Two additions, both VALET-repo:

### 4.1 Ship the ledger from production (Hand-X side)
When the worker runs observe_act (§6 M1), map the result JSON → the existing `job_fill_runs`
ingest exactly as the sweep tooling does (columns already match 1:1: `fill_rate→fieldFillRate`,
`results[]→committedData`, `outcomes.ESCALATE→fieldsEscalated`). Add `user_id uuid` +
`job_id uuid` columns to `job_fill_runs` (today it's batch/url keyed for sweeps) so per-user
aggregation is possible.

### 4.2 Read API + UI (VALET)
- `GET /api/v1/usage/applications?from&to` (user-scoped):
  `{sessions: n, fields_filled, cost_usd, time_s, hitl_questions, completed, failed}` —
  one SQL aggregate over `job_fill_runs` by `user_id`.
- `GET /api/v1/tasks/:id/usage` (per session): the row's cost/latency/fields + per-field
  `committedData` (label + outcome only — never trace internals to end users).
- UI surface: a "Usage" card on the existing application/task detail page + a totals row on the
  dashboard. The admin fill-quality dashboard (PR #259) stays as-is for ops.
Budget guard: keep `cost_tracker.py` enforcement; surface `remaining_budget()` in the status
endpoint so the UI can show "$0.31 of $0.50 used".

---

## 5. BROWSER / COOKIES LAYER (browser-use-CLI-like, user's real logins)

### 5.1 Primary: Desktop-owned persistent login browser (all pieces exist, one policy change)
- Desktop launches Chromium via `reviewBrowserCoordinator.launchDetachedBrowser`
  (`--remote-debugging-port` on `127.0.0.1` — already localhost-bound) and hands `GH_CDP_URL`
  to `ghosthands/cli.py`, which attaches with `keep_alive=True` and detaches without killing
  (`detach_keep_alive`). EXISTS.
- **Change:** introduce a *persistent per-user login profile dir* (e.g.
  `getDetachedBrowserUserDataDir('user-login')`) exempt from `slotBrowserPool.
  clearStaleProfileDir` wiping. First-run onboarding: "open your application browser and sign in
  to LinkedIn / your ATS accounts once". Cookies then persist locally across sessions — this IS
  the user's real logged-in browser for applications. (Attaching to their daily-driver Chrome
  default profile is impossible: Chrome 136+ blocks CDP on the default user-data-dir.)
- Engine wiring: worker/CLI sets `OA_CDP_URL` from `GH_CDP_URL` per session (one line);
  optionally target a specific tab via the existing `cdp_target_id` plumb (`cli.py:2050`).
- Security constraints (enforced, not aspirational):
  - port bound to `127.0.0.1` (already done in `reviewBrowserCoordinator.ts:238`);
  - **one automation session per profile at a time** — slotBrowserPool's ownership mutex already
    serializes; keep the login profile single-lease;
  - **cookies never leave the machine** — no cookie export API, no server upload; the only thing
    VALET ever sees is job status/ledger/screenshots (screenshots of application pages only, taken
    per-field-crop for HITL and end-of-fill proof);
  - ws URL passed via env (`GH_CDP_URL`) never argv (argv leaks via `ps aux`);
  - domain allowlist checked before navigation (`executor.validate_domain` /
    `BrowserProfile(allowed_domains=…)` — both exist).

### 5.2 Fallback: user not running the Desktop app → cloud headless + credential vault
- Worker launches its own headless chromium (current default path, `settings.headless`).
- No cookies available ⇒ logins come from the credential vault: implement the V2 claim/secrets
  split as designed (memory project_v2_claim_secrets_split):
  `credentialRefs` in the claim payload (never secrets); Desktop/worker calls
  `POST /api/v1/local-workers/jobs/:jobId/secrets` (lease-scoped, accessToken+sessionToken+leaseId);
  secrets delivered to the Hand-X process **via stdin** `{"type":"bootstrap","credentials":[…]}`
  (30 s receipt timeout), held in memory only — never argv, never env, never in
  `GH_USER_PROFILE_PATH` JSON. Partial plumbing exists
  (`/credentials/platforms/:credentialId/secret`, `/credentials/lookup`); the per-job lease
  bootstrap route is the missing piece.
- Verification-code emails in cloud mode remain a NEEDS_HUMAN question (`field_type:
  "verify_code"`) via §3 — do not build mailbox automation into this track (Workday email design
  is its own workstream).

---

## 6. MILESTONES — smallest first, each independently shippable

| # | Deliverable | Repo(s) | Depends |
|---|---|---|---|
| **M1** | **Engine-as-callable + worker swap.** Extract `run_one(url, profile, resume, session, control=None) -> result` from `oa_singlepage.py` (CLI stays a thin wrapper); call it from `worker/executor.py::_run_agent` as primary fill path (browser-use demoted to fallback). Ship the ledger: map `results[]` → `job_fill_runs.committedData` (+ `user_id`,`job_id` columns). | Hand-X (+1 VALET migration) | — |
| **M2** | **Pause/resume.** `control()` hook at the field boundary (§2.3); stdin `{"type":"pause"}` in `bridge/protocol.py`; cloud `pause|resume` via existing NOTIFY + new `POST /tasks/:id/pause|resume`; `paused` status; resume=rerun on kept-alive browser (30 min TTL). Replaces the `hitl.resume_not_implemented` stub. | Hand-X + VALET (2 routes) | M1 |
| **M3** | **HITL end-to-end.** NEEDS_HUMAN batch → openQuestionItem payload (+`screenshot_crop_url` schema field) → existing desktop open-question pipeline + cloud `interaction.questions[]`; answers via existing `answer_field` / resume-body; merge into session profile; persist to qa-bank (write side). Resume at field via M2 rerun. | Hand-X + VALET (schema field, qa-bank write, web answer UI) + Desktop (crop render — its answer relay already exists) | M2 |
| **M4** | **Usage read API + UI.** `GET /api/v1/usage/applications`, `GET /api/v1/tasks/:id/usage`; usage card in web app. Pure VALET; data flows from M1. | VALET only | M1 |
| **M5** | **Persistent login browser.** Per-user login profile exempt from pool wiping; onboarding "sign in once" flow; `OA_CDP_URL`←`GH_CDP_URL` wiring; single-lease enforcement. | Desktop (+1 line Hand-X) | M1 |
| **M6** | **Cloud secrets bootstrap (V2).** `POST /jobs/:jobId/secrets` lease-scoped route; stdin `{"type":"bootstrap"}` handler in Hand-X; pending→active credential lifecycle. Enables the no-desktop fallback with logins. | VALET + Hand-X + Desktop flag | M1 (independent of M2–M5) |

Ordering rationale: M1 unlocks everything (real engine + real data). M2 is the smallest new verb.
M3 rides on M2's pause plumbing plus the already-built open-question rails. M4 is read-only and
parallelizable. M5/M6 are the two halves of the browser story (with/without desktop) and don't
block each other.

Security invariants across all milestones: AES-256-GCM at rest (`integrations/credentials.py`),
domain allowlist pre-navigation, secrets via stdin/env-file never argv, cookies never leave the
user's machine, FILL-ONLY — the Submit click stays behind the existing awaiting-review gate
(`wait_for_review_command` / `POST /jobs/:jobId/awaiting-review`).
