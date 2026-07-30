# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Polymarket weather-market trading bot. It compares forecast data (ECMWF/HRRR/METAR via Open-Meteo and Aviation Weather, no keys required) against Polymarket "highest temperature in {city} on {date}" markets, computes EV/Kelly sizing, and paper-trades. No framework, no SDK — plain scripts run directly with `python`. There is no build step, linter config, or test suite in this repo.

**Real money** runs through a separate, small path: `live_trade_city.py` (parameterized per operation) + `live_trade_common.py` (shared exit engine) + `supervisor.py`. Everything else in the repo is paper.

## State as of 2026-07-28 (repo audit + cleanup)

Read this before trusting any performance number in here:

- **`favoritos_bot.py`'s +61% was an artifact and is now gated.** Two trades produced it: Milan 32C at $0.0005 ($20 = 40,000 shares, +$220) and Hong Kong 30C at $0.0010 (20,000 shares, +$140). Without them the strategy is −$238. Neither is executable — `shares = BET_PER_LEG / real_ask` assumed a full fill without checking depth. `MIN_ENTRY_PRICE`, `MIN_VOLUME` and `MAX_SHARES_POR_PATA` now block it, so pre-2026-07-28 results are **not comparable** to anything after.
- **Real-money P&L to date: −$11.79** (Munich +$3.88, Ankara −$4.40, Munich26 −$11.27, Helsinki26 $0). Closed states archived under `data/archive_real/`.
- Deleted in the cleanup: `bot_v1.py`, `chengdu_36c_watch.py`, `chengdu_afternoon_report.py`, `hong_kong_position_watch.py`, `wallet_watch_helsinki.py`, `live_trade_munich26.py`, `live_trade_helsinki26.py`, `report_munich26.py` — all hardcoded to dates/markets that had already resolved. The per-city live-trade scripts were replaced by the generic `live_trade_city.py`.

## State as of 2026-07-30 (PASSPORT web + calibration fix + IEM pipeline)

