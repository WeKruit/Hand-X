# Browser-Use CLI Implementation Plan

## Overview

This document outlines the implementation plan for the browser-use CLI as specified in CLI_SPEC.md.

## Architecture Summary

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              FAST CLI (main.py)                             │
│                         (stdlib only: <50ms startup)                        │
│                                                                             │
│   $ browser-use open https://example.com --session work --browser real      │
│   $ browser-use python "data = browser.scrape('table')"                     │
│   $ browser-use run "fill the form" --session work  # Requires API key      │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                          Unix Socket │ /tmp/browser-use-{session}.sock
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           SESSION SERVER (server.py)                        │
│                   (keeps BrowserSession instances alive)                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

## File Structure

```
browser_use/cli/
├── __init__.py              # Package init, expose main()
├── __main__.py              # Entry: python -m browser_use.cli
│
├── main.py                  # Fast CLI (STDLIB ONLY!)
│   ├── Argument parsing (argparse)
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

## Implementation Order

### Phase 1: Core Infrastructure
1. `protocol.py` - Message types for CLI↔Server communication
2. `utils.py` - Platform detection, socket paths, PID files
3. `main.py` - Fast CLI layer (stdlib only)
4. `server.py` - Basic socket server

### Phase 2: Session Management
5. `sessions.py` - Session registry
6. `api_key.py` - API key handling

### Phase 3: Commands
7. `commands/browser.py` - Basic browser control (open, click, type, etc.)
8. `commands/session.py` - Session management
9. `commands/python_exec.py` - Python execution
10. `commands/agent.py` - Agent tasks

### Phase 4: Python Execution
11. `python_session.py` - Jupyter-like REPL

### Phase 5: Integration
12. Update pyproject.toml entry point
13. Test all features
14. Create Claude skill description

## Implementation Details

### 1. protocol.py

```python
"""Wire protocol for CLI↔Server communication."""
from dataclasses import dataclass
from typing import Any
import json

@dataclass
class Request:
    id: str
    action: str
    session: str
    params: dict[str, Any]

    def to_json(self) -> str: ...
    @classmethod
    def from_json(cls, data: str) -> 'Request': ...

@dataclass
class Response:
    id: str
    success: bool
    data: Any = None
    error: str | None = None

    def to_json(self) -> str: ...
    @classmethod
    def from_json(cls, data: str) -> 'Response': ...
```

### 2. utils.py

```python
"""Platform utilities."""
import os
import sys
import tempfile
from pathlib import Path

def get_socket_path(session: str) -> str:
    """Get Unix socket path or TCP port for session."""
    if sys.platform == 'win32':
        # Windows: use TCP on deterministic port
        import hashlib
        port = 49152 + (int(hashlib.md5(session.encode()).hexdigest()[:4], 16) % 16383)
        return f"tcp://localhost:{port}"
    return f"/tmp/browser-use-{session}.sock"

def get_pid_path(session: str) -> Path:
    return Path(tempfile.gettempdir()) / f"browser-use-{session}.pid"

def is_server_running(session: str) -> bool: ...
def find_chrome_executable() -> str | None: ...
def get_chrome_profile_path(profile: str | None) -> str | None: ...
```

### 3. main.py (Critical: STDLIB ONLY)

```python
#!/usr/bin/env python3
"""Fast CLI for browser-use. STDLIB ONLY - must start in <50ms."""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

def get_socket_path(session: str) -> str: ...
def get_pid_path(session: str) -> str: ...

def is_server_running(session: str) -> bool:
    pid_path = get_pid_path(session)
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)  # Check if process exists
        return True
    except (OSError, ValueError):
        return False

def ensure_server(session: str, browser: str, headed: bool, profile: str | None) -> bool:
    """Start server if not running. Returns True if started."""
    if is_server_running(session):
        # Try to connect to verify it's responsive
        try:
            conn = connect_to_server(session, timeout=0.05)
            conn.close()
            return False  # Already running
        except:
            pass  # Server dead, restart

    # Start server as background process
    cmd = [
        sys.executable, '-m', 'browser_use.cli.server',
        '--session', session,
        '--browser', browser,
    ]
    if headed:
        cmd.append('--headed')
    if profile:
        cmd.extend(['--profile', profile])

    subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for server to be ready
    for _ in range(50):  # 2.5 seconds max
        if is_server_running(session):
            try:
                conn = connect_to_server(session, timeout=0.05)
                conn.close()
                return True
            except:
                pass
        time.sleep(0.05)

    print("Error: Failed to start server", file=sys.stderr)
    sys.exit(1)

