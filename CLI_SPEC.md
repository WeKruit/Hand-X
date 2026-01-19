# Browser-Use CLI: Complete Implementation Specification

> **Standalone spec for developers** - Everything needed to implement the browser-use CLI with persistent sessions, Python execution, and multi-browser support.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture Decision: Why NOT a Separate Daemon](#architecture-decision)
3. [The Correct Architecture: Session Server](#the-correct-architecture)
4. [Browser Modes](#browser-modes)
5. [Persistent Python Execution](#persistent-python-execution)
6. [API Key & Monetization](#api-key--monetization)
7. [Complete Command List](#complete-command-list)
8. [Code Structure](#code-structure)
9. [Implementation Heuristics](#implementation-heuristics)
10. [Future Work](#future-work)

---

## Overview

Build a **fast CLI** (`browser-use`) with:

- **Persistent browser sessions** - Browser stays open between commands
- **Multiple browser modes** - Chromium, Real Browser (user profile), Remote (Cloud)
- **Persistent Python execution** - Jupyter-like REPL for scraping/automation
- **Agent task execution** - Run AI agents on persistent sessions (requires API key)
- **Claude Code integration** - API for AI assistants to control browsers

### Design Principles

1. **Work WITH BrowserSession, not around it** - No conflicts with existing internals
2. **CLI must be instant** (<50ms) - Stdlib only in CLI layer
3. **Promote Browser-Use Cloud** - Remote browsers + proxies = revenue
4. **Python persistence is a killer feature** - Enable complex automation workflows

---

## Architecture Decision

### Why NOT a Separate Daemon?

After analyzing BrowserSession internals, a **separate daemon would cause major conflicts**:

| Conflict Area | Problem |
|---------------|---------|
| **Process Ownership** | `LocalBrowserWatchdog` owns the subprocess. Two owners = race conditions on kill/cleanup |
| **CDP Connection** | Single `_cdp_client_root` per session. Duplicate connections = event chaos |
| **Event Bus** | 11 watchdogs register handlers. Separate daemon misses critical events |
| **Session State** | Shared mutable state (focus, cache, downloads). Daemon would cause inconsistencies |
| **Cleanup** | BrowserSession.kill() stops all handlers. Daemon wouldn't participate |

### The go-rod Launcher Insight

The go-rod launcher is excellent for **spawning browsers and getting CDP URLs**, but browser-use already has this via:
- `LocalBrowserWatchdog` - Process management
- `BrowserProfile` - Configuration
- CDP URL discovery via `/json/version`

**We don't need to replace this. We need to keep BrowserSession instances alive.**

---

## The Correct Architecture

### Session Server (Not Daemon)

Instead of a daemon that manages browsers directly, we build a **server that manages BrowserSession instances**:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              FAST CLI                                       │
│                         (stdlib only: <50ms startup)                        │
│                                                                             │
│   $ browser-use open https://example.com --session work --browser real      │
│   $ browser-use python "data = browser.scrape('table')"                     │
│   $ browser-use run "fill the form" --session work  # Requires API key      │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                          Unix Socket │ /tmp/browser-use.sock
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           SESSION SERVER                                    │
│                   (keeps BrowserSession instances alive)                    │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────────────┐     │
│  │                      Session Registry                              │     │
│  │                                                                    │     │
│  │  sessions = {                                                      │     │
│  │    "work": {                                                       │     │
│  │      browser_session: BrowserSession,  # ← Uses existing class!   │     │
│  │      python_session: PythonSession,    # ← Jupyter-like REPL      │     │
│  │      mode: "real",                                                 │     │
│  │      ...                                                           │     │
│  │    }                                                               │     │
│  │  }                                                                 │     │
│  └────────────────────────────────────────────────────────────────────┘     │
│                                                                             │
│  The server USES BrowserSession, doesn't replace it.                        │
│  All watchdogs, CDP management, event bus work normally.                    │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      │ (BrowserSession handles everything)
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              BROWSER                                        │
│                                                                             │
│   Mode: chromium     │  Mode: real           │  Mode: remote               │
│   ─────────────────  │  ──────────────────   │  ─────────────────          │
│   Playwright binary  │  User's Chrome        │  Browser-Use Cloud          │
│   Temp profile       │  Real profile         │  CDP via API                │
│   Headless/headed    │  With extensions      │  Proxies available          │
│                      │  Logged-in sessions   │  BROWSER_USE_API_KEY req.   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Key Insight: BrowserSession Already Does Everything

BrowserSession handles:
- ✅ Browser process lifecycle (LocalBrowserWatchdog)
- ✅ CDP connection management (SessionManager)
- ✅ Event-driven architecture (EventBus + 11 watchdogs)
- ✅ Crash detection & recovery
- ✅ Screenshot, DOM, downloads, popups, etc.

**Our job is just to:**
1. Keep BrowserSession instances in memory (don't let them get garbage collected)
2. Provide a fast CLI to interact with them
3. Add Python execution on top
4. Route commands to the right session

---

## Browser Modes

Three browser modes, configured via `--browser` flag:

### 1. Chromium (Default)
```bash
browser-use open https://example.com --browser chromium
browser-use open https://example.com --browser chromium --headed
```

- Uses Playwright-managed Chromium
- Fresh profile (isolated)
- Fast, reliable, headless by default
- **No API key required**

### 2. Real Browser
```bash
browser-use open https://example.com --browser real
browser-use open https://example.com --browser real --profile "Profile 1"
```

- Uses user's actual Chrome installation
- Preserves login sessions, cookies, extensions
- Great for authenticated workflows
- See: https://docs.browser-use.com/customize/browser/real-browser
- **No API key required**

### 3. Remote (Browser-Use Cloud)
```bash
browser-use open https://example.com --browser remote
browser-use open https://example.com --browser remote --proxy residential
```

- Cloud-hosted browser via Browser-Use API
- Supports proxies (residential, datacenter)
- See: https://docs.browser-use.com/customize/browser/remote
- **Requires BROWSER_USE_API_KEY** ← Revenue!

### Implementation

Maps directly to existing BrowserSession/BrowserProfile:

```python
def create_browser_session(mode: str, headed: bool, profile: str | None) -> BrowserSession:
    if mode == "chromium":
        return BrowserSession(
            browser_profile=BrowserProfile(headless=not headed)
        )

    elif mode == "real":
        # Real browser with user profile
        return BrowserSession(
            browser_profile=BrowserProfile(
                use_own_browser=True,
                chrome_instance_path=find_chrome(),  # Auto-detect
                user_data_dir=get_chrome_profile(profile),
                headless=False,  # Real browser is always visible
            )
        )

    elif mode == "remote":
        require_api_key()  # Raises if missing
        return BrowserSession(
            use_cloud=True,
            cloud_config=CloudConfig(
                proxy_type=proxy,  # residential, datacenter, none
            )
        )
```

---

## Persistent Python Execution

### The Killer Feature

Inspired by bu-use's `PythonSession`, we add Jupyter-like persistent execution:

```bash
# Turn 1: Scrape data
$ browser-use python "products = browser.extract('all product names and prices')"

# Turn 2: Variables persist!
$ browser-use python "print(len(products))"
42

# Turn 3: Process and save
$ browser-use python "
import json
with open('products.json', 'w') as f:
    json.dump(products, f)
"

# Turn 4: Loop through pages (deterministic automation!)
$ browser-use python "
for page in range(1, 10):
    browser.click(f'page-{page}')
    data = browser.extract('prices')
    all_data.extend(data)
"
```

### Why This is Powerful

1. **Deterministic automation** - Write loops, conditionals in Python
2. **Data persistence** - Variables survive across commands
3. **Full Python ecosystem** - pandas, numpy, BeautifulSoup, etc.
4. **Browser access** - `browser` object provides full control
5. **Incremental development** - Test code step by step

### Implementation

```python
@dataclass
class PythonSession:
    """Jupyter-like persistent Python execution."""
    namespace: dict[str, Any] = field(default_factory=dict)
    execution_count: int = 0
    history: list[ExecutionRecord] = field(default_factory=list)

    def __post_init__(self):
        # Pre-populate namespace with useful imports
        self.namespace.update({
            'json': __import__('json'),
            're': __import__('re'),
            'Path': Path,
            # browser object injected per-session
        })

    def execute(self, code: str, browser: BrowserSession) -> ExecutionResult:
        """Execute code in persistent namespace."""
        self.namespace['browser'] = BrowserWrapper(browser)
        self.execution_count += 1

        # Capture stdout
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            try:
                # Try as expression first (for REPL-like behavior)
                result = eval(code, self.namespace)
                if result is not None:
                    print(repr(result))
            except SyntaxError:
                # Execute as statements
                exec(code, self.namespace)

        output = stdout.getvalue()
        self.history.append(ExecutionRecord(code, output))
        return ExecutionResult(success=True, output=output)
```

### BrowserWrapper

Provides convenient methods for Python code:

```python
class BrowserWrapper:
    """Convenient browser access for Python code."""

    def __init__(self, session: BrowserSession):
        self.session = session

    @property
    def url(self) -> str:
        return self.session.current_url

    @property
    def title(self) -> str:
        return self.session.title

    def goto(self, url: str):
        """Navigate to URL."""
        asyncio.run(self.session.navigate(url))

    def click(self, selector_or_index: str | int):
        """Click element by selector or index."""
        asyncio.run(self.session.click(selector_or_index))

    def type(self, text: str, selector: str | int | None = None):
        """Type text into element."""
        asyncio.run(self.session.type(text, selector))

    def extract(self, query: str) -> Any:
        """Extract structured data using LLM."""
        # Uses the existing extract tool
        asyncio.run(self.session.extract(query))

    def screenshot(self) -> bytes:
        """Take screenshot, return bytes."""
        return asyncio.run(self.session.screenshot())

    @property
    def html(self) -> str:
        """Get page HTML."""
        return asyncio.run(self.session.get_html())

    @property
    def text(self) -> str:
        """Get page text content."""
        return asyncio.run(self.session.get_text())
```

---

## API Key & Monetization

### Strategy

1. **Free tier** - Chromium and Real browser modes work without API key
2. **Paid features** - Remote browsers, proxies, and **agent tasks** require API key
3. **Easy injection** - Multiple ways to provide key

### When API Key is Required

| Feature | API Key Required? |
|---------|-------------------|
| `--browser chromium` | No |
| `--browser real` | No |
| `--browser remote` | **Yes** |
| `browser-use run "<task>"` (agent) | **Yes** |
| `browser-use python "..."` | No |
| Direct browser control (click, type, etc.) | No |

### API Key Injection (Easy for Users)

**Option 1: Environment variable** (recommended)
```bash
export BROWSER_USE_API_KEY=your_key_here
browser-use run "fill the form"
```

**Option 2: Config file** (`~/.config/browser-use/config.json`)
```json
{
  "api_key": "your_key_here"
}
```

**Option 3: CLI flag** (one-time)
```bash
browser-use run "fill the form" --api-key your_key_here
```

**Option 4: Interactive prompt** (first-time setup)
```bash
$ browser-use run "fill the form"

╭─────────────────────────────────────────────────────────────╮
│  🔑 Browser-Use API Key Required                            │
│                                                             │
│  Agent tasks require an API key.                            │
│  Get yours at: https://browser-use.com/dashboard            │
│                                                             │
│  Enter API key: ________________________________________    │
│                                                             │
│  [ ] Save to ~/.config/browser-use/config.json              │
╰─────────────────────────────────────────────────────────────╯
```

### Implementation

```python
def require_api_key(feature: str = "this feature") -> str:
    """Get API key or raise helpful error."""

    # 1. Check environment
    key = os.environ.get("BROWSER_USE_API_KEY")
    if key:
        return key

    # 2. Check config file
    config_path = Path.home() / ".config" / "browser-use" / "config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        if key := config.get("api_key"):
            return key

    # 3. Interactive prompt (if TTY)
    if sys.stdin.isatty():
        return prompt_for_api_key(feature)

    # 4. Error with helpful message
    raise APIKeyRequired(f"""
╭─────────────────────────────────────────────────────────────╮
│  🔑 Browser-Use API Key Required                            │
│                                                             │
│  {feature} requires an API key.                             │
│                                                             │
│  Get yours at: https://browser-use.com/dashboard            │
│                                                             │
│  Then set it via:                                           │
│    export BROWSER_USE_API_KEY=your_key_here                 │
│                                                             │
│  Or add to ~/.config/browser-use/config.json:               │
│    {{"api_key": "your_key_here"}}                           │
╰─────────────────────────────────────────────────────────────╯
""")
```

---

## Complete Command List

### Browser Control

| Command | Description | API Key? |
|---------|-------------|----------|
| `open <url>` | Navigate to URL | No |
| `click <index>` | Click element | No |
| `type <text>` | Type text | No |
| `input <index> <text>` | Type into element | No |
| `scroll [up\|down]` | Scroll page | No |
| `back` | Go back | No |
| `screenshot` | Take screenshot | No |
| `state` | Get browser state | No |
| `switch <tab>` | Switch tab | No |
| `close-tab <tab>` | Close tab | No |
| `keys <keys>` | Send keys | No |
| `select <index> <text>` | Select dropdown | No |
| `eval <js>` | Execute JS | No |
| `extract <query>` | Extract data (LLM) | No |

### Python Execution

| Command | Description | API Key? |
|---------|-------------|----------|
| `python "<code>"` | Execute Python | No |
| `python --file script.py` | Run Python file | No |
| `python --reset` | Clear namespace | No |
| `python --vars` | Show variables | No |

### Agent Tasks

| Command | Description | API Key? |
|---------|-------------|----------|
| `run "<task>"` | Run agent task | **Yes** |
| `run --max-steps N` | Limit steps | **Yes** |

### Session Management

| Command | Description | API Key? |
|---------|-------------|----------|
| `sessions` | List sessions | No |
| `close [--session X]` | Close session | No |
| `close --all` | Close all | No |

### Server Control

| Command | Description |
|---------|-------------|
| `server status` | Check server |
| `server stop` | Stop server |
| `server logs` | View logs |

### Global Flags

| Flag | Description | Default |
|------|-------------|---------|
| `--session NAME` | Session name | `"default"` |
| `--browser MODE` | chromium/real/remote | `"chromium"` |
| `--headed` | Show browser | `false` |
| `--profile NAME` | Chrome profile (real mode) | Default profile |
| `--proxy TYPE` | Proxy type (remote mode) | `none` |
| `--api-key KEY` | API key override | From env/config |
| `--json` | JSON output | `false` |

---

## Code Structure

```
browser_use/cli/
├── __init__.py
├── __main__.py              # Entry: python -m browser_use.cli
│
├── main.py                  # Fast CLI (STDLIB ONLY!)
│   ├── Argument parsing
│   ├── ensure_server()
│   ├── send_command()
│   └── main()
│
├── server.py                # Session server
│   ├── SessionServer class
│   ├── Socket server (asyncio)
│   ├── Command dispatch
│   └── Graceful shutdown
│
├── sessions.py              # Session registry
│   ├── SessionInfo dataclass
│   ├── SessionRegistry class
│   ├── create_browser_session()
│   └── Health checks
│
├── commands/                # Command handlers
│   ├── __init__.py
│   ├── browser.py           # open, click, type, etc.
│   ├── python_exec.py       # python command
│   ├── agent.py             # run command (requires API key)
│   └── session.py           # sessions, close
│
├── python_session.py        # Jupyter-like REPL
│   ├── PythonSession class
│   ├── BrowserWrapper class
│   └── execute()
│
├── api_key.py               # API key management
│   ├── require_api_key()
│   ├── prompt_for_api_key()
│   └── save_api_key()
│
├── protocol.py              # Message types
│   ├── Request/Response
│   └── Serialization
│
└── utils.py                 # Helpers
    ├── Platform detection
    └── Path utilities
```

### pyproject.toml Entry Point

```toml
[project.scripts]
browser-use = "browser_use.cli.main:main"
```

---

## Implementation Heuristics

### 1. CLI Speed is Sacred

**Rule**: The CLI file (`main.py`) must ONLY use stdlib imports.

```python
# ✅ GOOD - stdlib only
import argparse, json, os, socket, subprocess, sys, time

# ❌ BAD - external imports
import click  # NO!
import rich   # NO!
from browser_use import ...  # NO!
```

All heavy imports go in the server, which loads once and stays running.

### 2. BrowserSession is the Source of Truth

**Rule**: Never bypass BrowserSession. Always use its methods.

```python
# ✅ GOOD - use BrowserSession
session.browser_session.navigate(url)
await session.browser_session.click(index)

# ❌ BAD - direct CDP calls
cdp_client.send("Page.navigate", {"url": url})  # NO!
```

This ensures all watchdogs, events, and state stay in sync.

### 3. Promote Cloud Browsers Naturally

**Rule**: When remote features are needed, guide users to Browser-Use Cloud.

```python
# When user needs proxy
if needs_proxy and mode != "remote":
    print("💡 Tip: For proxy support, use --browser remote")
    print("   Get API key at: https://browser-use.com/dashboard")

# When agent task fails due to blocks
if blocked_by_captcha:
    print("💡 Tip: Browser-Use Cloud includes CAPTCHA solving")
    print("   Try: browser-use run '...' --browser remote")
```

### 4. Python Session Isolation

**Rule**: Each named session has its own Python namespace.

```bash
# Session "work" has its own variables
browser-use python "x = 1" --session work

# Session "personal" is isolated
browser-use python "print(x)" --session personal  # Error: x not defined
```

### 5. Graceful Degradation

**Rule**: Always clean up resources, even on crash.

```python
# Server shutdown
async def shutdown(self):
    for session in self.registry.all():
        try:
            await session.browser_session.stop()
        except:
            pass  # Browser might already be dead

    # Remove socket/PID files
    cleanup_files()
```

### 6. Error Messages Should Help

**Rule**: Every error should tell the user what to do next.

```python
# ❌ BAD
raise Exception("API key missing")

# ✅ GOOD
raise APIKeyRequired("""
API key required for agent tasks.
Get yours at: https://browser-use.com/dashboard
Set via: export BROWSER_USE_API_KEY=your_key
""")
```

### 7. Session Auto-Creation

**Rule**: Sessions are created on-demand, not explicitly.

```bash
# This creates session "work" if it doesn't exist
browser-use open https://example.com --session work

# No need for explicit "create session" command
```

### 8. Default to Headless

**Rule**: Chromium mode defaults to headless for speed/resources.

```bash
browser-use open https://example.com           # Headless
browser-use open https://example.com --headed  # Visible
browser-use open https://example.com --browser real  # Always visible
```

---

## Future Work

### From CodeAgent/BU Agent
- **Background shell processes** - `bash` with persistent shells
- **File operations** - `read`, `write`, `edit`, `glob`, `grep`
- **Todo tracking** - Task progress management
- **Context compaction** - Auto-compress long conversations

### From agent-browser
- **Live streaming** - Stream browser view to terminal
- **Screencast recording** - Record sessions as video
- **Multiple browser types** - Firefox, WebKit support

### From browser-use
- **Skills integration** - Load automation skills from API
- **Storage persistence** - Save/restore cookies across server restarts
- **Download management** - Track and organize downloads

### New Features
- **Session snapshots** - Save/restore full browser state
- **Event replay** - Record and replay automation sequences
- **HTTP API** - REST endpoint for programmatic access
- **Watch mode** - Re-run Python on file changes
- **Notebook export** - Export Python session as .ipynb

---

## Success Criteria

- [ ] `browser-use open <url>` works with <50ms CLI startup
- [ ] All three browser modes work (chromium, real, remote)
- [ ] Python execution persists variables across commands
- [ ] Agent tasks require and validate API key
- [ ] API key prompt is user-friendly
- [ ] Sessions are isolated and named
- [ ] Server integrates cleanly with BrowserSession (no conflicts)
- [ ] Graceful shutdown cleans all resources
- [ ] Works on macOS, Linux, Windows
