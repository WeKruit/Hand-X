# PRD: Desktop Bridge — Replace TS Engine with Hand-X Python Binary

**Version:** 1.0
**Date:** 2026-03-11
**Priority:** P0
**Owner:** WeKruit Engineering

---

## 1. Executive Summary

Replace the `@wekruit/ghosthands-engine` TypeScript package in the Electron desktop app with a spawned Hand-X Python subprocess that communicates via stdout JSONL. This eliminates the dual-engine maintenance burden, routes all LLM calls through VALET (no API keys on client), and unblocks the consumer release.

**Key value:** One automation engine (Hand-X/Python) instead of two (TS + Python), with zero API keys shipped in the desktop app.

---

## 2. Problem Statement

### Current Architecture (Broken)

```
GH-Desktop-App (Electron)
  └─ @wekruit/ghosthands-engine (TypeScript npm package)
       └─ Magnitude adapter (in-process)
            └─ Playwright + Anthropic SDK (direct API calls)
            └─ CostTracker, SmartApplyHandler (all in TS)
```

**Problems:**

1. **Two engines to maintain**: The TS engine (`@wekruit/ghosthands-engine`) and the Python engine (Hand-X) do the same thing. Features added to Hand-X (DomHand fill, platform guardrails, Workday support) don't exist in the TS engine.

2. **API keys in the desktop binary**: The current `runSmartApply.ts` sets `process.env.ANTHROPIC_API_KEY` directly (line 594). This means the key is in memory on the user's machine — a security and billing risk.

3. **SmartApplyHandler resolution is fragile**: `resolveSmartApplyHandlerCtor()` tries 6+ import paths to find the handler class (lines 63-129 of `runSmartApply.ts`). This breaks on every package structure change.

4. **Magnitude adapter is abandoned**: The TS engine uses the Magnitude adapter. Hand-X uses browser-use with DomHand. All new ATS platform support (Workday, Greenhouse, Lever) is in Hand-X only.

5. **No path to consumer release**: The desktop app cannot ship without code signing, and the in-process TS engine makes signing complex (native deps, Playwright, etc.).

### Target Architecture

```
GH-Desktop-App (Electron)
  └─ spawn("hand-x" binary, { env: GH_LLM_PROXY_URL, GH_LLM_RUNTIME_GRANT, ... })
       └─ stdout: JSONL events → parsed by Electron into ProgressEvent objects
       └─ stderr: logging (ignored by Electron, available for debug)
       └─ stdin: commands from Electron (cancel, complete_review)
       └─ Hand-X Python process
            └─ browser-use Agent + DomHand actions
            └─ LLM calls → VALET proxy → Anthropic/Google (no direct API keys)
```

---

## 3. Goals & Metrics

| ID | Goal | Metric | Target | Priority |
|----|------|--------|--------|----------|
| G1 | Single automation engine | Lines of TS engine code removed | >2000 lines | P0 |
| G2 | Zero API keys on client | API keys in desktop binary | 0 | P0 |
| G3 | All LLM calls through VALET | Direct API calls from desktop | 0 | P0 |
| G4 | Feature parity with current flow | Greenhouse + Workday test pass | Both pass | P0 |
| G5 | Browser stays open for review | Browser alive after agent done | Always | P0 |
| G6 | Cost tracking visible to user | Cost events in JSONL stream | Per-step | P1 |
| G7 | Cancel/review lifecycle works | Cancel mid-run, complete review | Both work | P0 |

---

## 4. Non-Goals (Explicit Boundaries)

- **NOT building the binary**: The Nuitka/PyInstaller CI pipeline is a separate work package. This PRD uses `python -m ghosthands` during development; the binary swap is mechanical once the bridge works.
- **NOT fixing Workday Create Account**: The checkbox/button click issue is a Hand-X bug, not a bridge concern. The bridge must faithfully relay whatever Hand-X reports.
- **NOT changing the VALET API**: The local worker claim/heartbeat/complete protocol in `localWorkerHost.ts` is unchanged. Only `runSmartApply.ts` and `engine.ts` change.
- **NOT removing the review browser coordinator**: The CDP-based review session management (`reviewBrowserCoordinator.ts`) stays because Hand-X leaves the browser open and the desktop app needs to manage its lifecycle.
- **NOT touching the renderer**: The UI already consumes `ProgressEvent` objects. As long as the bridge emits conforming events, the renderer is untouched.

