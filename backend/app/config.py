"""
Central configuration for the AI Text Humanizer backend.

All secrets are read from environment variables — never hardcode keys.
"""
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class Settings:
    # AI/ML API (https://aimlapi.com) — OpenAI-compatible endpoint.
    AIML_API_KEY: str = os.getenv("AIML_API_KEY", "")
    AIML_BASE_URL: str = os.getenv("AIML_BASE_URL", "https://api.aimlapi.com/v1")
    AIML_MODEL: str = os.getenv("AIML_MODEL", "gpt-4o-mini")

    # Clerk (JWT verification for protected routes)
    CLERK_JWKS_URL: str = os.getenv("CLERK_JWKS_URL", "")
    CLERK_ISSUER: str = os.getenv("CLERK_ISSUER", "")
    CLERK_AUTH_ENABLED: bool = os.getenv("CLERK_AUTH_ENABLED", "false").lower() == "true"

    # CORS
    FRONTEND_ORIGIN: str = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")

    # Processing
    MAX_FILE_SIZE_MB: int = int(os.getenv("MAX_FILE_SIZE_MB", "20"))
    LLM_TIMEOUT_SECONDS: int = int(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
    LLM_MAX_RETRIES: int = int(os.getenv("LLM_MAX_RETRIES", "5"))
    LLM_CONCURRENCY: int = int(os.getenv("LLM_CONCURRENCY", "5"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
