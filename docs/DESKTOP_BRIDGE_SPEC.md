# Desktop Bridge Integration Specification

**Version:** 1.0.0
**Date:** 2026-03-12
**Status:** Authoritative contract between Hand-X (Python) and GH-Desktop-App (Electron)

---

## 1. Overview

Hand-X is spawned as a subprocess by the Electron desktop app. The two processes communicate over three channels:

| Channel  | Direction             | Format               | Purpose                              |
|----------|-----------------------|----------------------|--------------------------------------|
| `stdout` | Hand-X --> Desktop    | JSONL (one object/line) | Structured events                  |
| `stdin`  | Desktop --> Hand-X    | JSON (one object/line)  | Commands (cancel, complete review) |
| `stderr` | Hand-X --> Desktop    | Free-form text       | Debug logging (not parsed)           |
| env vars | Desktop --> Hand-X    | Key=Value            | Configuration at spawn time          |

All stdout output is machine-readable JSONL. Hand-X installs a stdout guard (`install_stdout_guard()`) that redirects `sys.stdout` to `stderr`, reserving the real stdout file descriptor exclusively for JSONL events. No library code can corrupt the stream.

---

## 2. JSONL Event Contract (stdout)

Every event is a single JSON object on one line. Every event contains at minimum `type` (string) and `timestamp` (integer, Unix epoch milliseconds). Fields with `null` or empty-string values are omitted from the wire format to reduce payload size.

### 2.1 TypeScript Type Definition

```typescript
type HandXEvent =
  | StatusEvent
  | FieldFilledEvent
  | FieldFailedEvent
  | ProgressEvent
  | BrowserReadyEvent
  | AwaitingReviewEvent
  | DoneEvent
  | ErrorEvent
  | CostEvent;

interface StatusEvent {
  type: 'status';
  message: string;
  step?: number;        // Current agent step (1-indexed)
  maxSteps?: number;    // Configured step limit
  jobId?: string;       // Opaque job identifier for correlation
  timestamp: number;    // Unix epoch milliseconds
}

interface FieldFilledEvent {
  type: 'field_filled';
  field: string;        // Label/name of the form field
  value: string;        // Value that was set (may be truncated for display)
  method: string;       // "domhand" | "browser-use" | "manual"
  timestamp: number;
}

interface FieldFailedEvent {
  type: 'field_failed';
  field: string;        // Label/name of the form field
  error: string;        // Human-readable failure reason
  timestamp: number;
}

interface ProgressEvent {
  type: 'progress';
  filled: number;       // Cumulative fields successfully filled
  total: number;        // Cumulative fields attempted
  round: number;        // DomHand fill round (resets on page navigation)
  timestamp: number;
}

interface BrowserReadyEvent {
  type: 'browser_ready';
  cdpUrl: string;       // CDP WebSocket URL, e.g. "ws://127.0.0.1:9222/devtools/browser/..."
  timestamp: number;
}

interface AwaitingReviewEvent {
  type: 'awaiting_review';
  message: string;      // Human-readable description, e.g. "Application filled -- browser open for review"
  timestamp: number;
}

interface DoneEvent {
  type: 'done';
  success: boolean;
  message: string;
  fields_filled?: number;
  jobId?: string;
  leaseId?: string;
  resultData?: {
    success: boolean;
    steps: number;
    costUsd: number;
    finalResult: string | null;
    blocker: string | null;
    platform: string;
    [key: string]: unknown;
  };
  timestamp: number;
}

interface ErrorEvent {
  type: 'error';
  message: string;
  fatal: boolean;       // true = process will exit; false = recoverable
  jobId?: string;
  timestamp: number;
}

interface CostEvent {
  type: 'cost';
  total_usd: number;       // Cumulative LLM spend (USD), 6 decimal places
  prompt_tokens: number;   // Cumulative input tokens
  completion_tokens: number; // Cumulative output tokens
  timestamp: number;
}
```

### 2.2 Python Dataclass Equivalents

These are already implemented in `ghosthands/output/jsonl.py` via the `emit_event()` core emitter and typed convenience functions. The canonical Python-side representations:

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class StatusEvent:
    type: str = "status"          # literal "status"
    message: str = ""
    step: int | None = None
    maxSteps: int | None = None   # camelCase to match wire format
    jobId: str | None = None
    timestamp: int = 0            # set by emit_event()

