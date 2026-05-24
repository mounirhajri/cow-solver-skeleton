# Router + Logging Follow-ups

**Status:** §1 RESOLVED (skeleton PR #26 + edge PR #9). §2–§4 still open.
**Authored:** 2026-05-24, after the post-merge code-review pass that
produced PR #24 (which fixed the iter-EBBO + ceil_div + bounds-check
issues).
**Estimated effort:** 1–2 days total for remaining items.
**Priority:** these go in BEFORE Barn-readiness sign-off — items 1 and 2
have real production-correctness consequences.

This spec captures the review findings that did NOT make it into PR #24
because they need their own design step or touch the edge submodule.

---

## 1. `router._register_prices` phantom clearing prices (RESOLVED 2026-05-24)

**Resolved by PR `fix/router-phantom-clearing-prices`.** Diagnosis moved
from `setdefault` collision (suspected) to a different, more severe
issue: registering oracle `reference_price` as the clearing price
regardless of what the AMM quoted. Setdefault is still in place but
matters less now that the input is correct AMM-rate.

### What was wrong

`src/solver/router.py:382-410` (pre-fix) substituted
`token.reference_price` as `Solution.prices[token]` whenever oracle
references were available. CIP-14 scoring then computed
``bought = executed * cp_sell / cp_buy = executed * oracle_ratio``,
so surplus collapsed to the order's OTM headroom AT ORACLE — surplus
the AMM never actually realised.

PR #11 (sort-by-margin) made the bug dominant by ranking exactly the
orders with the largest oracle-headroom to the top of the routing
queue. Live impact 2026-05-24:

- `router-v2` median score 6.6 ETH per fill (134 fills/24 h), tight
  cluster (max 6.71, min 0.02) — systematic, not outlier.
- `composer` median 447 ETH — inherits router-v2's phantom clearing
  prices via token-disjoint merge. Composer does NOT call
  `price_refiner`; the contamination flows directly from
  `_register_prices` into composed solutions.
- `naive` median 462 ETH (also reference-priced via `naive.py:81-82`,
  excluded from submission by `test_naive_solution_is_never_submitted`).
- `estimate_economics.py` projected €67M/Mo net.

Confirmed mathematically against a real auction row: a BUY of 1 WBTC
with sellAmount=93,262.72 USDC limit and oracle ratio 76,240 USDC/WBTC
produces surplus_sell ≈ 17,000 USDC → score = 6.65 ETH, exactly the
persisted value.

### How it was fixed

`_register_prices` always uses the AMM execution ratio
(`executed_buy / executed_sell`) — no reference-price branch. CIP-14
scoring then reproduces the user's REALISED surplus only:
`surplus = signed_sell − amm_amount_in`. Reference prices remain
correct for the `native_price_buy` numéraire in
`shadow.scoring._score_*_trade` (oracle conversion to ETH wei), so
the score is in real ETH-equivalent terms.

### Related places still using reference_price as clearing price

- `src/solver/price_refiner.py:151,177-179` — same pattern. Inline
  comment (lines 168-179) explicitly cited cross-pair consistency as
  motivation; that argument has now been falsified by the live data
  and the comment has been replaced with a KNOWN-BAD warning in this
  PR. Code path is only safe today because the sole caller is naive,
  which is excluded from submission and from composer-input. Real fix
  needs CIP-67-uniform clearing prices (LP over per-pair AMM rates),
  tracked separately as a partial-fills design dependency — out of
  scope for this hotfix.
- `src/solver/naive.py:81-82` — same pattern but harmless: naive is
  excluded from submission and from composer-input by tests
  `test_naive_solution_is_never_submitted` and
  `test_composer_excludes_naive_when_other_strategy_solves`.

### Was-it-setdefault context (kept for archaeology)

The original 2026-05-24 spec hypothesised a setdefault collision on
sell+buy-on-same-pair as the failure mode. Inspection of the
actual high-score rows showed single-trade solutions — the surplus
explosion is entirely in the reference-price-as-clearing-price step,
not in repeated-token-touch handling. Setdefault is left untouched
for now; if a future case shows mixed sell+buy emitting inconsistent
ratios despite both using AMM-rate, revisit Option A (first-ratio
enforce-or-drop) or Option B (averaged price + re-validate).

### Post-PR-#26 audit (2026-05-24 evening, parallel reviewer agent)

Goal: find every other place that could produce the same bug class.

**Resolved here:**
- `edge/pool_indexer/long_tail_router.py:125-130` — same exact pattern,
  closed in edge PR #9. Currently disabled in prod via
  `LONG_TAIL_ENABLED=false`, but the composer would pick up its output
  if re-enabled. Fix now closes the door.

**Estimator over-counting (separately):**
- Multi-party already had ring-signature dedup. Router-v2 and bipartite
  did not. Live router-v2 data 2026-05-24 showed 134 wins/24h from
  ~10–15 distinct order UIDs (10× dup factor). Bipartite has the same
  exposure when both sides of a pair are persistent (e.g. two TWAPs).
  Both added to `_DEDUP_STRATEGIES` in `scripts/estimate_economics.py`.
  Dedup is now uniform: `frozenset({multi-party, bipartite, router-v2})`.

**Clean code paths verified (no fix needed):**
- `edge/matching/bipartite.py:235-236` — uses executed-amount ratio
  (AMM-realised). Correct.
- `edge/matching/multi_party.py:228,279` and `edge/matching/surplus.py:198-229`
  — LP-derived clearing prices anchored on `ring[0].sell_token`'s
  reference_price as numéraire ONLY; ratios are LP-achieved internal
  rates. Correct usage of reference_price (anchor, not clearing).
- `edge/matching/composer.py:88-91` — token-disjoint, inherits prices
  wholesale, no oracle injection. Clean once long-tail is fixed.
- `src/shadow/persist.py:74,251` — `reference_price` used as
  `native_prices` for ETH-numéraire conversion in CIP-14 scoring.
  This is the CORRECT use of oracle (numéraire, not clearing).
- `src/solver/router.py:47-58,84-94` — `reference_price` only used as
  sort-key / ITM-filter, never written to clearing prices.

**Known harmless (per existing guards):**
- `src/solver/naive.py:81-82` — oracle as clearing, but naive is
  excluded from submission (`test_naive_solution_is_never_submitted`)
  and from composer-input (`test_composer_excludes_naive_when_other_strategy_solves`).
- `src/solver/price_refiner.py:174-179` — same pattern, KNOWN-BAD
  comment added in PR #26. Only called by NaiveSolver. Real fix needs
  CIP-67-uniform clearing prices (LP over per-pair AMM rates) — tracked
  as partial-fills design dependency.



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
