# Hand-X Browser Architecture — Mermaid Diagrams

Four comprehensive diagrams documenting the Hand-X system design, browser lifecycle, stealth architecture, and wire protocol.

---

## Diagram 1: System Architecture Overview

Shows the full system flow from Desktop App → Hand-X CLI → Browser Session → ATS Websites, including CDP attach path, stealth injection, domain lockdown, and lease protocol.

```mermaid
graph TD
    A["🖥️ GH-Desktop-App<br/>Electron Process"] -->|spawn| B["🐍 Hand-X CLI<br/>Python Subprocess"]

    B -->|args| C["BrowserProfile<br/>engine=auto<br/>stealth=enabled<br/>cdp_url?"]

    C -->|parse args| D{"Bridge Mode?"}

    D -->|--cdp-url| E["CDP Attach Path<br/>Desktop-Owned Browser"]
    D -->|no --cdp-url| F["Standalone Path<br/>Launch Browser"]

    E -->|connect| G["Playwright<br/>CDP Session"]
    F -->|launch| H["BrowserProvider<br/>ProviderRegistry"]

    H -->|select_engine| I["RouteSelector<br/>URL → chromium/firefox"]
    I -->|resolve| J["ChromiumProvider OR<br/>CamoufoxProvider"]
    J -->|launch| K["Browser Process"]

    K -->|connect| G

    G -->|BrowserConnectedEvent| L["StealthWatchdog"]
    L -->|inject| M["JS Injection Layer<br/>Page.addScriptToEvaluateOnNewDocument<br/>webdriver, chrome.runtime<br/>plugins, languages, etc."]

    M -->|stealth active| N["✓ Anti-Detection<br/>Chromium appears as<br/>regular user browser"]

    G -->|navigate| O["DomainLockdown<br/>Allowlist enforcement"]
    O -->|validate| P{"Domain Allowed?"}
    P -->|yes| Q["Navigate to ATS Site"]
    P -->|no| R["❌ Reject & Log"]

    Q -->|browser-use Agent| S["Action Loop<br/>DomHand fill<br/>LLM answers<br/>browser-use fallback"]

    S -->|fill application| T["Form Fields<br/>Text, Select, Checkbox<br/>Radio, Textarea"]

    T -->|submit| U["Job Application<br/>Submitted"]

    B -->|emit| V["JSONL Protocol<br/>stdout"]

    V -->|events| W["handshake<br/>lease_acquired<br/>browser_ready<br/>status/progress<br/>field_filled<br/>awaiting_review<br/>done<br/>lease_released"]

    A -->|parse JSONL| X["ProgressEvent Handler"]
    X -->|display| Y["UI Progress<br/>Cost, Step, Status"]

    A -->|stdin| Z["Command Channel<br/>cancel<br/>complete_review"]

    Z -->|JSONL cmds| B
    B -->|listen| AA["listen_for_cancel<br/>wait_for_review_command"]

    AA -->|handle| AB["Pause/Resume/Cancel<br/>HITL Integration"]

    style A fill:#e1f5ff
    style B fill:#fff3e0
    style C fill:#f3e5f5
    style M fill:#fce4ec
    style N fill:#c8e6c9
    style L fill:#ffccbc
    style O fill:#ffe0b2
    style V fill:#b2dfdb
    style W fill:#b2dfdb
    style Z fill:#b2dfdb
    style AA fill:#ffe0b2

---

## Diagram 2: Browser Lifecycle (Sequence Diagram)

Shows the complete lifecycle from Desktop App spawning Hand-X through job completion, including handshake, lease acquisition, browser launch/attach, stealth injection, agent run, HITL review, and cleanup.

```mermaid
sequenceDiagram
    participant Desktop as 🖥️ Desktop App
    participant HandX as 🐍 Hand-X CLI
    participant Bridge as Bridge Protocol
    participant Browser as 🌐 Browser Process
    participant Agent as browser-use Agent
    participant Stealth as StealthWatchdog
    participant HITL as HITL Manager
    participant App as ATS Website

    Desktop->>HandX: spawn(hand-x --job-url ... --cdp-url/launch)
    HandX->>HandX: parse_args()
    HandX->>HandX: load_profile(camelCase->snake_case)
    HandX->>Bridge: emit_handshake(lease_id, job_id)
    Bridge->>Desktop: {event:handshake, leaseId, jobId}

    HandX->>HandX: validate domain_lockdown
    HandX->>HandX: RouteSelector.select_engine(url)

    alt CDP Attach Path
        HandX->>Browser: playwright.connect_over_cdp(--cdp-url)
    else Standalone Path
        HandX->>HandX: BrowserProvider.launch()
        HandX->>Browser: launch ChromiumProvider or CamoufoxProvider
    end

    HandX->>Bridge: emit_lease_acquired(lease_id)
    Bridge->>Desktop: {event:lease_acquired, leaseId}

    Browser-->>HandX: BrowserConnectedEvent (CDP ready)
    HandX->>Stealth: on_BrowserConnectedEvent()
    Stealth->>Stealth: get_stealth_scripts(config)
    Stealth->>Browser: Page.addScriptToEvaluateOnNewDocument() x6

    HandX->>Browser: navigate(url)
    Browser->>App: GET /job/123
    App-->>Browser: HTML + ATS Form

    HandX->>Bridge: emit_browser_ready(url)
    Bridge->>Desktop: {event:browser_ready, url}

    HandX->>Agent: create_job_agent(profile, resume)
    Agent->>Agent: register @tools.action hooks (DomHand, etc)

    activate Agent
    Agent->>Agent: agentic_loop(max_steps=50)
    Agent->>App: screenshot()
    Agent->>App: fill_field(name, value)
    Agent->>App: click(selector)
    Agent->>Bridge: emit_field_filled(field, value, method=domhand)
    Bridge->>Desktop: {event:field_filled, field, value}
    deactivate Agent

    HandX->>Bridge: emit_status(message, step=N, maxSteps=50)
    Bridge->>Desktop: {event:status, message, step}

    alt Agent Fills All Fields
        Agent->>App: submit()
        App-->>Agent: 200 OK / redirect
        HandX->>Bridge: emit_awaiting_review(field_count=12)
        Bridge->>Desktop: {event:awaiting_review, fieldCount}
    else Agent Gets Stuck
        HandX->>Bridge: emit_awaiting_review(error=..., field_count=8)
        Bridge->>Desktop: {event:awaiting_review, error, fieldCount}
    end

    Desktop->>HandX: (user reviews in browser)
    Desktop->>HandX: complete_review (stdin)
    HandX->>HITL: wait_for_review_command(timeout=3600)
    HITL-->>HandX: ReviewCommand(action=complete, ...)

    HandX->>Browser: (browser stays open, user sees final state)
    HandX->>Browser: close()
    Browser-->>HandX: process exit

    HandX->>Bridge: emit_done(success=true, message, fields_filled, lease_id)
    Bridge->>Desktop: {event:done, success, message, fieldsFilled, leaseId}

    HandX->>Bridge: emit_lease_released(lease_id)
    Bridge->>Desktop: {event:lease_released, leaseId}

    Desktop->>Desktop: cleanup browser window, update UI
    HandX->>HandX: exit(0)

    style Desktop fill:#e1f5ff
    style HandX fill:#fff3e0
    style Browser fill:#c8e6c9
    style Agent fill:#ffccbc
    style Stealth fill:#fce4ec
    style HITL fill:#ffe0b2
    style Bridge fill:#b2dfdb

