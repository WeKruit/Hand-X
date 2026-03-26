# Toy — Oracle HCM address cluster

Static HTML that mimics **Goldman / Oracle Candidate Experience**–style **Home Address**:

- **Address Line 1** is `role="combobox"` (not classified as a plain `textbox`).
- Suggestions appear as **`role="grid"` / `role="gridcell"`** rows after typing (async debounce), similar to live HCM.
- **ZIP / City / County** stay empty and show required errors until a **suggestion row is committed** via a trusted click; then they backfill.

This exists so we can reproduce and lock **DomHand** behavior (`domhand_fill` → `_fill_select_field_outcome` → `fill_interactive_dropdown`) without hitting production ATS URLs.

## Run locally

From the Hand-X repo root:

```bash
cd examples/toy-oracle-address
python -m http.server 8766
```

In another terminal:

```bash
./apply.sh "http://127.0.0.1:8766/" --max-steps 25
```

The host `127.0.0.1` is allowlisted for local runs. Use `--headless` for CI-style runs.

## Automated tests

```bash
uv run pytest tests/ci/test_toy_oracle_address_fixture.py -v
```

Requires Chromium once: `uv run playwright install chromium`.
