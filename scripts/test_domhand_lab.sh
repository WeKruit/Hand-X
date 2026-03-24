#!/usr/bin/env bash
# Run DomHand dropdown/control lab smoke tests (fixture HTML + Playwright + extract_visible_form_fields).
#
# Usage (paste the command only — do not include comment text after #, or use a normal ASCII space before #):
#   ./scripts/test_domhand_lab.sh
#   ./scripts/test_domhand_lab.sh --install-browsers
#   ./scripts/test_domhand_lab.sh -q
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ "${1:-}" == "--install-browsers" ]]; then
	shift
	echo "==> Installing Playwright Chromium (one-time / after upgrades)..."
	uv run playwright install chromium
	echo "==> Done."
fi

# If copy-paste used a weird Unicode space before "#", the shell may pass "#" and
# trailing words as args; pytest then errors: file or directory not found: #
pytest_args=()
for _arg in "$@"; do
	if [[ "$_arg" == "#" ]]; then
		break
	fi
	pytest_args+=("$_arg")
done

echo "==> pytest tests/ci/test_domhand_lab_fixture.py"
if ((${#pytest_args[@]} > 0)); then
	uv run pytest tests/ci/test_domhand_lab_fixture.py -v "${pytest_args[@]}"
else
	uv run pytest tests/ci/test_domhand_lab_fixture.py -v
fi
