#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Hand-X — Apply to a job (test harness)
#
# Uses the real CLI (ghosthands.cli) with --output-format human,
# matching the same code path the Desktop app uses.
#
# Usage:
#   ./apply.sh                                           # default Greenhouse job
#   ./apply.sh --max-steps 5                             # default URL + quick smoke test
#   ./apply.sh "https://company.wd5.myworkdayjobs.com/..." # any job URL
#   ./apply.sh "https://..." --max-steps 10              # quick test
#   ./apply.sh "https://..." --user-id <uuid> --resume-id <uuid>   # load real VALET profile
#   ./apply.sh "http://127.0.0.1:8765/" --max-steps 15   # local toy-job-app (see examples/toy-job-app/README.md)
#   ./apply.sh "https://..." --headless                  # no visible browser


# Existing-account auth override (local only; does NOT change applicant profile email):
# GH_EMAIL='existing-account@example.com' \
# GH_PASSWORD='testAbc123!' \
# GH_CREDENTIAL_SOURCE='user' \
# GH_CREDENTIAL_INTENT='existing_account' \
# ./apply.sh "https://higher.gs.com/roles/162133"
#
# Create-account auth override (local only; does NOT change applicant profile email):
# GH_EMAIL='abc12@nyu.edu' \
# GH_PASSWORD='testAbc123!' \
# GH_CREDENTIAL_SOURCE='user' \
# GH_CREDENTIAL_INTENT='create_account' \
# ./apply.sh "https://higher.gs.com/roles/162133"
# ─────────────────────────────────────────────────────────────
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Test data ─────────────────────────────────────────────────
RESUME="$DIR/examples/resume.pdf"
DATA="$DIR/examples/apply_to_job_sample_data.json"

# ── Activate venv ─────────────────────────────────────────────
if [ ! -d "$DIR/.venv" ]; then
  echo "ERROR: .venv not found. Run: uv venv --python 3.12 && uv pip install -e '.[dev]'"
  exit 1
fi
source "$DIR/.venv/bin/activate"

# ── Load .env ─────────────────────────────────────────────────
[ -f "$DIR/.env" ] && set -a && source "$DIR/.env" && set +a

