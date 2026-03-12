# Ship-Readiness Checklist — Hand-X + Desktop + VALET

Generated: 2026-03-12 | Providers: Codex + Gemini + Claude | Scope: Full stack

---

## How to use

Check items as you address them: `[ ]` → `[x]`
Items are ordered by severity within each category.

---

## 1. SECURITY (26 items)

### CRITICAL

- [x] **S-01: Domain lockdown not wired into Desktop CLI path** — `DomainLockdown` exists in `ghosthands/security/` but `cli.py` creates `BrowserProfile(headless=..., keep_alive=True)` with NO `allowed_domains`. The worker path (`agent/factory.py`) applies it, Desktop path does not. LLM agent can navigate anywhere. [Hand-X: cli.py, security/domain_lockdown.py]

- [x] **S-02: --profile CLI arg exposes full PII via ps aux** — When Desktop spawns `hand-x --profile '{"email":"...", "credentials":{...}}'`, the entire JSON including credentials is visible to any user on the system via `ps aux`, `/proc/PID/cmdline`, or Activity Monitor. Must use stdin or env vars exclusively. [Hand-X: cli.py:82]

- [x] **S-03: emit_account_created sends plaintext password over stdout** — Dead code but publicly exported. If wired up, sends cleartext credentials over IPC pipe, capturable in DevTools/crash reports. Remove password param or encrypt. [Hand-X: jsonl.py:201-215]

- [x] **S-04: No profile JSON schema validation** — `_load_profile()` trusts arbitrary JSON from `--profile`, `@file`, `--test-data`, or `GH_USER_PROFILE_TEXT` with no schema validation. Non-dict values (`[]`, `"string"`, `null`) can crash downstream. [Hand-X: cli.py]

### HIGH

- [x] **S-05: Raw exception strings forwarded to Desktop via JSONL** — `emit_error(str(e), fatal=True)` sends full Python exception strings including internal paths, connection strings, and API keys from third-party libraries. Must sanitize before emission. [Hand-X: cli.py:292, 523, 693]

- [ ] **S-06: GH_USER_PROFILE_TEXT env var exposes full PII to child processes** — Full profile (name, email, phone, address, education, work history) serialized into env var, inherited by all child processes (Playwright, browser-use, LLM client), readable via `/proc/PID/environ`. [Hand-X: cli.py:191]

- [ ] **S-07: field_filled events leak sensitive field values over stdout** — DomHand emits raw values for every form field including SSN, date of birth, salary. No deny-list for sensitive field types. Needs field-level PII redaction. [Hand-X: jsonl.py:122, field_events.py:74]

- [x] **S-08: No SIGTERM handler in CLI mode** — Worker mode has signal handling, but CLI entry point only catches KeyboardInterrupt. SIGTERM from Electron force-quit leaves browser open, CDP port exposed, env vars unreleased. [Hand-X: cli.py]

- [x] **S-09: No stdin line-size bound** — `protocol.py` reads arbitrary-length lines from stdin. A buggy Electron process could send multi-GB JSON line and exhaust Hand-X memory. Add 64KB limit. [Hand-X: bridge/protocol.py]

- [ ] **S-10: Profile defaults silently invent sensitive demographic answers** — When profile omits `gender`, `race_ethnicity`, `veteran_status`, `disability_status`, the adapter fills hardcoded US-centric defaults without user consent or warning. [Hand-X: bridge/profile_adapter.py:14-30]

- [ ] **S-11: Full applicant profile injected into LLM system prompt** — Demographic, work-authorization, disability, veteran, location, and salary data sent to Anthropic API with no minimization. PII is in the LLM context window. [Hand-X: agent/prompts.py]

- [x] **S-12: stdin JSON type not validated** — Protocol assumes `cmd.get("type")` but valid JSON like `[]`, `"x"`, or `1` will crash on `.get()` call. Need `isinstance(cmd, dict)` guard. [Hand-X: bridge/protocol.py:96-106]

