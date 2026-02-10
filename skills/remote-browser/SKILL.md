---
name: remote-browser
description: Controls a cloud browser from a sandboxed remote machine. Use when the agent is running in a sandbox (no GUI) and needs to navigate websites, interact with web pages, fill forms, take screenshots, or expose local dev servers via tunnels.
allowed-tools: Bash(browser-use:*)
---

# Remote Browser Automation for Sandboxed Agents

This skill is for agents running on **sandboxed remote machines** (cloud VMs, CI, coding agents) that need to control a browser. Install `browser-use` and drive a cloud browser — no local Chrome needed.

## Setup

**One-command install:**
```bash
curl -fsSL https://raw.githubusercontent.com/ShawnPana/browser-use/frictionless-install/install.sh | BROWSER_USE_BRANCH=frictionless-install bash
```

This installs Python dependencies, browser-use CLI, and validates the setup.

**Then configure your API key:**
```bash
export BROWSER_USE_API_KEY=bu_xxx   # Required for cloud browser
```

**Verify installation:**
```bash
browser-use doctor
```

## Core Workflow

```bash
browser-use --browser remote open https://example.com  # Open URL in cloud browser
browser-use state                                       # Get page elements with indices
browser-use click 5                                     # Click element by index
browser-use type "Hello World"                          # Type into focused element
browser-use input 3 "text"                              # Click element, then type
browser-use screenshot                                  # Take screenshot (base64)
browser-use screenshot page.png                         # Save screenshot to file
browser-use close                                       # Close browser
```

The `open` command returns a `live_url` for cloud browsers — a real-time browser viewport you can view in any browser.

## Exposing Local Dev Servers

If you're running a dev server on the remote machine and need the cloud browser to reach it:

```bash
# Start your dev server
python -m http.server 3000 &

# Expose it via Cloudflare tunnel
browser-use tunnel 3000
# → url: https://abc.trycloudflare.com

# Now the cloud browser can reach your local server
browser-use --browser remote open https://abc.trycloudflare.com
```

Tunnel commands:
```bash
browser-use tunnel <port>           # Start tunnel (returns URL)
browser-use tunnel <port>           # Idempotent - returns existing URL
browser-use tunnel list             # Show active tunnels
browser-use tunnel stop <port>      # Stop tunnel
browser-use close                   # Closing session stops all tunnels
```

Cloudflared auto-installs on first tunnel use (~20MB one-time download).

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
| `--browser remote` | Cloud browser (always use this on sandboxed machines) |
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
browser-use --browser remote open https://abc.trycloudflare.com
browser-use state
browser-use screenshot
```

### Form Submission

```bash
browser-use --browser remote open https://example.com/contact
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
browser-use --browser remote open https://example.com
for i in 1 2 3 4 5; do
  browser-use scroll down
  browser-use screenshot "page_$i.png"
done
```

## Tips

1. **Always use `--browser remote`** on sandboxed machines (no local Chrome available)
2. **Always run `state` first** to see available elements and their indices
3. **Sessions persist** across commands — the browser stays open
4. **Use `--json`** for programmatic parsing
5. **`tunnel` is idempotent** — calling it again for the same port returns the existing URL
6. **Close when done** — `browser-use close` cleans up browser and tunnels

## Troubleshooting

**Cloud browser won't start?**
- Verify `BROWSER_USE_API_KEY` is set
- Check your API key at https://browser-use.com

**Tunnel not working?**
- Cloudflared auto-installs on first use; check network connectivity
- `browser-use tunnel list` to check active tunnels
- `browser-use tunnel stop <port>` and retry

**Element not found?**
- Run `browser-use state` to see current elements
- `browser-use scroll down` then `browser-use state` — element might be below fold
- Page may have changed — re-run `state` to get fresh indices

## Cleanup

**Always close the browser when done:**

```bash
browser-use close
```

This stops all tunnels and releases the cloud browser session.