def connect_to_server(session: str, timeout: float = 5.0) -> socket.socket:
    sock_path = get_socket_path(session)
    if sock_path.startswith('tcp://'):
        # Windows TCP
        host, port = sock_path[6:].split(':')
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, int(port)))
    else:
        # Unix socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(sock_path)
    return sock

def send_command(session: str, action: str, params: dict) -> dict:
    import time
    request = {
        'id': f'r{int(time.time() * 1000000) % 1000000}',
        'action': action,
        'session': session,
        'params': params,
    }

    sock = connect_to_server(session)
    try:
        sock.sendall((json.dumps(request) + '\n').encode())

        # Read response
        data = b''
        while not data.endswith(b'\n'):
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk

        return json.loads(data.decode())
    finally:
        sock.close()

def main():
    parser = argparse.ArgumentParser(prog='browser-use', description='Browser automation CLI')
    parser.add_argument('--session', '-s', default='default', help='Session name')
    parser.add_argument('--browser', '-b', choices=['chromium', 'real', 'remote'], default='chromium')
    parser.add_argument('--headed', action='store_true', help='Show browser window')
    parser.add_argument('--profile', help='Chrome profile (real browser mode)')
    parser.add_argument('--json', action='store_true', help='JSON output')
    parser.add_argument('--api-key', help='API key override')

    subparsers = parser.add_subparsers(dest='command', required=True)

    # open <url>
    open_p = subparsers.add_parser('open', help='Navigate to URL')
    open_p.add_argument('url', help='URL to navigate to')

    # click <index>
    click_p = subparsers.add_parser('click', help='Click element')
    click_p.add_argument('index', type=int, help='Element index')

    # type <text>
    type_p = subparsers.add_parser('type', help='Type text')
    type_p.add_argument('text', help='Text to type')

    # ... more subparsers for all commands

    args = parser.parse_args()

    # Handle API key
    if args.api_key:
        os.environ['BROWSER_USE_API_KEY'] = args.api_key

    # Ensure server is running
    ensure_server(args.session, args.browser, args.headed, args.profile)

    # Build params from args
    params = vars(args).copy()
    del params['command']
    del params['session']
    del params['browser']
    del params['headed']
    del params['profile']
    del params['json']
    del params['api_key']

    # Send command
    response = send_command(args.session, args.command, params)

    # Output
    if args.json:
        print(json.dumps(response))
    else:
        if response.get('success'):
            if response.get('data'):
                print(response['data'])
        else:
            print(f"Error: {response.get('error')}", file=sys.stderr)
            sys.exit(1)

if __name__ == '__main__':
    main()
```

### 4. server.py

```python
"""Session server - keeps BrowserSession instances alive."""
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

# Heavy imports OK here - server loads once and stays running
from browser_use.browser.session import BrowserSession
from browser_use.browser.profile import BrowserProfile
from browser_use.cli.sessions import SessionRegistry
from browser_use.cli.commands import browser, python_exec, agent, session

