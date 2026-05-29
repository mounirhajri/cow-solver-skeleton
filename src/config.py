from pathlib import Path

from pydantic import Field, field_validator
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
    # 300 s entspricht der 2026-05-29 Live-Diagnose: bei 60 s rebroadcastet
    # jede Auktion (~jede Minute) ihren kompletten Pool-Set, was den RPC
    # unnötig belastet. 300 s deckt ~5 Auktionen pro Cache-Round ab, ohne
    # nennenswerte Reserve-Drift.  Über env POOL_CACHE_TTL_SECONDS überschreibbar.
    pool_cache_ttl_seconds: int = 300

    # Behaviour
    amm_state_lazy: bool = True
    # Sequential strategy budget: naive(4s) + bipartite(2.6s) + multi-party(2.6s)
    # + router-v2(11s) = ~20s worst-case. We're in shadow mode — no CoW
    # submission deadline applies, so the prior 13s cap was starving router-v2
    # before it could quote (verified 2026-05-25: 0 router-v2 rows persisted
    # since the partial-fills deploy slowed bipartite/multi-party).
    solve_timeout_seconds: float = 25.0

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

    # V2 fallback: when set, RouterSolver also instantiates one V2Source per
    # configured V2 router and falls back to V2 quoting for orders the
    # V3-batched pass couldn't fill. Off by default — V2 routing adds RPC
    # round-trips (getPair + getReserves per pair) and Phase 0b ships V3
    # encoding as the primary path. Enable in shadow first to observe the
    # incremental fill rate before flipping in prod.
    router_v2_fallback_enabled: bool = False

    # Multi-Party CoW Matching: OTM-tolerance (in basis points) widens the
    # viable-order graph beyond strict reference-price-ITM. At 100 bps (1 %)
    # the graph grows ~3-5×; Johnson stays cheap, more ring candidates reach
    # the LP. 0 = strict ITM (legacy behaviour).
    multi_party_otm_tolerance_bps: int = 100

    # Multi-Party CoW Matching: after emitting a ring, the order UIDs in that
    # ring are excluded from candidate-graph for this many seconds.  Without
    # this, a persistent TWAP order whose ring is feasible in every auction
    # gets re-emitted hundreds of times in shadow even though on-chain the
    # next chunk wouldn't be available until the TWAP interval elapses.
    # 600 s (10 min) is conservative for typical TWAP intervals on Arbitrum.
    # 0 disables the cooldown (legacy behaviour).
    multi_party_ring_cooldown_seconds: int = 600

    # LongTailRouter (Pool-Indexer). Bursts ~1200 RPC calls per auction on top
    # of RouterSolver's load. 2026-05-29 Live-Daten: 0 / 711 solved über 24 h
    # auf Arbitrum, gleichzeitig 90 % des RPC-Budgets aufgefressen, was
    # router-v2 (39 % solve-rate) bei Rate-Limits ausgehungert hat.  Default
    # false bis ein bezahlter RPC-Tier provisioniert ist; via LONG_TAIL_ENABLED=true
    # für Experimente wieder aktivierbar.
    long_tail_enabled: bool = False

    # EBBO (External Best Bid/Offer) pre-submission validator.
    # Checks every emitted sell trade against a fresh V3 quote; rejects the
    # whole composed solution when our effective clearing-price output falls
    # below external by more than `ebbo_tolerance_bps`.  Critical safety net
    # for Barn/Production — multi-party rings derive ring-internal prices
    # that may not beat external on every hop, and shipping EBBO-violating
    # solutions risks rejection or bond slashing.
    # Set false to disable (shadow-mode soak with EBBO observation only).
    ebbo_enabled: bool = True
    # Slack between our claimed user output and the external V3 quote, in
    # basis points.  50 bps = 0.5 %.  Tight enough to catch genuinely
    # uncompetitive solutions; loose enough to absorb pool drift between
    # the EBBO call and the production settlement window.
    ebbo_tolerance_bps: int = 50

    # On-chain settlement & liquidity contract addresses (Arbitrum One).
    # GPv2Settlement has the same address on every chain CoW supports — it's a
    # deterministic deploy. V3 uses the original SwapRouter (NOT Universal
    # Router, which has a different ABI).  V2 routers vary per DEX; the list
    # below is the order LiquidityAggregator queries.
    gpv2_settlement: str = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"
    v3_swap_router: str = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
    v2_routers: list[str] = Field(
        default_factory=lambda: [
            "0xc873fEcbd354f5A56E00E710B90EF4201db2448d",  # Camelot
            "0xAAA87963EFeB6f7E0a2711F397663105Acb1805e",  # Ramses
            "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",  # SushiSwap
        ]
    )

    # Encoder slippage protection — gap between quoted output and the
    # `amountOutMinimum` we encode into the V3 call. Tight enough to keep
    # claimed score honest; loose enough to absorb pool drift between the
    # quote and on-chain settlement. 50bps = 0.5 %. Increase if Tenderly
    # fork tests show frequent reverts; decrease only with very fresh
    # quoting and short settle windows.
    encoder_slippage_bps: int = 50

    # CoW solver competition rewards — set via REWARDS_ADDRESS env var.
    # Single address controlled by us on BOTH Arbitrum and Ethereum mainnet.
    # COW reward tokens are sent to this address on mainnet; native-token
    # gas reimbursements are sent on the chain where solving happens.
    # Validator rejects malformed values at startup rather than letting them
    # propagate into rewards distribution where misroutes would be irreversible.
    rewards_address: str = "0x0000000000000000000000000000000000000000"

    # Postgres
    database_url: str = "postgresql+asyncpg://solver:solver@localhost:5432/solver"

    # Shadow logging
    shadow_log_path: Path = Path("./shadow_data/log.jsonl")

    # Observability
    log_level: str = "INFO"
    prometheus_port: int = 8001

    @field_validator("encoder_slippage_bps")
    @classmethod
    def _check_slippage(cls, v: int) -> int:
        # Same shape of footgun as ebbo_tolerance: >=10000 means "accept any
        # output" (no protection), negative means "require more output than
        # quoted" (guaranteed revert). Reject at startup.
        if not (0 <= v < 10_000):
            raise ValueError(
                f"encoder_slippage_bps must be in [0, 10000); got {v}"
            )
        return v

    @field_validator("ebbo_tolerance_bps")
    @classmethod
    def _check_ebbo_tolerance(cls, v: int) -> int:
        # Out-of-range values would silently disable EBBO (>=10000 → all
        # solutions pass) or over-strict (negative → all solutions fail).
        # Both are dangerous: the former ships fantasy prices, the latter
        # denies revenue. Reject the config at startup instead.
        if not (0 <= v < 10_000):
            raise ValueError(
                f"ebbo_tolerance_bps must be in [0, 10000); got {v}"
            )
        return v

    @field_validator("rewards_address")
    @classmethod
    def _check_rewards_address(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", v):
            raise ValueError(
                f"rewards_address must be a 0x-prefixed 40-hex-char address; got {v!r}"
            )
        return v.lower()


settings = Settings()
