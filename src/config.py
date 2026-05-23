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

    # RouterSolver concurrency
    # Alchemy free tier: ~330 CU/s = ~12 eth_calls/s.  Each in-flight order
    # occupies one concurrent slot and issues sequential Multicall3 requests.
    # max_concurrent=3 keeps ≤3 requests in-flight at once → ~10 req/s = 260 CU/s,
    # safely under the rate limit.  With WETH-only intermediates each order needs
    # ~9 sequential calls (~2.7 s on Alchemy); 9 orders / 3 concurrent = 3 waves
    # × 2.7 s = 8.1 s, comfortably inside the 11 s per-strategy budget.
    router_max_orders: int = 9         # top-N sell orders by sell_amount
    router_max_concurrent: int = 3     # parallel RPC quote slots (semaphore)
    router_strategy_timeout: float = 11.0  # per-strategy timeout for router-v2 (s)
    # Intermediates for router-v2.  Restricted to WETH only (vs the full list used
    # by naive) to halve the per-order call count: direct + WETH 2-hop = 9 calls
    # instead of direct + WETH/USDC/USDT 2-hop = 21 calls.
    router_intermediate_tokens: list[str] = Field(
        default_factory=lambda: [
            "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH only
        ]
    )

    # RouterSolver path. V3-only batched mode skips V2 entirely and submits all
    # quotes for an auction in one Multicall3 round-trip. Dramatically reduces
    # RPC load (~80x) but loses access to V2-only pools (rare on Arbitrum).
    router_v3_only_batched: bool = True

    # LongTailRouter (Pool-Indexer). Bursts ~60 RPC calls per auction on top of
    # RouterSolver's load. On a tight Alchemy free-tier concurrent-connection
    # quota that pushes RouterSolver into "Authentication required" rejections.
    # Disable in prod until paid RPC tier is provisioned.
    long_tail_enabled: bool = True

    # Postgres
    database_url: str = "postgresql+asyncpg://solver:solver@localhost:5432/solver"

    # Shadow logging
    shadow_log_path: Path = Path("./shadow_data/log.jsonl")

    # Observability
    log_level: str = "INFO"
    prometheus_port: int = 8001


settings = Settings()
