# jobapply-core — AI job-application submitter (glue over browser-use)

A standalone, **reuse-first** submitter built on **upstream browser-use**. It owns
almost no logic: it wires together features browser-use already ships and adds
only (a) one reusable instruction string and (b) a small profile→variables mapper
for reruns. No custom replay engine, no custom cost/cache code, no verification
stack.

## What it reuses (and why that saves cost)

| Capability | browser-use feature reused |
|---|---|
| Fill the form (incl. multi-page wizards) | `Agent(...)` — `save_history`/`load_and_rerun` record & replay the **whole** trajectory |
| Read the email verification code | `integrations.gmail.register_gmail_actions` → the agent calls `get_recent_emails` itself |
| "Script cache" / cheap re-submit | `Agent.save_history()` + `load_and_rerun(variables=…)` — deterministic steps replay with **no LLM** |
| Input-token caching | **native** — browser-use prices `cache_read` per model; nothing to build |
| Cost reporting | `Agent(calculate_cost=True)` → `history.usage` |

The cost win: a **record** run pays full per-step LLM (~$0.5 on `bu-2-0`, ~$0.13 on
`gemini-3-flash-preview`). A **replay** of the same job with new data re-fills
deterministically for **$0 LLM** — the only cost is the live email-read step (if
there's a verification wall) plus the one mandatory rerun summary call, on top of
browser-use's native prompt caching.

## Setup (fresh venv, latest browser-use)

```bash
cd experiments/jobapply-core
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -U browser-use
playwright install chromium

export BROWSER_USE_API_KEY="..."     # for bu-2-0 (https://cloud.browser-use.com/new-api-key)
# Gmail read-only OAuth: pass --gmail-credentials/--gmail-token or --gmail-access-token,
# or drop gmail_credentials.json into the browser-use config dir.
```

## Use

```bash
# 1) Record a successful application (fill only — stops before final submit):
python jobapply.py record --job-url "https://job-boards.greenhouse.io/<org>/jobs/<id>" \
    --resume ~/resume.pdf --history jobA.json

# Compare models on the same job:
python jobapply.py record --job-url "..." --model gemini-3-flash-preview --history jobA_gemini.json

# Actually submit (IRREVERSIBLE — real application to the employer):
python jobapply.py record --job-url "..." --submit

# 2) Cheap re-submit of the SAME job with a different applicant:
python jobapply.py replay --history jobA.json --profile other_applicant.json
```

`record` writes `jobA.json` (the cached trajectory) and `jobA.vars.json` (the
fields browser-use auto-detected as substitutable). `replay` maps the new
profile onto those fields and re-runs deterministically, then prints the cost.

## Verification code (multi-page friendly)

The recorded flow includes the `get_recent_emails` action wherever the agent hit
the email-verification wall. On replay, browser-use re-evaluates that step live,
so a **fresh** code is fetched each time — the stale recorded code is never reused.
The applicant email must match the connected Gmail inbox (`gmail.readonly`).

## Tests

```bash
python tests/test_jobapply.py     # offline: instructions + profile-mapping, no deps
# or: pytest tests/
```

## Environment caveat

This cannot run e2e from the Claude-Code-on-the-web container: the network policy
denies `llm.api.browser-use.com` and `*.greenhouse.io` (403), and there's no
`BROWSER_USE_API_KEY`. The offline tests run anywhere. Run e2e on a machine with a
key + normal internet, or relaunch the web environment with a policy that
allowlists those hosts.

## Optional v2 (only if measurements justify it)

If the built-in `get_recent_emails` AI step proves too costly across many reruns,
port the deterministic no-LLM resolver from the `feat/gmail-email-verification`
branch (`page_state` + `selection` + `browser_helpers`) behind the same CLI. Decide
with real numbers — don't pre-build.
