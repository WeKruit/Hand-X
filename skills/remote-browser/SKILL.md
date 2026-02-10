---
name: remote-browser
description: Controls a cloud browser from a sandboxed remote machine. Use when the agent is running in a sandbox (no GUI) and needs to navigate websites, interact with web pages, fill forms, take screenshots, or expose local dev servers via tunnels.
allowed-tools: Bash(browser-use:*)
---

# Remote Browser Automation for Sandboxed Agents

This skill is for agents running on **sandboxed remote machines** (cloud VMs, CI, coding agents) that need to control a browser. Install `browser-use` and drive a cloud browser — no local Chrome needed.

## Setup

**Remote-only install (recommended for sandboxed agents)**
```bash
curl -fsSL https://browser-use.com/install.sh | bash -s -- --remote-only
```

This configures browser-use to only use cloud browsers:
- No Chromium download (~300MB saved)
- `browser-use open <url>` automatically uses remote mode (no `--browser` flag needed)
- If API key is available, you can also pass it during install:
  ```bash
  curl -fsSL https://browser-use.com/install.sh | bash -s -- --remote-only --api-key bu_xxx
  ```

**For development testing (from GitHub branch):**
```bash
curl -fsSL https://raw.githubusercontent.com/ShawnPana/browser-use/frictionless-install/install.sh | BROWSER_USE_BRANCH=frictionless-install bash -s -- --remote-only
```

**Manual install (alternative)**
```bash
# Install from PyPI (once released):
pip install "browser-use[cli]"

# Or from this branch for dev testing:
pip install "git+https://github.com/ShawnPana/browser-use@frictionless-install"

# Install cloudflared for tunneling:
# macOS:
brew install cloudflared

# Linux:
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o ~/.local/bin/cloudflared && chmod +x ~/.local/bin/cloudflared

# Windows:
winget install Cloudflare.cloudflared
```

**Then configure your API key:**
```bash
export BROWSER_USE_API_KEY=bu_xxx   # Required for cloud browser
```

**Verify installation:**
```bash
browser-use doctor
```

## Core Workflow

When installed with `--remote-only`, commands automatically use the cloud browser — no `--browser` flag needed:

```bash
# Step 1: Start session (automatically uses remote mode)
browser-use open https://example.com
# Returns: url, live_url (view the browser in real-time)

# Step 2+: All subsequent commands use the existing session
browser-use state                   # Get page elements with indices
browser-use click 5                 # Click element by index
browser-use type "Hello World"      # Type into focused element
browser-use input 3 "text"          # Click element, then type
browser-use screenshot              # Take screenshot (base64)
browser-use screenshot page.png     # Save screenshot to file

# Done: Close the session
browser-use close                   # Close browser and release resources
```

### Understanding Installation Modes

| Install Command | Available Modes | Default Mode | Use Case |
|-----------------|-----------------|--------------|----------|
| `--remote-only` | remote | remote | Sandboxed agents, no GUI |
| `--local-only` | chromium, real | chromium | Local development |
| `--full` | chromium, real, remote | chromium | Full flexibility |

When only one mode is installed, it becomes the default and no `--browser` flag is needed.

## Exposing Local Dev Servers

If you're running a dev server on the remote machine and need the cloud browser to reach it:

```bash
# Start your dev server
python -m http.server 3000 &

# Expose it via Cloudflare tunnel
browser-use tunnel 3000
# → url: https://abc.trycloudflare.com

# Now the cloud browser can reach your local server
browser-use open https://abc.trycloudflare.com
```

Tunnel commands:
```bash
browser-use tunnel <port>           # Start tunnel (returns URL)
browser-use tunnel <port>           # Idempotent - returns existing URL
browser-use tunnel list             # Show active tunnels
browser-use tunnel stop <port>      # Stop tunnel
browser-use tunnel stop --all       # Stop all tunnels
```

**Note:** Tunnels are independent of browser sessions. They persist across `browser-use close` and can be managed separately.

Cloudflared is installed by `install.sh --remote-only`. If missing, install manually (see Setup section).

## Commands

### Navigation
```bash
browser-use open <url>              # Navigate to URL
browser-use back                    # Go back in history
browser-use scroll down             # Scroll down
browser-use scroll up               # Scroll up
```

### Page State
```bash
browser-use state                   # Get URL, title, and clickable elements
browser-use screenshot              # Take screenshot (base64)
browser-use screenshot path.png     # Save screenshot to file
browser-use screenshot --full p.png # Full page screenshot
```

