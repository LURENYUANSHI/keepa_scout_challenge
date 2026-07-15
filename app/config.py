"""Application configuration.

Loads settings from environment variables / a local .env file. See
ARCHITECTURE.md §6 for the decided defaults (24h token TTL, UTC timezone).
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres, async driver (asyncpg). e.g.
    # postgresql+asyncpg://user:pass@db:5432/keepa_scout
    DATABASE_URL: str

    # Redis, used as the Celery broker/result backend.
    REDIS_URL: str

    # Comma-separated list of Keepa API keys (rotated on 402/429).
    KEEPA_API_KEYS: str = ""

    # LLM provider config (OpenAI-compatible endpoint). Left unset until a
    # provider is chosen — see ARCHITECTURE.md §4.4.
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    LLM_MODEL: str = ""

    # Auth token lifetime, in hours. See ARCHITECTURE.md §6.
    TOKEN_TTL_HOURS: int = 24

    # Timezone the app/worker/beat run in. Celery beat's daily refresh is
    # scheduled at 04:00 UTC — see ARCHITECTURE.md §1.
    TZ: str = "UTC"

    @property
    def keepa_api_keys_list(self) -> list[str]:
        """KEEPA_API_KEYS parsed into a list, stripped, empty entries dropped."""
        return [k.strip() for k in self.KEEPA_API_KEYS.split(",") if k.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
