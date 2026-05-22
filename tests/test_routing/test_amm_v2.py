from src.routing.amm_v2 import quote_v2_swap


def test_quote_v2_zero_inputs() -> None:
    assert quote_v2_swap(0, 100, 100) == 0
    assert quote_v2_swap(10, 0, 100) == 0
    assert quote_v2_swap(10, 100, 0) == 0


def test_quote_v2_basic_math() -> None:
    # 1 WETH @ pool with 100 WETH + 200_000 USDC ≈ should yield ~1980 USDC
    out = quote_v2_swap(
        amount_in=1_000_000_000_000_000_000,
        reserve_in=100 * 10**18,
        reserve_out=200_000 * 10**6,
        fee_bps=30,
    )
    assert 1_900_000_000 < out < 2_000_000_000


def test_quote_v2_fee_changes_output() -> None:
    args = dict(amount_in=10**18, reserve_in=100 * 10**18, reserve_out=200_000 * 10**6)
    fee30 = quote_v2_swap(**args, fee_bps=30)
    fee100 = quote_v2_swap(**args, fee_bps=100)
    assert fee30 > fee100
