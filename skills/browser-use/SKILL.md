---
name: browser-use
description: Automates browser interactions for web testing, form filling, screenshots, and data extraction. Use when the user needs to navigate websites, interact with web pages, fill forms, take screenshots, or extract information from web pages.
allowed-tools: Bash(bu:*)
---

# Browser Automation with browser-use CLI

The `bu` command provides fast, persistent browser automation. It maintains browser sessions across commands, enabling complex multi-step workflows.

## Quick Start

```bash
bu open https://example.com           # Navigate to URL
bu state                              # Get page elements with indices
bu click 5                            # Click element by index
bu type "Hello World"                 # Type text
bu screenshot                         # Take screenshot
bu close                              # Close browser
```

## Core Workflow

1. **Navigate**: `bu open <url>` - Opens URL (starts browser if needed)
2. **Inspect**: `bu state` - Returns clickable elements with indices
3. **Interact**: Use indices from state to interact (`bu click 5`, `bu input 3 "text"`)
4. **Verify**: `bu state` or `bu screenshot` to confirm actions
5. **Repeat**: Browser stays open between commands

## Browser Modes

```bash
bu --browser chromium open <url>      # Default: headless Chromium
bu --browser chromium --headed open <url>  # Visible Chromium window
bu --browser real open <url>          # User's Chrome with login sessions
bu --browser remote open <url>        # Cloud browser (requires API key)
```

- **chromium**: Fast, isolated, headless by default
- **real**: Uses your Chrome with cookies, extensions, logged-in sessions
- **remote**: Cloud-hosted browser with proxy support (requires BROWSER_USE_API_KEY)

## Commands

### Navigation
```bash
bu open <url>                    # Navigate to URL
bu back                          # Go back in history
bu scroll down                   # Scroll down
bu scroll up                     # Scroll up
```

### Page State
```bash
bu state                         # Get URL, title, and clickable elements
bu screenshot                    # Take screenshot (outputs base64)
bu screenshot path.png           # Save screenshot to file
bu screenshot --full path.png    # Full page screenshot
```

### Interactions (use indices from `bu state`)
```bash
bu click <index>                 # Click element
bu type "text"                   # Type text into focused element
bu input <index> "text"          # Click element, then type text
bu keys "Enter"                  # Send keyboard keys
bu keys "Control+a"              # Send key combination
bu select <index> "option"       # Select dropdown option
```

### Tab Management
```bash
bu switch <tab>                  # Switch to tab by index
bu close-tab                     # Close current tab
bu close-tab <tab>               # Close specific tab
```

### JavaScript & Data
```bash
bu eval "document.title"         # Execute JavaScript, return result
bu extract "all product prices"  # Extract data using LLM (requires API key)
```

### Python Execution (Persistent Session)
```bash
bu python "x = 42"               # Set variable
bu python "print(x)"             # Access variable (outputs: 42)
bu python "print(browser.url)"   # Access browser object
bu python --vars                 # Show defined variables
bu python --reset                # Clear Python namespace
bu python --file script.py       # Execute Python file
```

The Python session maintains state across commands. The `browser` object provides:
- `browser.url` - Current page URL
- `browser.title` - Page title
- `browser.goto(url)` - Navigate
- `browser.click(index)` - Click element
- `browser.type(text)` - Type text
- `browser.screenshot(path)` - Take screenshot
- `browser.scroll()` - Scroll page
- `browser.html` - Get page HTML

### Agent Tasks (Requires API Key)
```bash
bu run "Fill the contact form with test data"    # Run AI agent
bu run "Extract all product prices" --max-steps 50
```

Agent tasks use an LLM to autonomously complete complex browser tasks. Requires `BROWSER_USE_API_KEY` or configured LLM API key (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc).

### Session Management
```bash
bu sessions                      # List active sessions
bu close                         # Close current session
bu close --all                   # Close all sessions
```

### Server Control
```bash
bu server status                 # Check if server is running
bu server stop                   # Stop server
bu server logs                   # View server logs
```

## Global Options

| Option | Description |
|--------|-------------|
| `--session NAME` | Use named session (default: "default") |
| `--browser MODE` | Browser mode: chromium, real, remote |
| `--headed` | Show browser window (chromium mode) |
| `--profile NAME` | Chrome profile (real mode only) |
| `--json` | Output as JSON |
| `--api-key KEY` | Override API key |

## Examples

### Form Submission
```bash
bu open https://example.com/contact
bu state
# Shows: [0] input "Name", [1] input "Email", [2] textarea "Message", [3] button "Submit"
bu input 0 "John Doe"
bu input 1 "john@example.com"
bu input 2 "Hello, this is a test message."
bu click 3
bu state  # Verify success
```

### Multi-Session Workflows
```bash
bu --session work open https://work.example.com
bu --session personal open https://personal.example.com
bu --session work state    # Check work session
bu --session personal state  # Check personal session
bu close --all             # Close both sessions
```

### Data Extraction with Python
```bash
bu open https://example.com/products
bu python "
products = []
for i in range(20):
    browser.scroll('down')
browser.screenshot('products.png')
"
bu python "print(f'Captured {len(products)} products')"
```

### Using Real Browser (Logged-In Sessions)
```bash
bu --browser real open https://gmail.com
# Uses your actual Chrome with existing login sessions
bu state  # Already logged in!
```

## Tips

1. **Always run `bu state` first** to see available elements and their indices
2. **Use `--headed` for debugging** to see what the browser is doing
3. **Sessions persist** - the browser stays open between commands
4. **Use `--json` for parsing** output programmatically
5. **Python variables persist** across `bu python` commands within a session
6. **Real browser mode** preserves your login sessions and extensions

## Troubleshooting

**Browser won't start?**
```bash
bu server stop               # Stop any stuck server
bu --headed open <url>       # Try with visible window
```

**Element not found?**
```bash
bu state                     # Check current elements
bu scroll down               # Element might be below fold
bu state                     # Check again
```

**Session issues?**
```bash
bu sessions                  # Check active sessions
bu close --all               # Clean slate
bu open <url>                # Fresh start
```