_generate_fresh_create_account_email() {
  local base_email="$1"
  local default_domain="nyu.edu"
  local domain="${base_email#*@}"
  if [[ -z "$base_email" || "$domain" == "$base_email" || -z "$domain" ]]; then
    domain="$default_domain"
  fi

  local local_part=""
  while [[ ${#local_part} -lt 8 ]]; do
    local_part+=$(LC_ALL=C tr -dc 'a-z' </dev/urandom | head -c 8)
  done
  local_part="${local_part:0:8}"
  printf '%s@%s' "$local_part" "$domain"
}

if [[ "${GH_CREDENTIAL_SOURCE:-}" == "user" && "${GH_CREDENTIAL_INTENT:-}" == "create_account" ]]; then
  FRESH_CREATE_ACCOUNT_EMAIL="$(_generate_fresh_create_account_email "${GH_EMAIL:-}")"
  export GH_EMAIL="$FRESH_CREATE_ACCOUNT_EMAIL"
fi

GH_SUBMIT_INTENT="${GH_SUBMIT_INTENT:-review}"
export GH_SUBMIT_INTENT

# ── Stagehand (optional tools): needs MODEL_API_KEY; Browserbase optional ─
# Prefer Anthropic when unset (same as DomHand / Haiku). Override with explicit MODEL_API_KEY=...
if [ -z "${MODEL_API_KEY:-}" ]; then
  if [ -n "${ANTHROPIC_API_KEY:-}" ]; then export MODEL_API_KEY="$ANTHROPIC_API_KEY"
  elif [ -n "${GH_ANTHROPIC_API_KEY:-}" ]; then export MODEL_API_KEY="$GH_ANTHROPIC_API_KEY"
  elif [ -n "${OPENAI_API_KEY:-}" ]; then export MODEL_API_KEY="$OPENAI_API_KEY"
  elif [ -n "${GH_OPENAI_API_KEY:-}" ]; then export MODEL_API_KEY="$GH_OPENAI_API_KEY"
  elif [ -n "${GOOGLE_API_KEY:-}" ]; then export MODEL_API_KEY="$GOOGLE_API_KEY"
  fi
fi
if [ -z "${MODEL_API_KEY:-}" ]; then
  echo "WARNING: MODEL_API_KEY not set — Stagehand tools will not start. Set MODEL_API_KEY or one of OPENAI/ANTHROPIC/GOOGLE. To disable: export GH_STAGEHAND_DISABLE=1"
fi
# Remote Stagehand cloud API requires BROWSERBASE_API_KEY; without it, Hand-X uses local Stagehand (see GH_STAGEHAND_SERVER in .env.example).

# ── Check API keys ────────────────────────────────────────────
if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${GH_ANTHROPIC_API_KEY:-}" ]; then
  echo "ERROR: Set ANTHROPIC_API_KEY in .env or environment (needed for DomHand)"
  exit 1
fi
if [ -z "${GOOGLE_API_KEY:-}" ]; then
  echo "WARNING: GOOGLE_API_KEY not set — agent model (Gemini) won't work without it"
fi

# ── Parse args ────────────────────────────────────────────────
# First positional arg is the job URL only when it looks like a URL; otherwise
# default URL is used and flags (e.g. --max-steps 5) can come first.
# DEFAULT_URL="https://job-boards.greenhouse.io/starburst/jobs/5123053008"
DEFAULT_URL="https://job-boards.greenhouse.io/truveta/jobs/5659745004"
JOB_URL="${DEFAULT_URL}"
if [[ $# -gt 0 && ( "${1:-}" == http://* || "${1:-}" == https://* ) ]]; then
  JOB_URL="$1"
  shift
fi

DEFAULT_VALET_USER_ID="0f2050d9-bce8-4c5f-9760-e42b1c4aa7fa"
DEFAULT_VALET_RESUME_ID="f1281a7a-8f9d-4e75-bac9-5c6ae7ddf957"

USER_ID=""
RESUME_ID=""
USER_ID_EXPLICIT=0
RESUME_ID_EXPLICIT=0
PROFILE_SOURCE_EXPLICIT=0
ARGS=("$@")
FORWARDED_ARGS=()
for ((i=0; i<${#ARGS[@]}; i++)); do
  case "${ARGS[$i]}" in
    --user-id)
      USER_ID_EXPLICIT=1
      if (( i + 1 < ${#ARGS[@]} )); then USER_ID="${ARGS[$((i + 1))]}"; fi
      ((i++))
      ;;
    --resume-id)
      RESUME_ID_EXPLICIT=1
      if (( i + 1 < ${#ARGS[@]} )); then RESUME_ID="${ARGS[$((i + 1))]}"; fi
      ((i++))
      ;;
    --profile|--test-data)
      PROFILE_SOURCE_EXPLICIT=1
      FORWARDED_ARGS+=("${ARGS[$i]}")
      if (( i + 1 < ${#ARGS[@]} )); then FORWARDED_ARGS+=("${ARGS[$((i + 1))]}"); fi
      ((i++))
      ;;
    *)
      FORWARDED_ARGS+=("${ARGS[$i]}")
      ;;
  esac
done

if (( USER_ID_EXPLICIT == 0 && RESUME_ID_EXPLICIT == 0 && PROFILE_SOURCE_EXPLICIT == 0 )); then
  USER_ID="$DEFAULT_VALET_USER_ID"
  RESUME_ID="$DEFAULT_VALET_RESUME_ID"
fi

echo "══════════════════════════════════════════════════════════"
echo "  Hand-X — Job Application (CLI human mode)"
echo "══════════════════════════════════════════════════════════"
echo "  URL:    $JOB_URL"
echo "  Resume: $RESUME"
if [[ -n "$USER_ID" ]]; then
  echo "  Profile: VALET user_id=$USER_ID${RESUME_ID:+ resume_id=$RESUME_ID}"
else
  echo "  Data:   $DATA"
fi
if [[ "${GH_CREDENTIAL_SOURCE:-}" == "user" && "${GH_CREDENTIAL_INTENT:-}" == "create_account" ]]; then
  echo "  Auth:   create_account email=${GH_EMAIL:-}"
fi
echo "  Submit: $GH_SUBMIT_INTENT"
echo "══════════════════════════════════════════════════════════"
echo

CLI_ARGS=(
  --job-url "$JOB_URL"
  --resume "$RESUME"
  --output-format human
  --submit-intent "$GH_SUBMIT_INTENT"
)

if [[ -n "$USER_ID" ]]; then
  CLI_ARGS+=(--user-id "$USER_ID")
  if [[ -n "$RESUME_ID" ]]; then
    CLI_ARGS+=(--resume-id "$RESUME_ID")
  fi
elif (( PROFILE_SOURCE_EXPLICIT == 0 )); then
  CLI_ARGS+=(--test-data "$DATA")
fi

if (( ${#FORWARDED_ARGS[@]} > 0 )); then
  CLI_ARGS+=("${FORWARDED_ARGS[@]}")
fi

exec python -m ghosthands.cli "${CLI_ARGS[@]}"
