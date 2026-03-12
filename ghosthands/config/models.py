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
    # Anthropic
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
        input_cost_per_1k=0.0008,
        output_cost_per_1k=0.004,
        max_context=200_000,
    ),
    # OpenAI
    "gpt-4o": ModelConfig(
        model_id="gpt-4o",
        provider="openai",
        input_cost_per_1k=0.0025,
        output_cost_per_1k=0.01,
        max_context=128_000,
    ),
    "gpt-4o-mini": ModelConfig(
        model_id="gpt-4o-mini",
        provider="openai",
        input_cost_per_1k=0.00015,
        output_cost_per_1k=0.0006,
        max_context=128_000,
    ),
    # Google
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
    """Estimate LLM cost in dollars for a given token count."""
    model = get_model(model_id)
    return (input_tokens / 1000 * model.input_cost_per_1k) + (output_tokens / 1000 * model.output_cost_per_1k)
