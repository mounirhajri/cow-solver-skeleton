# Archive

Historical specs, plans, and progress reports. **Not authoritative** — for the current state of the project, see [`docs/current/STATUS.md`](../current/STATUS.md).

## Why these are archived

Each document here is one of:
- **Completed**: the work it describes has been delivered (see STATUS §1)
- **Superseded**: replaced by a newer spec or by code changes
- **Future**: a draft for work that hasn't started yet (see STATUS §2 / §5)
- **Progress log**: point-in-time session notes, kept for context

## Index

### specs/

| File | Status | Reason archived |
|---|---|---|
| `2026-05-23-solver-revenue-strategy-design.md` | Superseded | Router phantom-surplus bug it describes was fixed in PR #26; G6 gate math obsolete after verified shadow data showed ~10× lower volume than projected |
| `2026-05-25-partial-fills-design.md` | Future | Draft for partial-fill support; only Phase 1 LP-rounding tests merged (PR #30). See STATUS §2.1 |
| `2026-05-26-router-and-logging-followups.md` | Mostly completed | §1 phantom clearing-prices fixed (PR #26), §2 V3-buy-skip warning added, §4 signingScheme validator landed. §3 smart-wallet log triplication still open (STATUS §2.2) |

### plans/

| File | Status | Reason archived |
|---|---|---|
| `2026-05-22-cow-solver-phase1-4.md` | Completed | Phases 1-3 delivered in production; Phase 4 (settlement reconciler) deferred |
| `2026-05-22-phase0-1-skeleton-shadow.md` | Completed | All Phase 0-1 items live (shadow pipeline writing to Postgres) |
| `2026-05-23-ml-token-classifier-pipeline.md` | Partial (blocked) | Code complete (feature engineering, Optuna, training, inference), but `generate_labels()` produces zero labels because CoW public API doesn't expose clearingPrices. Unblocked by future Phase-4 settlement reconciler |
| `2026-05-24-partial-fills-implementation.md` | Partial | Phase 1 (LP rounding tests) merged; phases 2-4 not started |

### progress/

| File | Type | Notes |
|---|---|---|
| `2026-05-23-overnight-execution.md` | Session log | Snapshot of overnight Phase 0-1 delivery |