### MEDIUM

- [ ] **S-13: CLI arg validation missing** — `--job-url`, `--resume`, `--proxy-url`, `--max-steps`, `--max-budget` are not validated. Malformed URLs, missing files, zero/negative limits accepted silently. [Hand-X: cli.py]

- [ ] **S-14: CSP uses unsafe-inline for styles** — Desktop App `index.html` uses `style-src 'unsafe-inline'` for Tailwind/React. Style injection vector. [Desktop: src/renderer/index.html]

- [ ] **S-15: VALET grants stored unencrypted in Redis** — RuntimeGrant payloads containing userId, sessionToken, jobId, leaseId stored in Redis without encryption at rest. [VALET: local-worker-broker.service.ts]

- [ ] **S-16: No rate limit on invalid grant checks** — Checking an invalid/expired grant has no penalty. Attacker can brute-force grant hashes at network speed. [VALET: local-worker.routes.ts:236-244]

- [ ] **S-17: Grant hash is deterministic and logged** — `grantHash.slice(0, 16)` logged in audit trail. SHA256(normalized_grant) is deterministic — if logs leak, hash can be replayed. [VALET: local-worker.routes.ts:314]

---

## 2. INTEGRATION (24 items)

### CRITICAL

- [x] **I-01: Spec/code mismatch — `event` vs `type` discriminator** — Shipped JSONL uses `event` as top-level key, `docs/DESKTOP_BRIDGE_SPEC.md` defines `type`. Contract divergence. [Hand-X: output/jsonl.py vs docs/DESKTOP_BRIDGE_SPEC.md]

- [x] **I-02: `done` is not terminal — emitted before review** — `done(success=true)` emitted before review starts, then process continues waiting for stdin commands. One run can produce `done(success=true)` followed by `error(fatal=true)`. [Hand-X: cli.py:500-510]

- [x] **I-03: No terminal event for review outcomes** — Review complete = `status("Review complete")`, review cancel = `status("Review cancelled")` + exit 1, review timeout = `error(fatal)` after `done(success)`. Desktop must infer outcomes from text matching. [Hand-X: bridge/protocol.py:168-177]

- [x] **I-04: Spec documents removed --email/--password args** — `DESKTOP_BRIDGE_SPEC.md` still shows `--email` and `--password` as CLI args but they were removed for security. [Hand-X: docs/DESKTOP_BRIDGE_SPEC.md]

### HIGH

- [ ] **I-05: Handshake is informational only — no enforcement** — `protocol_version` and `min_desktop_version` are emitted but never enforced. Incompatible Desktop builds are not rejected. [Hand-X: output/jsonl.py, cli.py]

- [x] **I-06: Progress event schema mismatched** — Code emits `step`, `maxSteps`, `description`; spec documents `filled`, `total`, `round`. [Hand-X: output/jsonl.py vs docs/]

- [x] **I-07: field_failed schema mismatched** — Code uses `reason`, spec uses `error`. [Hand-X: output/jsonl.py vs docs/]

- [x] **I-08: account_created not in documented contract** — Exists in emitter and tests but not part of spec. [Hand-X: output/jsonl.py]

- [ ] **I-09: Profile adapter is not a schema boundary** — Remaps small subset of keys, keeps both camelCase and snake_case, no canonical input model enforced. Different code paths produce `first_name` vs `full_name`, `linkedin` vs `linkedin_url`. [Hand-X: bridge/profile_adapter.py]

- [ ] **I-10: browser_ready is best-effort** — If `browser.cdp_url` is missing, Hand-X logs to stderr only. No machine-readable fallback event to Desktop. [Hand-X: cli.py]

- [x] **I-11: Review timeout exits 0 after fatal error** — `run_agent_jsonl()` only exits 1 on `cancel`/`eof`, so timeout returns exit code 0 after emitting `error(fatal=true)`. [Hand-X: cli.py, bridge/protocol.py]

