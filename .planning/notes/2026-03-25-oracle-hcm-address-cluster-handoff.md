# Handoff: Oracle HCM address cluster (DomHand) + toy baseline

## Context

**Symptom:** On Goldman Sachs / Oracle Candidate Experience (`higher.gs.com` → `hdpc.fa.us2.oraclecloud.com`), Home Address fails under `domhand_fill`: Address Line 1, ZIP, City, County show as `field_key` like `select|…` in logs; validation stays red; user screenshot matches autocomplete + dependent comboboxes.

**Why it's not "Greenhouse broken":** Oracle uses `role="combobox"` / Fusion patterns; DomHand classifies those as `select` and routes `_fill_select_field_outcome` → `_fill_custom_dropdown_outcome`. Address Line 1 is really `type → async suggestions (often gridcell) → commit → backfill dependents`; generic dropdown timing/react-select assumptions don't always match live Oracle.

**Internal note:** `.planning/notes/2026-03-25-gs-oracle-address-combobox-regression.md` describes root cause and missing regression coverage (address cluster vs existing Oracle step 2/3 fixtures).

## Done in repo

### Toy: `examples/toy-oracle-address/index.html` + `examples/toy-oracle-address/README.md`

- Address Line 1 = combobox; suggestions in `role="grid"` / `role="gridcell"`; ZIP/City/County fill only after a row is committed.

### CI: `tests/ci/test_toy_oracle_address_fixture.py`

- Manual JS flow test + `_fill_select_field_outcome` on "Address Line 1" asserting ZIP/city backfill.
- Run: `uv run pytest tests/ci/test_toy_oracle_address_fixture.py -v` (needs Chromium available to BrowserSession; `uv run playwright install chromium` if needed).

## What's next (suggested)

1. **Reproduce live failure** with logging around `_fill_custom_dropdown_outcome` / `fill_interactive_dropdown` on Oracle only (host or wrapper class), or extend toy to match any missing behavior (e.g. `isTrusted`, shadow DOM, slower suggestion paint).

2. **If toy passes but live fails:** narrow diff (focus, overlay, gridcell visibility, verify step, dependent fields filled async after commit).

3. **Implement minimal Oracle-address fix** (label/role heuristics or host-gated path) and add regression tests against the toy + optional extension to `tests/fixtures/domhand_dropdown_control_lab.html` if you want one lab HTML for everything.

## Key files

| File | Role |
|------|------|
| `ghosthands/dom/field_extractor.py` | `role=combobox` → select classification |
| `ghosthands/dom/fill_executor.py` | `_fill_select_field_outcome`, `_fill_custom_dropdown_outcome`, `_fill_searchable_dropdown` / text path |
| `ghosthands/dom/dropdown_fill.py` | `fill_interactive_dropdown` |
| `tests/fixtures/domhand_dropdown_control_lab.html` | Existing Oracle-ish lab |
| `tests/ci/test_domhand_lab_fixture.py` | Lab fixture tests |
| `examples/toy-oracle-address/` | Oracle address auto-suggest fixture |
| `tests/ci/test_toy_oracle_address_fixture.py` | Address fixture CI test |

## Validation

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/ci/test_toy_oracle_address_fixture.py -v
```
