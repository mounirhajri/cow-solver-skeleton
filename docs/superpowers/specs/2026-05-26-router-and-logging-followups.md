# Router + Logging Follow-ups

**Status:** draft, not started.
**Authored:** 2026-05-24, after the post-merge code-review pass that
produced PR #24 (which fixed the iter-EBBO + ceil_div + bounds-check
issues).
**Estimated effort:** 1–2 days total.
**Priority:** these go in BEFORE Barn-readiness sign-off — items 1 and 2
have real production-correctness consequences.

This spec captures the review findings that did NOT make it into PR #24
because they need their own design step or touch the edge submodule.

---

## 1. `router._register_prices` setdefault collision (Critical)

### What's wrong

`src/solver/router.py:382-410` uses `dict.setdefault` to populate
clearing prices. When two trades touch the same token (e.g. a sell
and a buy on the same A→B pair) only the FIRST trade's ratio is
recorded. Subsequent trades on the same token reuse that ratio in
the composed `Solution.prices`, so their effective `our_buy_amount`
at scoring time is computed against the wrong ratio.

This was always a latent bug — multiple sell orders on the same pair
could already step on each other — but **PR #20 made it worse** by
introducing buy orders on the same pair as sells.

### Why it matters

CIP-67 requires a single uniform clearing price per token. If two
trades produce different V3 execution ratios for the same pair, the
solver MUST either:

- Pick a single ratio that satisfies both order limits (potentially
  leaving surplus on the table), or
- Drop one trade.

`setdefault` is neither — it picks the first ratio silently, leaves
the second trade in the solution with an inconsistent price claim,
and EBBO would (correctly) flag the resulting solution at submission.

Net effect: rare today (mixed sell+buy on same pair is uncommon),
but real once buy-order volume picks up. Likely a slow leak rather
than a daily incident.

### How to fix

Two acceptable designs:

**A. First-ratio enforcement with explicit drop.**
On second touch of a token, compare the new ratio against the stored
one. If they match within tolerance, keep both trades. If not, DROP
the lower-surplus trade and log it. Same invariant as the composer
already enforces at the candidate-solution level, just one layer
deeper.

**B. Average and re-validate.**
Average the ratios, re-check both trades' limits against the
averaged price, drop any that no longer satisfy. More math, more
test surface, higher upside (more trades survive).

Recommend (A) for the first pass — provably CIP-67 safe, matches the
strict-disjoint composer's spirit, low risk. Revisit (B) only if (A)
loses measurable trade volume.

### Test
- Auction with two sells on (A→B) at different V3 fee tiers → both
  trades emitted? Or higher-surplus kept?
- Auction with sell + buy on (A→B) → check `result.prices[A]` is
  consistent with EXACTLY one trade.

---

## 2. Legacy V3 path silently skips buy orders (High)

### What's wrong

`src/solver/router.py:308-316` (the `_solve_legacy` path triggered
when `ROUTER_V3_ONLY_BATCHED=false`) filters `o.kind == "sell"` and
drops every buy order without logging. The docstring still advertises
this mode as an escape hatch for "public-clone where V3 quoters
might be unavailable", which is true for the sell path but silently
loses 8% volume on the buy side.

### Why it matters

If we EVER set `ROUTER_V3_ONLY_BATCHED=false` in production (e.g.
during an Alchemy outage when v3_batched goes flaky) we revert to
ignoring buys with no operator-visible signal.

### How to fix

Either:

- Extend `_quote_one` to dispatch on `o.kind` and use the new
  `quoteExactOutput` helpers introduced in PR #20 — straightforward
  if PR #20's path-encoding is sound (it is, per the reviewer's
  unit-test pinning), or
- Add a `log.warning("router_legacy_path_skips_buys", n_dropped=...)`
  on entry so the gap is visible in shadow logs.

Recommend BOTH. The warning is 1 line and ships safety today; the
real buy support in legacy mode is 2 hours and removes the gap
entirely.

### Test
- Existing `test_router_legacy_mode_emits_trades_when_v3_only_disabled`
  fixture with a mixed sell+buy auction; assert buy emits in legacy.

---

## 3. Smart-wallet log fires three times per auction (Medium)

### What's wrong

PR #19 added `smart_wallet_orders_observed` log events at INFO level
inside `router.py:147`, `edge/matching/bipartite.py:76`,
`edge/matching/multi_party.py:142`. Same observation, three log
emissions per auction → 3× INFO noise in the journal.

### Why it matters

Pure observability hygiene. Not a correctness issue. Becomes
noticeable once journal volume matters (Loki / Prometheus log-based
metrics).

### How to fix

Move the log to the orchestrator: count smart-wallet orders once,
right after auction parsing, before the strategy loop. Strategies
keep the count locally for branching decisions but don't log.

Touches one file in parent (`orchestrator.py`) plus two files in the
edge submodule (`bipartite.py`, `multi_party.py`) — two PRs (parent
+ edge submodule bump). Estimated 1 hour total.

### Test
- One INFO event per auction at exactly one location; existing
  test patches can drop their assertions on the per-strategy log.

---

## 4. Unknown `signingScheme` is silently classified as EOA (Medium)

### What's wrong

`src/models/order.py:11` hard-codes `_SMART_WALLET_SCHEMES = {presign,
eip1271, erc1271}`. If CoW adds a new scheme tomorrow (or uses a
casing/spelling we don't recognise) the order is silently classified
as EOA → `is_smart_wallet_signed = False`. Any downstream branching
that depends on this flag will behave wrong without an observable
signal.

### Why it matters

Soft failure mode. We'd find out months later when we wonder why a
new wallet type behaves oddly. Better to log loudly when we encounter
something unexpected.

### How to fix

In `Order` or wherever `signing_scheme` is parsed:

```python
@field_validator("signing_scheme")
@classmethod
def _warn_unknown_scheme(cls, v: str | None) -> str | None:
    if v and v.lower() not in _SMART_WALLET_SCHEMES.union({"eip712", "ethsign"}):
        log.warning("unknown_signing_scheme_observed", scheme=v)
    return v
```

(EIP-712 + ethsign are the EOA-style schemes we expect; anything else
is novel.)

Estimated 30 minutes including the test (validate a fixture with
`"signingScheme": "future-scheme-name"` triggers the warning exactly
once).

---

## Sequencing

1. **Today/tomorrow:** items 2 (legacy buy gap) + 4 (unknown
   scheme warning). Both <2 hours. No edge submodule.
2. **Next session:** item 1 (setdefault collision). Needs a real
   design choice — pick option A or B above — and a test
   strategy.
3. **When edge submodule touched anyway:** item 3 (log dedup).
   Cheapest as a rider on a future edge change.

## Out of scope here

- Buy-side EBBO check. Already documented as deferred in
  `src/solver/ebbo.py`'s module docstring; tracked separately when
  partial fills land.
- Multi-hop reversed-path live verification on Arbitrum mainnet. Not
  reachable from CI; needs a smoke-quote tool that the reviewer
  flagged. Open as a manual verification step before Barn.
