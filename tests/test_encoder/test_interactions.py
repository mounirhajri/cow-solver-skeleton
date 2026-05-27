"""Tests for the GPv2 Interaction primitive and its wire serialisation."""

import pytest

from src.encoder.interactions import Interaction

V3_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"


def test_to_gpv2_dict_emits_driver_wire_format() -> None:
    """The CoW driver expects {target, value, callData} with value as
    decimal string and callData as 0x-prefixed hex. Wire-format drift
    here would silently reject every solution."""
    i = Interaction(target=V3_ROUTER, value=0, call_data=bytes.fromhex("deadbeef"))
    d = i.to_gpv2_dict()
    assert d == {
        "target": V3_ROUTER,
        "value": "0",
        "callData": "0xdeadbeef",
    }


def test_to_gpv2_dict_preserves_nonzero_value() -> None:
    """value is wei; pure ERC-20 paths use 0 but ETH-receiving interactions
    pass real wei values. Stringify-as-decimal must not corrupt."""
    i = Interaction(target=V3_ROUTER, value=10**18, call_data=b"")
    assert i.to_gpv2_dict()["value"] == "1000000000000000000"


def test_call_data_must_be_bytes_not_str() -> None:
    """A hex-string in place of bytes is a common mistake that would
    serialise as "0x" + repr(str) — wrong wire format, silent revert."""
    with pytest.raises(TypeError, match="call_data must be bytes"):
        Interaction(target=V3_ROUTER, value=0, call_data="0xdead")  # type: ignore[arg-type]


def test_value_must_be_non_negative() -> None:
    with pytest.raises(ValueError, match="value must be non-negative"):
        Interaction(target=V3_ROUTER, value=-1, call_data=b"")


@pytest.mark.parametrize(
    "bad_target",
    [
        "0xshort",
        "0xE592427A0AEce92De3Edee1F18E0157C0586156",  # 41 chars
        "E592427A0AEce92De3Edee1F18E0157C05861564",  # missing 0x
        "",
    ],
)
def test_target_must_be_0x_20byte_address(bad_target: str) -> None:
    with pytest.raises(ValueError, match="20-byte address"):
        Interaction(target=bad_target, value=0, call_data=b"")
