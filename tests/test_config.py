import pytest

from src.config import Settings


def test_settings_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEINCH_API_KEY", "test-key")
    monkeypatch.setenv("RPC_ARBITRUM", "https://arb1.example/rpc")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/3")
    monkeypatch.setenv("SHADOW_LOG_PATH", "/tmp/shadow.jsonl")
    s = Settings()
    assert s.oneinch_api_key == "test-key"
    assert s.rpc_arbitrum == "https://arb1.example/rpc"
    assert s.redis_url == "redis://localhost:6379/3"
    assert s.redis_key_prefix == "solver:"  # default
    assert s.pool_cache_max_entries == 800  # default for shadow phase
    assert str(s.shadow_log_path) == "/tmp/shadow.jsonl"


def test_feasibility_defaults() -> None:
    from src.config import Settings

    s = Settings()
    assert s.feasibility_enabled is True
    # A public, allowlisted CoW solver address (checksummed, 42 chars).
    assert s.feasibility_solver_address.startswith("0x")
    assert len(s.feasibility_solver_address) == 42