---

## 5. User Personas

### 5.1 End User (Job Applicant)
- Opens desktop app, signs in, queues a job URL
- Expects to see progress events (field filled, step N of M, cost)
- Reviews the filled application in a browser window
- Clicks "Submit" or "Cancel" in the desktop UI

### 5.2 Developer (WeKruit Engineer)
- Runs `python -m ghosthands --job-url "..." --output-format jsonl` locally
- Debugs by reading stderr logs
- Tests bridge by piping JSONL into a mock Electron consumer

---

## 6. Functional Requirements

### FR-001: Process Spawning (P0)

**Replace** the in-process `SmartApplyHandler` execution in `runSmartApply.ts` with `child_process.spawn()`.

**Current** (lines 589-615 of `runSmartApply.ts`):
```typescript
const SmartApplyHandler = resolveSmartApplyHandlerCtor();
const handler = new SmartApplyHandler();
const result = await handler.execute(ctx);
```

**Target:**
```typescript
import { spawn } from 'node:child_process';

const handX = spawn(getHandXBinaryPath(), [
  '--job-url', targetUrl,
  '--profile', JSON.stringify(profile),
  '--resume', resumePath,
  '--output-format', 'jsonl',
  '--proxy-url', llmRuntime.baseUrl,
  '--runtime-grant', runtimeGrant,
  '--max-steps', '50',
  ...(headless ? ['--headless'] : []),
  ...(email ? ['--email', email] : []),
  ...(password ? ['--password', password] : []),
], {
  env: {
    ...process.env,
    GH_LLM_PROXY_URL: llmRuntime.baseUrl,
    GH_LLM_RUNTIME_GRANT: runtimeGrant,
    GH_USER_PROFILE_TEXT: JSON.stringify(profile),
    GH_RESUME_PATH: resumePath,
    PLAYWRIGHT_BROWSERS_PATH: getPlaywrightBrowsersPath(),
    PYTHONUNBUFFERED: '1',
  },
  stdio: ['pipe', 'pipe', 'pipe'],
});
```

**Binary resolution:**
```typescript
function getHandXBinaryPath(): string {
  // Production: bundled binary in extraResources
  const resourcePath = process.resourcesPath ?? app.getAppPath();
  const platform = process.platform; // darwin, win32, linux
  const arch = process.arch; // arm64, x64
  const binaryName = platform === 'win32' ? 'hand-x.exe' : 'hand-x';
  const bundled = join(resourcePath, 'hand-x', `hand-x-${platform}-${arch}`, binaryName);
  if (existsSync(bundled)) return bundled;

  // Development: run via Python
  const devPython = join(__dirname, '..', '..', '..', 'Hand-X', '.venv', 'bin', 'python3');
  if (existsSync(devPython)) return devPython;

  // Fallback: system Python
  return 'python3';
}

function getHandXArgs(binaryPath: string, params: SpawnParams): string[] {
  // If running via Python interpreter, prepend -m ghosthands
  if (binaryPath.endsWith('python3') || binaryPath.endsWith('python')) {
    return ['-m', 'ghosthands', ...buildCliArgs(params)];
  }
  return buildCliArgs(params);
}
```

### FR-002: JSONL → ProgressEvent Mapping (P0)

Parse each stdout line as JSON and map to the desktop app's `ProgressEvent` interface.

**Hand-X JSONL events** (from `ghosthands/output/jsonl.py`):

| Hand-X `event` value | Desktop ProgressEvent Type | Notes |
|----------------------|--------------------------|-------|
| `status` | `status` | Direct map. `message` field passes through. |
| `field_filled` | `action` | Map to `message: "Filled: {field} = {value}"` |
| `field_failed` | `action` | Map to `message: "Failed: {field} — {reason}"` |
| `progress` | `status` | Map to `message: "Filled {filled}/{total} fields (round {round})"` |
| `done` | `complete` | Map `success` → message. Set `runSnapshot` from accumulated cost. |
| `error` | `error` | Direct map. `fatal` field → stop process. |
| `cost` | `status` | Update running cost snapshot. Emit as status with cost info. |

