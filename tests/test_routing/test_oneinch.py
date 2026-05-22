import httpx
import pytest
import respx

from src.routing.oneinch import OneInchClient, OneInchQuote


@respx.mock
async def test_quote_returns_parsed_response() -> None:
    respx.get("https://api.1inch.dev/swap/v6.0/42161/quote").mock(
        return_value=httpx.Response(
            200,
            json={
                "dstAmount": "3450000000",
                "gas": 200000,
            },
        )
    )
    client = OneInchClient(api_key="test", chain_id=42161)
    quote = await client.quote(
        src="0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
        dst="0xaf88d065e77c8cc2239327c5edb3a432268e5831",
        amount=10**18,
    )
    assert isinstance(quote, OneInchQuote)
    assert quote.dst_amount == 3_450_000_000
    assert quote.gas == 200000
    await client.close()


@respx.mock
async def test_quote_raises_on_5xx() -> None:
    respx.get("https://api.1inch.dev/swap/v6.0/42161/quote").mock(
        return_value=httpx.Response(503, json={"error": "service unavailable"})
    )
    client = OneInchClient(api_key="test", chain_id=42161)
    with pytest.raises(httpx.HTTPStatusError):
        await client.quote(
            src="0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
            dst="0xaf88d065e77c8cc2239327c5edb3a432268e5831",
            amount=10**18,
        )
    await client.close()
