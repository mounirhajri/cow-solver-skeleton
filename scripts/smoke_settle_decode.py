"""Encoder-fidelity gate: re-encode a REAL on-chain settle() and prove byte-equality.

Run inside the cow-solver container:
    python -m scripts.smoke_settle_decode            # scan recent blocks
    python -m scripts.smoke_settle_decode --tx 0x..  # check one known tx
    python -m scripts.smoke_settle_decode --blocks 20000

This is the gold-standard proof that ``src.encoder.settle.encode_settle`` produces
bytes identical to what real solvers submit on Arbitrum. It finds a recent
GPv2Settlement.settle() transaction, decodes its calldata with canonical
``eth_abi`` (``decode_settle``), then re-encodes the decoded fields with OUR
encoder and asserts the bytes match exactly. A mismatch means our encoder
(token indexing / trade flags / interaction packing / ABI offsets) diverges
from ground truth — fix it before trusting any phantom verdict.

Our encoder models the AMM-only settlement shape (empty pre/post interactions),
so settlements that use pre/post hooks are skipped as out-of-scope — they aren't
encoder bugs, just calls our encoder doesn't claim to reproduce. The script keeps
scanning until it finds an AMM-only settlement to use as the oracle, and SKIPs
honestly (non-zero exit only on a genuine byte mismatch) if none is found.
"""

from __future__ import annotations

import argparse
import sys

from eth_utils import keccak
from hexbytes import HexBytes
from web3 import Web3
from web3.providers.rpc import HTTPProvider

from src.config import settings
from src.encoder.settle import DecodedSettle, decode_settle, encode_settle

# GPv2Settlement emits `Settlement(address indexed solver)` once per settle() call.
# Its topic0 is the cheapest way to find settle() transactions via eth_getLogs.
_SETTLEMENT_EVENT_TOPIC = "0x" + keccak(text="Settlement(address)").hex()


def _to_bytes(data: object) -> bytes:
    """Normalise a web3 tx ``input`` (HexBytes | bytes | 0x-hex str) to bytes."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    s = str(data)
    return bytes.fromhex(s[2:] if s.startswith("0x") else s)


def _first_diff(a: bytes, b: bytes) -> int:
    """Index of the first differing byte, or -1 if one is a prefix of the other."""
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return i
    return -1 if len(a) == len(b) else min(len(a), len(b))


def _check_one(tx_hash: str, calldata: bytes) -> bool | None:
    """Run the fidelity gate on one settle() calldata blob.

    Returns True  = byte-equal (encoder proven against this tx),
            False = byte MISMATCH (encoder bug — the whole point of the gate),
            None  = out of scope (decode failed or uses pre/post hooks).
    """
    try:
        decoded: DecodedSettle = decode_settle(calldata)
    except ValueError as exc:
        print(f"  {tx_hash}: not a settle() call ({exc}) — skipping")
        return None

    if decoded.pre_interactions or decoded.post_interactions:
        print(
            f"  {tx_hash}: uses pre/post hooks "
            f"(pre={len(decoded.pre_interactions)}, post={len(decoded.post_interactions)})"
            " — out of scope for AMM-only encoder, skipping"
        )
        return None

    reencoded = encode_settle(
        tokens=decoded.tokens,
        clearing_prices=decoded.clearing_prices,
        trades=decoded.trades,
        intra_interactions=decoded.intra_interactions,
    )
    if reencoded == calldata:
        print(
            f"  {tx_hash}: FEASIBLE re-encode — bytes match exactly "
            f"({len(calldata)} bytes, {len(decoded.trades)} trades, "
            f"{len(decoded.intra_interactions)} intra interactions). ENCODER OK."
        )
        return True

    diff = _first_diff(calldata, reencoded)
    print(
        f"  {tx_hash}: BYTE MISMATCH at offset {diff} "
        f"(real={len(calldata)}B, ours={len(reencoded)}B). ENCODER BUG."
    )
    print(f"    real[{diff}:{diff + 32}] = {calldata[diff:diff + 32].hex()}")
    print(f"    ours[{diff}:{diff + 32}] = {reencoded[diff:diff + 32].hex()}")
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tx", help="check a single known settle() tx hash")
    ap.add_argument(
        "--blocks",
        type=int,
        default=10_000,
        help="how many recent blocks to scan for settlements (default 10000)",
    )
    args = ap.parse_args()

    w3 = Web3(HTTPProvider(settings.rpc_arbitrum, request_kwargs={"timeout": 15}))
    settlement = Web3.to_checksum_address(settings.gpv2_settlement)

    # ── single known tx ──────────────────────────────────────────────────────
    if args.tx:
        tx = w3.eth.get_transaction(HexBytes(args.tx))
        verdict = _check_one(args.tx, _to_bytes(tx["input"]))
        sys.exit(0 if verdict is not False else 1)

    # ── scan recent blocks for settle() calls ────────────────────────────────
    latest = w3.eth.block_number
    from_block = max(0, latest - args.blocks)
    print(
        f"scanning blocks {from_block}..{latest} for {settlement} settlements "
        f"(topic {_SETTLEMENT_EVENT_TOPIC})"
    )
    logs = w3.eth.get_logs({
        "fromBlock": from_block,
        "toBlock": latest,
        "address": settlement,
        "topics": [_SETTLEMENT_EVENT_TOPIC],
    })
    if not logs:
        print(
            f"SKIP: no settlements in the last {args.blocks} blocks. "
            "Re-run with a larger --blocks window."
        )
        return

    # Most recent first; dedupe tx hashes (a tx can settle several solver events).
    seen: set[str] = set()
    print(f"found {len(logs)} settlement event(s); searching for an AMM-only one...")
    for log in reversed(logs):
        tx_hash_hb = HexBytes(log["transactionHash"])
        tx_hash = tx_hash_hb.hex()
        if tx_hash in seen:
            continue
        seen.add(tx_hash)
        tx = w3.eth.get_transaction(tx_hash_hb)
        verdict = _check_one(tx_hash, _to_bytes(tx["input"]))
        if verdict is True:
            return
        if verdict is False:
            sys.exit(1)  # genuine encoder bug — fail loudly

    print(
        f"SKIP: scanned {len(seen)} settlement tx(s) but all use pre/post hooks "
        "(no AMM-only settlement to use as an oracle). Try a larger --blocks "
        "window or pass an AMM-only --tx directly."
    )


if __name__ == "__main__":
    main()