**Parser implementation:**
```typescript
function parseHandXEvent(line: string): ProgressEvent | null {
  try {
    const raw = JSON.parse(line);
    switch (raw.event) {
      case 'status':
        return { type: 'status', message: raw.message, timestamp: raw.timestamp };
      case 'field_filled':
        return { type: 'action', message: `Filled: ${raw.field} = ${raw.value}`, timestamp: raw.timestamp };
      case 'field_failed':
        return { type: 'action', message: `Failed: ${raw.field} — ${raw.reason}`, timestamp: raw.timestamp };
      case 'progress':
        return { type: 'status', message: `Filled ${raw.filled}/${raw.total} fields`, timestamp: raw.timestamp };
      case 'done':
        return { type: 'complete', message: raw.message, timestamp: raw.timestamp };
      case 'error':
        return { type: 'error', message: raw.message, timestamp: raw.timestamp };
      case 'cost':
        return { type: 'status', message: `Cost: $${raw.total_usd.toFixed(4)}`, timestamp: raw.timestamp };
      default:
        return null;
    }
  } catch {
    return null;
  }
}
```

### FR-003: LocalRunSnapshot from JSONL Cost Events (P1)

The current `runSmartApply.ts` builds `LocalRunSnapshot` objects from `CostTracker`. The bridge must synthesize equivalent snapshots from JSONL `cost` events.

```typescript
interface RunningCostState {
  totalCostUsd: number;
  promptTokens: number;
  completionTokens: number;
  actionCount: number;
  currentStep: string;
}

function buildLocalRunSnapshot(state: RunningCostState, workflowRunId: string): LocalRunSnapshot {
  return {
    workflowRunId,
    step: state.currentStep,
    description: STEP_DESCRIPTIONS[state.currentStep] ?? state.currentStep,
    progressPct: estimateProgressPct(state.currentStep),
    executionMode: 'hand-x',
    actionCount: state.actionCount,
    taskBudgetUsd: 0.50, // from --max-budget
    remainingBudgetUsd: 0.50 - state.totalCostUsd,
    totalCostUsd: state.totalCostUsd,
    imageCostUsd: 0,
    reasoningCostUsd: 0,
    updatedAt: new Date().toISOString(),
  };
}
```

### FR-004: Process Lifecycle — Cancel and Shutdown (P0)

**Cancel a running job:**
```typescript
// Write cancel command to Hand-X stdin
handXProcess.stdin.write(JSON.stringify({ event: 'cancel' }) + '\n');

// If process doesn't exit within 5s, SIGTERM
setTimeout(() => {
  if (!handXProcess.killed) handXProcess.kill('SIGTERM');
}, 5000);
```

**Note:** Hand-X CLI must implement stdin command reading. Currently it does not — this is a new requirement.

**Required Hand-X change** (`ghosthands/cli.py`):
```python
async def _listen_for_commands(proc: asyncio.subprocess.Process) -> None:
    """Read stdin for commands from the desktop app."""
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        line = await reader.readline()
        if not line:
            break
        cmd = json.loads(line.decode())
        if cmd.get("event") == "cancel":
            # Signal the agent to stop
            raise KeyboardInterrupt
```

**Shutdown:** When the Electron app quits, it sends `SIGTERM` to all spawned processes. Hand-X already handles `KeyboardInterrupt` and cleans up.

### FR-005: Browser Lifecycle — Keep-Alive for Review (P0)

**Critical:** After the agent finishes, the browser MUST stay open for user review. The desktop app manages the browser lifecycle:

