# Next Steps — 2026-05-26 morning

Session-Handoff vom 2026-05-25 abends. Backfill läuft seit ~21:05 UTC,
sammelt competition data alle 15min. analyze_competitors.py zeigt
phantom "100% rank #1" (gleiche Bug-Class wie €67M-phantom vom 24.).

---

## Priority 1 — Phantom-Rank Hypothesis verifizieren (10 min)

Erst bestätigen WAS die phantom-rank verursacht, dann fixen.

### Query A — welche strategy treibt die "wins"?

```bash
docker exec cow-solver python -c "
import asyncio
from sqlalchemy import text
from src.persistence.db import get_session_factory

async def main():
    async with get_session_factory()() as s:
        r = await s.execute(text('''
            SELECT ss.strategy,
                   count(*) AS n_wins,
                   round((percentile_cont(0.5) WITHIN GROUP (ORDER BY ss.our_score_wei) / 1e18)::numeric, 4) AS median_eth
            FROM shadow_solutions ss
            JOIN shadow_competitors sc ON sc.auction_id = ss.auction_id
            WHERE ss.our_score_wei IS NOT NULL
              AND ss.our_score_wei > (
                SELECT max(sc2.score) FROM shadow_competitors sc2
                WHERE sc2.auction_id = ss.auction_id
              )
            GROUP BY ss.strategy
            ORDER BY n_wins DESC
        '''))
        print('Strategy distribution of supposed wins:')
        for row in r: print(f'  {row[0]:<30} {row[1]:>5} wins, median {row[2]} ETH')

asyncio.run(main())
"
```

**Erwartet:** `router-v2` + `composer` mit median ~6 ETH (phantom). Bipartite + multi-party fast 0.

→ Wenn Output Hypothese bestätigt → Priority 2.

### Query B — bipartite-only ehrlicher Vergleich

```bash
docker exec cow-solver python -c "
import asyncio
from sqlalchemy import text
from src.persistence.db import get_session_factory

async def main():
    async with get_session_factory()() as s:
        r = await s.execute(text('''
            WITH our_bipartite AS (
              SELECT auction_id, our_score_wei
              FROM shadow_solutions
              WHERE strategy = 'cow-matching-bipartite'
                AND our_score_wei IS NOT NULL
            ),
            comp_wins AS (
              SELECT auction_id, max(score) AS winner_score
              FROM shadow_competitors
              WHERE is_winner = true
              GROUP BY auction_id
            )
            SELECT
              count(*) AS auctions_compared,
              count(*) FILTER (WHERE ob.our_score_wei > cw.winner_score) AS we_beat_winner,
              count(*) FILTER (WHERE ob.our_score_wei <= cw.winner_score) AS winner_beats_us
            FROM our_bipartite ob
            JOIN comp_wins cw USING (auction_id)
        '''))
        for row in r: print(dict(row._mapping))

asyncio.run(main())
"
```

**Das** ist die ehrliche bipartite-vs-real-competition Zahl. Erwartet:
- ~30-50% wins (gegen volume-floor-spam von kaiser)
- 0% gegen helixbox (die spielen anderes Spiel)

---

## Priority 2 — Fix analyze_competitors.py (30 min)

Aktuell: `max(our_score_wei)` per auction → picks router-v2 phantom.

**Fix:** per-strategy comparison + --strategy flag:

```bash
docker exec cow-solver python -m scripts.analyze_competitors --strategy cow-matching-bipartite
docker exec cow-solver python -m scripts.analyze_competitors --strategy router-v2
docker exec cow-solver python -m scripts.analyze_competitors  # zeigt all
```

### Implementation skeleton

In `scripts/analyze_competitors.py` view 1 query:

```python
# ALT (broken):
SELECT max(our_score_wei) AS our_best FROM shadow_solutions ...

# NEU:
SELECT our_score_wei, strategy FROM shadow_solutions
WHERE strategy = :strategy  -- filtered
  AND our_score_wei IS NOT NULL
```

Plus CLI:
```python
parser.add_argument("--strategy", choices=["cow-matching-bipartite",
                                          "cow-matching-multi-party",
                                          "router-v2", "composer"],
                    help="Limit comparison to one strategy")
```

Output sollte für `--strategy cow-matching-bipartite` ehrliche Zahlen zeigen.

---

## Priority 3 — Erweitere Sub-Dust-Filter auf Router-Phantom (15 min)