- [ ] **I-12: Spec env precedence is inaccurate** — `GH_HEADLESS`, `GH_MAX_STEPS_PER_JOB`, `GH_MAX_BUDGET_PER_JOB` are documented as configurable but argparse defaults override them. [Hand-X: cli.py vs docs/]

### MEDIUM

- [ ] **I-13: Desktop heartbeat mandatory for grant refresh** — VALET grant expires in 30 min. If Desktop stops heartbeating (network lag), grant expires mid-job, LLM proxy returns 401, job stalls silently. [VALET: local-worker-broker.service.ts]

- [ ] **I-14: Fire-and-forget usage increment** — `incrementGrantUsage(grantHash).catch(() => {})` silently swallows Redis failures. Budget tracking lost on Redis blip. [VALET: local-worker.routes.ts:348]

- [ ] **I-15: Cost ceiling not enforced** — `budgetCentsUsd` exists in grant but only request count is checked. A single expensive request counts as 1 against `maxRequestCount`. [VALET: local-worker.routes.ts]

- [ ] **I-16: Callback URL includes GH_SERVICE_SECRET as query param** — Secret visible in proxy logs, server request logs, crash dumps. [VALET: local-worker-broker.service.ts:204-212]

- [ ] **I-17: No idempotency on complete/fail endpoints** — Duplicate `complete(leaseId=X)` calls update job status twice, firing duplicate webhooks. [VALET: local-worker.routes.ts]

- [ ] **I-18: Pipe broken suppresses all events including done/error** — If Electron closes stdout mid-job, `_pipe_broken` suppresses all subsequent events. Warning logged to stderr but Desktop gets no machine-readable final state. [Hand-X: output/jsonl.py]

---

## 3. CI/CD (22 items)

### CRITICAL

- [x] **C-01: CI never runs unit or integration tests** — `test.yaml` only discovers `tests/ci/test_*.py`. The 67 bridge integration tests and unit tests are never executed in CI. [Hand-X: .github/workflows/test.yaml]

- [x] **C-02: Binary builds not gated on tests** — `build-binary.yml` triggers on `v*` tags and `main` push with no dependency on test workflow. Broken code can ship as release binaries. [Hand-X: .github/workflows/build-binary.yml]

- [x] **C-03: publish.yml has pytest sanity check commented out** — Release publishing happens without any test execution. [Hand-X: .github/workflows/publish.yml]

### HIGH

- [x] **C-04: Binary smoke test always passes** — Uses `--help || true`, so even a completely broken binary passes. [Hand-X: .github/workflows/build-binary.yml]

- [x] **C-05: Unit tests are stale** — `tests/unit/test_desktop_bridge.py` imports removed symbols (`_listen_for_cancel`, `_read_stdin_line`). Hidden because CI never runs it. [Hand-X: tests/unit/test_desktop_bridge.py]

- [ ] **C-06: No code signing or notarization** — macOS and Windows binaries are unsigned. Users must bypass OS security warnings (Gatekeeper/SmartScreen). [Hand-X: .github/workflows/build-binary.yml]

- [ ] **C-07: No security scanning in any workflow** — No CodeQL, `pip-audit`, `bandit`, `safety`, secret scanning, or SBOM generation. [Hand-X: .github/workflows/]

- [ ] **C-08: No dependency audit in Desktop CI** — Missing `npm audit` and static analysis (CodeQL) in Desktop App CI pipeline. [Desktop: .github/workflows/]

- [x] **C-09: PyInstaller unpinned in build** — Build uses `pip install pyinstaller` without version pin. Binary contents can drift across builds. [Hand-X: .github/workflows/build-binary.yml]

### MEDIUM

- [ ] **C-10: Build uses pip instead of uv** — `build-binary.yml` and `build/build.sh` use `pip install -e ".[dev]"` instead of project's standard `uv` toolchain. [Hand-X: .github/workflows/build-binary.yml, build/]

