# Next Steps — 2026-05-29 (abends)

Status: 12h clean data ✅, validate_data alle Prüfungen grün ✅, G6 PASS €4.947/Mo NET ✅

---

## Priority 1 — ERLEDIGT: validate_data + Economics

Alle 8 Prüfungen grün (2026-05-29 ~22:00 UTC):
- Prüfung 1 (Score-Sanity): ✅ Alle Scores in Range
- Prüfung 2 (JSON-Felder): ✅ prices+trades vorhanden
- Prüfung 3 (Join-Integrität): ✅ 0 verwaiste IDs, 1 Duplikat (minimal)
- Prüfung 4 (Score-Gap): ✅ router-v2 88.9%, bipartite 270%, composer 340%
- Prüfung 5 (EBBO): ✅ Keine Ablehnungen
- Prüfung 6 (NULL-Scores): ✅ nur naive=NULL (by design)
- Prüfung 8 (Composer-Fix): ✅ alle Composer >= beste Einzel-Strategie

Economics (12h Fenster, ETH=€1700): **€4.947/Mo NET — G6 PASS**

---

## Priority 1 (neu) — 24h Daten für stabile Projektion
- Prüfung 8 (Composer-Fix): alle Composer-Scores >= beste Einzel-Strategie

---

## Priority 2 — 24h Daten sammeln (passiv)

Nach dem Router-Fix läuft der Solver sauber. Nächste Analyse nach 24h:

```bash
docker exec cow-solver python -m scripts.verify_24h --hours 24
docker exec cow-solver python -m scripts.estimate_economics --hours 24 \
  --eth-price-eur 2700 --server-cost-eur 60
```

Erwartung: G6 PASS stabil, Score-Gap ~90%, router-v2 solve-rate 40–60%.

---

## Priority 3 — Branch mergen

`claude/cow-solver-review-j7I9q` hat nur 1 Commit auf main (validate_data.py ROUND-Fix).
Kein Konflikt mehr. Nach Priority 1 bestätigen und mergen.

---

## Priority 4 — cow-driver RPC-Fix (auf Hetzner)

cow-driver nutzt noch Alchemy-URL → 429-Spam in Logs. Fix: PublicNode eintragen.

```bash
# Prüfen welche URL der driver nutzt:
docker exec cow-driver env | grep -i rpc
# Fix: in /opt/mhagentic/stack/.env den driver-RPC auf PublicNode setzen,
# dann: docker compose -f docker-compose.prod.yml up -d cow-driver
```

---

## Priority 5 — Bucket-4-Gap fixen (mittelfristig)

Bucket 4 ("groß", 0.01–0.1 ETH) = 76% des Surplus-Volumens, 0% Win-Rate.

Optionen:
1. `intermediate_tokens` erweitern: WETH + USDC + USDT + WBTC statt nur WETH+USDC
2. Sort-Key ändern: `headroom × log(eth_value)` statt reinem Headroom-Sort
3. `ROUTER_MAX_ORDERS` auf 6 erhöhen (testen ob PublicNode noch stabil)

Umsetzung: Spec nötig bevor Implementierung.

---

## Priority 6 — Alchemy-Key rotieren

Key erschien in Chat-Logs. Rotieren unter: dashboard.alchemy.com → Apps → API Keys → Rotate.
Dann in `/opt/mhagentic/stack/.env` ersetzen und Container neu starten.

---

## Priority 7 — CoW Discord Bewerbung (#become-a-solver)

Nach 24h sauberer Daten + G6 PASS:
- `verify_24h --hours 24` Screenshot
- `estimate_economics --hours 24` Screenshot
- Score-Gap % und Win-Rate als Kennzahlen
- Discord: `#become-a-solver` mit Shadow-Performance-Daten

**Vorher sicherstellen:** Caddy/nginx-Endpunkt für `{base_url}/shadow/arbitrum-one` aufsetzen (CoW-Protokoll erwartet dieses URL-Format für die Whitelist-Bewerbung).

---

## Offene Risiken

1. **cow-driver idle** — 403 vom Autopilot (`/auction` braucht CoW-Whitelist), 429 vom Driver (Alchemy). Beide Container laufen aber sind funktional inaktiv. Optional: `docker compose stop cow-autopilot cow-driver`
2. **Alchemy-Key** — im Chat-Log erschienen, sollte rotiert werden
3. **Automated Deploy** — `HETZNER_SSH_KEY` GHA-Secret ist passphrase-geschützt → CI-Deploys scheitern. Alle Deploys manuell via SSH.

---

## Current State (quick reference)

- **Router-v2**: ✅ 45–57% solve-rate post-fix (war 0%)
- **Bipartite**: ✅ 12h-Daten sauber
- **Composer**: ✅ CIP-14-basiertes Ranking aktiv
- **validate_data**: ✅ Prüfungen 1+2 grün; Prüfungen 3–8 noch ausstehend
- **Phantom-Score**: ✅ naive=NULL, Score-Guard in persist.py
- **Ghost-Detection**: ✅ läuft seit 2026-05-26
- **EBBO**: ✅ sell+buy validiert
- **G6**: PASS (8h-Fenster, ~€146/Mo NET point estimate)
