# Toy job application (from GHOST-HANDS)

Static HTML that mimics ATS-style forms (including react-select–style dropdowns). Vendored from [GHOST-HANDS `packages/ghosthands/toy-job-app`](https://github.com/WeKruit/GHOST-HANDS/tree/staging/packages/ghosthands/toy-job-app) for local DomHand / agent testing without hitting production ATS URLs.

## Run locally

From the Hand-X repo root:

```bash
cd examples/toy-job-app
python -m http.server 8765
```

In another terminal:

```bash
./apply.sh "http://127.0.0.1:8765/" --max-steps 15
```

The job URL’s host (`127.0.0.1`) is included in the session allowlist automatically. Use `--headless` for CI-style runs.

## Email verification harness

The static app expects local auth endpoints for the Google-login verification
gate. To test the deterministic fake-inbox recovery harness without real Gmail
or a live ATS, run:

```bash
uv run pytest tests/ci/test_email_verification_toy_harness.py -q
```

That test serves this HTML fixture with mocked `/api/auth/google/*` endpoints,
uses `FakeInboxClient` to provide the verification code, fills the code through
the email-verification browser helper, and asserts the application unlocks.

To refresh the fixture from upstream:

```bash
curl -fsSL "https://raw.githubusercontent.com/WeKruit/GHOST-HANDS/staging/packages/ghosthands/toy-job-app/index.html" -o index.html
```