@dataclass(frozen=True)
class FieldFilledEvent:
    type: str = "field_filled"
    field: str = ""
    value: str = ""
    method: str = "domhand"
    timestamp: int = 0

@dataclass(frozen=True)
class FieldFailedEvent:
    type: str = "field_failed"
    field: str = ""
    error: str = ""
    timestamp: int = 0

@dataclass(frozen=True)
class ProgressEvent:
    type: str = "progress"
    filled: int = 0
    total: int = 0
    round: int = 1
    timestamp: int = 0

@dataclass(frozen=True)
class BrowserReadyEvent:
    type: str = "browser_ready"
    cdpUrl: str = ""
    timestamp: int = 0

@dataclass(frozen=True)
class AwaitingReviewEvent:
    type: str = "awaiting_review"
    message: str = ""
    timestamp: int = 0

@dataclass(frozen=True)
class DoneEvent:
    type: str = "done"
    success: bool = False
    message: str = ""
    fields_filled: int = 0
    jobId: str | None = None
    leaseId: str | None = None
    resultData: dict[str, Any] | None = None
    timestamp: int = 0

@dataclass(frozen=True)
class ErrorEvent:
    type: str = "error"
    message: str = ""
    fatal: bool = False
    jobId: str | None = None
    timestamp: int = 0

@dataclass(frozen=True)
class CostEvent:
    type: str = "cost"
    total_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    timestamp: int = 0
```

### 2.3 Wire Format Examples

Each line is compact JSON with no whitespace padding (uses `separators=(",",":")`) followed by a newline character (`\n`).

```jsonl
{"type":"status","message":"Hand-X engine initialized","jobId":"abc-123","timestamp":1710288000000}
{"type":"status","message":"Setting up agent...","jobId":"abc-123","timestamp":1710288001200}
{"type":"status","message":"DomHand actions registered","jobId":"abc-123","timestamp":1710288001500}
{"type":"browser_ready","cdpUrl":"ws://127.0.0.1:9222/devtools/browser/a1b2c3d4","timestamp":1710288003000}
{"type":"status","message":"Starting application: https://boards.greenhouse.io/example/jobs/123","step":1,"maxSteps":50,"jobId":"abc-123","timestamp":1710288004000}
{"type":"field_filled","field":"First Name","value":"Jane","method":"domhand","timestamp":1710288010000}
{"type":"field_filled","field":"Last Name","value":"Doe","method":"domhand","timestamp":1710288010050}
{"type":"field_failed","field":"Phone Type","error":"No matching option found","timestamp":1710288010100}
{"type":"progress","filled":2,"total":3,"round":1,"timestamp":1710288010150}
{"type":"cost","total_usd":0.003421,"prompt_tokens":1200,"completion_tokens":350,"timestamp":1710288015000}
{"type":"status","message":"Reviewing filled fields and fixing failures","step":2,"maxSteps":50,"jobId":"abc-123","timestamp":1710288016000}
{"type":"cost","total_usd":0.007890,"prompt_tokens":2800,"completion_tokens":720,"timestamp":1710288025000}
{"type":"done","success":true,"message":"Application filled -- browser open for review","fields_filled":15,"jobId":"abc-123","leaseId":"lease-456","resultData":{"success":true,"steps":8,"costUsd":0.00789,"finalResult":"All fields filled","blocker":null,"platform":"greenhouse"},"timestamp":1710288030000}
{"type":"awaiting_review","message":"Browser open for review. Send complete_review or cancel.","timestamp":1710288030100}
```

### 2.4 Event Ordering Guarantees

The following ordering is guaranteed:

1. One or more `status` events (engine initialization)
2. `browser_ready` (exactly once, after Playwright browser launches)
3. Interleaved `status`, `field_filled`, `field_failed`, `progress`, `cost` events during the agent loop
4. Exactly one terminal event: either `done` or `error` with `fatal: true`
5. If `done.success === true`: one `awaiting_review` event follows, then Hand-X blocks on stdin
6. If `done.success === false` or fatal error: process exits (no `awaiting_review`)

Non-fatal `error` events can appear at any point without terminating the process.

`cost` events are cumulative snapshots emitted after each agent step. The final `cost` event before the `done` event represents the total job cost.

---

## 3. Stdin Command Contract

Commands are JSON objects, one per line, terminated by `\n`. Hand-X reads stdin in a blocking loop after emitting `awaiting_review`.

### 3.1 TypeScript Type Definition

```typescript
type HandXCommand =
  | { type: 'cancel' }
  | { type: 'complete_review' };
