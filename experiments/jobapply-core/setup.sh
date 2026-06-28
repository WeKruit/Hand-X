#!/usr/bin/env bash
# One-shot local bootstrap for jobapply-core.
# Creates a fresh venv with the LATEST upstream browser-use (not the repo's
# vendored fork), installs Chromium, and copies the env template.
#
#   cd experiments/jobapply-core && ./setup.sh && source .venv/bin/activate
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3.12}"
command -v "$PY" >/dev/null 2>&1 || PY=python3

if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.12 .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  uv pip install -U browser-use python-dotenv
else
  "$PY" -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install --upgrade pip
  pip install -U browser-use python-dotenv
fi

python -m playwright install chromium

[ -f .env ] || cp .env.example .env

echo
echo "✓ Ready. Next:"
echo "    source experiments/jobapply-core/.venv/bin/activate"
echo "    edit experiments/jobapply-core/.env   (add BROWSER_USE_API_KEY, GOOGLE_API_KEY, Gmail creds)"
echo "    python jobapply.py compare --job-url \"https://job-boards.greenhouse.io/<org>/jobs/<id>\" --resume ~/resume.pdf"
