# python-build-standalone Build Pipeline for Hand-X

This directory contains scripts to build Hand-X as a portable, relocatable Python distribution using **python-build-standalone** instead of PyInstaller.

## Why python-build-standalone?

- **Portability:** Creates self-contained Python distributions that work on any machine without system Python
- **Relocatability:** Output directories can be moved or copied without breaking paths
- **Size predictability:** 20-30MB vs PyInstaller's single-file binary approach
- **Transparency:** No obfuscation; code is readable and debuggable
- **Electron integration:** Ideal for bundling inside desktop apps
- **Platform support:** macOS ARM64/x64, Linux x64, Windows x64

## Build Scripts

### `download-python.sh`

**Purpose:** Download and extract the correct python-build-standalone distribution for your platform.

**Usage:**
```bash
./build/download-python.sh <target-dir> [python-version]
```

**Example:**
```bash
./build/download-python.sh ./python-dist 3.12
```

**What it does:**
1. Detects OS (macOS, Linux, Windows) and architecture (ARM64, x64)
2. Queries GitHub API for the latest python-build-standalone release
3. Downloads the `install_only_stripped` variant (smallest)
4. Extracts to target directory
5. Verifies Python binary works

**Output:**
- `<target-dir>/python/` — standalone Python distribution
- `<target-dir>/python/bin/python3` (or `.exe` on Windows)

### `build-standalone.sh`

**Purpose:** Complete end-to-end build of Hand-X with a portable Python distribution.

**Usage:**
```bash
./build/build-standalone.sh              # build for current platform
./build/build-standalone.sh --clean      # remove previous artifacts first
./build/build-standalone.sh --help       # show help
```

**What it does:**
1. Downloads python-build-standalone (via `download-python.sh`)
2. Creates a virtualenv using the downloaded Python
3. Installs Hand-X and all dependencies via `pip install -e .`
4. Copies application code (ghosthands/, browser_use/)
5. Creates entry point wrappers (shell script for macOS/Linux, batch for Windows)
6. Verifies all packages are importable
7. Reports output directory and size

**Output structure:**
```
dist/hand-x-{os}-{arch}/
├── python/                 # Standalone Python 3.12 distribution
│   ├── bin/
│   │   ├── python3
│   │   ├── pip
│   │   ├── playwright
│   │   └── ...
│   └── lib/python3.12/     # Standard library
├── venv/                   # Virtualenv with Hand-X + deps
│   ├── bin/activate        # Activate script
│   ├── bin/python          # Python symlink
│   ├── pyvenv.cfg
│   └── lib/python3.12/site-packages/
│       ├── ghosthands/     # Installed package
│       ├── playwright/
│       ├── anthropic/
│       ├── pydantic/
│       └── ...
├── ghosthands/            # Source code (reference/debug)
├── browser_use/           # Source code (reference/debug)
├── run-ghosthands.sh      # Entry point wrapper (macOS/Linux)
└── run-ghosthands.bat     # Entry point wrapper (Windows)
```

**Size estimate:**
- Python runtime: ~80-100MB
- Dependencies: ~150-200MB
- Total: ~250-300MB per platform

## Quick Start

### Build for current platform

```bash
cd Hand-X
./build/build-standalone.sh
```

Output: `build/dist/hand-x-{darwin|linux|win}-{arm64|x64}/`

### Clean and rebuild

```bash
./build/build-standalone.sh --clean
```

### Test the build

```bash
# Run ghosthands help
./build/dist/hand-x-darwin-arm64/run-ghosthands.sh --help

# Or use Python directly
./build/dist/hand-x-darwin-arm64/venv/bin/python -m ghosthands.main --help
```

## Runtime Configuration

### Using the bundled distribution in Electron

In your Electron app, invoke the Python process like this:

```javascript
const { spawn } = require('child_process');
const path = require('path');

// Assume bundled at: app.asar/python-runtime/
const runtimeDir = path.join(__dirname, '..', 'python-runtime');
const pythonBinary = path.join(runtimeDir, 'venv', 'bin', 'python');

// Or use the wrapper script:
const wrapperScript = path.join(runtimeDir, 'run-ghosthands.sh');

const env = {
    ...process.env,
    // Point Playwright to bundled browser directory
    PLAYWRIGHT_BROWSERS_PATH: path.join(runtimeDir, 'browsers'),
    // Point to database (or other config)
    GH_DATABASE_URL: 'postgresql://...',
};

const child = spawn(pythonBinary, ['-m', 'ghosthands.main'], { env });
```

