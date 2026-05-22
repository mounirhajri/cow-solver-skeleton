from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All env-driven configuration. Read once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # External APIs
    oneinch_api_key: str = ""
    rpc_arbitrum: str = "https://arb1.arbitrum.io/rpc"

    # AMM router
    intermediate_tokens: list[str] = Field(
        default_factory=lambda: [
            "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
            "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC native
            "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",  # USDT
        ]
    )

    # Cache
    redis_url: str = "redis://localhost:6379/3"
    redis_key_prefix: str = "solver:"
    pool_cache_max_entries: int = 800
    pool_cache_ttl_seconds: int = 60

    # Behaviour
    amm_state_lazy: bool = True
    solve_timeout_seconds: float = 13.0  # slightly below CoW's 15s deadline

    # Postgres
    database_url: str = "postgresql+asyncpg://solver:solver@localhost:5432/solver"

    # Shadow logging
    shadow_log_path: Path = Path("./shadow_data/log.jsonl")

    # Observability
    log_level: str = "INFO"
    prometheus_port: int = 8001


settings = Settings()
