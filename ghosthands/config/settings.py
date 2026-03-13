"""Pydantic BaseSettings — all environment variables for GhostHands."""

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """GhostHands configuration loaded from environment variables.

    All env vars are prefixed with GH_ (e.g. GH_DATABASE_URL, GH_WORKER_ID).
    The ANTHROPIC_API_KEY env var is also accepted without the GH_ prefix.
    A .env file in the project root is loaded automatically.
    """

    model_config = {"env_prefix": "GH_", "env_file": ".env", "extra": "ignore"}

    # --- Database ---
    database_url: str = Field("", description="Postgres connection string (asyncpg format)")

    # --- LLM ---
    anthropic_api_key: str = Field(
        "",
        alias="ANTHROPIC_API_KEY",
        description="Anthropic API key for agent + Haiku answer gen",
    )
    openai_api_key: str = Field("", description="OpenAI API key (for browser-use if using GPT)")
    google_api_key: str = Field(
        "",
        alias="GOOGLE_API_KEY",
        description="Google API key for Gemini models (agent model)",
    )
    agent_model: str = Field("gemini-3.1-flash-lite-preview", description="Model for agent decisions")
    domhand_model: str = Field(
        "gemini-3.1-flash-lite-preview",
        description="Cheap model for DomHand answer generation",
    )

    # --- LLM Proxy (VALET) ---
    llm_proxy_url: str = Field(
        "",
        description="VALET LLM proxy base URL. When set, all LLM calls route through VALET. "
        "Example: https://api.valet.wekruit.com/api/v1/local-workers/anthropic",
    )
    llm_runtime_grant: str = Field(
        "",
        description="Runtime grant token for VALET managed inference auth",
    )

    # --- Worker ---
    worker_id: str = Field("hand-x-1", description="Worker identity")
    poll_interval_seconds: float = Field(2.0, description="Seconds between job poll cycles")
    max_steps_per_job: int = Field(100, description="Max browser-use steps before aborting a job")
    max_budget_per_job: float = Field(0.50, description="Max LLM spend in $ per job")

    # --- VALET integration ---
    valet_api_url: str = Field("", description="VALET API base URL for callbacks")
    valet_callback_secret: str = Field("", description="Shared secret for callback auth (HMAC)")

    # --- Security ---
    credential_encryption_key: str = Field(
        "",
        description="64 hex chars for AES-256-GCM credential encryption",
    )
    email: str = Field("", description="Login email for ATS (prefer env var over CLI arg)")
    password: str = Field("", description="Login password for ATS (prefer env var over CLI arg)")
    allowed_domains: list[str] = Field(
        default_factory=lambda: [
            "myworkdayjobs.com",
            "myworkday.com",
            "wd5.myworkday.com",
            "greenhouse.io",
            "boards.greenhouse.io",
            "lever.co",
            "jobs.lever.co",
            "smartrecruiters.com",
        ],
        description="Allowed ATS domains for navigation",
    )

    # --- Desktop bridge identifiers ---
    user_id: str = Field("", description="User ID for desktop bridge tracking")
    job_id: str = Field("", description="Job ID for desktop bridge tracking")
    lease_id: str = Field("", description="Lease ID for desktop bridge tracking")

    # --- Browser ---
    headless: bool = Field(True, description="Run browser headless")
    browser_timeout: int = Field(30_000, description="Browser operation timeout in ms")
    wait_between_actions: float = Field(
        1.8,
        description="Seconds to wait between actions within the same agent step",
    )
    agent_max_actions_per_step: int = Field(
        1,
        description="Maximum browser-use actions to execute in a single agent step",
    )
    cdp_url: str | None = Field(
        None, description="CDP URL of an existing browser to connect to (Desktop-owned browser mode)"
    )

    # --- Testing ---
    resume_json_path: str = Field(
        "",
        description="Path to JSON resume for testing without DB",
    )


settings = Settings()