### Interactions (use indices from `state`)
```bash
browser-use click <index>           # Click element
browser-use type "text"             # Type into focused element
browser-use input <index> "text"    # Click element, then type
browser-use keys "Enter"            # Send keyboard keys
browser-use keys "Control+a"        # Key combination
browser-use select <index> "option" # Select dropdown option
browser-use hover <index>           # Hover over element
browser-use dblclick <index>        # Double-click
browser-use rightclick <index>      # Right-click
```

### JavaScript & Data
```bash
browser-use eval "document.title"   # Execute JavaScript
browser-use get title               # Get page title
browser-use get html                # Get page HTML
browser-use get html --selector "h1"  # Scoped HTML
browser-use get text <index>        # Get element text
browser-use get value <index>       # Get input value
browser-use get attributes <index>  # Get element attributes
```

### Wait Conditions
```bash
browser-use wait selector "h1"                         # Wait for element
browser-use wait selector ".loading" --state hidden    # Wait for element to disappear
browser-use wait text "Success"                        # Wait for text
browser-use wait selector "#btn" --timeout 5000        # Custom timeout (ms)
```

### Cookies
```bash
browser-use cookies get             # Get all cookies
browser-use cookies set <name> <val>  # Set a cookie
browser-use cookies clear           # Clear all cookies
browser-use cookies export <file>   # Export to JSON
browser-use cookies import <file>   # Import from JSON
```

### Tab Management
```bash
browser-use switch <tab>            # Switch tab by index
browser-use close-tab               # Close current tab
browser-use close-tab <tab>         # Close specific tab
```

### Agent Tasks
```bash
browser-use run "Fill the contact form with test data"   # AI agent
browser-use run "Extract all product prices" --max-steps 50
```

### Session Management
```bash
browser-use sessions                # List active sessions
browser-use close                   # Close current session
browser-use close --all             # Close all sessions
```

### Global Options

| Option | Description |
|--------|-------------|
| `--session NAME` | Named session (default: "default") |
| `--browser MODE` | Browser mode (only if multiple modes installed) |
| `--profile ID` | Cloud profile ID for persistent cookies |
| `--json` | Output as JSON |
| `--api-key KEY` | Override API key |

## Common Patterns

### Test a Local Dev Server with Cloud Browser

```bash
# Start dev server
npm run dev &  # localhost:3000

# Tunnel it
browser-use tunnel 3000
# → url: https://abc.trycloudflare.com

# Browse with cloud browser
browser-use open https://abc.trycloudflare.com
browser-use state
browser-use screenshot
```

### Form Submission

```bash
browser-use open https://example.com/contact
browser-use state
# Shows: [0] input "Name", [1] input "Email", [2] textarea "Message", [3] button "Submit"
browser-use input 0 "John Doe"
browser-use input 1 "john@example.com"
browser-use input 2 "Hello, this is a test message."
browser-use click 3
browser-use state   # Verify success
```

### Screenshot Loop for Visual Verification

```bash
browser-use open https://example.com
for i in 1 2 3 4 5; do
  browser-use scroll down
  browser-use screenshot "page_$i.png"
done
```

## Tips

1. **Install with `--remote-only`** for sandboxed environments — no `--browser` flag needed
2. **Always run `state` first** to see available elements and their indices
3. **Sessions persist** across commands — the browser stays open until you close it
4. **Tunnels are independent** — they don't require or create a browser session, and persist across `browser-use close`
5. **Use `--json`** for programmatic parsing
6. **`tunnel` is idempotent** — calling it again for the same port returns the existing URL
7. **Close when done** — `browser-use close` closes the browser; `browser-use tunnel stop --all` stops tunnels

## Troubleshooting

**"Browser mode 'chromium' not installed"?**
- You installed with `--remote-only` which doesn't include local modes
- This is expected behavior for sandboxed agents
- If you need local browser, reinstall with `--full`

**Cloud browser won't start?**
- Verify `BROWSER_USE_API_KEY` is set
- Check your API key at https://browser-use.com

**Tunnel not working?**
- Verify cloudflared is installed: `which cloudflared`
- If missing, install manually (see Setup section) or re-run `install.sh --remote-only`
- `browser-use tunnel list` to check active tunnels
- `browser-use tunnel stop <port>` and retry

**Element not found?**
- Run `browser-use state` to see current elements
- `browser-use scroll down` then `browser-use state` — element might be below fold
- Page may have changed — re-run `state` to get fresh indices

## Cleanup

**Close the browser when done:**

```bash
browser-use close              # Close browser session
browser-use tunnel stop --all  # Stop all tunnels (if any)
```

Browser sessions and tunnels are managed separately, so close each as needed.