```

### 3.2 Command Descriptions

| Command            | When to Send                       | Hand-X Behavior                                                     |
|--------------------|------------------------------------|---------------------------------------------------------------------|
| `complete_review`  | User approved the filled application | Emits a final `done` event with `message: "Review complete"`, closes browser, exits with code 0 |
| `cancel`           | User wants to abort                | Emits a `done` event with `success: false`, `message: "Job cancelled by user"`, closes browser, exits with code 0 |

### 3.3 Wire Format

```
{"type":"complete_review"}\n
```

```
{"type":"cancel"}\n
```

### 3.4 Stdin Behavior Notes

- Hand-X only actively reads stdin during the `awaiting_review` phase (after the agent loop completes successfully). During the agent loop, stdin is not consumed.
- If stdin is closed (EOF) before a command is received, Hand-X interprets this as the desktop app dying and closes the browser gracefully.
- Malformed JSON lines on stdin are silently ignored.
- Unknown command types are silently ignored.
- The legacy command `cancel_job` is also accepted as a synonym for `cancel`.

---

## 4. Environment Variable Contract

The desktop app sets these environment variables when spawning the Hand-X process. They are in addition to (not a replacement for) CLI arguments. Both channels are used because some values are security-sensitive and should not appear in the process argument list visible via `ps`.

### 4.1 Required Variables

| Variable                  | Example Value                                                        | Description                                                  |
|---------------------------|----------------------------------------------------------------------|--------------------------------------------------------------|
| `PYTHONUNBUFFERED`        | `1`                                                                  | Forces unbuffered stdout/stderr. Required for real-time JSONL streaming. |
| `GH_LLM_PROXY_URL`       | `https://api.valet.wekruit.com/api/v1/local-workers/anthropic/`      | VALET LLM proxy base URL. All LLM calls route through this. |
| `GH_LLM_RUNTIME_GRANT`   | `lwrg_v1_abc123...`                                                  | Ephemeral auth token for the VALET proxy. Expires after 4 hours. |

### 4.2 Security-Sensitive Variables (must be env vars, never CLI args)

| Variable                  | Example Value                       | Description                                        |
|---------------------------|-------------------------------------|----------------------------------------------------|
| `GH_EMAIL`                | `user@example.com`                  | ATS login email. Readable from env only.           |
| `GH_PASSWORD`             | `s3cret`                            | ATS login password. Readable from env only.        |

**Note:** The CLI also accepts `--email` and `--password` flags for development convenience, but the desktop app MUST use environment variables for credentials to avoid them appearing in the process argument list.

### 4.3 Optional Variables

| Variable                  | Default          | Description                                              |
|---------------------------|------------------|----------------------------------------------------------|
| `GH_HEADLESS`             | `true`           | `true` or `false`. Whether to run the browser headless.  |
| `PLAYWRIGHT_BROWSERS_PATH`| (system default)  | Absolute path to Playwright browser binaries. Set when browsers are bundled with the app. |
| `GH_USER_PROFILE_TEXT`    | (none)           | Full applicant profile as JSON string. Passed via env to avoid shell escaping issues with `--profile`. |
| `GH_RESUME_PATH`          | (none)           | Absolute path to the resume PDF file.                    |
| `GH_MAX_BUDGET_PER_JOB`   | `0.50`           | Maximum LLM spend in USD. Overridden by `--max-budget` CLI arg. |
| `GH_AGENT_MODEL`          | `gemini-3-flash-preview` | Agent model name. Overridden by `--model` CLI arg. When proxy is active, non-Anthropic models are automatically mapped to `claude-sonnet-4-20250514`. |
| `BROWSER_USE_SETUP_LOGGING`| `false`         | Set to `false` to suppress browser-use's own logging setup. Hand-X sets this automatically. |

### 4.4 Environment Variable Precedence

When both an env var and a CLI arg provide the same value, the CLI arg wins (it is set into `os.environ` after parsing). The full precedence chain:

```
CLI arg > Environment variable > .env file > Pydantic default
```

---

## 5. CLI Argument Contract

The full invocation signature when spawning Hand-X:

