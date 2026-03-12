#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# download-python.sh -- Download and extract python-build-standalone
#
# Downloads the correct python-build-standalone distribution for the current
# platform and extracts it to a target directory.
#
# Usage:
#   ./build/download-python.sh <target-dir> [python-version]
#
# Defaults:
#   python-version = 3.12  (latest 3.12.x release)
#   target-dir     = required
#
# Environment variables:
#   PYTHON_BUILD_STANDALONE_URL  Override the release URL
#   PYTHON_BUILD_STANDALONE_DIR  Override the extracted dir name
# ---------------------------------------------------------------------------
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "ERROR: target-dir is required"
    echo "Usage: $0 <target-dir> [python-version]"
    exit 1
fi

TARGET_DIR="$1"
PYTHON_VERSION="${2:-3.12}"

# Ensure target dir exists
mkdir -p "$TARGET_DIR"

# ---------------------------------------------------------------------------
# Detect platform and architecture
# ---------------------------------------------------------------------------
OS_RAW=$(uname -s)
ARCH_RAW=$(uname -m)

case "$OS_RAW" in
    Darwin)
        case "$ARCH_RAW" in
            arm64)
                PLATFORM="aarch64-apple-darwin"
                ;;
            x86_64)
                PLATFORM="x86_64-apple-darwin"
                ;;
            *)
                echo "ERROR: Unsupported macOS architecture: $ARCH_RAW"
                exit 1
                ;;
        esac
        ;;
    Linux)
        if [[ "$ARCH_RAW" == "x86_64" ]]; then
            PLATFORM="x86_64-unknown-linux-gnu"
        else
            echo "ERROR: Unsupported Linux architecture: $ARCH_RAW"
            exit 1
        fi
        ;;
    MINGW*|MSYS*|CYGWIN*|Windows_NT)
        if [[ "$ARCH_RAW" == "x86_64" ]]; then
            PLATFORM="x86_64-pc-windows-msvc"
        else
            echo "ERROR: Unsupported Windows architecture: $ARCH_RAW"
            exit 1
        fi
        ;;
    *)
        echo "ERROR: Unsupported OS: $OS_RAW"
        exit 1
        ;;
esac

echo "Detected platform: $PLATFORM"
echo "Python version: $PYTHON_VERSION"
echo ""

# ---------------------------------------------------------------------------
# Fetch the latest release info from GitHub
# ---------------------------------------------------------------------------
GITHUB_API="https://api.github.com/repos/astral-sh/python-build-standalone/releases"
echo "Fetching release info from GitHub..."

RELEASES=$(curl -s "$GITHUB_API" | head -100)

# Find the first release matching the target Python version and platform
# The pattern is: cpython-3.12.*+*-<platform>-install_only_stripped.tar.gz
RELEASE_TAG=$(echo "$RELEASES" | grep -o "\"tag_name\": \"[^\"]*\"" | head -1 | cut -d'"' -f4)
if [[ -z "$RELEASE_TAG" ]]; then
    echo "ERROR: Could not find any python-build-standalone releases"
    exit 1
fi

echo "Using release: $RELEASE_TAG"

# Construct the expected filename
# Pattern: cpython-3.12.*+*-<platform>-install_only_stripped.tar.gz
TARBALL_PATTERN="cpython-${PYTHON_VERSION}*+*-${PLATFORM}-install_only_stripped.tar.gz"

# Get the download URL from the release assets
DOWNLOAD_URL=$(echo "$RELEASES" \
    | grep -o "\"browser_download_url\": \"[^\"]*${PLATFORM}-install_only_stripped[^\"]*\"" \
    | head -1 \
    | cut -d'"' -f4)

if [[ -z "$DOWNLOAD_URL" ]]; then
    echo "ERROR: Could not find download URL for platform: $PLATFORM"
    echo "Release assets may not include this platform."
    exit 1
fi

echo "Download URL: $DOWNLOAD_URL"
echo ""

# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
TARBALL_NAME=$(basename "$DOWNLOAD_URL")
TEMP_TARBALL="/tmp/$TARBALL_NAME"

echo "Downloading $TARBALL_NAME..."
curl -L -o "$TEMP_TARBALL" "$DOWNLOAD_URL"

# Verify download size
FILE_SIZE=$(stat -f%z "$TEMP_TARBALL" 2>/dev/null || stat -c%s "$TEMP_TARBALL" 2>/dev/null || echo 0)
if [[ $FILE_SIZE -lt 1000000 ]]; then
    echo "ERROR: Downloaded file is suspiciously small ($FILE_SIZE bytes). Download may have failed."
    rm -f "$TEMP_TARBALL"
    exit 1
fi

echo "Downloaded: $FILE_SIZE bytes"
echo ""

# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------
echo "Extracting to $TARGET_DIR..."
tar -xzf "$TEMP_TARBALL" -C "$TARGET_DIR"

rm -f "$TEMP_TARBALL"

# Check what was extracted
if [[ -d "$TARGET_DIR/python" ]]; then
    PYTHON_DIR="$TARGET_DIR/python"
elif [[ -d "$TARGET_DIR/python-build-standalone" ]]; then
    PYTHON_DIR="$TARGET_DIR/python-build-standalone"
else
    # Might be directly at root
    PYTHON_DIR="$TARGET_DIR"
fi

echo "Extracted to: $PYTHON_DIR"
echo ""

# ---------------------------------------------------------------------------
# Verify the Python binary works
# ---------------------------------------------------------------------------
PYTHON_BIN="$PYTHON_DIR/bin/python3"
if [[ ! -f "$PYTHON_BIN" ]]; then
    # Try without the 3
    PYTHON_BIN="$PYTHON_DIR/bin/python"
fi

if [[ ! -f "$PYTHON_BIN" ]]; then
    echo "ERROR: Could not find Python binary in extracted archive"
    echo "Expected: $PYTHON_DIR/bin/python3 or $PYTHON_DIR/bin/python"
    ls -la "$PYTHON_DIR/bin/" 2>/dev/null || echo "(bin/ does not exist)"
    exit 1
fi

echo "Testing Python binary..."
PYTHON_VERSION_OUTPUT=$("$PYTHON_BIN" --version 2>&1)
echo "  $PYTHON_VERSION_OUTPUT"

# Verify it's the right version
if ! "$PYTHON_BIN" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>/dev/null; then
    echo "ERROR: Python version is not 3.12+"
    exit 1
fi

echo ""
echo "=== Python successfully downloaded and verified ==="
echo "Python binary: $PYTHON_BIN"
echo "Target directory: $TARGET_DIR"
