# LLM cost accounting (Hand-X)

## Token counts

- **Planner (browser-use):** `agent.history.usage` totals come from the library’s cost service when `calculate_cost=True` (LiteLLM pricing), driven by **provider-reported** usage on the main agent LLM calls.
- **DomHand batch LLM** (`domhand_fill` → `_generate_answers`): reads `response.usage.prompt_tokens` / `completion_tokens` from the same `get_chat_model().ainvoke()` wrapper. If the response has **no** `usage`, DomHand records **0** tokens for that call (cost for that call is then **0** in metadata).

`ActionResult.metadata` on `domhand_fill` includes `input_tokens`, `output_tokens`, and `step_cost` **aggregated per tool invocation** (multiple LLM rounds in one fill add up).

## What the UI / `cost_usd` number is

Per step, [`StepHooks`](../ghosthands/agent/hooks.py) sets:

`cumulative ≈ browser_use.history.usage.total_cost + Σ(ActionResult.metadata["step_cost"])`

- **Planner + other browser-use–tracked models** use browser-use `TokenCost` with **LiteLLM** cached pricing.
- **DomHand batch LLM** inside `domhand_fill` uses **`ghosthands.config.models.estimate_cost`** and the local **`MODEL_CATALOG`**.

Those two catalogs can disagree slightly for the same model id.

## Proxy / desktop (`GH_LLM_PROXY_URL`)

The proxy only changes the HTTP endpoint. Token counts should still come from provider responses. If the proxy strips `usage` / `usage_metadata`, costs will under-report.

## What this is not

- Not a match for VALET invoices or negotiated rates.
- Not a substitute for provider dashboards when debugging spend.
