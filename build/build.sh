#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build.sh -- Build Hand-X into a standalone binary for the current platform.
#
# Produces a single-file executable at build/dist/hand-x-{os}-{arch}[.exe].
# The Electron desktop app downloads and spawns this binary at runtime.
#
# Usage:
#   ./build/build.sh           # build for current platform
#   ./build/build.sh --clean   # remove previous artifacts first
# ---------------------------------------------------------------------------
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$DIR/.."

# ---------------------------------------------------------------------------
# Clean (optional)
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--clean" ]]; then
    echo "Cleaning previous build artifacts..."
    rm -rf "$DIR/dist" "$DIR/work"
fi

echo "=== Building Hand-X binary ==="
echo "Platform: $(uname -s) $(uname -m)"
echo "Python:   $(python3 --version 2>&1 || echo 'not found')"
echo ""

# ---------------------------------------------------------------------------
# Activate venv
# ---------------------------------------------------------------------------
if [[ -f "$ROOT/.venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$ROOT/.venv/bin/activate"
elif [[ -f "$ROOT/.venv/Scripts/activate" ]]; then
    # Windows Git Bash / MSYS2
    # shellcheck disable=SC1091
    source "$ROOT/.venv/Scripts/activate"
else
    echo "ERROR: No virtual environment found at $ROOT/.venv/"
    echo "Run:  uv venv --python 3.12 && uv pip install -e '.[dev]'"
    exit 1
fi

echo "Using Python: $(which python)"
echo "Python version: $(python --version)"
echo ""

# ---------------------------------------------------------------------------
# Install PyInstaller if needed
# ---------------------------------------------------------------------------
if ! python -c "import PyInstaller" 2>/dev/null; then
    echo "Installing PyInstaller..."
    pip install pyinstaller --quiet
fi

PYINSTALLER_VERSION=$(python -c "import PyInstaller; print(PyInstaller.__version__)")
echo "PyInstaller: $PYINSTALLER_VERSION"
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

BINARY_NAME="hand-x-${OS}-${ARCH}"
if [[ "$OS" == "win" ]]; then
    BINARY_NAME="${BINARY_NAME}.exe"
fi

echo "Target binary: $BINARY_NAME"
echo ""

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
echo "Running PyInstaller..."
cd "$ROOT"

pyinstaller build/hand-x.spec \
    --distpath "build/dist" \
    --workpath "build/work" \
    --noconfirm \
    --log-level WARN

# ---------------------------------------------------------------------------
# Rename to platform-specific name
# ---------------------------------------------------------------------------
if [[ -f "build/dist/hand-x" ]]; then
    mv "build/dist/hand-x" "build/dist/$BINARY_NAME"
elif [[ -f "build/dist/hand-x.exe" ]]; then
    mv "build/dist/hand-x.exe" "build/dist/$BINARY_NAME"
else
    echo "ERROR: Build produced no output binary!"
    echo "Contents of build/dist/:"
    ls -la "build/dist/" 2>/dev/null || echo "  (directory does not exist)"
    exit 1
fi

# ---------------------------------------------------------------------------
# Optional UPX compression (Windows only -- causes issues on macOS/Linux)
# ---------------------------------------------------------------------------
if [[ "$OS" == "win" ]] && command -v upx &>/dev/null; then
    echo ""
    echo "Compressing with UPX..."
    BEFORE_SIZE=$(stat -c%s "build/dist/$BINARY_NAME" 2>/dev/null || stat -f%z "build/dist/$BINARY_NAME")
    upx --best --lzma "build/dist/$BINARY_NAME" || echo "UPX compression failed (non-fatal)"
    AFTER_SIZE=$(stat -c%s "build/dist/$BINARY_NAME" 2>/dev/null || stat -f%z "build/dist/$BINARY_NAME")
    echo "UPX: ${BEFORE_SIZE} -> ${AFTER_SIZE} bytes"
fi

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
echo ""
echo "=== Build complete ==="
ls -lh "build/dist/$BINARY_NAME"
echo ""
echo "Binary: build/dist/$BINARY_NAME"

# Verify the binary runs (quick smoke test)
echo ""
echo "Smoke test (--help)..."
if "build/dist/$BINARY_NAME" --help >/dev/null 2>&1; then
    echo "  OK -- binary executes successfully"
else
    EXIT_CODE=$?
    # Exit code 2 is common for argparse/click when --help is not defined yet
    if [[ $EXIT_CODE -eq 2 ]]; then
        echo "  OK -- binary starts (exit code 2 = no --help handler yet)"
    else
        echo "  WARNING -- binary exited with code $EXIT_CODE"
    fi
fi