### Environment variables at runtime

The build script does NOT install Playwright browsers. That's handled separately.

When running, set these environment variables:

| Variable | Purpose | Example |
|----------|---------|---------|
| `PLAYWRIGHT_BROWSERS_PATH` | Browser cache directory | `/path/to/app/browsers` |
| `GH_DATABASE_URL` | Postgres connection | `postgresql://user:pass@host/db` |
| `GH_ANTHROPIC_API_KEY` | Anthropic API key | `sk-ant-...` |
| `GH_WORKER_ID` | Worker identity | `electron-1` |
| (others) | See `.env.example` | |

## Platform-Specific Notes

### macOS

- Supports both ARM64 (Apple Silicon) and x86_64 (Intel)
- Download URL pattern: `cpython-3.12.*+*-aarch64-apple-darwin-install_only_stripped.tar.gz`
- Virtualenv is relocatable by default

### Linux

- Only x86_64 is supported
- Tested on Ubuntu 20.04+, CentOS 7+
- Download URL pattern: `cpython-3.12.*+*-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz`
- May require glibc 2.17+ (usually available)

### Windows

- Only x86_64 is supported (32-bit not tested)
- Build on Windows requires:
  - Git Bash, MSYS2, or WSL2
  - curl, tar, gzip
- Download URL pattern: `cpython-3.12.*+*-x86_64-pc-windows-msvc-install_only_stripped.tar.gz`
- Entry point: `run-ghosthands.bat`

## Troubleshooting

### "Could not find download URL for platform"

The python-build-standalone release may not include your platform yet. Check:
1. Visit https://github.com/astral-sh/python-build-standalone/releases
2. Look for the latest release with your platform tag
3. If missing, file an issue on the project

### "Python is not 3.12+"

The downloaded tarball doesn't contain Python 3.12. Check:
1. Are you specifying the right Python version? (default is 3.12)
2. Is the GitHub release recent enough?

### "virtualenv activation failed"

Make sure you're using `source` to activate (not just `./venv/bin/activate`):
```bash
source ./build/dist/hand-x-*/venv/bin/activate
```

### "ghosthands package NOT importable"

The package install failed. Check:
1. Did step 3 complete without errors?
2. Is `pyproject.toml` valid?
3. Try running manually in the venv:
   ```bash
   source ./build/dist/hand-x-*/venv/bin/activate
   python -m pip install -e .
   ```

### "playwright package NOT importable"

Same as above, but for Playwright specifically. It may need native C libraries on Linux:
```bash
# Ubuntu/Debian
sudo apt-get install libglib2.0-0 libx11-6
```

## Next Steps: Browser Installation

The bundled distribution does NOT include Playwright browsers. They are installed separately, typically:

1. **At build time** (in CI/CD before packaging):
   ```bash
   python -m playwright install chromium
   PLAYWRIGHT_BROWSERS_PATH=/path/to/browsers
   ```

2. **At first runtime** (Electron app installs on first launch):
   ```bash
   PLAYWRIGHT_BROWSERS_PATH=$APP_DATA/browsers \
     python -m playwright install chromium
   ```

3. **Pre-downloaded** (ship with app):
   - Commit browsers to version control or download server
   - Copy to app directory at install time

## Integration with CI/CD

Example GitHub Actions workflow:

```yaml
name: Build Hand-X Standalone

on:
  push:
    branches: [main]

jobs:
  build:
    strategy:
      matrix:
        include:
          - os: macos-latest
            arch: arm64
          - os: macos-13
            arch: x64
          - os: ubuntu-latest
            arch: x64
          - os: windows-latest
            arch: x64

    runs-on: ${{ matrix.os }}

    steps:
      - uses: actions/checkout@v4

      - name: Build with python-build-standalone
        run: |
          cd Hand-X
          ./build/build-standalone.sh

      - name: Upload artifact
        uses: actions/upload-artifact@v4
        with:
          name: hand-x-${{ matrix.os }}-${{ matrix.arch }}
          path: Hand-X/build/dist/hand-x-*/
```

## References

- [python-build-standalone GitHub](https://github.com/astral-sh/python-build-standalone)
- [python-build-standalone releases](https://github.com/astral-sh/python-build-standalone/releases)
- [Playwright browser management](https://playwright.dev/python/docs/browsers)
- [Hand-X CLAUDE.md](../CLAUDE.md)