1. Hand-X spawns the browser (Playwright via browser-use) with `keep_alive=True`
2. Agent runs, fills the form, reports `done` event
3. Hand-X process exits but the browser stays open (it's a separate process)
4. Desktop app tracks the browser's CDP URL from Hand-X's event stream
5. User reviews in the browser → clicks "Complete" in desktop UI
6. Desktop app closes the browser page via CDP

**Required Hand-X JSONL event:**
```json
{"event": "browser_ready", "cdpUrl": "ws://127.0.0.1:9222/devtools/browser/...", "timestamp": 1710000000000}
```

**Required Hand-X change:** Emit `browser_ready` event after browser launches with the CDP WebSocket URL.

### FR-006: VALET Proxy Enforcement (P0)

The desktop app MUST NOT pass raw API keys to Hand-X. Instead:

1. Desktop app receives `runtimeGrant` from VALET when claiming a job (already implemented in `localWorkerHost.ts`)
2. Desktop app passes `--proxy-url` and `--runtime-grant` to Hand-X CLI
3. Hand-X routes all LLM calls through VALET proxy (already implemented in `ghosthands/llm/client.py`)

**Validation:** If `--proxy-url` is set, Hand-X MUST reject any direct API key usage. Add a guard:
```python
if settings.llm_proxy_url and (settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")):
    logger.warning("llm.proxy_mode_active_ignoring_direct_key")
```

### FR-007: Remove @wekruit/ghosthands-engine (P1)

After the bridge is verified working:

1. Remove `@wekruit/ghosthands-engine` from `package.json`
2. Delete `runSmartApply.ts` (replace with `runHandX.ts`)
3. Delete `taskContextAdapter.ts` (Hand-X builds its own context)
4. Delete `profileConverter.ts` → `toWorkdayProfile()` (Hand-X handles profile normalization internally)
5. Simplify `engine.ts` to delegate to the new `runHandX.ts`
6. Remove `SmartApplyHandler` resolution logic (lines 63-129)
7. Remove `adapters.createAdapter('magnitude')` usage
8. Remove `CostTracker` usage (cost comes from JSONL events)

**Estimated deletion:** ~800 lines from `runSmartApply.ts`, ~200 lines from supporting files.

---

## 7. Implementation Phases

### Phase 1: Hand-X CLI Enhancements (Hand-X repo)
**Depends on:** Nothing
**Files:** `ghosthands/cli.py`, `ghosthands/output/jsonl.py`

- [ ] Emit `browser_ready` event with CDP URL after browser launches
- [ ] Implement stdin command reader for `cancel` command
- [ ] Emit `awaiting_review` event when agent finishes with `keep_alive=True`
- [ ] Ensure `--proxy-url` + `--runtime-grant` work end-to-end (already mostly done)
- [ ] Fix browser hold-open bug (keep_alive=None despite True — see bug #1 in handoff)

### Phase 2: Desktop Bridge Core (GH-Desktop-App repo)
**Depends on:** Phase 1
**Files:** New `src/main/runHandX.ts`, modified `src/main/engine.ts`

- [ ] Create `runHandX.ts` with `spawn()` + JSONL parser
- [ ] Implement `parseHandXEvent()` → `ProgressEvent` mapping
- [ ] Implement `RunningCostState` for `LocalRunSnapshot` synthesis
- [ ] Implement `getHandXBinaryPath()` (dev mode: python3, prod: bundled binary)
- [ ] Wire `runHandX()` into `engine.ts` as replacement for `runSmartApplyLocally()`
- [ ] Implement cancel via stdin write + SIGTERM fallback

### Phase 3: localWorkerHost Integration (GH-Desktop-App repo)
**Depends on:** Phase 2
**Files:** `src/main/localWorkerHost.ts`

- [ ] Replace `runSmartApplyLocally()` call in `executeClaimedJob()` with `runHandX()`
- [ ] Map `llmRuntime.apiKey` → `--runtime-grant` (NOT `--api-key`)
- [ ] Map `llmRuntime.baseUrl` → `--proxy-url`
- [ ] Pass `runtimeGrant` from claim payload as `--runtime-grant`
- [ ] Preserve `awaiting_review` → `reviewHandle` mapping (use CDP URL from `browser_ready` event)
- [ ] Preserve cancel/complete-review lifecycle
- [ ] Preserve heartbeat loop (unchanged — it doesn't interact with the engine)

### Phase 4: Remove Old Engine (GH-Desktop-App repo)
**Depends on:** Phase 3 verified working
**Files:** `package.json`, delete `runSmartApply.ts`, `taskContextAdapter.ts`, `profileConverter.ts`

- [ ] Remove `@wekruit/ghosthands-engine` from `package.json`
- [ ] Delete `runSmartApply.ts`
- [ ] Delete `taskContextAdapter.ts`
- [ ] Delete `profileConverter.ts`
- [ ] Simplify `engine.ts`
- [ ] Update imports in `localWorkerHost.ts`, `ipc.ts`
- [ ] Run full test suite

### Phase 5: E2E Verification
**Depends on:** Phase 4

- [ ] Desktop app: sign in → queue Greenhouse job → watch progress → review → complete
- [ ] Desktop app: sign in → queue Workday job → watch progress → review → complete
- [ ] Cancel mid-run: start a job → cancel → verify cleanup
- [ ] LLM proxy: verify zero direct API calls (inspect network/process env)
- [ ] Browser review: verify browser stays open after agent finishes
- [ ] Cost tracking: verify cost events reach the UI

---

## 8. Risks & Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Hand-X binary not built yet (Nuitka/PyInstaller) | Can't test prod binary path | High | Use `python -m ghosthands` in dev mode. Binary swap is mechanical. |
| Browser hold-open bug in browser-use | Browser closes after agent finishes | High | Fix bug first (Phase 1). The log shows `keep_alive=None` — trace BrowserProfile propagation in `browser_use/agent/service.py`. |
| JSONL event schema drift | Desktop parser breaks | Medium | Define shared schema in `Hand-X/schemas/events.json`. Validate both sides. |
| Hand-X process crashes | Electron hangs waiting for events | Medium | Implement process exit handler. If exit code != 0 and no `done` event, emit synthetic `error` event. |
| CDP URL changes between runs | Review coordinator can't find browser | Low | Hand-X emits `browser_ready` with fresh CDP URL each time. Desktop app updates its tracking. |
| Workday flow still broken in Hand-X | User sees failures on Workday | High | Not a bridge blocker. Bridge faithfully relays Hand-X errors. Fix Workday separately. |

---

## 9. File-Level Change Map

### Hand-X Repo (`VALET & GH/Hand-X/`)

| File | Action | What Changes |
|------|--------|--------------|
| `ghosthands/cli.py` | Modify | Add stdin command reader, emit `browser_ready` event |
| `ghosthands/output/jsonl.py` | Modify | Add `emit_browser_ready()`, `emit_awaiting_review()` |
| `browser_use/agent/service.py` | Fix | Debug keep_alive=None bug (line 3918) |

### GH-Desktop-App Repo (`VALET & GH/GH-Desktop-App/`)

| File | Action | What Changes |
|------|--------|--------------|
| `src/main/runHandX.ts` | **Create** | Process spawning, JSONL parser, ProgressEvent mapping |
| `src/main/engine.ts` | Modify | Replace `runSmartApplyLocally` → `runHandX` |
| `src/main/localWorkerHost.ts` | Modify | Wire `runHandX` into `executeClaimedJob` |
| `src/main/runSmartApply.ts` | **Delete** | Replaced by `runHandX.ts` |
| `src/main/taskContextAdapter.ts` | **Delete** | Hand-X builds its own context |
| `src/main/profileConverter.ts` | **Delete** | Hand-X normalizes profiles internally |
| `package.json` | Modify | Remove `@wekruit/ghosthands-engine` dependency |

---

## 10. JSONL Event Contract (Shared Schema)

```typescript
// Events emitted by Hand-X on stdout (one JSON object per line)
// NOTE: the wire key is "event", not "type".  "field_failed" uses "reason", not "error".
type HandXEvent =
  | { event: 'status'; message: string; step?: number; maxSteps?: number; jobId?: string; timestamp: number }
  | { event: 'field_filled'; field: string; value: string; method: string; timestamp: number }
  | { event: 'field_failed'; field: string; reason: string; timestamp: number }
  | { event: 'progress'; filled: number; total: number; round: number; timestamp: number }
  | { event: 'browser_ready'; cdpUrl: string; timestamp: number }
  | { event: 'awaiting_review'; message: string; timestamp: number }
  | { event: 'done'; success: boolean; message: string; fields_filled: number; jobId?: string; leaseId?: string; resultData?: Record<string, unknown>; timestamp: number }
  | { event: 'error'; message: string; fatal: boolean; jobId?: string; timestamp: number }
  | { event: 'cost'; total_usd: number; prompt_tokens: number; completion_tokens: number; timestamp: number }

// Commands sent to Hand-X on stdin (one JSON object per line)
// NOTE: stdin commands use "type" (not "event") as the discriminator key.
// This is intentional — stdout events use "event", stdin commands use "type".
type HandXCommand =
  | { type: 'cancel' }
  | { type: 'complete_review' }
```

---

## 11. Self-Score (100-point Framework)

| Category | Max | Score | Notes |
|----------|-----|-------|-------|
| **AI-Specific Optimization** | 25 | 22 | Clear phase ordering, explicit file-level changes, JSONL contract defined |
| **Traditional PRD Core** | 25 | 23 | Problem quantified, personas defined, non-goals explicit |
| **Implementation Clarity** | 30 | 27 | Code examples for every FR, dependency chain clear, delete list explicit |
| **Completeness** | 20 | 18 | Risks mitigated, change map complete, E2E verification defined |
| **Total** | 100 | **90** | |

---

## 12. Handoff Prompt for Implementation Session

Copy this into a new Claude Code session in the `GH-Desktop-App` directory:

```
I'm implementing the Desktop Bridge — replacing @wekruit/ghosthands-engine with a spawned Hand-X Python subprocess.

## Read These First
1. PRD: ../Hand-X/PRD-DESKTOP-BRIDGE.md (the full spec)
2. Current bridge: src/main/runSmartApply.ts (what we're replacing)
3. Worker host: src/main/localWorkerHost.ts (what calls the bridge)
4. Hand-X CLI: ../Hand-X/ghosthands/cli.py (the Python side)
5. JSONL events: ../Hand-X/ghosthands/output/jsonl.py (event format)
6. Shared types: src/shared/types.ts (ProgressEvent interface)

## What To Do

### Step 1: Create src/main/runHandX.ts
- Spawn Hand-X via child_process.spawn()
- Parse stdout JSONL line-by-line → ProgressEvent objects
- Map Hand-X events to desktop ProgressEvent types (see FR-002 in PRD)
- Implement getHandXBinaryPath() for dev (python3 -m ghosthands) and prod (bundled binary)
- Handle process exit (exit code != 0 → synthetic error event)
- Handle cancel via stdin write + SIGTERM fallback

### Step 2: Wire into engine.ts
- Replace runSmartApplyLocally() call with runHandX()
- Keep the same RunSmartApplyResult interface
- Map llmRuntime.apiKey → --runtime-grant, llmRuntime.baseUrl → --proxy-url

### Step 3: Wire into localWorkerHost.ts
- Replace runSmartApplyLocally() call in executeClaimedJob() with runHandX()
- Preserve the review handle lifecycle (browser stays open)
- Preserve cancel/complete-review commands

### Step 4: Delete old engine
- Remove @wekruit/ghosthands-engine from package.json
- Delete runSmartApply.ts, taskContextAdapter.ts, profileConverter.ts
- Update all imports

## Key Constraint
ALL LLM calls must go through VALET proxy. Never pass raw API keys to Hand-X.
The claim payload already has runtimeGrant — pass it as --runtime-grant.
The bootstrap payload has llmRuntime.baseUrl — pass it as --proxy-url.

## Architecture
Electron → spawn(hand-x, {env: GH_LLM_PROXY_URL, GH_LLM_RUNTIME_GRANT}) → stdout JSONL → ProgressEvent

## Test
1. Run: python -m ghosthands --job-url "https://job-boards.greenhouse.io/starburst/jobs/5123053008" --test-data ../Hand-X/examples/apply_to_job_sample_data.json --resume ../Hand-X/examples/resume.pdf --output-format jsonl
2. Verify JSONL events come out on stdout
3. Then wire the spawn into the desktop app
```

---

*Generated for WeKruit Engineering — Hand-X Consumer Release*
