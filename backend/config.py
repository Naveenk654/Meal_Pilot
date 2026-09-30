from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_secret: str = ""

    llm_primary_provider: str = "gemini"
    llm_primary_model: str = "gemini-2.0-flash"
    llm_primary_api_key: str = ""

    llm_fallback_provider: str = "groq"
    llm_fallback_model: str = "llama-3.1-8b-instant"
    llm_fallback_api_key: str = ""

    llm_request_timeout_s: int = 30
    llm_max_retries: int = 3

    max_candidates: int = Field(default=4, ge=1, le=10)
    max_revisions: int = Field(default=2, ge=0, le=10)
    confidence_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    # In plan_mode='mess_only', invoke canteen fallback when the best mess
    # candidate's soft score is below this threshold, even if the plan
    # technically validated. Catches "4 servings of tea to hit macros"-style
    # plans that pass constraints but fail practicality/variety soft scoring.
    fallback_quality_threshold: float = Field(default=0.6, ge=0.0, le=1.0)

    default_timezone: str = "Asia/Kolkata"
    app_env: str = "dev"
    log_level: str = "INFO"

    # Optional: surface HITL #7 (plan approval) after every commit. Off in dev
    # because it spams the review panel; on for M7 pilot users if requested.
    hitl_always_approve_plan: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
