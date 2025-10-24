#!/usr/bin/env bash
# This script is used to run the formatter, linter, and type checker pre-commit hooks.
# Usage:
#   $ ./bin/lint.sh [--fail-fast]
#
# Options:
#   --fail-fast    Exit immediately on first failure (faster feedback)

set -o pipefail
IFS=$'\n'

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$SCRIPT_DIR/.." || exit 1

# Parse arguments
FAIL_FAST=0
if [[ "$1" == "--fail-fast" ]]; then
    FAIL_FAST=1
fi

# Create temp directory for logs
TEMP_DIR=$(mktemp -d)
trap "rm -rf $TEMP_DIR" EXIT

# Helper function to show spinner while waiting for process
spinner() {
    local pid=$1
    local name=$2
    local spin='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local i=0
    while kill -0 "$pid" 2>/dev/null; do
        i=$(( (i+1) %10 ))
        printf "\r[${spin:$i:1}] Running %s..." "$name"
        sleep 0.1
    done
    printf "\r"
}

# Helper to wait for job and handle result
wait_for_job() {
    local pid=$1
    local name=$2
    local logfile=$3
    local start_time=$4

    wait "$pid"
    local exit_code=$?
    local duration=$(($(date +%s) - start_time))

    if [ $exit_code -ne 0 ]; then
        printf "%-25s ❌ (%.1fs)\n" "$name" "$duration"
        if [ -s "$logfile" ]; then
            echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            cat "$logfile"
            echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        fi
        return 1
    else
        printf "%-25s ✅ (%.1fs)\n" "$name" "$duration"
        return 0
    fi
}

echo "[*] Running linter, formatter, type checker, and pre-commit hooks in parallel..."
echo ""

START_TIME=$(date +%s)

# Launch all checks in parallel
uv run ruff check --fix > "$TEMP_DIR/ruff-check.log" 2>&1 &
RUFF_CHECK_PID=$!
RUFF_CHECK_START=$(date +%s)

uv run ruff format > "$TEMP_DIR/ruff-format.log" 2>&1 &
RUFF_FORMAT_PID=$!
RUFF_FORMAT_START=$(date +%s)

uv run pyright > "$TEMP_DIR/pyright.log" 2>&1 &
PYRIGHT_PID=$!
PYRIGHT_START=$(date +%s)

SKIP=ruff-check,ruff-format,pyright uv run pre-commit run --all-files > "$TEMP_DIR/other-checks.log" 2>&1 &
OTHER_PID=$!
OTHER_START=$(date +%s)

# Track failures
FAILED=0
FAILED_CHECKS=""

# Wait for each job in order of expected completion (fastest first)
# This allows --fail-fast to exit as soon as any check fails

# Ruff format is typically fastest
spinner $RUFF_FORMAT_PID "ruff format"
if ! wait_for_job $RUFF_FORMAT_PID "ruff format" "$TEMP_DIR/ruff-format.log" $RUFF_FORMAT_START; then
    FAILED=1
    FAILED_CHECKS="$FAILED_CHECKS ruff-format"
    if [ $FAIL_FAST -eq 1 ]; then
        kill $RUFF_CHECK_PID $PYRIGHT_PID $OTHER_PID 2>/dev/null
        wait $RUFF_CHECK_PID $PYRIGHT_PID $OTHER_PID 2>/dev/null
        echo ""
        echo "❌ Fast-fail: Exiting early due to ruff format failure"
        exit 1
    fi
fi

# Ruff check is second fastest
spinner $RUFF_CHECK_PID "ruff check"
if ! wait_for_job $RUFF_CHECK_PID "ruff check" "$TEMP_DIR/ruff-check.log" $RUFF_CHECK_START; then
    FAILED=1
    FAILED_CHECKS="$FAILED_CHECKS ruff-check"
    if [ $FAIL_FAST -eq 1 ]; then
        kill $PYRIGHT_PID $OTHER_PID 2>/dev/null
        wait $PYRIGHT_PID $OTHER_PID 2>/dev/null
        echo ""
        echo "❌ Fast-fail: Exiting early due to ruff check failure"
        exit 1
    fi
fi

# Pre-commit hooks are medium speed
spinner $OTHER_PID "other pre-commit hooks"
if ! wait_for_job $OTHER_PID "other pre-commit hooks" "$TEMP_DIR/other-checks.log" $OTHER_START; then
    FAILED=1
    FAILED_CHECKS="$FAILED_CHECKS pre-commit"
    if [ $FAIL_FAST -eq 1 ]; then
        kill $PYRIGHT_PID 2>/dev/null
        wait $PYRIGHT_PID 2>/dev/null
        echo ""
        echo "❌ Fast-fail: Exiting early due to pre-commit hooks failure"
        exit 1
    fi
fi

# Pyright is slowest (wait last for maximum parallelism)
spinner $PYRIGHT_PID "pyright"
if ! wait_for_job $PYRIGHT_PID "pyright" "$TEMP_DIR/pyright.log" $PYRIGHT_START; then
    FAILED=1
    FAILED_CHECKS="$FAILED_CHECKS pyright"
fi

TOTAL_TIME=$(($(date +%s) - START_TIME))

echo ""
if [ $FAILED -eq 1 ]; then
    echo "❌ Checks failed:$FAILED_CHECKS (${TOTAL_TIME}s total)"
    exit 1
fi

echo "✅ All checks passed! (${TOTAL_TIME}s total)"
exit 0
