#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Hand-X — Apply to a job
#
# Usage:
#   ./apply.sh                                           # default Greenhouse job
#   ./apply.sh "https://company.wd5.myworkdayjobs.com/..." # any job URL
#   ./apply.sh "https://..." --max-steps 10              # quick test
#   ./apply.sh "https://..." --headless                  # no visible browser
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

# ── Check API keys ────────────────────────────────────────────
if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${GH_ANTHROPIC_API_KEY:-}" ]; then
  echo "ERROR: Set ANTHROPIC_API_KEY in .env or environment (needed for DomHand)"
  exit 1
fi
if [ -z "${GOOGLE_API_KEY:-}" ]; then
  echo "WARNING: GOOGLE_API_KEY not set — agent model (Gemini) won't work without it"
fi

# ── Parse args ────────────────────────────────────────────────
# JOB_URL="${1:-https://job-boards.greenhouse.io/starburst/jobs/5123053008}"
JOB_URL="${1:-https://jobs.smartrecruiters.com/oneclick-ui/company/Laxir1/publication/79d18a58-32ce-485c-80b4-f04058efa20b?dcr_ci=Laxir1}"
shift 2>/dev/null || true

echo "══════════════════════════════════════════════════════════"
echo "  Hand-X — Job Application"
echo "══════════════════════════════════════════════════════════"
echo "  URL:    $JOB_URL"
echo "  Resume: $RESUME"
echo "  Data:   $DATA"
echo "══════════════════════════════════════════════════════════"
echo

exec python examples/apply_to_job.py \
  --job-url "$JOB_URL" \
  --resume "$RESUME" \
  --test-data "$DATA" \
  "$@"