```
hand-x \
  --job-url <URL> \
  --profile <JSON_STRING_OR_@FILEPATH> \
  --resume <PATH_TO_PDF> \
  --output-format jsonl \
  --proxy-url <VALET_PROXY_URL> \
  --runtime-grant <EPHEMERAL_TOKEN> \
  --max-steps <INT> \
  --max-budget <FLOAT> \
  --job-id <STRING> \
  --lease-id <STRING> \
  --model <MODEL_NAME> \
  [--headless] \
  [--email <EMAIL>] \
  [--password <PASSWORD>] \
  [--browsers-path <PATH>]
```

When running via the Python interpreter (development mode), the invocation becomes:

```
python3 -m ghosthands \
  --job-url <URL> \
  ...
```

The optional `apply` subcommand is accepted and silently stripped for backwards compatibility:

```
hand-x apply --job-url <URL> ...
```

### 5.1 Argument Reference

| Argument           | Required | Type    | Description                                        |
|--------------------|----------|---------|----------------------------------------------------|
| `--job-url`        | Yes      | String  | The job posting URL to navigate to and fill         |
| `--profile`        | Yes*     | String  | Applicant profile as inline JSON or `@filepath`     |
| `--test-data`      | Yes*     | String  | Path to applicant data JSON file (dev mode)         |
| `--resume`         | No       | Path    | Absolute path to resume PDF                         |
| `--output-format`  | No       | Enum    | `jsonl` (default) or `human`                        |
| `--proxy-url`      | No       | URL     | VALET LLM proxy base URL                           |
| `--runtime-grant`  | No       | String  | VALET runtime grant token                           |
| `--max-steps`      | No       | Int     | Max agent steps. Default: 50                        |
| `--max-budget`     | No       | Float   | Max LLM cost in USD. Default: 0.50                  |
| `--job-id`         | No       | String  | Job ID for event correlation                        |
| `--lease-id`       | No       | String  | Lease ID for event correlation                      |
| `--model`          | No       | String  | LLM model override                                 |
| `--headless`       | No       | Flag    | Run browser headless (flag, no value)               |
| `--email`          | No       | String  | ATS login email                                     |
| `--password`       | No       | String  | ATS login password                                  |
| `--browsers-path`  | No       | Path    | Path to Playwright browser binaries                 |

*One of `--profile` or `--test-data` is required.

---

## 6. Process Lifecycle

### 6.1 Sequence Diagram

```
Desktop (Electron)                        Hand-X (Python)
      |                                        |
      |-- spawn(hand-x, args, env) ----------->|
      |                                        |-- Initialize engine
      |                                        |-- Setup logging (stderr)
      |                                        |-- Install stdout guard
      |                                        |-- Load profile
      |                                        |-- Configure LLM proxy
      |                                        |
      |<--- {"type":"status","message":"Hand-X engine initialized"} ---|
      |<--- {"type":"status","message":"Setting up agent..."} --------|
      |<--- {"type":"status","message":"DomHand actions registered"} --|
      |                                        |-- Launch Playwright browser
      |<--- {"type":"browser_ready","cdpUrl":"ws://..."} -------------|
      |                                        |
      |    [Store cdpUrl for review session]    |
      |                                        |
      |<--- {"type":"status","step":1,...} ----|
      |                                        |-- Agent loop begins
      |<--- {"type":"field_filled",...} -------|  (repeated per field)
      |<--- {"type":"field_failed",...} -------|
      |<--- {"type":"progress",...} -----------|
      |<--- {"type":"cost",...} ---------------|
      |<--- {"type":"status","step":2,...} ----|
      |        ...                             |-- (steps repeat)
      |<--- {"type":"cost",...} ---------------|
      |                                        |
      |                                        |-- Agent loop ends
      |<--- {"type":"done","success":true,...} |
      |<--- {"type":"awaiting_review",...} ----|
      |                                        |
      |    [Show review UI to user]            |-- Blocking on stdin
      |                                        |
      |--- {"type":"complete_review"}\n ------>|
      |                                        |-- Close browser
      |<--- {"type":"done","message":"Review complete",...} ---|
      |                                        |
      |    [Process exits with code 0]         |
```

### 6.2 Exit Codes

| Code | Meaning                                   | When                                              |
|------|-------------------------------------------|----------------------------------------------------|
| `0`  | Success                                   | Normal completion (success or clean failure)        |
| `1`  | Error                                     | Fatal error, unhandled exception, or failed job     |
| `130`| Cancelled                                 | KeyboardInterrupt (SIGINT) or user cancel           |