class SessionServer:
    def __init__(self, session_name: str, browser_mode: str, headed: bool, profile: str | None):
        self.session_name = session_name
        self.browser_mode = browser_mode
        self.headed = headed
        self.profile = profile
        self.registry = SessionRegistry()
        self.running = True

    async def handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        while True:
            try:
                line = await reader.readline()
                if not line:
                    break

                request = json.loads(line.decode())
                response = await self.dispatch(request)
                writer.write((json.dumps(response) + '\n').encode())
                await writer.drain()

            except Exception as e:
                response = {'id': '', 'success': False, 'error': str(e)}
                writer.write((json.dumps(response) + '\n').encode())
                await writer.drain()

        writer.close()
        await writer.wait_closed()

    async def dispatch(self, request: dict) -> dict:
        action = request.get('action')
        params = request.get('params', {})
        req_id = request.get('id', '')

        try:
            # Get or create session
            session_info = await self.registry.get_or_create(
                self.session_name,
                self.browser_mode,
                self.headed,
                self.profile,
            )

            # Dispatch to handler
            if action in browser.COMMANDS:
                result = await browser.handle(action, session_info, params)
            elif action == 'python':
                result = await python_exec.handle(session_info, params)
            elif action == 'run':
                result = await agent.handle(session_info, params)
            elif action in session.COMMANDS:
                result = await session.handle(action, self.registry, params)
            elif action == 'close':
                await self.shutdown()
                result = {'closed': True}
            else:
                return {'id': req_id, 'success': False, 'error': f'Unknown action: {action}'}

            return {'id': req_id, 'success': True, 'data': result}

        except Exception as e:
            return {'id': req_id, 'success': False, 'error': str(e)}

    async def shutdown(self):
        self.running = False
        await self.registry.close_all()
        # Remove socket/PID files
        cleanup_files(self.session_name)

    async def run(self):
        # Write PID file
        pid_path = get_pid_path(self.session_name)
        pid_path.write_text(str(os.getpid()))

        # Setup signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))

        # Start server
        sock_path = get_socket_path(self.session_name)
        if sock_path.startswith('tcp://'):
            host, port = sock_path[6:].split(':')
            server = await asyncio.start_server(
                self.handle_connection, host, int(port)
            )
        else:
            # Remove stale socket
            if os.path.exists(sock_path):
                os.unlink(sock_path)
            server = await asyncio.start_unix_server(
                self.handle_connection, sock_path
            )

        async with server:
            while self.running:
                await asyncio.sleep(0.1)

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', required=True)
    parser.add_argument('--browser', default='chromium')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--profile')
    args = parser.parse_args()

    server = SessionServer(args.session, args.browser, args.headed, args.profile)
    asyncio.run(server.run())

if __name__ == '__main__':
    main()
```

### 5. sessions.py

```python
"""Session registry - manages BrowserSession instances."""
from dataclasses import dataclass, field
from typing import Any

from browser_use.browser.session import BrowserSession
from browser_use.browser.profile import BrowserProfile
from browser_use.cli.python_session import PythonSession

@dataclass
class SessionInfo:
    name: str
    browser_mode: str
    headed: bool
    profile: str | None
    browser_session: BrowserSession
    python_session: PythonSession = field(default_factory=PythonSession)

class SessionRegistry:
    def __init__(self):
        self._sessions: dict[str, SessionInfo] = {}

    async def get_or_create(
        self,
        name: str,
        browser_mode: str,
        headed: bool,
        profile: str | None,
    ) -> SessionInfo:
        if name in self._sessions:
            return self._sessions[name]

        browser_session = await create_browser_session(
            browser_mode, headed, profile
        )
        await browser_session.start()

        session_info = SessionInfo(
            name=name,
            browser_mode=browser_mode,
            headed=headed,
            profile=profile,
            browser_session=browser_session,
        )
        self._sessions[name] = session_info
        return session_info

    def list_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                'name': s.name,
                'browser_mode': s.browser_mode,
                'headed': s.headed,
            }
            for s in self._sessions.values()
        ]

    async def close_session(self, name: str) -> bool:
        if name not in self._sessions:
            return False
        session = self._sessions.pop(name)
        await session.browser_session.kill()
        return True

    async def close_all(self):
        for session in self._sessions.values():
            try:
                await session.browser_session.kill()
            except:
                pass
        self._sessions.clear()

async def create_browser_session(
    mode: str,
    headed: bool,
    profile: str | None,
) -> BrowserSession:
    """Create BrowserSession based on mode."""
    if mode == 'chromium':
        return BrowserSession(
            headless=not headed,
        )

    elif mode == 'real':
        from browser_use.cli.utils import find_chrome_executable, get_chrome_profile_path
        return BrowserSession(
            executable_path=find_chrome_executable(),
            user_data_dir=get_chrome_profile_path(profile),
            headless=False,  # Real browser always visible
        )

    elif mode == 'remote':
        from browser_use.cli.api_key import require_api_key
        require_api_key('Remote browser')
        return BrowserSession(
            use_cloud=True,
        )

    else:
        raise ValueError(f'Unknown browser mode: {mode}')
```

### 6. api_key.py

```python
"""API key management."""
import json
import os
import sys
from pathlib import Path

class APIKeyRequired(Exception):
    pass

