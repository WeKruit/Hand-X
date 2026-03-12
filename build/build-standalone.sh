#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build-standalone.sh -- Build Hand-X with python-build-standalone
#
# Creates a relocatable, portable Python distribution bundled with Hand-X
# and all dependencies. Output is in dist/hand-x-{platform}/ containing:
#
#   python/              — the standalone Python distribution
#   venv/                — virtualenv with Hand-X + all deps installed
#   ghosthands/          — application code (symlink or copy)
#
# The Electron app can then bundle this and invoke:
#   ./hand-x-{platform}/python/bin/python -m ghosthands.main
#
# Usage:
#   ./build/build-standalone.sh              # build for current platform
#   ./build/build-standalone.sh --clean      # remove previous artifacts
#   ./build/build-standalone.sh --help       # show this message
#
# Requirements:
#   - macOS 12+, Linux x64, or Windows x64
#   - curl (to download python-build-standalone)
#   - tar, gzip
#   - ~1.5GB free disk space
#
# Environment variables (optional):
#   PYTHON_VERSION              Set Python version (default: 3.12)
#   SKIP_PLAYWRIGHT_INSTALL     Skip browser installation (default: false)
#   PLAYWRIGHT_BROWSERS_PATH    Browser cache path (only used at runtime)
# ---------------------------------------------------------------------------
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$DIR/.."

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
SKIP_PLAYWRIGHT="${SKIP_PLAYWRIGHT_INSTALL:-false}"
BUILD_CLEAN="${1:-}"

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
if [[ "$BUILD_CLEAN" == "--help" || "$BUILD_CLEAN" == "-h" ]]; then
    cat "$0" | grep "^# " | head -50
    exit 0
fi

# ---------------------------------------------------------------------------
# Clean (optional)
# ---------------------------------------------------------------------------
if [[ "$BUILD_CLEAN" == "--clean" ]]; then
    echo "Cleaning previous build artifacts..."
    rm -rf "$DIR/dist" "$DIR/work"
fi

echo "=== Hand-X Standalone Build ==="
echo "Platform: $(uname -s) $(uname -m)"
echo "Python version: $PYTHON_VERSION"
echo ""

# ---------------------------------------------------------------------------
# Determine platform naming
# ---------------------------------------------------------------------------
OS_RAW=$(uname -s)
ARCH_RAW=$(uname -m)

case "$OS_RAW" in
    Darwin)      OS="darwin" ;;
    Linux)       OS="linux" ;;
    MINGW*|MSYS*|CYGWIN*|Windows_NT)
                 OS="win" ;;
    *)           OS=$(echo "$OS_RAW" | tr '[:upper:]' '[:lower:]') ;;
esac

case "$ARCH_RAW" in
    arm64|aarch64)   ARCH="arm64" ;;
    x86_64|amd64)    ARCH="x64" ;;
    *)               ARCH="$ARCH_RAW" ;;
esac

DIST_DIR="$DIR/dist/hand-x-${OS}-${ARCH}"
WORK_DIR="$DIR/work/hand-x-${OS}-${ARCH}"
PYTHON_WORK_DIR="$WORK_DIR/python-dist"

echo "Target platform: $OS-$ARCH"
echo "Output directory: $DIST_DIR"
echo ""

# Create directories
mkdir -p "$WORK_DIR" "$PYTHON_WORK_DIR" "$DIST_DIR"

# ---------------------------------------------------------------------------
# Step 1: Download python-build-standalone
# ---------------------------------------------------------------------------
echo "Step 1: Downloading python-build-standalone ($PYTHON_VERSION)..."
echo ""

"$DIR/download-python.sh" "$PYTHON_WORK_DIR" "$PYTHON_VERSION"

echo ""
echo "Step 1: OK"
echo ""

# Find the extracted Python directory
if [[ -d "$PYTHON_WORK_DIR/python" ]]; then
    PYTHON_EXTRACTED="$PYTHON_WORK_DIR/python"
elif [[ -d "$PYTHON_WORK_DIR/python-build-standalone" ]]; then
    PYTHON_EXTRACTED="$PYTHON_WORK_DIR/python-build-standalone"