### 6.3 Signal Handling

| Signal    | Hand-X Behavior                                              |
|-----------|--------------------------------------------------------------|
| `SIGTERM` | Caught by Python. Triggers `KeyboardInterrupt`. Browser is closed. Process exits with code 130. |
| `SIGINT`  | Same as SIGTERM.                                             |
| `SIGKILL` | Immediate termination. Browser process may be orphaned. Desktop must clean up via CDP. |

### 6.4 Lifecycle State Machine

```
SPAWNED
  |
  v
INITIALIZING  -->  (fatal error)  -->  EXIT(1)
  |
  v
BROWSER_LAUNCHING
  |
  v
AGENT_RUNNING  -->  (fatal error)  -->  EXIT(1)
  |                  (budget exceeded)  -->  done(success=false) --> EXIT(1)
  |                  (cancel via stdin)  -->  done(success=false) --> EXIT(0)
  |                  (SIGTERM)  -->  EXIT(130)
  v
AGENT_DONE
  |
  +--> success=true  -->  AWAITING_REVIEW  -->  complete_review  -->  EXIT(0)
  |                                        -->  cancel           -->  EXIT(0)
  |                                        -->  stdin EOF        -->  EXIT(0)
  |                                        -->  SIGTERM          -->  EXIT(130)
  |
  +--> success=false  -->  EXIT(1)
```

---

## 7. Error Handling

### 7.1 Process Crash (No `done` Event)

If the Hand-X process exits with a non-zero exit code and no `done` or fatal `error` event was received, the desktop app MUST synthesize an error:

```typescript
handXProcess.on('exit', (code, signal) => {
  if (!receivedDoneEvent) {
    const synthetic: ErrorEvent = {
      type: 'error',
      message: signal
        ? `Hand-X killed by signal ${signal}`
        : `Hand-X exited with code ${code}`,
      fatal: true,
      timestamp: Date.now(),
    };
    handleEvent(synthetic);
  }
});
```

### 7.2 Non-Fatal Errors

`error` events with `fatal: false` are informational. The agent continues running. Examples:

- Budget warning (approaching limit but not exceeded)
- A single field fill failure that the agent can work around
- Transient network error on a non-critical request

### 7.3 Fatal Errors

`error` events with `fatal: true` indicate the process will exit shortly. The desktop app should:

1. Stop expecting further events
2. Wait for the process exit event
3. Report the error to the user
4. Report failure to VALET

### 7.4 Timeout Handling (Desktop Side)

The desktop app should implement a global timeout. If no events are received for a configurable period (recommended: 120 seconds), the desktop should:

1. Send `{"type":"cancel"}\n` to stdin
2. Wait 5 seconds
3. If still alive, send `SIGTERM`
4. Wait 5 seconds
5. If still alive, send `SIGKILL`
6. Synthesize an error event

### 7.5 Stdin EOF Behavior

If the desktop app's stdin pipe to Hand-X is closed (e.g., desktop crashes):

- **During agent loop:** Hand-X continues running to completion. It cannot receive cancel commands but will finish naturally.
- **During `awaiting_review`:** Hand-X detects the closed stdin, closes the browser, and exits with code 0.

---

## 8. VALET Integration Architecture

### 8.1 Responsibility Split

```
Desktop App                              Hand-X
-----------                              ------
Claim job from VALET                     (not involved)
Receive runtimeGrant + leaseId           (not involved)
Start heartbeat loop                     (not involved)
Spawn Hand-X with grant + proxy URL      Receive grant + proxy URL
                                         Route ALL LLM calls through proxy
Parse JSONL events                       Emit JSONL events
Map events to VALET callbacks:
  - status/progress -> reportProgress
  - done(success) -> reportCompletion
  - done(fail) -> reportFailure
  - cost -> aggregate for final report
Manage lease renewal                     (not involved)
Complete/Fail job on VALET               (not involved)
```

**Key principle:** Hand-X does NOT talk to VALET directly when spawned by the desktop app. It has no database connection, no VALET API URL, and no callback secret. All VALET communication is the desktop app's responsibility. Hand-X's only outbound network calls are LLM requests through the proxy.

### 8.2 Runtime Grant

