# Toy Workday Fixture

This fixture is a post-auth Workday-style application flow for local Hand-X testing.

It is designed to preserve the existing Workday-specific Hand-X paths by serving the page on a loopback host whose URL still contains `myworkdayjobs.com`.

## What It Covers

- Multi-step post-auth application shell
- Prompt-search widgets
- Nested referral source tree selection
- Conditional reveal flows
- Work Experience and Education repeaters
- Skills multi-select behavior
- Segmented date inputs
- Voluntary disclosures
- Self identify
- Review step

## Serve Locally

From the repo root:

```bash
uv run python -m http.server 8768 --bind 127.0.0.1 --directory examples/toy-workday
```

Then use this URL in the browser:

```text
http://company.myworkdayjobs.com.lvh.me:8768/index.html
```

`lvh.me` resolves to `127.0.0.1`, so Hand-X still sees a Workday-looking hostname while everything stays local.

## Run Hand-X Against It

CLI entrypoint:

```bash
uv run python -m ghosthands.cli \
  --job-url "http://company.myworkdayjobs.com.lvh.me:8768/index.html" \
  --test-data examples/apply_to_job_sample_data.json \
  --resume examples/resume.pdf \
  --submit-intent review \
  --output-format human
```

Example script entrypoint:

```bash
uv run python examples/apply_to_job.py \
  --job-url "http://company.myworkdayjobs.com.lvh.me:8768/index.html" \
  --test-data examples/apply_to_job_sample_data.json \
  --resume examples/resume.pdf
```

## Verification

The toy fixture is covered by:

```bash
uv run pytest tests/ci/test_toy_workday_fixture.py -q
uv run pytest tests/ci/test_toy_repeater_prefill.py -q
```