else
    # Try to find the directory with bin/python
    PYTHON_EXTRACTED=$(find "$PYTHON_WORK_DIR" -maxdepth 2 -name "bin" -type d | head -1 | xargs dirname)
fi

if [[ ! -d "$PYTHON_EXTRACTED" ]]; then
    echo "ERROR: Could not find extracted Python directory"
    ls -la "$PYTHON_WORK_DIR"
    exit 1
fi

PYTHON_BIN="$PYTHON_EXTRACTED/bin/python3"
if [[ ! -f "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$PYTHON_EXTRACTED/bin/python"
fi

echo "Python binary: $PYTHON_BIN"

# Copy Python to dist (the final location)
echo ""
echo "Copying Python to output directory..."
cp -r "$PYTHON_EXTRACTED" "$DIST_DIR/python"
PYTHON_BIN_FINAL="$DIST_DIR/python/bin/python3"
if [[ ! -f "$PYTHON_BIN_FINAL" ]]; then
    PYTHON_BIN_FINAL="$DIST_DIR/python/bin/python"
fi

# ---------------------------------------------------------------------------
# Step 2: Create and initialize virtualenv
# ---------------------------------------------------------------------------
echo ""
echo "Step 2: Creating virtualenv with venv module..."
echo ""

"$PYTHON_BIN_FINAL" -m venv "$DIST_DIR/venv" --upgrade-deps --copies

echo "Step 2: OK"
echo ""

# Activate the venv
if [[ -f "$DIST_DIR/venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$DIST_DIR/venv/bin/activate"
    VENV_PYTHON="$DIST_DIR/venv/bin/python"
else
    echo "ERROR: Could not activate virtualenv"
    exit 1
fi

echo "Venv Python: $VENV_PYTHON"
echo "Venv Python version: $($VENV_PYTHON --version)"
echo ""

# ---------------------------------------------------------------------------
# Step 3: Install Hand-X and dependencies
# ---------------------------------------------------------------------------
echo ""
echo "Step 3: Installing Hand-X and dependencies..."
echo ""

cd "$ROOT"

# Upgrade pip, setuptools, wheel
echo "Upgrading build tools..."
"$VENV_PYTHON" -m pip install --quiet --upgrade pip setuptools wheel

# Install Hand-X in development mode
# This also installs all dependencies from pyproject.toml
echo "Installing ghosthands package..."
"$VENV_PYTHON" -m pip install --quiet -e .

echo "Step 3: OK"
echo ""

# ---------------------------------------------------------------------------
# Step 4: Install Playwright (unless skipped)
# ---------------------------------------------------------------------------
if [[ "$SKIP_PLAYWRIGHT" != "true" ]]; then
    echo ""
    echo "Step 4: Installing Playwright browser..."
    echo ""

    # Note: We don't install browsers here. That's done separately by the
    # Electron app. Instead, we just ensure playwright is installed (done above
    # via pyproject.toml dependency).
    echo "Playwright package installed (browsers installed separately at runtime)"

    echo "Step 4: OK"
    echo ""
else
    echo "Step 4: Skipped (SKIP_PLAYWRIGHT_INSTALL=true)"
    echo ""
fi

# ---------------------------------------------------------------------------
# Step 5: Copy application code
# ---------------------------------------------------------------------------
echo ""
echo "Step 5: Copying application code..."
echo ""

# Create a symlink or copy of the application code for reference
# (the actual code is already in the venv's site-packages, but this is useful
# for debugging and development)
if [[ -d "$ROOT/ghosthands" ]]; then
    cp -r "$ROOT/ghosthands" "$DIST_DIR/ghosthands"
    echo "Copied ghosthands/ to $DIST_DIR/ghosthands"
fi

if [[ -d "$ROOT/browser_use" ]]; then
    cp -r "$ROOT/browser_use" "$DIST_DIR/browser_use"
    echo "Copied browser_use/ to $DIST_DIR/browser_use"
fi

echo "Step 5: OK"
echo ""

# ---------------------------------------------------------------------------
# Step 6: Create entry point wrapper (optional)
# ---------------------------------------------------------------------------
echo ""
echo "Step 6: Creating entry point wrapper..."
echo ""

# Create a simple shell wrapper that runs Hand-X with the correct Python
WRAPPER_SCRIPT="$DIST_DIR/run-ghosthands.sh"
cat > "$WRAPPER_SCRIPT" <<'EOF'
#!/usr/bin/env bash
# Wrapper script to run GhostHands with the bundled Python

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/venv/bin/python"

# Allow overriding PLAYWRIGHT_BROWSERS_PATH
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$SCRIPT_DIR/browsers}"

exec "$PYTHON" -m ghosthands.main "$@"
EOF

chmod +x "$WRAPPER_SCRIPT"
echo "Created: $WRAPPER_SCRIPT"

# For Windows, create a batch wrapper
WRAPPER_BAT="$DIST_DIR/run-ghosthands.bat"
cat > "$WRAPPER_BAT" <<'EOF'
@echo off
REM Wrapper script to run GhostHands with the bundled Python on Windows

setlocal enabledelayedexpansion
set SCRIPT_DIR=%~dp0
set PYTHON=%SCRIPT_DIR%venv\Scripts\python.exe

if not exist "%PYTHON%" (
    echo ERROR: Python not found at %PYTHON%
    exit /b 1
)

REM Set PLAYWRIGHT_BROWSERS_PATH if not already set
if not defined PLAYWRIGHT_BROWSERS_PATH (
    set PLAYWRIGHT_BROWSERS_PATH=%SCRIPT_DIR%browsers
)

"%PYTHON%" -m ghosthands.main %*
EOF

echo "Created: $WRAPPER_BAT"
echo "Step 6: OK"
echo ""

# ---------------------------------------------------------------------------
# Step 7: Verify the build
# ---------------------------------------------------------------------------
echo ""
echo "Step 7: Verifying build..."
echo ""

# Test 1: Python version
PYTHON_VERIFY=$("$VENV_PYTHON" --version 2>&1)
echo "  Python: $PYTHON_VERIFY"

# Test 2: ghosthands package
if "$VENV_PYTHON" -c "import ghosthands" 2>/dev/null; then
    echo "  ✓ ghosthands package importable"
else
    echo "  ✗ ghosthands package NOT importable"
    exit 1
fi

# Test 3: playwright package
if "$VENV_PYTHON" -c "import playwright" 2>/dev/null; then
    echo "  ✓ playwright package importable"
else
    echo "  ✗ playwright package NOT importable"
    exit 1
fi

# Test 4: anthropic package (for LLM)
if "$VENV_PYTHON" -c "import anthropic" 2>/dev/null; then
    echo "  ✓ anthropic package importable"
else
    echo "  ✗ anthropic package NOT importable"
    exit 1
fi

# Test 5: Key dependencies
for pkg in pydantic asyncpg httpx cryptography structlog; do
    if "$VENV_PYTHON" -c "import $pkg" 2>/dev/null; then
        echo "  ✓ $pkg package importable"
    else
        echo "  ✗ $pkg package NOT importable"
    fi
done

echo "Step 7: OK"
echo ""

# ---------------------------------------------------------------------------
# Step 8: Report
# ---------------------------------------------------------------------------
echo ""
echo "=== Build Complete ==="
echo ""
echo "Output directory: $DIST_DIR"
echo "Directory structure:"
ls -lhd "$DIST_DIR"/*

echo ""
echo "Total size:"
du -sh "$DIST_DIR"

echo ""
echo "To use this build:"
echo ""
echo "  1. Copy dist/hand-x-${OS}-${ARCH}/ to your Electron app bundle"
echo ""
echo "  2. Run with the wrapper script:"
echo "       ./hand-x-${OS}-${ARCH}/run-ghosthands.sh [args]"
echo ""
echo "  3. Or run directly with the venv Python:"
echo "       ./hand-x-${OS}-${ARCH}/venv/bin/python -m ghosthands.main [args]"
echo ""
echo "  4. Set PLAYWRIGHT_BROWSERS_PATH for browser installation:"
echo "       export PLAYWRIGHT_BROWSERS_PATH=/path/to/browsers"
echo "       ./hand-x-${OS}-${ARCH}/run-ghosthands.sh"
echo ""