- The runtime grant (`lwrg_v1_...`) is an ephemeral bearer token issued by VALET when the desktop app claims a job.
- It authorizes LLM API calls through the VALET proxy for a specific job.
- Default expiration: 4 hours from issuance.
- If the grant expires during a long-running job, LLM calls will fail with HTTP 401/403. Hand-X will emit an `error` event. The desktop app should handle this by failing the job on VALET.

### 8.3 Cost Event Aggregation

The desktop app should maintain a running cost state from `cost` events:

```typescript
interface RunningCostState {
  totalCostUsd: number;
  promptTokens: number;
  completionTokens: number;
  actionCount: number;       // Incremented on each status event with a step number
  lastUpdated: number;       // Timestamp of last cost event
}
```

Cost events are cumulative (each event contains the total spend so far, not a delta). The desktop app should use the most recent `cost` event's values directly, not sum them.

When reporting completion to VALET, include:

```typescript
{
  totalCostUsd: costState.totalCostUsd,
  actionCount: costState.actionCount,
  totalTokens: costState.promptTokens + costState.completionTokens,
}
```

### 8.4 Event-to-VALET Callback Mapping

| JSONL Event         | VALET Callback         | Notes                                    |
|---------------------|------------------------|------------------------------------------|
| `status` (with step)| `reportProgress`       | Map `step`/`maxSteps` to progress percentage |
| `done` (success)    | `reportCompletion`     | Include aggregated cost data              |
| `done` (failure)    | `reportFailure`        | Include error message and cost data       |
| `error` (fatal)     | `reportFailure`        | Only if no `done` event follows           |
| `cost`              | (aggregated locally)   | Not reported individually; included in completion |
| `browser_ready`     | (not reported)         | Used locally for review session management |
| `field_filled`      | (not reported)         | UI display only                           |
| `awaiting_review`   | (not reported)         | Triggers review UI in desktop app         |

---

## 9. Binary Resolution

The desktop app must locate the Hand-X executable at spawn time. Three resolution strategies are tried in order:

### 9.1 Production: Bundled Binary

```
<app.getAppPath()>/
  resources/
    hand-x/
      hand-x-darwin-arm64/
        hand-x              <-- standalone binary (PyInstaller/Nuitka output)
      hand-x-darwin-x64/
        hand-x
      hand-x-win32-x64/
        hand-x.exe
      hand-x-linux-x64/
        hand-x
```

Resolution logic:

```typescript
const resourcePath = process.resourcesPath ?? app.getAppPath();
const binaryName = process.platform === 'win32' ? 'hand-x.exe' : 'hand-x';
const bundled = path.join(
  resourcePath, 'hand-x',
  `hand-x-${process.platform}-${process.arch}`,
  binaryName
);
```

When the bundled binary is found, it is invoked directly with CLI arguments. No Python interpreter is needed.

### 9.2 Development: Local venv Python

```typescript
const devPython = path.join(
  __dirname, '..', '..', '..', 'Hand-X', '.venv', 'bin', 'python3'
);
```

When the dev Python is found, the invocation is:

```
/path/to/Hand-X/.venv/bin/python3 -m ghosthands --job-url ... --output-format jsonl ...
```

### 9.3 Fallback: System Python

```typescript
const fallback = 'python3';  // Assumes ghosthands is pip-installed
```

Invocation:

```
python3 -m ghosthands --job-url ... --output-format jsonl ...
```

### 9.4 Resolution Function

```typescript
function resolveHandXBinary(): { binary: string; prependArgs: string[] } {
  // 1. Bundled binary
  const resourcePath = process.resourcesPath ?? app.getAppPath();
  const binaryName = process.platform === 'win32' ? 'hand-x.exe' : 'hand-x';
  const bundled = path.join(
    resourcePath, 'hand-x',
    `hand-x-${process.platform}-${process.arch}`,
    binaryName
  );
  if (fs.existsSync(bundled)) {
    return { binary: bundled, prependArgs: [] };
  }

  // 2. Dev venv
  const devPython = path.join(
    __dirname, '..', '..', '..', 'Hand-X', '.venv', 'bin', 'python3'
  );
  if (fs.existsSync(devPython)) {
    return { binary: devPython, prependArgs: ['-m', 'ghosthands'] };
  }

  // 3. System Python
  return { binary: 'python3', prependArgs: ['-m', 'ghosthands'] };
}
```

---

## 10. Desktop App Integration Pattern

