# Hand-X Mermaid Diagrams — Quick Start

Four comprehensive diagrams are in `MERMAID-ARCHITECTURE-DIAGRAMS.md`.

## How to View

### Option 1: GitHub (Recommended)
1. Push to `feat/handx-unified` branch
2. Open PR on GitHub → diagrams render automatically in the markdown

### Option 2: Mermaid Live Editor
1. Visit https://mermaid.live
2. Copy each diagram code block from `MERMAID-ARCHITECTURE-DIAGRAMS.md`
3. Paste into editor, hit "Render"

### Option 3: Local VSCode with Mermaid Preview
1. Install extension: `Mermaid Markdown Syntax Highlighting` by bpruitt-goddard
2. Open `MERMAID-ARCHITECTURE-DIAGRAMS.md` in VSCode
3. Right-click → "Open Preview to the Side"

## Which Diagram to Use When

| Use Case | Diagram | Why |
|----------|---------|-----|
| **Explaining Hand-X to stakeholders** | Diagram 1 (System Overview) | High-level flow, shows all major components |
| **Onboarding new engineers** | Diagram 2 (Browser Lifecycle) | Walks through the entire sequence step-by-step |
| **Code reviews (browser providers, stealth)** | Diagram 3 (Class Diagram) | Shows abstraction layers, inheritance, composition |
| **Debugging protocol issues** | Diagram 4 (State Diagram) | Shows all possible event transitions and error paths |
| **Writing tests** | Diagram 2 + Diagram 4 | Test the lifecycle and state machine paths |
| **Integration with Desktop app** | Diagram 1 + Diagram 4 | Desktop app perspective (spawn, JSONL parsing, stdin) |

## Key Architectural Decisions Visualized

### 1. Bridge Protocol (Diagram 1, 4)
- Desktop App → spawn Hand-X as subprocess
- stdout: JSONL events (protocol-agnostic)
- stdin: commands (cancel, complete_review)
- stderr: structured logging

**Why:** Zero API keys in desktop binary. All LLM calls routed through VALET proxy.

### 2. Provider Abstraction (Diagram 1, 3)
- RouteSelector: URL domain → engine choice (Workday → Firefox, Greenhouse → Chromium)
- ProviderRegistry: engine name → provider class lookup
- BrowserProvider ABC: launch(), kill(), supports_cdp, engine_name

**Why:** Workday aggressively blocks Chromium. Firefox (Camoufox) bypasses detection better.

### 3. Stealth Injection Layer (Diagram 1, 3, 4)
- 7 JavaScript patches injected via `Page.addScriptToEvaluateOnNewDocument`
- Survives page navigation and iframe creation
- Disabled by default (`StealthConfig.enabled=False`)

**Why:** ATS platforms use bot-detection systems. Patches make Chromium appear as regular user.

### 4. Lease Protocol (Diagram 2, 4)
- Desktop spawns Hand-X with `lease_id` (for cost accounting)
- Hand-X emits `lease_acquired` at startup, `lease_released` on cleanup
- Enables client-side lease state management

**Why:** Tracks LLM cost per job. Desktop app needs to know when lease is released for billing.

### 5. HITL (Human-in-the-Loop) Integration (Diagram 2, 4)
- Agent fills form, emits `awaiting_review` event
- HITL Manager pauses (`pause_job()`)
- Desktop app shows browser window, waits for user action
- User sends `complete_review` command via stdin
- Hand-X resumes and emits `done`

**Why:** User reviews filled form before submission. Can cancel or let Hand-X submit.

## Diagram Validation Checklist

Before using these diagrams in documentation/presentations:

- [ ] Diagram 1 (System): All arrows flow left-to-right (Desktop → Hand-X → Browser → ATS)
- [ ] Diagram 2 (Lifecycle): Stealth injection happens BEFORE DomainLockdown validation
- [ ] Diagram 2 (Lifecycle): HITL pause happens AFTER `awaiting_review` emit
- [ ] Diagram 3 (Classes): BrowserProvider is ABC, both ChromiumProvider and CamoufoxProvider inherit from it
- [ ] Diagram 3 (Classes): StealthWatchdog depends on StealthConfig and StealthScripts
- [ ] Diagram 4 (States): Happy path: Spawned → ... → Done → Cleanup
- [ ] Diagram 4 (States): Error paths lead to error emit and cleanup
- [ ] All color codes are consistent across diagrams (blue=protocol, green=lifecycle, etc.)

## Editing the Diagrams

To modify a diagram:

1. Open `MERMAID-ARCHITECTURE-DIAGRAMS.md`
2. Find the diagram (search by ## title)
3. Edit the Mermaid syntax between ` ```mermaid ` and ` ``` `
4. Test on https://mermaid.live
5. Commit changes: `git commit -am "refactor(diagrams): update X for Y reason"`

## Common Mermaid Edits

### Add a new event type (Diagram 4)
1. Find the state where the event is emitted
2. Add edge: `StateA --> StateB: emit <event_type>`

### Add a new provider (Diagram 3)
1. Find the BrowserProvider class
2. Add: `class NewProvider { ... }`
3. Add relationship: `BrowserProvider <|-- NewProvider`

### Add a new stealth patch (Diagram 3)
1. Find StealthConfig class
2. Add field: `+<patch_name>_patch: bool = True`
3. Update StealthScripts to reference it

## Related Documentation

- **BROWSER-ARCHITECTURE-PLAN.md** — 7-week implementation roadmap (Streams 0-6)
- **PRD-DESKTOP-BRIDGE.md** — Functional requirements for Electron ↔ Hand-X bridge
- **README.md** — Quick start for developers
- **CLAUDE.md** — Architecture overview and conventions

