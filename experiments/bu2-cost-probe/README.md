# bu-2-0 cost probe (vanilla browser-use)

A standalone experiment to measure the **real per-job LLM cost** of applying to a
single Greenhouse posting with browser-use's own `bu-2-0` model — using **fresh,
latest, upstream browser-use** with **none** of Hand-X's additions (no DomHand,
no platform guardrails, no custom system prompt, no VALET proxy).

`run_cost_probe.py` imports only `browser_use`. It is intentionally decoupled
from the `ghosthands` package so the cost it reports is "vanilla browser-use +
the model", nothing else.

## Setup (fresh venv, latest browser-use)

```bash
cd experiments/bu2-cost-probe
uv venv --python 3.12 .venv-bu2
source .venv-bu2/bin/activate
uv pip install -U browser-use          # latest upstream, NOT the vendored fork
playwright install chromium

# bu-2-0 routes to browser-use cloud and REQUIRES a key:
export BROWSER_USE_API_KEY="..."       # https://cloud.browser-use.com/new-api-key
```

## Run

```bash
# Fill only — stops before the final Submit button (safe default):
python run_cost_probe.py --job-url "https://job-boards.greenhouse.io/<org>/jobs/<id>"

# Head-to-head against our current default (needs GOOGLE_API_KEY instead):
python run_cost_probe.py --job-url "..." --model gemini-3-flash-preview

# Actually submit — IRREVERSIBLE, sends a real application to the employer:
python run_cost_probe.py --job-url "..." --submit
```

At the end it prints `history.usage`:

```
TOTAL COST:        $0.xxxx
prompt tokens:     ...  ($...)
cached tokens:     ...  ($...)
completion tokens: ...  ($...)
```

Cost is computed by browser-use's own `calculate_cost=True`, which prices
`bu-2-0` from `browser_use/tokens/custom_pricing.py`
($0.60 / $3.50 / $0.06 per 1M input / output / cache-read).

## Important: this can't run from the Claude-Code-on-the-web container

The remote session environment that generated this file **cannot execute the
probe**, for two independent reasons:

1. **No `BROWSER_USE_API_KEY`** is present, and `ChatBrowserUse` raises without one.
2. The environment's **network policy denies** outbound CONNECT to both
   `llm.api.browser-use.com` (bu-2-0's endpoint) and `boards.greenhouse.io`
   (the target site) — both return `403` at the agent proxy.

Run it on a machine (or an environment whose network policy allowlists
`llm.api.browser-use.com` and `*.greenhouse.io`) where you hold a
`BROWSER_USE_API_KEY`.

## Safety

- Default is **fill-only**: the agent stops before the final Submit button, so no
  real application is sent. Use this for cost measurement.
- `--submit` sends a **real application to a real employer** — only use it on a
  job you genuinely intend to apply to, or on a sandbox/test posting.
- `sample_profile.json` is fictitious test data. Replace it with a real profile
  (and pass `--resume`) only when you actually intend to submit.

## Expected cost (rate-card projection, pending a real run)

For a typical ~40-step Greenhouse fill with caching on the stable prefix:

| Model | Projected per job |
|-------|-------------------|
| `bu-2-0` | ~$0.50 |
| `gemini-3-flash-preview` (current default) | ~$0.13 |

bu-2-0's per-token rates are ~4× input / ~5.8× output, so it only becomes
cheaper if it finishes the job in dramatically fewer steps. That step-count
question is exactly what running both models through this probe answers.