def require_api_key(feature: str = 'this feature') -> str:
    """Get API key or raise helpful error."""

    # 1. Check environment
    key = os.environ.get('BROWSER_USE_API_KEY')
    if key:
        return key

    # 2. Check config file
    config_path = Path.home() / '.config' / 'browser-use' / 'config.json'
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            if key := config.get('api_key'):
                return key
        except:
            pass

    # 3. Interactive prompt (if TTY)
    if sys.stdin.isatty():
        return prompt_for_api_key(feature)

    # 4. Error with helpful message
    raise APIKeyRequired(f'''
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
''')

def prompt_for_api_key(feature: str) -> str:
    """Interactive prompt for API key."""
    print(f'''
╭─────────────────────────────────────────────────────────────╮
│  🔑 Browser-Use API Key Required                            │
│                                                             │
│  {feature} requires an API key.                             │
│  Get yours at: https://browser-use.com/dashboard            │
╰─────────────────────────────────────────────────────────────╯
''')
    key = input('Enter API key: ').strip()
    if not key:
        raise APIKeyRequired('No API key provided')

    save = input('Save to config? [y/N]: ').strip().lower()
    if save == 'y':
        save_api_key(key)

    return key

def save_api_key(key: str):
    """Save API key to config file."""
    config_path = Path.home() / '.config' / 'browser-use' / 'config.json'
    config_path.parent.mkdir(parents=True, exist_ok=True)

    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except:
            pass

    config['api_key'] = key
    config_path.write_text(json.dumps(config, indent=2))
    print(f'Saved to {config_path}')
```

### 7. python_session.py

```python
"""Jupyter-like persistent Python execution."""
import asyncio
import io
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from browser_use.browser.session import BrowserSession

@dataclass
class ExecutionResult:
    success: bool
    output: str = ''
    error: str | None = None

@dataclass
class PythonSession:
    """Jupyter-like persistent Python execution."""
    namespace: dict[str, Any] = field(default_factory=dict)
    execution_count: int = 0

    def __post_init__(self):
        # Pre-populate namespace with useful imports
        self.namespace.update({
            'json': __import__('json'),
            're': __import__('re'),
            'Path': Path,
        })

    def execute(self, code: str, browser: BrowserSession) -> ExecutionResult:
        """Execute code in persistent namespace."""
        self.namespace['browser'] = BrowserWrapper(browser)
        self.execution_count += 1

        stdout = io.StringIO()
        try:
            with redirect_stdout(stdout):
                try:
                    # Try as expression first (for REPL-like behavior)
                    result = eval(code, self.namespace)
                    if result is not None:
                        print(repr(result))
                except SyntaxError:
                    # Execute as statements
                    exec(code, self.namespace)

            return ExecutionResult(success=True, output=stdout.getvalue())

        except Exception as e:
            return ExecutionResult(success=False, error=str(e), output=stdout.getvalue())

    def reset(self):
        """Clear namespace."""
        self.namespace.clear()
        self.__post_init__()

    def get_variables(self) -> dict[str, str]:
        """Get user-defined variables."""
        return {
            k: type(v).__name__
            for k, v in self.namespace.items()
            if not k.startswith('_') and k not in ('json', 're', 'Path', 'browser')
        }

class BrowserWrapper:
    """Convenient browser access for Python code."""

    def __init__(self, session: BrowserSession):
        self._session = session
        self._loop = asyncio.new_event_loop()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    @property
    def url(self) -> str:
        # Get current URL from session
        return self._run(self._get_url())

    async def _get_url(self) -> str:
        state = await self._session.get_state()
        return state.url if state else ''

    def goto(self, url: str):
        """Navigate to URL."""
        self._run(self._session.navigate(url))

    def click(self, index: int):
        """Click element by index."""
        from browser_use.browser.events import ClickElementEvent
        self._run(self._session.event_bus.adispatch(
            ClickElementEvent(index=index)
        ))

    def type(self, text: str):
        """Type text."""
        from browser_use.browser.events import TypeTextEvent
        self._run(self._session.event_bus.adispatch(
            TypeTextEvent(text=text)
        ))

    def screenshot(self) -> bytes:
        """Take screenshot, return bytes."""
        return self._run(self._session.take_screenshot())

    @property
    def html(self) -> str:
        """Get page HTML."""
        return self._run(self._session.get_html())

    def scroll(self, direction: str = 'down', amount: int = 500):
        """Scroll page."""
        from browser_use.browser.events import ScrollEvent
        self._run(self._session.event_bus.adispatch(
            ScrollEvent(direction=direction, amount=amount)
        ))
