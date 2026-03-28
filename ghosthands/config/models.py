"""LLM model catalog — pricing, context limits, and provider mapping."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelConfig:
    """Configuration for a single LLM model."""

    model_id: str
    provider: str  # "anthropic" | "openai" | "google"
    input_cost_per_1k: float  # $ per 1K input tokens
    output_cost_per_1k: float  # $ per 1K output tokens
    max_context: int  # max context window in tokens


MODEL_CATALOG: dict[str, ModelConfig] = {
    # ── Anthropic ──────────────────────────────────────────────────
    "claude-sonnet-4-20250514": ModelConfig(
        model_id="claude-sonnet-4-20250514",
        provider="anthropic",
        input_cost_per_1k=0.003,
        output_cost_per_1k=0.015,
        max_context=200_000,
    ),
    "claude-haiku-4-5-20251001": ModelConfig(
        model_id="claude-haiku-4-5-20251001",
        provider="anthropic",
        input_cost_per_1k=0.001,
        output_cost_per_1k=0.005,
        max_context=200_000,
    ),
    # ── OpenAI (current gen) ──────────────────────────────────────
    "gpt-5.4": ModelConfig(
        model_id="gpt-5.4",
        provider="openai",
        input_cost_per_1k=0.0025,
        output_cost_per_1k=0.015,
        max_context=1_050_000,
    ),
    "gpt-5.4-mini": ModelConfig(
        model_id="gpt-5.4-mini",
        provider="openai",
        input_cost_per_1k=0.00075,
        output_cost_per_1k=0.0045,
        max_context=1_050_000,
    ),
    "gpt-5.4-nano": ModelConfig(
        model_id="gpt-5.4-nano",
        provider="openai",
        input_cost_per_1k=0.0002,
        output_cost_per_1k=0.00125,
        max_context=1_050_000,
    ),
    # ── OpenAI (legacy, still in use) ─────────────────────────────
    "gpt-4.1": ModelConfig(
        model_id="gpt-4.1",
        provider="openai",
        input_cost_per_1k=0.002,
        output_cost_per_1k=0.008,
        max_context=1_047_576,
    ),
    "gpt-4.1-mini": ModelConfig(
        model_id="gpt-4.1-mini",
        provider="openai",
        input_cost_per_1k=0.0004,
        output_cost_per_1k=0.0016,
        max_context=1_047_576,
    ),
    "gpt-4.1-nano": ModelConfig(
        model_id="gpt-4.1-nano",
        provider="openai",
        input_cost_per_1k=0.0001,
        output_cost_per_1k=0.0004,
        max_context=1_047_576,
    ),
    # ── Google ────────────────────────────────────────────────────
    "gemini-3-flash-preview": ModelConfig(
        model_id="gemini-3-flash-preview",
        provider="google",
        input_cost_per_1k=0.0003,
        output_cost_per_1k=0.0025,
        max_context=1_048_576,
    ),
    "gemini-3.1-flash-lite-preview": ModelConfig(
        model_id="gemini-3.1-flash-lite-preview",
        provider="google",
        input_cost_per_1k=0.000075,
        output_cost_per_1k=0.0003,
        max_context=1_048_576,
    ),
}


def get_model(model_id: str) -> ModelConfig:
    """Look up a model by ID. Raises KeyError if not found."""
    if model_id not in MODEL_CATALOG:
        available = ", ".join(MODEL_CATALOG.keys())
        raise KeyError(f"Unknown model '{model_id}'. Available: {available}")
    return MODEL_CATALOG[model_id]


def estimate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate LLM cost in dollars for a given token count.

    For unknown models, falls back to a conservative estimate using
    gemini-3.1-flash-lite-preview pricing rather than raising.
    """
    try:
        model = get_model(model_id)
    except KeyError:
        # Unknown model — use cheap fallback pricing so cost tracking
        # still works instead of logging a warning every LLM call.
        fallback = MODEL_CATALOG.get("gemini-3.1-flash-lite-preview")
        if fallback:
            return (input_tokens / 1000 * fallback.input_cost_per_1k) + (
                output_tokens / 1000 * fallback.output_cost_per_1k
            )
        return 0.0
    return (input_tokens / 1000 * model.input_cost_per_1k) + (output_tokens / 1000 * model.output_cost_per_1k)