Aktuell filtert `src/shadow/persist.py:115` Sub-Dust mit EPSILON_WEI=10^12.
Aber: router-v2 phantom-arb scores sind > EPSILON (6 ETH range), durchstehen
den Filter, treiben downstream phantom-analysis.

**Option A:** Cap our_score_wei wenn router_high_surplus_observed >> 100bps
(haben wir bereits den log — `router.py:514`)

**Option B:** Zusätzlich `EPSILON_HIGH_WEI = 10^18` (= 1 ETH), über dem
wir nichts persistieren (alles über 1 ETH ist phantom-suspekt auf Arbitrum).

**Option C:** Score von router-v2 immer NULL'en wenn surplus_bps > 100 in
den orders. Konservativster Ansatz.

Empfehlung: **Option B** — pragmatisch, fängt phantom-router + harm-naive,
1-Zeilen-fix in persist.py.

Test: `tests/test_shadow/test_persist.py` — neue test für upper-cap.

---

## Priority 4 — 24h Daten sammeln (passiv)

Loop läuft. Bei ~75 auctions/Stunde * 8 solver/auction = ~600 rows/h.
Morgen früh erwartet: ~7000-10000 rows nach 12h.

Statistische Signifikanz erst dann:
- helixbox 72% bleibt? (jetzt n=25, Konfidenzintervall huge)
- Sigmaresearch nur 1 bid → wird sich klären
- Win-Rate per token-pair berechenbar

Backfill historischer Daten (parallel zu cron):
```bash
docker exec cow-solver python -m scripts.sync_competitions --days 30 --limit 5000
```

(15min Sleep zwischen batches, also dauert das ~30 batches mit 200 auctions
each = 7.5h Wandzeit. Über Nacht passt.)

---

## Priority 5 — Pitch-Story basierend auf echten Daten (Mittwoch?)

Wenn Discord-Antwort kommt + echte Konkurrenz-Daten in DB:

### Was du WIRKLICH sagen kannst (kein hand-waving mehr)

> "We benchmarked our solver against 14 active Arbitrum-One solvers.
> Helixbox dominates with 72% win rate (private liquidity, not in our
> league). Kaisersolver volume-floor-spams second at 37.5% with sub-bps
> margins. The 28% non-helixbox tail is where we compete — specifically
> stablecoin partial-fill TWAPs where our bipartite matcher achieves
> [bipartite_win_rate from Query B]% against the volume-floor segment."

Specific, defendable, no claims unbacked by data.

### Was du NICHT sagen darfst (mehr)

- "We're competitive with top solvers" — falsch, only kaiser
- "Multi-party gives us an edge" — nicht durch competition data validated
- "[€500/Mo Net]" — phantom-route, see Priority 1+2

---

## Current State (für quick reference)

- **All 5 strategies persisting rows**: ✅ (post 2026-05-25 fixes)
- **shadow_competitors syncing**: ✅ (curl_cffi, ~75 auctions/h)
- **Phantom guards in scoring**: limit-violation→0 + naive→NULL + sub-dust filter
- **EBBO**: sell + buy validated, truncation rejects
- **Production-safety**: LP-overfill rejection, ring-cooldown, 25s solve-timeout
- **Submission**: still OFF (longtail_enabled=false, no submit_enabled flag)
- **Test suite**: 423/423 green

## Pending (carried over)

- Spec §3 smart-wallet log dedup — already done (PR #31, verify with grep)
- Monitoring/Alerting spec — open, 2-3h
- Automated deploy SSH key rotation — open, 30min

## Open Risks

1. **CoW Discord still silent** (since 2026-05-24). Realistic: Mi/Do.
2. **GoPlus credentials in docker-compose.yml** — hardcoded. Move to .env.
3. **Container "unhealthy" Status für comp-sync** — kosmetisch (inherited image healthcheck). Add `healthcheck: disable: true` zum service.

---

## Tomorrow's Routine (suggested)

1. Coffee
2. Query A + B (10 min)
3. If hypothesis confirmed → fix analyze_competitors.py (Priority 2, 30 min)
4. Optional: Priority 3 (sub-dust upper cap, 15 min)
5. Run fixed analyze_competitors.py with --strategy bipartite → get HONEST win-rate
6. Check Discord
7. If Discord responded → reply with the honest numbers
8. If not → continue building OR rest

Stop point if running out of energy: after Priority 1+2.
The data will still be there.