---

## Diagram 3: Stealth + Provider Architecture (Class Diagram)

Shows the abstraction layers: BrowserProvider ABC with ChromiumProvider and CamoufoxProvider implementations, ProviderRegistry for engine lookup, RouteSelector for URL-based routing, and StealthConfig/StealthWatchdog for anti-detection JS injection.

```mermaid
classDiagram
    class BrowserProvider {
        <<abstract>>
        +launch(profile) tuple[str, int|None]
        +kill() void
        +get_default_args(profile) list[str]
        +engine_name str
        +supports_cdp bool
    }

    class ChromiumProvider {
        +launch(profile)
        +kill()
        +get_default_args(profile)
        +engine_name: chromium
        +supports_cdp: true
    }

    class CamoufoxProvider {
        +launch(profile)
        +kill()
        +get_default_args(profile)
        +engine_name: firefox
        +supports_cdp: true
    }

    BrowserProvider <|-- ChromiumProvider
    BrowserProvider <|-- CamoufoxProvider

    class ProviderRegistry {
        -_providers: dict[str, type[BrowserProvider]]
        +register(name, provider_class) void
        +get(name) type[BrowserProvider]
        +available() list[str]
        +reset() void
    }

    ProviderRegistry "1" --> "many" BrowserProvider

    class RouteSelector {
        -FIREFOX_PREFERRED: dict[str, list[str]]
        -CHROMIUM_PREFERRED: dict[str, list[str]]
        +select_engine(url, platform) str
        -_extract_domain(url) str
    }

    RouteSelector "1" --> "1" ProviderRegistry

    class StealthConfig {
        +enabled: bool = False
        +webdriver_patch: bool = True
        +chrome_runtime_patch: bool = True
        +plugins_patch: bool = True
        +languages_patch: bool = True
        +permissions_patch: bool = True
        +webgl_patch: bool = True
        +iframe_contentwindow_patch: bool = False
        +media_codecs_patch: bool = True
    }

    class StealthWatchdog {
        -LISTENS_TO: [BrowserConnectedEvent]
        +on_BrowserConnectedEvent(event)
    }

    class StealthScripts {
        +WEBDRIVER_PATCH: str
        +CHROME_RUNTIME_PATCH: str
        +PLUGINS_PATCH: str
        +LANGUAGES_PATCH: str
        +PERMISSIONS_PATCH: str
        +WEBGL_PATCH: str
        +MEDIA_CODECS_PATCH: str
        +get_stealth_scripts(config) list[str]
    }

    StealthWatchdog "1" --> "1" StealthConfig
    StealthWatchdog "1" --> "1" StealthScripts
    StealthScripts "1" --> "many" "JS Injection Patches"

    class BrowserProfile {
        +stealth: StealthConfig
        +engine: str
        +cdp_url: str|None
        +headless: bool
        +disable_blink_features: bool
    }

    BrowserProfile "1" --> "1" StealthConfig
    BrowserProfile "1" --> "1" ChromiumProvider
    BrowserProfile "1" --> "1" CamoufoxProvider

    class BrowserSession {
        +browser_profile: BrowserProfile
        +_setup_watchdogs() void
        +_cdp_add_init_script(js) void
    }

    BrowserSession "1" --> "1" BrowserProfile
    BrowserSession "1" --> "1" StealthWatchdog

    style BrowserProvider fill:#e1f5ff
    style ChromiumProvider fill:#b3e5fc
    style CamoufoxProvider fill:#b3e5fc
    style ProviderRegistry fill:#fff3e0
    style RouteSelector fill:#fff3e0
    style StealthConfig fill:#fce4ec
    style StealthWatchdog fill:#ffccbc
    style StealthScripts fill:#ffccbc
    style BrowserProfile fill:#f3e5f5
    style BrowserSession fill:#f3e5f5

---

## Diagram 4: JSONL Wire Protocol (State Diagram)

Shows the event flow from handshake through job completion. Desktop App spawns Hand-X → lease acquired → browser ready → agent loop (status/progress/field_filled/field_failed events) → awaiting_review (HITL) → done → lease_released. Includes error paths and state transitions.

```mermaid
stateDiagram-v2
    [*] --> Spawned

    Spawned --> Handshake: Hand-X CLI starts
    Handshake --> LeaseAcquired: emit handshake\n{event:handshake, leaseId, jobId}

    LeaseAcquired --> BrowserLaunch: emit lease_acquired\n{event:lease_acquired, leaseId}

    BrowserLaunch --> StealthInjection: Launch or attach browser
    StealthInjection --> DomainValidation: Inject anti-detection JS\nPage.addScriptToEvaluateOnNewDocument

    DomainValidation --> DomainReject: Domain not allowed
    DomainReject --> ErrorEmit: emit error\n{event:error, message}
    ErrorEmit --> CleanupFailed: Shutdown browser

    DomainValidation --> BrowserReady: Domain validated\nemit browser_ready\n{event:browser_ready, url}

    BrowserReady --> AgentStart: Create browser-use Agent\nRegister @tools.action handlers

    AgentStart --> AgentLoop: Begin agentic loop\nmax_steps=50

    AgentLoop --> ScreenshotTaken: Take screenshot
    ScreenshotTaken --> Thinking: Vision + LLM reasoning

    Thinking --> FillField: Identify form field
    Thinking --> ClickElement: Perform click
    Thinking --> TypeText: Enter text
    Thinking --> SelectOption: Choose dropdown

    FillField --> FieldFilledEmit: emit field_filled\n{event:field_filled, field, value, method}
    ClickElement --> FieldFilledEmit
    TypeText --> FieldFilledEmit
    SelectOption --> FieldFilledEmit

    FieldFilledEmit --> StepProgress: emit status + progress\n{event:status, step, maxSteps}
    StepProgress --> LoopDecision{Max steps?}

    LoopDecision -->|No| AgentLoop
    LoopDecision -->|Yes| StopAgent

    Thinking --> FieldFailedPath: LLM: fallback needed
    FieldFailedPath --> FieldFailedEmit: emit field_failed\n{event:field_failed, field, reason}
    FieldFailedEmit --> StepProgress

    AgentLoop --> SubmitButton: All fields filled
    SubmitButton --> ClickSubmit: click(submit_button)
    ClickSubmit --> Submitted: Application submitted

    Submitted --> AwaitingReview: emit awaiting_review\n{event:awaiting_review, fieldCount, leaseId}
    AwaitingReview --> HITLPause: HITL Manager pause_job()
    HITLPause --> WaitStdin: listen_for_review_command(stdin)\ntimeout=3600s

    WaitStdin --> ReviewReceived: Desktop sends complete_review (stdin)\nProgressEvent: {action:complete, ...}
    ReviewReceived --> ReviewResume: HITL resume()

    WaitStdin --> Timeout: No command in 3600s
    Timeout --> CancelJob: emit done(success=false, message)

    ReviewResume --> Done: emit done\n{event:done, success, message, fieldsFilled, leaseId}
    CancelJob --> LeaseRelease

    Done --> LeaseRelease: emit lease_released\n{event:lease_released, leaseId}

    CancelJob --> LeaseRelease

    CleanupFailed --> [*]
    LeaseRelease --> Cleanup: Close browser
    Cleanup --> [*]

    state AgentLoop {
        [*] --> ScreenshotTaken
        ScreenshotTaken --> Thinking
        Thinking --> StepProgress
    }

    style Handshake fill:#b2dfdb
    style LeaseAcquired fill:#b2dfdb
    style BrowserLaunch fill:#c8e6c9
    style StealthInjection fill:#fce4ec
    style DomainValidation fill:#ffe0b2
    style DomainReject fill:#ffccbc
    style BrowserReady fill:#b2dfdb
    style AgentStart fill:#fff3e0
    style AgentLoop fill:#fff3e0
    style ScreenshotTaken fill:#fff3e0
    style Thinking fill:#fff3e0
    style FieldFilledEmit fill:#b2dfdb
    style FieldFailedEmit fill:#ffccbc
    style AwaitingReview fill:#f3e5f5
    style HITLPause fill:#f3e5f5
    style WaitStdin fill:#f3e5f5
    style ReviewReceived fill:#f3e5f5
    style Done fill:#c8e6c9
    style LeaseRelease fill:#b2dfdb
    style Cleanup fill:#c8e6c9
