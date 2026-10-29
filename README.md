# layer0-gate-data

Automated fetch of ten public macro/market series, published as `data/gate_state.json` for a personal regime-monitoring dashboard. Public data only — nothing here is proprietary, and no brokerage, account or portfolio information is stored in this repository.

## Series

| id | source | fallback |
|---|---|---|
| hy_oas | FRED `BAMLH0A0HYM2` (ICE BofA US High Yield OAS) | — |
| move | Yahoo Finance `^MOVE` | — |
| nfci | FRED `NFCI` (Chicago Fed National Financial Conditions Index) | Chicago Fed CSV |
| dgs30 | FRED `DGS30` (30-year Treasury constant maturity) | Federal Reserve H.15 |
| claims | FRED `IC4WSA` (initial claims, 4-week MA) | — |
| aaii | AAII Investor Sentiment Survey (bullish − bearish, %) | — |
| vix_term | CBOE `VIX9D` / `VIX` daily closes (ratio) | Yahoo Finance |
| vvix | CBOE `VVIX` daily close | Yahoo Finance |
| usdjpy | Yahoo Finance `JPY=X` | — |
| acm_tp | NY Fed ACM 10-year term premium (`ACMTP10`) | — |

## Behaviour

- Runs twice daily (GitHub Actions, see `.github/workflows/gate.yml`) and commits `data/gate_state.json` only when it changes.
- A series whose fetch fails keeps its previous readings and is listed in `failed_series`; a fallback source may not overwrite fresher data already held.
- `FRED_API_KEY` (optional repository secret) switches the four FRED series to the FRED API; without it `fredgraph.csv` is used.

## Format

```json
{
  "version": "pipeline-1.0",
  "generated_at": "2026-10-29T22:40:00Z",
  "readings": { "<id>": [ { "date": "YYYY-MM-DD", "value": 0.0 } ] },
  "meta": { "<id>": { "asof": "YYYY-MM-DD", "stale_days": 0, "cadence": "daily|weekly", "source": "…", "n": 0 } },
  "failed_series": []
}
```

Data remain the property of their respective publishers; see each source's terms.