- [ ] **C-11: Version inconsistency** — `pyproject.toml` is `0.1.0`, `build-binary.yml` triggers on `v*` tags, `publish.yml` creates prerelease tags without `v` prefix. Version mismatch between package and release. [Hand-X: pyproject.toml vs workflows]

- [ ] **C-12: Prerelease tags don't trigger binary builds** — `publish.yml` creates `0.2.1rc1` (no `v` prefix), `build-binary.yml` only matches `v*` tags. Prereleases ship without binaries. [Hand-X: .github/workflows/]

- [ ] **C-13: notify-desktop doesn't fire for prereleases** — Only fires on GitHub Releases or manual dispatch. Prereleases don't notify Desktop repo. [Hand-X: .github/workflows/notify-desktop.yml]

- [ ] **C-14: No tag-version consistency check** — No workflow verifies git tag matches `pyproject.toml` version before packaging. [Hand-X: .github/workflows/]

- [ ] **C-15: package.yaml tests only browser_use import** — Only validates `import browser_use` succeeds. Doesn't test `ghosthands`, `hand-x` entry point, or bridge CLI. [Hand-X: .github/workflows/package.yaml]

- [ ] **C-16: No coverage collection or threshold** — No workflow enforces coverage minimum. Untested Desktop path invisible in CI. [Hand-X: .github/workflows/]

- [ ] **C-17: No checksums verification or SBOM** — Checksums generated but no binary signatures, provenance attestation, or software bill of materials. [Hand-X: .github/workflows/build-binary.yml]

- [ ] **C-18: notify-desktop is fire-and-forget** — Dispatches `repository_dispatch` but doesn't verify Desktop consumed it, updated manifests, or passed downstream tests. [Hand-X: .github/workflows/notify-desktop.yml]

### LOW

- [ ] **C-19: No CI test for VALET grant expiry + heartbeat race** — Concurrency tests use mock Redis without actual TTL behavior. [VALET: tests/]

- [ ] **C-20: VALET deploy doesn't health-check ATM** — Deploy validates build but doesn't verify ATM endpoint or LLM config accessibility. [VALET: .github/workflows/]

- [ ] **C-21: Desktop checksum verification depends on release body format** — `hand-x-update.yml` greps checksums from release body text. Format change breaks verification silently. [Desktop: .github/workflows/hand-x-update.yml]

- [ ] **C-22: No end-to-end integration test across Desktop → Hand-X → VALET** — No workflow spawns the full stack and verifies a job flows from Desktop through Hand-X to VALET and back.

---

## 4. USER EXPERIENCE (18 items)

### CRITICAL

- [x] **U-01: done(success=true) emitted before user approves** — Desktop shows "completed" before human review. User hasn't actually approved the application. Contradictory UI state. [Hand-X: cli.py:500]

- [x] **U-02: No terminal event for review outcomes** — Desktop must infer review complete/cancel/timeout from status text + exit code. No machine-readable review result event. [Hand-X: bridge/protocol.py]

### HIGH

- [x] **U-03: Review timeout produces contradictory events** — `done(success=true)` then `error(fatal=true, "Review timed out")` — Desktop sees both success and fatal error from same run. [Hand-X: cli.py, bridge/protocol.py]

- [ ] **U-04: No progress during agent run gaps** — Progress only emits during DomHand field fills. If agent is navigating, loading pages, or running generic browser actions, Desktop shows silence. No heartbeat/idle signal. [Hand-X: cli.py]

- [ ] **U-05: No retry/resume after failure** — Profile-load failure, browser crash, review timeout, review cancel all exit the process. No retry or resume workflow. User must restart manually. [Hand-X: cli.py]

- [x] **U-06: Error messages are raw exception strings** — Desktop users see `"Failed to load profile: JSONDecodeError('Expecting value: line 1 column 1 (char 0)')"` instead of actionable guidance. [Hand-X: cli.py]

