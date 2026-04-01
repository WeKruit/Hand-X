#!/usr/bin/env bash
# dev-deploy.sh — Build Hand-X binary and install it where the Desktop app expects.
#
# Simulates the real customer environment: Desktop spawns a compiled binary,
# not Python source. Run this after making changes, then `npm run dev` in
# GH-Desktop-App to test end-to-end.
#
# Usage:
#   ./scripts/dev-deploy.sh            # build + install
#   ./scripts/dev-deploy.sh --skip-build  # install existing binary (faster iteration)
#   ./scripts/dev-deploy.sh --clean       # remove dev binary, restore to bundled/downloaded

set -euo pipefail

# ---------- Platform detection ----------
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
  Darwin)  PLATFORM="darwin" ;;
  Linux)   PLATFORM="linux" ;;
  MINGW*|MSYS*|CYGWIN*) PLATFORM="win" ;;
  *) echo "Unsupported OS: $OS"; exit 1 ;;
esac

case "$ARCH" in
  arm64|aarch64) ARCH_KEY="arm64" ;;
  x86_64|amd64)  ARCH_KEY="x64" ;;
  *) echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

if [ "$PLATFORM" = "win" ]; then
  BINARY_NAME="hand-x-${PLATFORM}-${ARCH_KEY}.exe"
else
  BINARY_NAME="hand-x-${PLATFORM}-${ARCH_KEY}"
fi

# ---------- Paths ----------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Desktop app userData path
if [ -n "${GH_DESKTOP_USER_DATA_PATH:-}" ]; then
  APP_DATA="$GH_DESKTOP_USER_DATA_PATH"
else
  case "$PLATFORM" in
    darwin) APP_DATA="$HOME/Library/Application Support/Valet" ;;
    linux)  APP_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/gh-desktop-app" ;;
    win)    APP_DATA="${APPDATA}/gh-desktop-app" ;;
  esac
fi

BIN_DIR="$APP_DATA/bin"
BINARY_DEST="$BIN_DIR/$BINARY_NAME"
VERSION_STATE="$BIN_DIR/hand-x-downloaded-version.json"

# ---------- Flags ----------
SKIP_BUILD=false
CLEAN=false
for arg in "$@"; do
  case "$arg" in
    --skip-build) SKIP_BUILD=true ;;
    --clean)      CLEAN=true ;;
    -h|--help)
      echo "Usage: $0 [--skip-build] [--clean]"
      echo ""
      echo "  --skip-build  Skip PyInstaller build, install existing binary"
      echo "  --clean       Remove dev binary and version state"
      exit 0
      ;;
  esac
done

# ---------- Clean mode ----------
if [ "$CLEAN" = true ]; then
  echo "Removing dev binary and version state..."
  rm -f "$BINARY_DEST"
  rm -f "$VERSION_STATE"
  echo "Done. Desktop will fall back to bundled binary or Python source."
  exit 0
fi

# ---------- Build ----------
cd "$REPO_ROOT"

if [ "$SKIP_BUILD" = false ]; then
  echo "Building Hand-X binary for $PLATFORM-$ARCH_KEY..."
  echo ""

  # Ensure deps are installed
  if [ ! -d ".venv" ]; then
    echo "No .venv found. Run: uv venv --python 3.12 && uv pip install -e '.[dev]'"
    exit 1
  fi

  # Always activate project venv (overrides conda/system Python)
  source .venv/bin/activate

  # Install PyInstaller if missing
  if ! python -c "import PyInstaller" 2>/dev/null; then
    echo "Installing PyInstaller..."
    pip install pyinstaller==6.13.0
  fi

  # Skip browser download — Desktop manages browsers separately
  export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

  # Build
  pyinstaller build/hand-x.spec \
    --distpath "build/dist" \
    --workpath "build/work" \
    --noconfirm \
    --log-level WARN

  # Copy to platform-specific name (use cat to avoid inheriting macOS provenance xattrs)
  if [ "$PLATFORM" = "win" ]; then
    cat "build/dist/hand-x.exe" > "build/dist/$BINARY_NAME"
  else
    cat "build/dist/hand-x" > "build/dist/$BINARY_NAME"
  fi
  chmod +x "build/dist/$BINARY_NAME"

  echo ""
  echo "Build complete: build/dist/$BINARY_NAME"
  ls -lh "build/dist/$BINARY_NAME"
