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
# By default, this script auto-generates a simple +lastname alias:
#   happy@ucla.edu -> happy+smith@ucla.edu
# Pass --reuse-email if you want to reuse the exact email instead.
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
if [ -z "${GH_LLM_PROXY_URL:-}" ] && [ -z "${GOOGLE_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${GH_ANTHROPIC_API_KEY:-}" ]; then
  echo "ERROR: Set one of GH_LLM_PROXY_URL, GOOGLE_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY"
  exit 1
fi
if [ -z "${GOOGLE_API_KEY:-}" ] && [ -z "${GH_LLM_PROXY_URL:-}" ]; then
  echo "WARNING: GOOGLE_API_KEY not set — default Gemini Flash Lite model will need a different provider override"
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
INLINE_EMAIL=""
INLINE_PASSWORD=""
REUSE_EMAIL=0
PASSTHROUGH_ARGS=()
MODEL_SPECIFIED=0

make_fresh_workday_email() {
  local base_email="$1"
  local applicant_last_name="$2"
  if [[ "$base_email" != *"@"* ]]; then
    echo "$base_email"
    return
  fi

  local local_part="${base_email%@*}"
  local domain_part="${base_email#*@}"
  local clean_local="${local_part%%+*}"
  local clean_last_name

  clean_last_name="$(printf '%s' "$applicant_last_name" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')"
  if [ -z "$clean_last_name" ]; then
    echo "$base_email"
    return
  fi

  echo "${clean_local}+${clean_last_name}@${domain_part}"
}

get_applicant_last_name() {
  python - "$DATA" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text())
except Exception:
    print("")
    raise SystemExit(0)

last_name = str(data.get("last_name") or "").strip()
print(last_name)
PY
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --email)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --email requires a value"
        exit 1
      fi
      INLINE_EMAIL="$2"
      shift 2
      ;;
    --password)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --password requires a value"
        exit 1
      fi
      INLINE_PASSWORD="$2"
      shift 2
      ;;
    --reuse-email)
      REUSE_EMAIL=1
      shift
      ;;
    *)
      if [ "$1" = "--model" ]; then
        MODEL_SPECIFIED=1
      fi
      PASSTHROUGH_ARGS+=("$1")
      shift
      ;;
  esac
done

BASE_EMAIL="${INLINE_EMAIL:-${WORKDAY_TEST_EMAIL:-}}"
EFFECTIVE_PASSWORD="${INLINE_PASSWORD:-${WORKDAY_TEST_PASSWORD:-}}"
EFFECTIVE_EMAIL="$BASE_EMAIL"
APPLICANT_LAST_NAME="$(get_applicant_last_name)"

if [ -n "$BASE_EMAIL" ] && [ "$REUSE_EMAIL" -ne 1 ]; then
  EFFECTIVE_EMAIL="$(make_fresh_workday_email "$BASE_EMAIL" "$APPLICANT_LAST_NAME")"
fi

AUTH_ARGS=()

if [ -n "$EFFECTIVE_EMAIL" ]; then
  AUTH_ARGS+=(--email "$EFFECTIVE_EMAIL")
fi
if [ -n "$EFFECTIVE_PASSWORD" ]; then
  AUTH_ARGS+=(--password "$EFFECTIVE_PASSWORD")
fi

if [ -n "$EFFECTIVE_EMAIL" ]; then
  CREDENTIAL_STATUS="$EFFECTIVE_EMAIL"
  if [ -n "$EFFECTIVE_PASSWORD" ]; then
    CREDENTIAL_STATUS="$CREDENTIAL_STATUS (password set)"
  else
    CREDENTIAL_STATUS="$CREDENTIAL_STATUS (password missing)"
  fi
else
  CREDENTIAL_STATUS="not set (sign-in will be skipped)"
fi

echo "══════════════════════════════════════════════════════════"
echo "  Hand-X — Workday Application Test"
echo "══════════════════════════════════════════════════════════"
echo "  URL:         $JOB_URL"
echo "  Resume:      $RESUME"
echo "  Test Data:   $DATA"
if [ -n "$BASE_EMAIL" ] && [ "$BASE_EMAIL" != "$EFFECTIVE_EMAIL" ]; then
  echo "  Base Email:  $BASE_EMAIL"
fi
echo "  Credentials: $CREDENTIAL_STATUS"
echo "══════════════════════════════════════════════════════════"
echo
echo "  NOTE: Workday requires per-tenant account creation."
echo "  This runner uses a simple +lastname alias by default so the"
echo "  Workday account email still looks normal during testing."
echo "  Pass --reuse-email if you intentionally want to sign in with the exact"
echo "  email you supplied."
echo
echo "  If the agent encounters a login wall without credentials,"
echo "  it will report 'blocker: login required'."
echo
echo "  To test account creation + sign-in:"
echo "    export WORKDAY_TEST_EMAIL='your-test@email.com'"
echo "    export WORKDAY_TEST_PASSWORD='YourTestPassword123!'"
echo
echo "══════════════════════════════════════════════════════════"
echo

CMD=(
  python examples/apply_to_job.py
  --job-url "$JOB_URL"
  --resume "$RESUME"
  --test-data "$DATA"
  --max-steps 80
)

if [ "$MODEL_SPECIFIED" -ne 1 ]; then
  CMD+=(--model "gemini-3-flash-preview")
fi

if [ "${#AUTH_ARGS[@]}" -gt 0 ]; then
  CMD+=("${AUTH_ARGS[@]}")
fi

if [ "${#PASSTHROUGH_ARGS[@]}" -gt 0 ]; then
  CMD+=("${PASSTHROUGH_ARGS[@]}")
fi

exec "${CMD[@]}"