- [ ] **U-07: Grant budget exhaustion gives no reason** — 429 "Runtime grant budget exhausted" doesn't indicate whether it's request count or cost. Desktop can't explain to user. [VALET: local-worker.routes.ts:263]

### MEDIUM

- [ ] **U-08: Silent demographic auto-fill without consent** — Profile adapter fills gender="Male", race="Asian", veteran="not a protected veteran" by default. User may submit false EEO answers without knowing. [Hand-X: bridge/profile_adapter.py]

- [ ] **U-09: DomHand failure message is noisy** — Desktop receives `"DomHand unavailable: {raw exception}, using generic actions"` — technical, not user-friendly. No structured degraded-mode signal. [Hand-X: cli.py]

- [ ] **U-10: Success defined incorrectly** — `history.is_done() and bool(final_result)` means a successful run with empty result text is reported as failure. [Hand-X: cli.py]

- [ ] **U-11: CDP URL failure is silent to Desktop** — If `browser.cdp_url` is missing, Hand-X logs to stderr only. Desktop user gets no signal that live review attachment is unavailable. [Hand-X: cli.py]

- [ ] **U-12: Cancel semantics inconsistent** — Cancel during run emits `done(success=false)` + exit 1. Cancel during review emits `status` + exit 1. Different events for same user action. [Hand-X: cli.py, bridge/protocol.py]

- [ ] **U-13: No main-run timeout** — Review has 30-min timeout but the main agent run has no CLI-level timeout. Agent can hang indefinitely with no user-facing timeout messaging. [Hand-X: cli.py]

- [ ] **U-14: EOF treated differently by phase** — EOF during run = cancellation. EOF during review = "eof" result. No distinct "Desktop disconnected" outcome for UI. [Hand-X: bridge/protocol.py]

### LOW

- [ ] **U-15: Step hooks don't classify failure type** — `agent/hooks.py` reports step/cost/is_done but not failure type, retryability, timeout state, or degraded mode. Desktop can't show smart recovery options. [Hand-X: agent/hooks.py]

- [ ] **U-16: Lease-write failure is vague** — "Lease write failed after dispatch" — Desktop doesn't know if job is queued, claimed, or lost. [VALET: local-worker-broker.service.ts]

- [ ] **U-17: Concurrent claim returns empty response** — Desktop interprets `null/null/null/null` as "no jobs" and polls again, creating busy-loop. [VALET: local-worker-broker.service.ts]

- [ ] **U-18: Profile validation error doesn't say which field** — "local_worker_profile invalid: {zod error}" — user sees validation failure but not which field (email, phone, etc.). [VALET: local-worker-broker.service.ts]

---

## Summary

| Category | Critical | High | Medium | Low | Total |
|----------|----------|------|--------|-----|-------|
| Security | 4 | 8 | 5 | 0 | 17 |
| Integration | 4 | 8 | 6 | 0 | 18 |
| CI/CD | 3 | 6 | 9 | 4 | 22 |
| UX | 2 | 5 | 7 | 4 | 18 |
| **Total** | **13** | **27** | **27** | **8** | **75** |

### Priority order for fixing

1. **S-01** — Domain lockdown not wired (attacker-controlled navigation)
2. **C-01** — CI never runs tests (all quality gates bypassed)
3. **I-01** — Spec/code event key mismatch (contract broken)
4. **U-01** — done emitted before review (false success state)
5. **S-02** — PII in CLI args (ps aux exposure)
6. **C-02** — Binary builds not gated on tests
7. **I-02** — done is not terminal
8. **S-04** — No profile validation
9. **I-03** — No review terminal events
10. **C-03** — publish.yml tests commented out
11. **S-05** — Raw exceptions in JSONL errors
12. **S-06** — PII in env vars
13. **C-06** — No code signing
