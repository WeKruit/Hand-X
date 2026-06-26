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

## Local setup (one command)

On your local machine (not the web container — see caveat below):

```bash
cd experiments/jobapply-core
./setup.sh                       # fresh venv + latest browser-use + Chromium + .env
source .venv/bin/activate
$EDITOR .env                     # add BROWSER_USE_API_KEY (+ GOOGLE_API_KEY for gemini)
```

`.env` is auto-loaded. For Gmail verification codes, either drop a
`gmail_credentials.json` into the browser-use config dir (first run does the OAuth
consent) or pass `--gmail-credentials/--gmail-token`.

## Use

```bash
# Side-by-side cost: same job through bu-2-0 AND gemini (fill-only, never submits):
python jobapply.py compare --job-url "https://job-boards.greenhouse.io/<org>/jobs/<id>" --resume ~/resume.pdf

# Record one model's trajectory (fill only — stops before final submit):
python jobapply.py record --job-url "..." --resume ~/resume.pdf --history jobA.json

# Actually submit (IRREVERSIBLE — real application to the employer):
python jobapply.py record --job-url "..." --submit

# Cheap re-submit of the SAME job with a different applicant:
python jobapply.py replay --history jobA.json --profile other_applicant.json
```

`record`/`compare` write `*.json` (the cached trajectory) and `*.vars.json` (the
fields browser-use auto-detected as substitutable). `replay` maps a new profile
onto those fields and re-runs deterministically, then prints the cost.

> **Substitution scope:** for job-application flows browser-use detects
> substitutable fields by element attributes (name/id/placeholder/aria-label) and
> deliberately disables value-pattern guessing (to avoid over-substituting). So a
> field only gets the new applicant's value if its input is labeled; unlabeled
> fields replay the recorded value. Either way the **replay cost** measurement is
> valid — re-record per applicant if you need every field swapped.

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