- **`bot_v2.py` calibration is fixed and running.** It was permanently stuck at `{}` (`CALIBRATION_MIN=30` per city+source, but no city ever had 30 resolved samples of its own, and market files closed via stop-loss/TP never even called `get_actual_temp` to begin with). Fixed by (1) backfilling `actual_temp` on every past market file, (2) calling `get_actual_temp` for any market missing it during `scan_and_update`'s auto-resolution pass regardless of how it closed, and (3) a **pooled fallback** in `get_sigma`/`get_bias` (`_pooled_{unit}_{source}` keys in `data/calibration.json`, `POOLED_MIN=40` samples) so cities without their own 30 still get a real sigma instead of the old hardcoded `SIGMA_F=2.0`/`SIGMA_C=1.2` guess. Measured result: ECMWF was running **51% overconfident on °F cities and 24% on °C cities**, plus a **+1.9°F warm bias** on US cities — this is why EV numbers looked inflated project-wide, not just in the cheap-bucket cases below.
- **A second, parallel analysis layer exists now: PASSPORT**, a private web dashboard (published via the `Artifact` tool, not part of the repo's own file-serving) backed by new scripts below. It duplicates none of `bot_v2`'s trading logic — it's read-only analysis/monitoring, no orders placed.
- **The EV-inflation trap recurs anywhere a bucket price is very low** (same shape as the favoritos_bot bug above) — found again in PASSPORT's own `mejor_bucket` picker (Ankara 26C at $0.055 showed +387% EV) and fixed the same way: a `confianza` tier (`escalable` ≥$0.20 / `moderado` $0.10–0.20 / `cautela` <$0.10) that both the web and `husky_daily_digest.py` sort and label by, instead of just filtering a hard floor. The tiers themselves come from an audit of 1,680 real trades across the watched wallets (2026-07-28): $0.20–$0.40 is the only band with both a real hit rate (37%) and real ROI (+30%) at scale ($25.5K); anything under $0.10 traded like a lottery in that data (+200% "ROI" on ~$3 average tickets).

### PASSPORT — the web dashboard and its pipeline

One-time historical extraction (already done, frozen, no ongoing dependency):
- `data/husky/passport/{ICAO}.json`, `data/husky/edge/{ICAO}.json`, `data/husky/consolidated_full.json`, `data/husky/stations_index.json` — pulled 2026-07-29 from a **paid third-party service** (huskyweather.com) via the user's own session token (manual "copy as cURL" from browser devtools; the token is never stored in any file). Covers 50 stations × ~1,257 days each (2023-01-01 → 2026-07-04), season/hour/wind-conditioned heating curves. **Not gitignored data but also never referenced by name in any script comment beyond this file** — the user asked the brand not to appear in the published web page.
- **Cancelling that subscription breaks nothing already built** — every script here only ever reads the already-saved local JSON, none of them call the paid API live. It only matters if the historical baseline needs *updating* later (new season data) — see the IEM pipeline below for the free alternative for that.
- One known bad station code from the paid source: Istanbul is listed as `LTMF` (their typo, letters transposed) — the real ICAO code is `LTFM`. Handled via `HISTORICAL_STATION_OVERRIDE` in `husky_live_snapshot.py` (live METAR/model fetch uses `LTFM`, historical-curve lookup uses `LTMF`) rather than fixing the source file.
- **Independent, free replacement pipeline**: `iem_download.py` pulls raw METAR (temp/wind/sky, `report_type=3,4` for both routine+special reports) for all 50 stations from IEM (Iowa Environmental Mesonet, Iowa State University — public, no key, confirmed reliable; same underlying ASOS/AWOS network as `aviationweather.gov`) into `data/iem_raw/{ICAO}.csv`. `iem_build_climatology.py` replicates the paid service's own season/hour/wind aggregation from that raw data into `data/iem/consolidated_full.json`, same schema — cross-validated against the paid data across 5 stations incl. one southern-hemisphere case (Buenos Aires), all within a degree or two. **Not yet wired into the live page** (user chose to keep using the paid-sourced data for now); this is the fallback if the subscription lapses or the data needs refreshing.

Live layer (runs on demand or via the scheduled task, re-fetches every time):
- **`husky_live_snapshot.py`** — for all 51 stations (the 30 `bot_v2.LOCATIONS` cities + 21 extra Husky-only stations monkey-patched into `bot_v2.LOCATIONS`/`TIMEZONES` at import time, same regional-model-per-region convention as `bot_v2.LOCATIONS` itself), computes three independent peak-temperature estimates: **METAR now**, **model** (ECMWF+regional blend, calibrated, same math as `bot_v2.take_forecast_snapshot`), **empírico** (METAR now + the historical heating-curve's expected remaining rise for the current local hour/wind, via `husky_query.consultar`) — averaged into **`peak_final`**. Also computes `mejor_bucket`: the best Polymarket bucket by EV/Kelly using `peak_final`, gated by the `confianza` tiers above. Writes `data/live_peak_snapshot.json` (overwritten each run) and **appends** one record per city+market-date to `data/live_predictions_log.json` (deduplicated, never overwritten) for track-record purposes.
- **`husky_query.py`** — pure read layer over the historical passport JSON; `consultar(station, hour, wind_dir, trend, mes)` does hemisphere-correct season assignment + a fallback chain (exact wind+trend → wind-only → trend-only → unconditioned "any" → nearest available hour) and reports which level of specificity it actually matched.
- **`husky_daily_digest.py`** — one consolidated Telegram message from the current snapshot (replaces what would otherwise be N separate bot reports), sorted by confidence tier then Kelly, with a spread-disagreement warning (`SPREAD_ALERTA=3.0°`) when model and empírico disagree a lot (found via São Paulo: model said 30.0°C, empírico said 19.2°C, blended `peak_final` happened to land just under a bucket edge and produced a misleadingly clean-looking +501% EV pick).
- **`husky_check_resolutions.py`** — closes the loop: for each pending record in `live_predictions_log.json` past its market's 20h-after-resolution window, checks Polymarket for the actual winning bucket + `bot_v2.get_actual_temp`, and records `forecast_error` (peak_final vs actual) and `pick_won`. This is what the web's "Track record" panel reads. **Must import the same 21-extra-city patch as `husky_live_snapshot.py`** — it runs as a separate subprocess (not an import), so without its own copy of the patch, `bot_v2.LOCATIONS[city_slug]` raises `KeyError` for any extra-city record and — before 2026-07-30 — that exception had no per-record `try/except`, so it silently killed resolution-checking for *all* 51 cities every cycle once the first extra-city prediction aged past the 20h mark, and never saved partial progress. Fixed: patch + per-record `try/except` (failures just retry next cycle).
- **`husky_scheduled_run.py`** — chains `husky_live_snapshot.py` → `husky_daily_digest.py` → `husky_check_resolutions.py`, logs to `data/husky_scheduled_run.log`. Run by two Windows Scheduled Tasks, `HuskyDigestAM` (10:00) and `HuskyDigestPM` (16:00), both `schtasks /sc DAILY`, both "run only when logged on" (won't fire if the laptop is off/locked) — same limitation as `WeatherbotSupervisor`. `start_husky_digest.bat` is the launcher, mirrors `start_supervisor.bat`'s pattern.
- **The web page itself** is a single self-contained HTML file (`scripts_tmp/husky_passport.html`, built by `scripts_tmp/build_passport.py` from `scripts_tmp/passport_template.html` + the JSON data files above, all embedded inline — no external requests, works offline) published via the `Artifact` tool. **It cannot fetch anything live on its own** — Artifact pages only get network access through explicitly declared capabilities, and only `downloads`/`mcp` are available to this user, neither of which does arbitrary HTTP — so "refresh" always means: rerun `husky_live_snapshot.py`, rerun `build_passport.py`, republish to the same URL. The "última lectura: hace X min" text and the per-station live clock do self-update client-side (the clock is real, the "last fetched" age is not the same as live data).

### Known regional-model choices for the 21 extra (untracked) stations

Same convention as `bot_v2.LOCATIONS`'s own per-region logic (see `data/husky/extra_locations.json`): `gfs_seamless` for the two extra US cities (Austin, San Francisco), `icon_eu` for extra Europe (Helsinki, Milan, Moscow, Istanbul, Paris–Le Bourget uses `meteofrance_arome_france_hd` like our own Paris instead), `icon_seamless` for extra China (Chongqing, Guangzhou, Shenzhen, Wuhan, Qingdao) and Hong Kong, `jma_gsm` for Busan (Korea, same as Seoul) — `None` (no defensible regional edge) for Cape Town, Jeddah, Karachi, Panama City, Manila, Jakarta, Mexico City. These are extension choices, not individually verified against each market's resolution text the way the original 30's `station` fields were.

Paris has two distinct station entries on purpose: our operated one resolves via `LFPG` (Charles de Gaulle); the extra historical-only one is `LFPB` (Le Bourget) — genuinely different airports, not a typo, so `SIN_HISTORICO_COMPATIBLE` skips cross-referencing the historical curve for our operated Paris rather than silently mixing them.

## Running the scripts

Install deps: `pip install requests matplotlib` (matplotlib + `zoneinfo` are only needed by the per-city dashboard scripts below).

Main bot (`bot_v2.py` — the current/full version; README calls it `weatherbet.py` but the file on disk is `bot_v2.py`):
```
python bot_v2.py           # main loop: full scan hourly, position monitoring every 10 min
python bot_v2.py status    # balance + open positions
python bot_v2.py report    # full breakdown of resolved markets, by city
```

Standalone per-city monitors (each independently pollable, each writes its own dashboard PNG to `data/images/` and saves a snapshot for `dashboard_combinado.py` — none of the 6 push their individual PNG to Telegram anymore, see below):
```
python check_chengdu.py            # Chengdu (ZUUU) ECMWF+ICON dashboard/tracking loop
python check_helsinki.py           # Helsinki (EFHK) ICON-EU+UKMO+ECMWF dashboard/tracking loop
python chengdu_early_entry.py      # watches for brand-new Chengdu markets, simulates early entry (no real orders)
python forecast_10day.py           # Chengdu 10-day ECMWF vs ICON projection image
python analyze_ecmwf_accuracy.py   # Chengdu ECMWF forecast-vs-actual accuracy report/chart
python wallet_watch_global.py      # polls a specific wallet's Polymarket activity, alerts on weather-market trades
python dashboard_combinado.py      # combined 2x3 grid PNG for the 6 check_*.py cities, reads their snapshot JSONs only
```

Real-money path:
```
python live_trade_city.py plan     # print the configured plan without touching anything — ALWAYS start here
python live_trade_city.py status   # current position per bucket
python live_trade_city.py once     # one cycle
python live_trade_city.py run      # permanent loop (what supervisor.py launches)
python supervisor.py status        # what's up / down, no restarts
```
`live_trade_city.py` holds one operation at a time in its CONFIG block (city, date, per-bucket `TOKENS`, `PLAN` of price levels and budgets). `validar_config()` refuses to place orders on a half-configured plan. Buys drip in small self-replacing limit orders; exits use `live_trade_common.check_tiered_exit` (stop-loss −50%, TP1 +200% sells 30% of original, TP2 +400% sells another 30%, remaining 40% runs free). Every sell goes through a price floor **and** is clipped to real book depth (`sell_partial`) — the Ankara 2026-07-25 bug where an unfloored sell swept the book and turned +164% into a realized loss.

`supervisor.py`'s `PROCESSES` list must contain **only live operations** — it is launched at boot by the Windows Scheduled Task `WeatherbotSupervisor` → `start_supervisor.bat`, so anything listed there revives itself unattended. When an operation ends, remove its entry the same day (don't comment it out). A process that dies on startup is retried at most `MAX_REINTENTOS` (5) times before the supervisor gives up and says so once, instead of restart-looping forever.

PASSPORT web dashboard (see "State as of 2026-07-30" above for the full architecture):
```
python husky_live_snapshot.py            # all 51 stations, writes data/live_peak_snapshot.json + appends to the predictions log
python husky_live_snapshot.py LTAC       # one station only, by ICAO code — for quick testing
python husky_daily_digest.py print       # preview the Telegram digest without sending
python husky_daily_digest.py             # send it
python husky_check_resolutions.py        # grade pending predictions whose market has resolved
python husky_scheduled_run.py            # chains all three — what the two scheduled tasks below actually run
python scripts_tmp/build_passport.py     # rebuild scripts_tmp/husky_passport.html from the template + current data
python iem_download.py                   # (rarely needed) refresh the free IEM raw METAR archive, all 50 stations
python iem_build_climatology.py          # rebuild data/iem/consolidated_full.json from the IEM raw data
```
Two Windows Scheduled Tasks run `husky_scheduled_run.py` unattended: `HuskyDigestAM` (10:00) and `HuskyDigestPM` (16:00), via `start_husky_digest.bat`. Same "run only when logged on" caveat as `WeatherbotSupervisor`. Rebuilding+republishing the web page itself is **not** part of that automation (Artifact publish can only be called from an active Claude session) — do it by hand (or ask) after a scheduled run if you want the page caught up.

All scripts must be run from the repo root — they load `config.json` and write under `data/` using relative paths.

There's no test suite; the closest thing to verification is running a script's one-shot invocations (e.g. `status`/`report`) and inspecting output/dashboard images, or comparing against the JSON files under `data/`.

## Config

`config.json` at the repo root drives every script (loaded at import time — a missing file crashes immediately). It holds live secrets (`vc_key`, `telegram_token`, the `telegram_chat_*` ids) and **is gitignored** (`.gitignore:210`), as is `secrets_trading.json` (`.gitignore:213`), which holds the **live-trading wallet private key**. Never print either into commit messages, PR bodies, or anything that leaves the machine.

Historical note: `config.json` *was* tracked in 5 commits before `c43ebee` removed it, so old secrets sit in the history of the public repo `github.com/alteregoeth-ai/weatherbot`. Verified 2026-07-28 that the current `vc_key` and `telegram_token` differ from those — the exposed ones are dead. `secrets_trading.json` was never committed.

Key fields: `balance`, `max_bet`, `min_ev`, `max_price`, `min_volume`, `min_hours`/`max_hours` (time-to-resolution window), `kelly_fraction`, `max_slippage`, `scan_interval`, `calibration_min`.

## Architecture (bot_v2.py)

- **`LOCATIONS`** — the single source of truth for the 30 tracked cities: lat/lon, resolving METAR **airport station** (not city center — see README's "Why Airport Coordinates Matter"; verified against each market's actual resolution text, not assumed — e.g. Houston resolves via Hobby/KHOU not Bush Intercontinental, Denver via Buckley SFB/KBKF not DEN), unit (F for US cities, C elsewhere), region (US-only gates the short-range HRRR-blend window, and is used for the correlated-risk grouping in `REGIONES`), and `regional_model` — the Open-Meteo model id with real regional skill for that specific city (UKMO for London, AROME for Paris, ICON-D2 for Munich, ICON-EU for the rest of Europe, JMA-GSM for Tokyo/Seoul/Taipei, GEM for Toronto, ICON-seamless for the Chinese cities, GFS-seamless/HRRR-blend for US cities), or `None` where no model has a defensible regional edge over plain ECMWF (Singapore, Kuala Lumpur, Lucknow, Tel Aviv, Sao Paulo, Buenos Aires, Wellington). `TIMEZONES` is a parallel dict keyed the same way.
- **Forecast fetch layer** — `get_ecmwf` (Open-Meteo, all cities, bias-corrected), `get_regional` (Open-Meteo, per-city model from `LOCATIONS["regional_model"]`, `{}` if `None`; US cities keep the original D+0/D+1-only window, other regional models are valid for the full multi-day window like ECMWF), `get_metar` (Aviation Weather, real-time, D+0 only), `get_actual_temp` (Visual Crossing, post-resolution ground truth). Each retries transient failures and degrades to `None` rather than raising.
- **`take_forecast_snapshot`** picks a `best`/`best_source` forecast per date: the city's regional model wins when available (and calibrated inverse-variance-blended with ECMWF once enough resolved samples exist), otherwise plain ECMWF.
- **Polymarket layer** — `get_polymarket_event` resolves a city+date to a Gamma API event via a predictable slug (`highest-temperature-in-{city}-on-{month}-{day}-{year}`); `parse_temp_range` regexes the bucket range out of each market's question text (handles "X or below", "X or higher", "between X-Y", "be X on"); `check_market_resolved` polls for close + winning side.
- **Math** — `bucket_prob` (exact match for closed buckets, normal-CDF tail probability for open-ended edge buckets using per-city/source `sigma`), `calc_ev`, `calc_kelly` (fractional Kelly via `KELLY_FRACTION`), `bet_size` (Kelly-sized, capped at `MAX_BET`).
- **Calibration** (`run_calibration`) — recomputes `sigma`/`bias` per city+source from resolved markets' forecast-vs-actual error once `CALIBRATION_MIN` (10, lowered from 30 — see "State as of 2026-07-30") resolved samples exist for that city+source; below that, `get_sigma`/`get_bias` fall back to a **pooled** `_pooled_{unit}_{source}` estimate (`POOLED_MIN=40` combined samples across all cities of that unit) rather than the old hardcoded `SIGMA_F=2.0`/`SIGMA_C=1.2` guess. Persisted to `data/calibration.json`, re-loaded into the module-level `_cal` dict each run.
- **`scan_and_update`** is the core cycle, run hourly: for every city × next-4-days, load-or-create a market record, refresh outcome/price snapshots, then in order: check stop-loss/trailing-stop on any open position, close it immediately if today's actual observed temp (`get_actual_temp`, same WU/Visual Crossing source used for post-resolution ground truth) has already cleared the bucket's upper edge by more than a buffer — the daily max only rises, so this is a mathematically-certain loss regardless of forecast/price (ported from `favoritos_bot.py`'s `INVALIDACION_BUFFER`, D+0 only), close the position if the forecast has moved out of its bucket by more than a buffer, otherwise consider opening a new position (single matching bucket only — ambiguous forecasts are skipped), re-validate the fill against Polymarket's real bestAsk/bestBid before committing (slippage re-check), then auto-resolve any market Polymarket has closed.
- **`monitor_positions`** runs every 10 minutes between full scans — cheap stop-loss/take-profit check without re-fetching forecasts. Take-profit is a **flat `entry * 1.55`**; an earlier version of this file described a threshold that tightened with time-to-resolution (no TP under 24h, 0.85 at 24-48h, 0.75 beyond) — that logic does not exist in the code. `hours_left` is computed at `bot_v2.py:1593` and only ever printed.
- **`run_loop`** ties it together: full `scan_and_update` every `SCAN_INTERVAL` (default hourly), `monitor_positions` every `MONITOR_INTERVAL` (10 min) otherwise; reconnect-and-continue on `ConnectionError`.

## Data storage

Everything lives under `data/` (gitignored is NOT set for this — check before assuming it's excluded):
- `data/markets/{city}_{date}.json` — one file per market: forecast snapshots, market price snapshots, position lifecycle, resolution outcome. This is the append-only history `run_calibration` and the report scripts read back.
- `data/state.json` — balance, trade counts, peak balance (paper-trading ledger for `bot_v2.py`).
- `data/calibration.json` — per-city/source sigma values.
- `data/chengdu_readings.json`, `data/chengdu_tracking.json`, `data/chengdu_stats.json`, `data/helsinki_readings.json`, etc. — state for the standalone per-city monitor scripts, independent of `bot_v2.py`'s own market files.
- `data/images/` — dashboard PNGs generated by the monitor scripts (matplotlib, `Agg` backend) and pushed to Telegram.

## Notes on the standalone monitor scripts

`check_chengdu.py`, `check_helsinki.py`, `chengdu_early_entry.py`, `forecast_10day.py`, `analyze_ecmwf_accuracy.py`, and the `wallet_watch_*.py` scripts are self-contained, city-specific tools layered on top of (not imported by) `bot_v2.py` — they duplicate small amounts of logic (norm_cdf, Telegram sending, temp-range parsing) rather than sharing a module. Several are partly commented/documented in Spanish. `chengdu_early_entry.py` explicitly does not place real trades — it only logs and alerts on simulated early entries. The `wallet_watch_*` scripts are read-only observers of a third-party wallet's public Polymarket activity, not related to this bot's own trading.

Each of the 6 `check_{city}.py` scripts (Buenos Aires, Helsinki, Hong Kong, Milan, New York, Chengdu) still renders its own dashboard PNG locally and writes a small `data/dashboard_snapshot_{city}.json` at the end of its own `run_check()` — `{city, brand, date, updated_at, updated_at_utc, unit, real_temp, wind_dir, sky, models}` — but no longer sends that PNG to Telegram itself (the `send_telegram_photo(dash_path, ...)` call was removed from each). `dashboard_combinado.py` is a separate, independent script that reads those 6 snapshot files (no network calls of its own besides the Telegram send) and renders one 2x3 grid PNG summarizing all 6 cities, which is now the only per-cycle image sent to `telegram_chat_dashboards`. The 6 scripts' plain-text Telegram summaries (via `send_telegram`, not `send_telegram_photo`) are untouched.