```

### 8. commands/browser.py

```python
"""Browser control commands."""
from typing import Any
from browser_use.cli.sessions import SessionInfo
from browser_use.browser.events import (
    NavigateToUrlEvent,
    ClickElementEvent,
    TypeTextEvent,
    ScrollEvent,
    SendKeysEvent,
    SwitchTabEvent,
    CloseTabEvent,
)

COMMANDS = {
    'open', 'click', 'type', 'input', 'scroll', 'back',
    'screenshot', 'state', 'switch', 'close-tab', 'keys',
    'select', 'eval', 'extract',
}

async def handle(action: str, session: SessionInfo, params: dict) -> Any:
    bs = session.browser_session

    if action == 'open':
        await bs.event_bus.adispatch(NavigateToUrlEvent(url=params['url']))
        return {'url': params['url']}

    elif action == 'click':
        await bs.event_bus.adispatch(ClickElementEvent(index=params['index']))
        return {'clicked': params['index']}

    elif action == 'type':
        await bs.event_bus.adispatch(TypeTextEvent(text=params['text']))
        return {'typed': params['text']}

    elif action == 'input':
        await bs.event_bus.adispatch(ClickElementEvent(index=params['index']))
        await bs.event_bus.adispatch(TypeTextEvent(text=params['text']))
        return {'input': params['text'], 'to': params['index']}

    elif action == 'scroll':
        direction = params.get('direction', 'down')
        await bs.event_bus.adispatch(ScrollEvent(direction=direction))
        return {'scrolled': direction}

    elif action == 'back':
        # TODO: Implement back navigation
        return {'back': True}

    elif action == 'screenshot':
        data = await bs.take_screenshot()
        if params.get('path'):
            from pathlib import Path
            Path(params['path']).write_bytes(data)
            return {'saved': params['path']}
        import base64
        return {'screenshot': base64.b64encode(data).decode()}

    elif action == 'state':
        state = await bs.get_state()
        return {
            'url': state.url if state else '',
            'title': state.title if state else '',
        }

    elif action == 'switch':
        await bs.event_bus.adispatch(SwitchTabEvent(tab_index=params['tab']))
        return {'switched': params['tab']}

    elif action == 'close-tab':
        await bs.event_bus.adispatch(CloseTabEvent(tab_index=params.get('tab')))
        return {'closed': params.get('tab')}

    elif action == 'keys':
        await bs.event_bus.adispatch(SendKeysEvent(keys=params['keys']))
        return {'sent': params['keys']}

    elif action == 'eval':
        # TODO: Implement JS eval
        return {'eval': params['js']}

    elif action == 'extract':
        # TODO: Implement LLM extraction
        return {'query': params['query']}

    raise ValueError(f'Unknown browser action: {action}')
```

### 9. Claude Skill Description

Located at: `skills/browser-use/SKILL.md`

## Testing Plan

1. **Unit Tests**
   - Protocol serialization
   - Session registry operations
   - API key handling

2. **Integration Tests**
   - Server startup/shutdown
   - CLI↔Server communication
   - Browser session creation

3. **E2E Tests**
   - Full workflow: open → click → type → screenshot
   - Python execution with variable persistence
   - Multi-session isolation

## Open Questions

1. **Real Browser Detection**: How to reliably find Chrome on different platforms?
   - macOS: `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
   - Linux: `which google-chrome` or `which chromium`
   - Windows: Check registry or common paths

2. **Cloud Proxy Configuration**: The spec mentions `--proxy residential` for remote mode. Need to verify CloudBrowserParams supports this.

3. **Agent Task Implementation**: The `run` command requires the full Agent class. Need to determine:
   - Which LLM to use (from config?)
   - How to stream output back to CLI

4. **Event Loop Management**: The Python session needs to run async browser methods from sync code. Current approach uses a separate event loop but this may conflict with the server's loop.

5. **Backward Compatibility**: The existing `browser_use/cli.py` is a TUI app. We need to:
   - Keep the TUI as a separate command (`browser-use tui`)?
   - Or replace it entirely?

## Notes

- The CLI layer (main.py) MUST only use stdlib imports for <50ms startup
- All heavy imports go in the server layer
- Use Unix sockets on macOS/Linux, TCP on Windows
- Session names allow parallel browser instances
- API key is only required for remote browsers and agent tasks
