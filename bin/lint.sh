#!/usr/bin/env bash
# This script is used to run the formatter, linter, and type checker pre-commit hooks.
# Usage:
#   $ ./bin/lint.sh

IFS=$'\n'

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

cd "$SCRIPT_DIR/.." || exit 1

echo "[*] Running ruff linter, formatter, pyright type checker, and other pre-commit checks in parallel..."

# Create temp directory for logs
TEMP_DIR=$(mktemp -d)
trap "rm -rf $TEMP_DIR" EXIT

# Run expensive checks in parallel
# Pyright is the slowest, so we run it alongside other checks
uv run pyright > "$TEMP_DIR/pyright.log" 2>&1 &
PYRIGHT_PID=$!

# Run ruff checks (fast but run in parallel anyway)
uv run ruff check --fix > "$TEMP_DIR/ruff-check.log" 2>&1 &
RUFF_CHECK_PID=$!

uv run ruff format > "$TEMP_DIR/ruff-format.log" 2>&1 &
RUFF_FORMAT_PID=$!

# Run the lightweight pre-commit hooks in parallel
# Using SKIP to exclude ruff and pyright since we're running them separately
SKIP=ruff-check,ruff-format,pyright uv run pre-commit run --all-files > "$TEMP_DIR/other-checks.log" 2>&1 &
OTHER_PID=$!

# Wait for all parallel jobs and collect their exit codes
FAILED=0

wait $PYRIGHT_PID
PYRIGHT_EXIT=$?
if [ $PYRIGHT_EXIT -ne 0 ]; then
    echo "❌ Pyright failed:"
    cat "$TEMP_DIR/pyright.log"
    FAILED=1
else
    echo "✅ Pyright passed"
fi

wait $RUFF_CHECK_PID
RUFF_CHECK_EXIT=$?
if [ $RUFF_CHECK_EXIT -ne 0 ]; then
    echo "❌ Ruff check failed:"
    cat "$TEMP_DIR/ruff-check.log"
    FAILED=1
else
    echo "✅ Ruff check passed"
fi

wait $RUFF_FORMAT_PID
RUFF_FORMAT_EXIT=$?
if [ $RUFF_FORMAT_EXIT -ne 0 ]; then
    echo "❌ Ruff format failed:"
    cat "$TEMP_DIR/ruff-format.log"
    FAILED=1
else
    echo "✅ Ruff format passed"
fi

wait $OTHER_PID
OTHER_EXIT=$?
if [ $OTHER_EXIT -ne 0 ]; then
    echo "❌ Other pre-commit checks failed:"
    cat "$TEMP_DIR/other-checks.log"
    FAILED=1
else
    echo "✅ Other checks passed"
fi

if [ $FAILED -eq 1 ]; then
    echo ""
    echo "❌ Linting failed - see errors above"
    exit 1
fi

echo ""
echo "✅ All checks passed!"
exit 0