### 10.1 Spawn Example

```typescript
import { spawn, ChildProcess } from 'node:child_process';
import { createInterface } from 'node:readline';

function spawnHandX(params: {
  jobUrl: string;
  profile: Record<string, unknown>;
  resumePath: string;
  proxyUrl: string;
  runtimeGrant: string;
  jobId: string;
  leaseId: string;
  maxSteps?: number;
  maxBudget?: number;
  headless?: boolean;
  email?: string;
  password?: string;
  browsersPath?: string;
}): ChildProcess {
  const { binary, prependArgs } = resolveHandXBinary();

  const cliArgs = [
    ...prependArgs,
    '--job-url', params.jobUrl,
    '--profile', JSON.stringify(params.profile),
    '--resume', params.resumePath,
    '--output-format', 'jsonl',
    '--proxy-url', params.proxyUrl,
    '--runtime-grant', params.runtimeGrant,
    '--job-id', params.jobId,
    '--lease-id', params.leaseId,
    '--max-steps', String(params.maxSteps ?? 50),
    '--max-budget', String(params.maxBudget ?? 0.50),
    ...(params.headless ? ['--headless'] : []),
    ...(params.email ? ['--email', params.email] : []),
    ...(params.password ? ['--password', params.password] : []),
    ...(params.browsersPath ? ['--browsers-path', params.browsersPath] : []),
  ];

  return spawn(binary, cliArgs, {
    env: {
      ...process.env,
      PYTHONUNBUFFERED: '1',
      GH_LLM_PROXY_URL: params.proxyUrl,
      GH_LLM_RUNTIME_GRANT: params.runtimeGrant,
      ...(params.browsersPath && { PLAYWRIGHT_BROWSERS_PATH: params.browsersPath }),
    },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
}
```

### 10.2 Event Parsing

```typescript
function attachEventParser(
  proc: ChildProcess,
  onEvent: (event: HandXEvent) => void,
): void {
  const rl = createInterface({ input: proc.stdout!, crlfDelay: Infinity });

  rl.on('line', (line: string) => {
    if (!line.trim()) return;
    try {
      const event = JSON.parse(line) as HandXEvent;
      if (event.type && event.timestamp) {
        onEvent(event);
      }
    } catch {
      // Not valid JSON -- ignore (should not happen with stdout guard)
    }
  });
}
```

### 10.3 Cancel Flow

```typescript
function cancelHandX(proc: ChildProcess): void {
  // Step 1: Send cancel command via stdin
  if (proc.stdin && !proc.stdin.destroyed) {
    proc.stdin.write(JSON.stringify({ type: 'cancel' }) + '\n');
  }

  // Step 2: SIGTERM after 5s if still alive
  const termTimer = setTimeout(() => {
    if (!proc.killed) {
      proc.kill('SIGTERM');
    }
  }, 5000);

  // Step 3: SIGKILL after 10s if still alive
  const killTimer = setTimeout(() => {
    if (!proc.killed) {
      proc.kill('SIGKILL');
    }
  }, 10000);

  proc.on('exit', () => {
    clearTimeout(termTimer);
    clearTimeout(killTimer);
  });
}
```

---

## 11. Security Considerations

### 11.1 Credential Handling

- ATS credentials (email, password) MUST be passed via environment variables, not CLI arguments. CLI arguments are visible in process listings (`ps aux`).
- The desktop app uses env vars `GH_EMAIL` and `GH_PASSWORD`. The CLI flags `--email` and `--password` exist only for developer convenience.
- Hand-X never logs credential values. They are passed through browser-use's `sensitive_data` mechanism which redacts them from prompt logs.

### 11.2 No API Keys on Client

