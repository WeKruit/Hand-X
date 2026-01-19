# Browser-Use CLI: Open Questions & Implementation Notes

## Implementation Status

### Completed Features
- [x] Fast CLI layer (stdlib only, <50ms startup)
- [x] Session server with Unix socket IPC (TCP on Windows)
- [x] Session registry with named sessions
- [x] Browser modes: chromium, real, remote
- [x] Browser commands: open, click, type, input, scroll, back, screenshot, state, switch, close-tab, keys, select, eval
- [x] Python execution with persistent namespace (Jupyter-like REPL)
- [x] API key management with environment, config file, and interactive prompt support
- [x] Agent task execution (basic implementation)
- [x] JSON output mode
- [x] Claude skill description (SKILL.md)

### Features Not Fully Implemented
- [ ] `extract` command - Returns placeholder, needs LLM integration
- [ ] Agent task `run` command - Basic implementation, may need tuning
- [ ] Cloud browser proxy configuration (`--proxy residential`)

## Open Questions

### 1. TUI vs CLI Coexistence

**Current State**:
- The existing `browser_use/cli.py` is a Textual TUI application
- The new CLI is in `browser_use/skill_cli/` with entry point `bu`
- Both coexist: `browser-use` launches TUI, `bu` launches fast CLI

**Question**: Should we:
1. Keep both (`browser-use` = TUI, `bu` = fast CLI)
2. Replace TUI with fast CLI (`browser-use` = fast CLI, deprecate TUI)
3. Make TUI a subcommand (`browser-use tui`, `browser-use` = fast CLI)

**Recommendation**: Option 3 - Make TUI accessible via `browser-use tui` and have `browser-use` launch the fast CLI. This provides a migration path.

### 2. Agent Task LLM Configuration

**Current State**:
The `run` command tries to auto-detect LLM from environment variables (OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY).

**Question**: Should we:
1. Require explicit LLM configuration in `~/.config/browser-use/config.json`
2. Keep auto-detection from environment
3. Add `--llm` flag to specify model

**Recommendation**: Keep auto-detection, add optional `--llm` flag for overrides.

### 3. Remote Browser Proxy Configuration

**Spec mentions**: `--proxy residential` for remote browser mode

**Current State**: Not implemented. The `CloudBrowserParams` supports `proxy_country_code` but not proxy type.

**Question**: What proxy types does Browser-Use Cloud support?
- residential
- datacenter
- none

**Action Needed**: Verify with Browser-Use Cloud API documentation.

### 4. Real Browser Mode Profile Handling

**Current State**:
- `--profile NAME` passes the profile name to `user_data_dir`
- This may not work correctly as Chrome expects specific paths

**Question**: How should profile selection work?
1. Pass profile name as subdirectory
2. Use Chrome's `--profile-directory` flag
3. Let user specify full path

**Recommendation**: Support both `--profile "Profile 1"` (Chrome's named profile) and `--user-data-dir /path` for full control.

### 5. Session Cleanup on Server Restart

**Current State**:
- Server writes PID and socket files to temp directory
- On shutdown, files are cleaned up
- If server crashes, files may remain

**Question**: Should we:
1. Clean up stale files on CLI startup
2. Add a `bu server clean` command
3. Both

**Recommendation**: Clean up stale files on startup (check if PID exists, if not, remove socket).

### 6. Event Loop Handling in Python Session

**Current State**:
The `BrowserWrapper` in `python_session.py` uses `concurrent.futures.ThreadPoolExecutor` to run async operations from sync context.

**Potential Issues**:
- May conflict with the server's event loop
- Thread pool overhead

**Question**: Is there a better approach? Consider:
1. Using `asyncio.run_coroutine_threadsafe()`
2. Making Python execution async and handling differently

### 7. Browser State Element Indices

**Current State**:
The `state` command returns elements from `dom_state.selector_map` with their original indices.

**Observation**: Indices are not sequential (e.g., 18, 19, 20...). This is because they're mapped to actual DOM elements.

**Question**: Should we:
1. Keep original indices (consistent with how the agent works)
2. Re-number 0, 1, 2... (easier for humans but may cause confusion)

**Recommendation**: Keep original indices for consistency with agent.

## Architecture Notes

### Socket Communication
- Unix sockets on macOS/Linux: `/var/.../browser-use-{session}.sock`
- TCP on Windows: `tcp://localhost:{port}` where port is hash-derived
- Line-delimited JSON protocol

### Session Lifecycle
```
CLI Command → ensure_server() → connect_to_server() → send_command()
                    ↓
           (if not running)
                    ↓
         spawn server process
                    ↓
         wait for socket ready
```

### Server Architecture
```
SessionServer
    ├── SessionRegistry (manages BrowserSession instances)
    ├── Socket Server (asyncio.start_unix_server)
    └── Command Dispatch
            ├── browser.py (open, click, type, etc.)
            ├── python_exec.py (Python REPL)
            ├── agent.py (run command)
            └── session.py (sessions, close)
```

## Testing Notes

All basic features have been tested:
- `bu open <url>` - Works (headed and headless)
- `bu state` - Works (returns elements with indices)
- `bu click <index>` - Works
- `bu type "text"` - Works
- `bu screenshot` - Works (base64 and file output)
- `bu eval "js"` - Works (returns JS result)
- `bu python "code"` - Works (persistent namespace)
- `bu python --vars` - Works
- `bu close` - Works
- `bu server status/stop` - Works
- `--json` mode - Works
- `--headed` mode - Works

## Future Improvements

1. **Streaming Output**: For long-running agent tasks, stream progress updates
2. **Tab Completion**: Add shell completion for commands
3. **History**: Remember command history across sessions
4. **Config File**: Support more configuration options
5. **Plugins**: Allow extending with custom commands
6. **HTTP API**: Add REST endpoint for programmatic access
7. **Watch Mode**: Re-run Python on file changes
8. **Notebook Export**: Export Python session as .ipynb