```

---

## Legend & Key Concepts

### Colors (Semantic)

- **Blue (#b2dfdb)** — JSONL Protocol Events (emit_* calls)
- **Green (#c8e6c9)** — Browser Lifecycle (launch, connect, cleanup)
- **Orange (#fff3e0)** — Agent/LLM Actions
- **Pink (#fce4ec)** — Stealth/Security Layer
- **Salmon (#ffccbc)** — Error Paths & Cleanup
- **Yellow (#ffe0b2)** — Validation & Routing
- **Purple (#f3e5f5)** — HITL/Human-in-the-Loop

### Key Protocols

**JSONL Stream (stdout)**
- `{event: <type>, timestamp: <ms>, ...fields}`
- Must be line-buffered and atomic (thread-safe via `_emit_lock`)
- Pipe breakage is caught and suppressed

**stdin Command Channel (Desktop → Hand-X)**
- `listen_for_cancel()` reads lines concurrently with agent
- `wait_for_review_command()` blocks HITL on `awaiting_review` event
- Commands: `cancel`, `complete_review`, `pause`

**Browser Stealth Injection**
- 7 patches injected via `Page.addScriptToEvaluateOnNewDocument`
- Survives page navigation and iframe creation
- Controlled by `StealthConfig.enabled` flag

**Provider Abstraction**
- Decouples engine selection from implementation
- `ProviderRegistry` maps engine names to classes
- `RouteSelector` picks engine based on target URL domain

**Lease Protocol**
- Desktop passes `lease_id` at spawn time (for cost tracking)
- Hand-X emits `lease_acquired` on startup
- Hand-X emits `lease_released` on cleanup
- Enables client-side lease state management & cost accumulation

---

## File Locations (Key Modules)

| Component | File |
|-----------|------|
| Stealth Config | `browser_use/browser/stealth/config.py` |
| Stealth Watchdog | `browser_use/browser/watchdogs/stealth_watchdog.py` |
| Stealth Scripts | `browser_use/browser/stealth/scripts.py` |
| BrowserProvider ABC | `browser_use/browser/providers/base.py` |
| ChromiumProvider | `browser_use/browser/providers/chromium.py` |
| CamoufoxProvider | `browser_use/browser/providers/camoufox.py` |
| ProviderRegistry | `browser_use/browser/providers/registry.py` |
| RouteSelector | `browser_use/browser/providers/route_selector.py` |
| JSONL Emitter | `ghosthands/output/jsonl.py` |
| Bridge Protocol | `ghosthands/bridge/protocol.py` |
| HITL Manager | `ghosthands/worker/hitl.py` |
| CLI Entry Point | `ghosthands/cli.py` |
| Domain Lockdown | `ghosthands/security/domain_lockdown.py` |