- The desktop app MUST NOT pass `ANTHROPIC_API_KEY`, `GH_ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, or `OPENAI_API_KEY` to Hand-X.
- All LLM calls are routed through the VALET proxy using the ephemeral runtime grant.
- If `GH_LLM_PROXY_URL` is set and a direct API key is also present in the environment, Hand-X logs a warning and uses the proxy (the proxy URL takes precedence in `get_chat_model()`).

### 11.3 Domain Lockdown

- Hand-X enforces URL allowlisting via `DomainLockdown`. Only known ATS domains and CDN domains are permitted for navigation.
- The desktop app does not need to enforce domain restrictions separately; Hand-X handles this internally.

### 11.4 Runtime Grant Scope

- Each runtime grant is scoped to a single job and a specific VALET-managed LLM quota.
- The grant does not provide access to any other VALET APIs (user data, job management, etc.).
- Grant expiration is server-enforced. Expired grants return HTTP 401/403 from the proxy.

---

## 12. Mapping JSONL Events to Desktop ProgressEvent

The desktop app's existing renderer consumes `ProgressEvent` objects. The bridge layer maps Hand-X events to these:

| Hand-X Event    | Desktop ProgressEvent Type | Mapping Logic                                                  |
|-----------------|----------------------------|----------------------------------------------------------------|
| `status`        | `status`                   | Pass `message` through directly. Include step info if present. |
| `field_filled`  | `action`                   | `message: "Filled: ${field} = ${value}"`                       |
| `field_failed`  | `action`                   | `message: "Failed: ${field} -- ${error}"`                      |
| `progress`      | `status`                   | `message: "Filled ${filled}/${total} fields (round ${round})"` |
| `browser_ready` | (internal)                 | Store `cdpUrl` for review session. Do not emit to renderer.    |
| `awaiting_review`| `status`                  | `message: "Ready for review"`. Trigger review UI.              |
| `done`          | `complete`                 | Include `success`, `message`, and `resultData` in completion.  |
| `error`         | `error`                    | Pass `message` through. If `fatal`, stop processing.           |
| `cost`          | (internal)                 | Update `RunningCostState`. Optionally emit as status: `"Cost: $0.0034"` |

---

## 13. Testing the Bridge

### 13.1 Manual JSONL Smoke Test

Run Hand-X directly and verify the JSONL stream:

```bash
cd Hand-X
source .venv/bin/activate

python -m ghosthands \
  --job-url "https://job-boards.greenhouse.io/starburst/jobs/5123053008" \
  --test-data examples/apply_to_job_sample_data.json \
  --resume examples/resume.pdf \
  --output-format jsonl \
  2>/dev/null
```

Each line of stdout should be valid JSON. Verify with:

```bash
python -m ghosthands \
  --job-url "https://job-boards.greenhouse.io/starburst/jobs/5123053008" \
  --test-data examples/apply_to_job_sample_data.json \
  --resume examples/resume.pdf \
  --output-format jsonl \
  2>/dev/null | while IFS= read -r line; do
    echo "$line" | python3 -c "import sys,json; json.loads(sys.stdin.read()); print('OK')" 2>&1 || echo "INVALID: $line"
  done
```

### 13.2 Stdin Command Test

In a separate terminal, pipe a command after the agent finishes:

```bash
echo '{"type":"complete_review"}' | python -m ghosthands \
  --job-url "..." \
  --test-data examples/apply_to_job_sample_data.json \
  --output-format jsonl
```

### 13.3 Contract Validation

Both sides should validate against the shared event types. A TypeScript consumer can use a discriminated union parser:

```typescript
function isValidHandXEvent(obj: unknown): obj is HandXEvent {
  if (typeof obj !== 'object' || obj === null) return false;
  const e = obj as Record<string, unknown>;
  if (typeof e.type !== 'string') return false;
  if (typeof e.timestamp !== 'number') return false;
  const validTypes = [
    'status', 'field_filled', 'field_failed', 'progress',
    'browser_ready', 'awaiting_review', 'done', 'error', 'cost',
  ];
  return validTypes.includes(e.type);
}
```

---

## Appendix A: Complete Spawn Checklist

Before spawning Hand-X, the desktop app must ensure:

- [ ] Runtime grant is obtained from VALET (not expired)
- [ ] Proxy URL is available from bootstrap/claim payload
- [ ] Applicant profile is serialized to JSON
- [ ] Resume file path is absolute and file exists
- [ ] Browser binaries path is set (if bundled)
- [ ] `PYTHONUNBUFFERED=1` is in the environment
- [ ] Credentials are passed via env vars, not CLI args
- [ ] No raw API keys are in the spawn environment
- [ ] stdout is piped and line-buffered reader is attached
- [ ] stdin is piped for command delivery
- [ ] Process exit handler is registered for crash detection
- [ ] Heartbeat loop is running for the VALET lease

## Appendix B: Version History

| Version | Date       | Changes                           |
|---------|------------|-----------------------------------|
| 1.0.0   | 2026-03-12 | Initial specification             |
