# Hand-X Architecture — Visual Guide

This guide helps you understand the Hand-X browser automation engine through four comprehensive Mermaid diagrams.

## Quick Links

| Need | Go To | Purpose |
|------|-------|---------|
| **Understand the big picture** | [Diagram 1: System Architecture](./MERMAID-ARCHITECTURE-DIAGRAMS.md#diagram-1-system-architecture-overview) | Shows all layers: Desktop App → Hand-X → Browser → ATS |
| **Follow the step-by-step flow** | [Diagram 2: Browser Lifecycle](./MERMAID-ARCHITECTURE-DIAGRAMS.md#diagram-2-browser-lifecycle-sequence-diagram) | Traces from spawn through completion |
| **Study abstraction layers** | [Diagram 3: Classes & Providers](./MERMAID-ARCHITECTURE-DIAGRAMS.md#diagram-3-stealth--provider-architecture-class-diagram) | BrowserProvider ABC, Stealth injection, Registry pattern |
| **Debug protocol issues** | [Diagram 4: State Machine](./MERMAID-ARCHITECTURE-DIAGRAMS.md#diagram-4-jsonl-wire-protocol-state-diagram) | All possible states and transitions |
| **How to use these diagrams** | [DIAGRAMS-USAGE.md](./DIAGRAMS-USAGE.md) | Tips for viewing, editing, sharing |

## The Architecture at a Glance

```
┌─────────────────────────────────────────────────────────────┐
│                  GH-Desktop-App (Electron)                  │
│  Spawns Hand-X subprocess, parses JSONL, shows UI progress  │
└──────────────────────────┬──────────────────────────────────┘
                           │
                    spawn + args
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              Hand-X CLI (Python Subprocess)                 │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ Bridge Mode: CDP Attach or Standalone Launch?        │  │
│  └────────┬──────────────────────┬─────────────────────┘  │
│           │                      │                        │
│         --cdp-url           no --cdp-url                  │
│           │                      │                        │
│           ▼                      ▼                        │
│  ┌─────────────────┐    ┌──────────────────────────┐     │
│  │ CDP Attach      │    │ Launch Browser           │     │
│  │ (Desktop-owned) │    │ RouteSelector→Engine→   │     │
│  └─────────────────┘    │ Provider.launch()        │     │
│           │             └──────────────────────────┘     │
│           └──────────────────┬──────────────────────────┘  │
│                              │                             │
│                    ┌─────────▼─────────┐                   │
│                    │  Browser Session  │                   │
│                    │  (Playwright)     │                   │
│                    └─────────┬─────────┘                   │
│                              │                             │
│            ┌─────────────────┼─────────────────┐           │
│            │                 │                 │           │
│            ▼                 ▼                 ▼           │
│    ┌──────────────┐  ┌──────────────┐  ┌───────────────┐ │
│    │   Stealth    │  │   Domain     │  │  Agent Loop   │ │
│    │  Watchdog    │  │  Lockdown    │  │  (DomHand +   │ │
│    │ (JS Inject)  │  │ (Validate)   │  │   LLM)        │ │
│    └──────────────┘  └──────────────┘  └───────────────┘ │
│                              │                             │
│                    stdout: JSONL Events                    │
│                    stdin: Commands                         │
│                                                             │
│    handshake → lease_acquired → browser_ready → ...        │
│    → awaiting_review (HITL pause) → done → lease_released │
└─────────────────────────────────────────────────────────────┘
```

## Six Core Architectural Insights

### 1. Bridge Protocol (Desktop ↔ Hand-X)
**Problem:** Electron process can't directly call Python libraries.
**Solution:** Spawn Hand-X as subprocess, communicate via JSONL + stdin.

```
Desktop (Electron)
  │
  ├─ stdout (JSONL) ← progress events
  │  {event:handshake, leaseId, jobId}
  │  {event:browser_ready, url}
  │  {event:field_filled, field, value}
  │  {event:done, success, message}
  │  {event:lease_released, leaseId}
  │
  ├─ stdin (commands) → control flow
  │  cancel
  │  complete_review
  │
  └─ All LLM calls → VALET proxy (no API keys in desktop app)
```

### 2. Provider Abstraction (Engine Selection)
**Problem:** Workday aggressively blocks Chromium. Firefox is better. But Greenhouse works fine with Chromium.
**Solution:** Abstract BrowserProvider, use RouteSelector to pick engine per domain.

```
RouteSelector rules:
  Workday domains  → firefox (CamoufoxProvider)
  Greenhouse, Lever, iCIMS, Taleo, SuccessFactors → chromium (ChromiumProvider)
  Unknown domains  → chromium (fallback)

ProviderRegistry (class-level dict):
  'chromium' → ChromiumProvider
  'firefox' → CamoufoxProvider
  (extensible for new engines)
```

### 3. Stealth Layer (Anti-Detection)
**Problem:** ATS platforms use bot-detection JavaScript. They block Chromium if `navigator.webdriver` is true.
**Solution:** Inject 7 patches via `Page.addScriptToEvaluateOnNewDocument`.

```
Patches:
  1. navigator.webdriver = false
  2. chrome.runtime overridden
  3. navigator.plugins = fake array
  4. navigator.languages = ['en-US']
  5. Permissions API mocked
  6. WebGL properties spoofed
  7. Media codec detection bypassed

When: BrowserConnectedEvent (after successful CDP connection)
Survival: Persists across page navigation + iframe creation
Control: StealthConfig.enabled flag (default=False)
```

### 4. Lease Protocol (Cost Tracking)
**Problem:** Desktop app needs to track LLM cost per job application.
**Solution:** Use lease_id (UUID passed at spawn) to correlate events.

```
Timeline:
  [Desktop] spawn(..., lease_id=<uuid>)
  [Hand-X] emit_handshake(lease_id=<uuid>)
  [Hand-X] emit_lease_acquired(lease_id=<uuid>)
  [Hand-X] emit_status(step=1) [each step may have LLM cost]
  [Hand-X] emit_done(lease_id=<uuid>, cost=$0.15)
  [Hand-X] emit_lease_released(lease_id=<uuid>)
  [Desktop] associate cost with job_id via lease_id
```

### 5. HITL (Human-in-the-Loop)
**Problem:** Agent might fill form incorrectly. User needs to review before submission.
**Solution:** Pause agent on `awaiting_review` event, wait for `complete_review` command on stdin.

```
Workflow:
  [Agent] fills form → emits field_filled events
  [Agent] completes → emits awaiting_review
  [HITL] pause_job() [agent blocked in wait_for_resume_command()]
  [Desktop] shows browser window to user
  [User] reviews in browser, clicks "Submit" or "Cancel"
  [Desktop] sends complete_review command via stdin
  [Hand-X] resumes, emits done + lease_released
```

### 6. Domain Lockdown (Security)
**Problem:** Agent could be tricked to navigate to malicious site.
**Solution:** Validate target URL against allowlist before navigation.

```
Allowlist:
  Default: *.myworkdayjobs.com, *.greenhouse.io, *.lever.co, etc.
  Custom: --allowed-domains parameter (comma-separated)

On navigation:
  [Agent] calls page.goto(url)
  [DomainLockdown] validates URL domain
  [If allowed] proceed to navigation
  [If blocked] emit error, stop agent
```

## How These Work Together

### Happy Path (Full Lifecycle)

```
1. [Desktop] spawn('hand-x', { --job-url='...', --lease-id='uuid-123' })
2. [Hand-X] parse_args() → BrowserProfile(engine='auto', stealth.enabled=True)
3. [Hand-X] emit_handshake(lease_id='uuid-123', job_id='job-456')
4. [Hand-X] select_engine('https://company.myworkdayjobs.com') → 'firefox'
5. [Hand-X] CamoufoxProvider.launch() → browser process starts
6. [Hand-X] emit_lease_acquired(lease_id='uuid-123')
7. [StealthWatchdog] on BrowserConnectedEvent → inject 7 JS patches
8. [DomainLockdown] validate 'company.myworkdayjobs.com' → allowed
9. [Hand-X] page.goto('https://company.myworkdayjobs.com/job/123')
10. [Agent] screenshot → vision → think → fill fields
11. [Agent] emit_field_filled('first_name', 'Jane', method='domhand')
12. [Agent] emit_status(message='Filled form fields', step=5, maxSteps=50)
13. [Agent] click(submit) → page submitted
14. [Hand-X] emit_awaiting_review(field_count=12, lease_id='uuid-123')
15. [HITL] pause_job() ← agent blocks in wait_for_review_command()
16. [Desktop] display browser window to user
17. [User] clicks "Submit" in desktop app
18. [Desktop] echo 'complete_review' > hand-x.stdin
19. [Hand-X] read_stdin_line() → 'complete_review'
20. [Hand-X] resume from HITL
21. [Hand-X] emit_done(success=True, message='Application submitted', lease_id='uuid-123')
22. [Hand-X] emit_lease_released(lease_id='uuid-123')
23. [Hand-X] cleanup() → browser.close()
24. [Hand-X] exit(0)
25. [Desktop] parse all JSONL events → update UI → final state
```

## Testing Strategy (by Diagram)

| Diagram | What to Test | How |
|---------|--------------|-----|
| **1** | System paths | Unit test: parse_args, bridge mode decision, RouteSelector.select_engine |
| **2** | Lifecycle transitions | Integration test: real browser, mock LLM, trace all events in order |
| **3** | Provider abstraction | Mock test: register fake provider, test registry lookup, test stealth injection |
| **4** | State machine | State test: drive state machine to all 30 states, verify legal transitions |

## Where Each Diagram Appears

- **BROWSER-ARCHITECTURE-PLAN.md** — Referenced in Streams 0-6 discussion
- **PRD-DESKTOP-BRIDGE.md** — Explains bridge protocol requirements
- **CLAUDE.md** — Architecture overview section
- **PR descriptions** — Help reviewers understand changes
- **Code reviews** — Visual aid for discussing abstraction layers

## Next Steps

1. Read Diagram 1 (system overview) — understand the layers
2. Read Diagram 2 (lifecycle) — see how data flows
3. Read Diagram 3 (classes) — study the abstraction
4. Read Diagram 4 (states) — drive test case generation
5. Use DIAGRAMS-USAGE.md to learn how to edit/share

For questions on specific components, refer to the file locations table in the Mermaid document.