fi

# ---------- Verify binary ----------
BUILD_BINARY="build/dist/$BINARY_NAME"
if [ ! -f "$BUILD_BINARY" ]; then
  echo "Binary not found at $BUILD_BINARY"
  echo "Run without --skip-build first."
  exit 1
fi

# Smoke test
echo ""
echo "Smoke testing binary..."
chmod +x "$BUILD_BINARY"
# Remove macOS quarantine + provenance (Gatekeeper kills unsigned binaries)
if [ "$PLATFORM" = "darwin" ]; then
  xattr -cr "$BUILD_BINARY" 2>/dev/null || true
  # Re-sign adhoc after stripping xattrs
  codesign --force --sign - "$BUILD_BINARY" 2>/dev/null || true
fi
if "$BUILD_BINARY" --help >/dev/null 2>&1; then
  echo "  --help: OK"
else
  EXIT_CODE=$?
  if [ "$EXIT_CODE" -eq 2 ]; then
    echo "  --help: OK (exit 2 — argparse convention)"
  else
    echo "  --help: FAILED (exit $EXIT_CODE)"
    echo "Binary may be broken. Check build output."
    exit 1
  fi
fi

VERSION_OUTPUT=$("$BUILD_BINARY" --version 2>/dev/null || true)
echo "  --version: $VERSION_OUTPUT"

# Extract semver from --version output, fallback to __init__.py, fallback to git tag
VERSION=$(echo "$VERSION_OUTPUT" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
if [ -z "$VERSION" ]; then
  # Try reading from source
  VERSION=$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' "$REPO_ROOT/ghosthands/__init__.py" 2>/dev/null | head -1 || true)
fi
if [ -z "$VERSION" ]; then
  # Try latest git tag
  VERSION=$(git describe --tags --abbrev=0 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)
fi
if [ -z "$VERSION" ]; then
  VERSION="0.0.0-dev"
  echo "  Warning: Could not determine version, using $VERSION"
fi

# ---------- Install ----------
echo ""
echo "Installing to Desktop app binary location..."
mkdir -p "$BIN_DIR"

# Compute SHA-256 without depending on perl-based shasum (locale-sensitive on macOS)
SHA256=$(
  python - <<PY
from hashlib import sha256
from pathlib import Path
print(sha256(Path(r"$BUILD_BINARY").read_bytes()).hexdigest())
PY
)

# Copy binary (use cat to avoid inheriting macOS provenance/quarantine xattrs)
cat "$BUILD_BINARY" > "$BINARY_DEST"
chmod 755 "$BINARY_DEST"

# Remove macOS quarantine + provenance, re-sign adhoc
if [ "$PLATFORM" = "darwin" ]; then
  xattr -cr "$BINARY_DEST" 2>/dev/null || true
  codesign --force --sign - "$BINARY_DEST" 2>/dev/null || true
fi

# Write version state (Desktop checks this)
TIMESTAMP=$(
  python - <<'PY'
from datetime import datetime, timezone
print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
PY
)
cat > "$VERSION_STATE" <<EOF
{
  "version": "$VERSION",
  "releaseTag": "v${VERSION}-dev",
  "sha256": "$SHA256",
  "downloadedAt": "$TIMESTAMP",
  "binaryName": "$BINARY_NAME"
}
EOF

echo "  Binary:  $BINARY_DEST"
echo "  Version: $VERSION"
echo "  SHA-256: ${SHA256:0:16}..."
echo ""
echo "Done. Now run 'npm run dev' in GH-Desktop-App and trigger a job."
echo "To revert: $0 --clean"
