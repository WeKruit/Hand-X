#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Hand-X — Workday Job Application Test
#
# Tests the full Workday flow including:
#   - Account creation (email + password + confirm password)
#   - Sign-in (if account already exists)
#   - Multi-page form filling (Personal Info → Experience → Questions → Review)
#   - Resume upload
#   - Demographic disclosures
#
# Usage:
#   ./examples/workday_test.sh                                    # default test job
#   ./examples/workday_test.sh "https://company.wd5.myworkdayjobs.com/..."
#   ./examples/workday_test.sh "https://..." --max-steps 25       # quick test
#
# For account creation, set credentials:
#   export WORKDAY_TEST_EMAIL="your-test@email.com"
#   export WORKDAY_TEST_PASSWORD="YourTestPassword123!"
#
# Or pass inline:
#   ./examples/workday_test.sh "https://..." --email test@email.com --password Pass123!
# ─────────────────────────────────────────────────────────────
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"

# ── Test data ─────────────────────────────────────────────────
RESUME="$DIR/examples/resume.pdf"
DATA="$DIR/examples/workday_test_data.json"

# ── Activate venv ─────────────────────────────────────────────
if [ ! -d "$DIR/.venv" ]; then
  echo "ERROR: .venv not found. Run: uv venv --python 3.12 && uv pip install -e '.[dev]'"
  exit 1
fi
source "$DIR/.venv/bin/activate"

# ── Load .env ─────────────────────────────────────────────────
[ -f "$DIR/.env" ] && set -a && source "$DIR/.env" && set +a

# ── Check API keys ────────────────────────────────────────────
if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${GH_ANTHROPIC_API_KEY:-}" ]; then
  echo "ERROR: Set ANTHROPIC_API_KEY in .env or environment (needed for DomHand)"
  exit 1
fi
if [ -z "${GOOGLE_API_KEY:-}" ]; then
  echo "WARNING: GOOGLE_API_KEY not set — agent model (Gemini) won't work without it"
fi

# ── Parse args ────────────────────────────────────────────────
# Default: a sample Workday job URL (update with a real one for testing)
JOB_URL="${1:-}"
shift 2>/dev/null || true

if [ -z "$JOB_URL" ]; then
  echo "══════════════════════════════════════════════════════════"
  echo "  Workday Test — No URL provided"
  echo "══════════════════════════════════════════════════════════"
  echo
  echo "  Usage: ./examples/workday_test.sh <workday-job-url>"
  echo
  echo "  Example URLs:"
  echo "    https://starbucks.wd1.myworkdayjobs.com/en-US/StarbucksExternal/details/..."
  echo "    https://tesla.wd5.myworkdayjobs.com/Tesla_External/..."
  echo "    https://company.wd3.myworkdayjobs.com/External/job/Location/Title_ID"
  echo
  echo "  For account creation, also pass credentials:"
  echo "    ./examples/workday_test.sh <url> --email test@email.com --password Pass123!"
  echo
  exit 1
fi

# ── Credentials ───────────────────────────────────────────────
EMAIL_ARG=""
PASS_ARG=""

# Check environment variables first
if [ -n "${WORKDAY_TEST_EMAIL:-}" ]; then
  EMAIL_ARG="--email $WORKDAY_TEST_EMAIL"
fi
if [ -n "${WORKDAY_TEST_PASSWORD:-}" ]; then
  PASS_ARG="--password $WORKDAY_TEST_PASSWORD"
fi

echo "══════════════════════════════════════════════════════════"
echo "  Hand-X — Workday Application Test"
echo "══════════════════════════════════════════════════════════"
echo "  URL:         $JOB_URL"
echo "  Resume:      $RESUME"
echo "  Test Data:   $DATA"
echo "  Credentials: ${WORKDAY_TEST_EMAIL:+$WORKDAY_TEST_EMAIL}${WORKDAY_TEST_EMAIL:-not set (sign-in will be skipped)}"
echo "══════════════════════════════════════════════════════════"
echo
echo "  NOTE: Workday requires per-tenant account creation."
echo "  If the agent encounters a login wall without credentials,"
echo "  it will report 'blocker: login required'."
echo
echo "  To test account creation + sign-in:"
echo "    export WORKDAY_TEST_EMAIL='your-test@email.com'"
echo "    export WORKDAY_TEST_PASSWORD='YourTestPassword123!'"
echo
echo "══════════════════════════════════════════════════════════"
echo

exec python examples/apply_to_job.py \
  --job-url "$JOB_URL" \
  --resume "$RESUME" \
  --test-data "$DATA" \
  --max-steps 80 \
  $EMAIL_ARG \
  $PASS_ARG \
  "$@"
