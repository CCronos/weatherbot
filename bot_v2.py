#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weatherbet.py — Weather Trading Bot for Polymarket
=====================================================
Tracks weather forecasts from 3 sources (ECMWF, a per-city regional model, METAR),
compares with Polymarket markets, paper trades using Kelly criterion.

Usage:
    python weatherbet.py          # main loop
    python weatherbet.py report   # full report
    python weatherbet.py status   # balance and open positions
"""

import re
import sys
import json
import math
import time
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Windows consoles often default to a legacy codepage (cp1252) that can't encode
# the em-dashes/symbols used in these prints — force UTF-8 so a print() never
# crashes the whole scan/loop mid-cycle.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# =============================================================================
# CONFIG
# =============================================================================

with open("config.json", encoding="utf-8") as f:
    _cfg = json.load(f)

BALANCE          = _cfg.get("balance", 10000.0)
MAX_BET          = _cfg.get("max_bet", 20.0)        # max bet per trade
MIN_EV           = _cfg.get("min_ev", 0.10)
MAX_PRICE        = _cfg.get("max_price", 0.45)
MIN_VOLUME       = _cfg.get("min_volume", 500)
MIN_HOURS        = _cfg.get("min_hours", 2.0)
MAX_HOURS        = _cfg.get("max_hours", 72.0)
KELLY_FRACTION   = _cfg.get("kelly_fraction", 0.25)
MAX_SLIPPAGE     = _cfg.get("max_slippage", 0.03)  # max allowed ask-bid spread
SCAN_INTERVAL    = _cfg.get("scan_interval", 3600)   # every hour
CALIBRATION_MIN  = _cfg.get("calibration_min", 10)  # muestras por CIUDAD+fuente. Era 30, pero
                                   # con ~15 fechas de historia por ciudad ninguna llegaba nunca:
                                   # la calibracion jamas corrio y data/calibration.json quedo en
                                   # {} (verificado 2026-07-28). 10 es ruidoso pero real; por
                                   # debajo de eso manda la sigma agrupada, ver get_sigma().
POOLED_MIN       = 40              # muestras minimas para la sigma agrupada por unidad+fuente
FRENO_EMERGENCIA_DRAWDOWN = 0.50  # pausar compras nuevas si el equity cae -50% desde
                                   # el capital inicial (mismo umbral que favoritos_bot.py)
VC_KEY           = _cfg.get("vc_key", "")
TELEGRAM_TOKEN   = _cfg.get("telegram_token", "")
TELEGRAM_CHAT_ID = _cfg.get("telegram_chat_bot_principal") or _cfg.get("telegram_chat_id", "")

SIGMA_F = 2.0
SIGMA_C = 1.2

DATA_DIR         = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE       = DATA_DIR / "state.json"
MARKETS_DIR      = DATA_DIR / "markets"
MARKETS_DIR.mkdir(exist_ok=True)
CALIBRATION_FILE = DATA_DIR / "calibration.json"

def atomic_write(path, text):
    """Write via a temp file + replace so a crash mid-write can't corrupt the target JSON."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)

# "regional_model" — id de modelo de Open-Meteo con mejor cobertura regional que el
# ECMWF global para esa ciudad especifica, o None si no hay alternativa defendible
# (mismo criterio ya usado en los scripts standalone de ciudad: "sin alternativa
# regional fuerte -> usar las opciones globales", no se inventa una tabla sin base
# real). Reemplaza el viejo esquema de "HRRR solo para EEUU" — ver get_regional().
LOCATIONS = {
    "nyc":          {"lat": 40.7772,  "lon":  -73.8726, "name": "New York City", "station": "KLGA", "unit": "F", "region": "us",   "regional_model": "gfs_seamless"},
    "chicago":      {"lat": 41.9742,  "lon":  -87.9073, "name": "Chicago",       "station": "KORD", "unit": "F", "region": "us",   "regional_model": "gfs_seamless"},
    "miami":        {"lat": 25.7959,  "lon":  -80.2870, "name": "Miami",         "station": "KMIA", "unit": "F", "region": "us",   "regional_model": "gfs_seamless"},
    "dallas":       {"lat": 32.8471,  "lon":  -96.8518, "name": "Dallas",        "station": "KDAL", "unit": "F", "region": "us",   "regional_model": "gfs_seamless"},
    "seattle":      {"lat": 47.4502,  "lon": -122.3088, "name": "Seattle",       "station": "KSEA", "unit": "F", "region": "us",   "regional_model": "gfs_seamless"},
    "atlanta":      {"lat": 33.6407,  "lon":  -84.4277, "name": "Atlanta",       "station": "KATL", "unit": "F", "region": "us",   "regional_model": "gfs_seamless"},
    "houston":      {"lat": 29.6454,  "lon":  -95.2789, "name": "Houston",       "station": "KHOU", "unit": "F", "region": "us",   "regional_model": "gfs_seamless"},
    "los-angeles":  {"lat": 33.9425,  "lon": -118.4081, "name": "Los Angeles",   "station": "KLAX", "unit": "F", "region": "us",   "regional_model": "gfs_seamless"},
    "denver":       {"lat": 39.7017,  "lon": -104.7514, "name": "Denver",        "station": "KBKF", "unit": "F", "region": "us",   "regional_model": "gfs_seamless"},
    "london":       {"lat": 51.5048,  "lon":    0.0495, "name": "London",        "station": "EGLC", "unit": "C", "region": "eu",   "regional_model": "ukmo_seamless"},
    "paris":        {"lat": 48.9962,  "lon":    2.5979, "name": "Paris",         "station": "LFPG", "unit": "C", "region": "eu",   "regional_model": "meteofrance_arome_france_hd"},
    "munich":       {"lat": 48.3537,  "lon":   11.7750, "name": "Munich",        "station": "EDDM", "unit": "C", "region": "eu",   "regional_model": "icon_d2"},
    "ankara":       {"lat": 40.1281,  "lon":   32.9951, "name": "Ankara",        "station": "LTAC", "unit": "C", "region": "eu",   "regional_model": "icon_eu"},
    "madrid":       {"lat": 40.4936,  "lon":   -3.5668, "name": "Madrid",        "station": "LEMD", "unit": "C", "region": "eu",   "regional_model": "icon_eu"},
    "amsterdam":    {"lat": 52.3086,  "lon":    4.7639, "name": "Amsterdam",     "station": "EHAM", "unit": "C", "region": "eu",   "regional_model": "icon_eu"},
    "warsaw":       {"lat": 52.1657,  "lon":   20.9671, "name": "Warsaw",        "station": "EPWA", "unit": "C", "region": "eu",   "regional_model": "icon_eu"},
    "seoul":        {"lat": 37.4691,  "lon":  126.4505, "name": "Seoul",         "station": "RKSI", "unit": "C", "region": "asia", "regional_model": "jma_gsm"},
    "tokyo":        {"lat": 35.7647,  "lon":  140.3864, "name": "Tokyo",         "station": "RJTT", "unit": "C", "region": "asia", "regional_model": "jma_gsm"},
    "taipei":       {"lat": 25.0694,  "lon":  121.5522, "name": "Taipei",        "station": "RCSS", "unit": "C", "region": "asia", "regional_model": "jma_gsm"},
    "shanghai":     {"lat": 31.1443,  "lon":  121.8083, "name": "Shanghai",      "station": "ZSPD", "unit": "C", "region": "asia", "regional_model": "icon_seamless"},
    "chengdu":      {"lat": 30.5728,  "lon":  103.9469, "name": "Chengdu",       "station": "ZUUU", "unit": "C", "region": "asia", "regional_model": "icon_seamless"},
    "beijing":      {"lat": 40.0801,  "lon":  116.5846, "name": "Beijing",       "station": "ZBAA", "unit": "C", "region": "asia", "regional_model": "icon_seamless"},
    "singapore":    {"lat":  1.3502,  "lon":  103.9940, "name": "Singapore",     "station": "WSSS", "unit": "C", "region": "asia", "regional_model": None},
    "kuala-lumpur": {"lat":  2.7456,  "lon":  101.7099, "name": "Kuala Lumpur",  "station": "WMKK", "unit": "C", "region": "asia", "regional_model": None},
    "lucknow":      {"lat": 26.7606,  "lon":   80.8893, "name": "Lucknow",       "station": "VILK", "unit": "C", "region": "asia", "regional_model": None},
    "tel-aviv":     {"lat": 32.0114,  "lon":   34.8867, "name": "Tel Aviv",      "station": "LLBG", "unit": "C", "region": "asia", "regional_model": None},
    "toronto":      {"lat": 43.6772,  "lon":  -79.6306, "name": "Toronto",       "station": "CYYZ", "unit": "C", "region": "ca",   "regional_model": "gem_seamless"},
    "sao-paulo":    {"lat": -23.4356, "lon":  -46.4731, "name": "Sao Paulo",     "station": "SBGR", "unit": "C", "region": "sa",   "regional_model": None},
    "buenos-aires": {"lat": -34.8222, "lon":  -58.5358, "name": "Buenos Aires",  "station": "SAEZ", "unit": "C", "region": "sa",   "regional_model": None},
    "wellington":   {"lat": -41.3272, "lon":  174.8052, "name": "Wellington",    "station": "NZWN", "unit": "C", "region": "oc",   "regional_model": None},
}

# Agrupacion geografica AMPLIA — proxy simple para detectar riesgo correlacionado
# (varias posiciones abiertas a la vez que en realidad dependen del mismo patron
# climatico regional). No es deteccion real de sistema sinoptico (eso necesitaria
# datos de presion/geopotencial que hoy no consultamos) — es solo un punto de partida
# barato para ver si el portafolio esta mas concentrado geograficamente de lo que
# parece a simple vista.
REGIONES = {
    "US-Este":               ["nyc", "chicago", "miami", "atlanta"],
    "US-Oeste":               ["seattle", "dallas", "houston", "los-angeles", "denver"],
    "Europa":                 ["london", "paris", "munich", "madrid", "amsterdam", "warsaw"],
    "Asia-Este":               ["seoul", "tokyo", "shanghai", "beijing", "chengdu", "taipei"],
    "Asia-Sur":                ["singapore", "lucknow", "kuala-lumpur"],
    "Medio Oriente/Turquia":   ["ankara", "tel-aviv"],
    "Sudamerica":              ["sao-paulo", "buenos-aires"],
    "Oceania/Canada":          ["wellington", "toronto"],
}
CITY_TO_REGION = {c: r for r, cs in REGIONES.items() for c in cs}

TIMEZONES = {
    "nyc": "America/New_York", "chicago": "America/Chicago",
    "miami": "America/New_York", "dallas": "America/Chicago",
    "seattle": "America/Los_Angeles", "atlanta": "America/New_York",
    "london": "Europe/London", "paris": "Europe/Paris",
    "munich": "Europe/Berlin", "ankara": "Europe/Istanbul",
    "seoul": "Asia/Seoul", "tokyo": "Asia/Tokyo",
    "shanghai": "Asia/Shanghai", "singapore": "Asia/Singapore",
    "lucknow": "Asia/Kolkata", "tel-aviv": "Asia/Jerusalem",
    "toronto": "America/Toronto", "sao-paulo": "America/Sao_Paulo",
"buenos-aires": "America/Argentina/Buenos_Aires", "wellington": "Pacific/Auckland",
    "chengdu": "Asia/Shanghai", "beijing": "Asia/Shanghai",
    "houston": "America/Chicago", "los-angeles": "America/Los_Angeles", "denver": "America/Denver",
    "madrid": "Europe/Madrid", "amsterdam": "Europe/Amsterdam", "warsaw": "Europe/Warsaw",
    "taipei": "Asia/Taipei", "kuala-lumpur": "Asia/Kuala_Lumpur",
}

MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=(3, 5))
    except Exception as e:
        print(f"  [WARN] Telegram send failed: {e}")

# =============================================================================
# MATH
# =============================================================================

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bucket_prob(forecast, t_low, t_high, sigma=None):
    """Probability the actual temp lands in [t_low, t_high] under a normal forecast-error model."""
    s = sigma or 2.0
    f = float(forecast)
    if t_low == -999:
        return norm_cdf((t_high - f) / s)
    if t_high == 999:
        return 1.0 - norm_cdf((t_low - f) / s)
    # Point buckets ("be X on") resolve on rounding, i.e. an effective [X-0.5, X+0.5] window —
    # matches in_bucket()'s rounding rule.
    lo, hi = (t_low - 0.5, t_high + 0.5) if t_low == t_high else (t_low, t_high)
    return max(0.0, norm_cdf((hi - f) / s) - norm_cdf((lo - f) / s))

def calc_ev(p, price):
    if price <= 0 or price >= 1: return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)

def calc_kelly(p, price):
    if price <= 0 or price >= 1: return 0.0
    b = 1.0 / price - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * KELLY_FRACTION, 1.0), 4)

def bet_size(kelly, balance):
    raw = kelly * balance
    return round(min(raw, MAX_BET), 2)

# =============================================================================
# CALIBRATION
# =============================================================================

_cal: dict = {}

def load_cal():
    if not CALIBRATION_FILE.exists():
        return {}
    try:
        return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [WARN] {CALIBRATION_FILE.name} corrupto, se ignora: {e}")
        return {}

def get_sigma(city_slug, source="ecmwf"):
    """sigma de la ciudad si ya tiene suficientes muestras propias; si no, la sigma
    AGRUPADA de todas las ciudades de esa misma unidad+fuente; y recien en ultima
    instancia la constante inventada. El escalon del medio se agrego el 2026-07-28:
    antes solo existian "ciudad calibrada" o "constante a ojo", y como CALIBRATION_MIN
    era 30 muestras POR CIUDAD (con ~15 fechas de historia por ciudad, inalcanzable),
    en la practica el bot siempre uso la constante. Una sigma agrupada sobre cientos de
    observaciones reales es mucho mejor estimador que un 2.0/1.2 puesto a mano."""
    key = f"{city_slug}_{source}"
    if key in _cal:
        return _cal[key]["sigma"]
    unit = LOCATIONS[city_slug]["unit"]
    pooled = _cal.get(f"_pooled_{unit}_{source}")
    if pooled:
        return pooled["sigma"]
    return SIGMA_F if unit == "F" else SIGMA_C

def get_bias(city_slug, source="ecmwf"):
    key = f"{city_slug}_{source}"
    if key in _cal:
        return _cal[key].get("bias", 0.0)
    unit = LOCATIONS[city_slug]["unit"]
    return _cal.get(f"_pooled_{unit}_{source}", {}).get("bias", 0.0)

BIAS_DECAY = 0.25  # weight of the newest error in the running bias average — a model's
                    # directional miss can drift with the season, so a flat lifetime
                    # average would lag; recent misses need to count more.

def run_calibration(markets):
    """Recalculates sigma (spread) AND bias (systematic directional error) from resolved
    markets. Sigma alone tells you how wide the error usually is; bias tells you which
    way the model usually leans (e.g. GFS running cold in a given city) — without it,
    part of every "edge" computed later is really just the model's own known miss,
    not a real market mispricing. bias = forecast - actual, so a positive bias means
    the model runs warm and take_forecast_snapshot() should subtract it back out."""
    # Cualquier mercado con temperatura real conocida sirve para medir el error del
    # modelo, tenga o no posicion y sin importar por que se cerro. Antes se exigia
    # status=="resolved", que dejaba fuera a los "closed" (cerrados por tiempo, sin
    # posicion) — la mayoria del historial.
    resolved = [m for m in markets if m.get("actual_temp") is not None]
    cal = load_cal()
    updated = []

    # Muestras agrupadas por unidad+fuente, para la sigma de respaldo de las ciudades
    # que todavia no juntan historia propia (ver get_sigma).
    pool = {}

    for source in ["ecmwf", "regional", "metar"]:
        for city in set(m["city"] for m in resolved):
            if city not in LOCATIONS:
                continue   # ciudad retirada de LOCATIONS: sus archivos viejos siguen ahi
            group = sorted((m for m in resolved if m["city"] == city), key=lambda m: m.get("date", ""))
            errors, signed_errors, fechas = [], [], []
            for m in group:
                snap = next((s for s in reversed(m.get("forecast_snapshots", []))
                             if s.get(source) is not None), None)
                if snap:
                    errors.append(abs(snap[source] - m["actual_temp"]))
                    signed_errors.append(snap[source] - m["actual_temp"])
                    fechas.append(m.get("date", ""))

            unit = LOCATIONS[city]["unit"]
            agg = pool.setdefault(f"_pooled_{unit}_{source}", {"errors": [], "signed": []})
            agg["errors"].extend(errors)
            # Se guarda (fecha, error) y NO solo el error: el bias se calcula con una
            # media exponencial que pondera lo mas reciente, y si se concatenan las
            # ciudades una tras otra sin ordenar, "lo mas reciente" termina siendo
            # "la ultima ciudad del bucle" en vez de la fecha mas nueva. Con las
            # muestras a medias eso hacia saltar el bias de +1.90 a -2.15.
            agg["signed"].extend(zip(fechas, signed_errors))

            if len(errors) < CALIBRATION_MIN:
                continue
            mae  = sum(errors) / len(errors)
            bias = signed_errors[0]
            for e in signed_errors[1:]:
                bias = BIAS_DECAY * e + (1 - BIAS_DECAY) * bias
            key       = f"{city}_{source}"
            old_sigma = cal.get(key, {}).get("sigma", SIGMA_F if LOCATIONS[city]["unit"] == "F" else SIGMA_C)
            old_bias  = cal.get(key, {}).get("bias", 0.0)
            new_sigma = round(mae, 3)
            new_bias  = round(bias, 3)
            cal[key] = {
                "sigma": new_sigma, "bias": new_bias,
                "n": len(errors), "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            changes = []
            if abs(new_sigma - old_sigma) > 0.05:
                changes.append(f"sigma {old_sigma:.2f}->{new_sigma:.2f}")
            if abs(new_bias - old_bias) > 0.05:
                changes.append(f"bias {old_bias:+.2f}->{new_bias:+.2f}")
            if changes:
                updated.append(f"{LOCATIONS[city]['name']} {source}: " + ", ".join(changes))

    # Sigma/bias agrupados: junta TODAS las ciudades de la misma unidad y fuente. Con
    # ~15 fechas por ciudad ninguna llega sola a CALIBRATION_MIN, pero sumadas dan
    # cientos de observaciones reales — un estimador muy superior a la constante fija.
    for key, agg in pool.items():
        if len(agg["errors"]) < POOLED_MIN:
            continue
        # Media SIMPLE, no exponencial. El EWMA (BIAS_DECAY=0.25) tiene sentido por
        # ciudad, donde la serie es temporal y el sesgo estacional del modelo deriva:
        # ahi conviene que lo reciente pese mas. Agrupando varias ciudades no: con 133
        # muestras, (1-0.25)^133 ~ 0, o sea que el EWMA se queda mirando las ultimas ~10
        # observaciones — que ademas son "las ultimas ciudades del ultimo dia", no una
        # tendencia. El objetivo del agrupado es justo lo contrario, un centro estable.
        serie = [e for _, e in agg["signed"]]
        bias = sum(serie) / len(serie)
        cal[key] = {
            "sigma": round(sum(agg["errors"]) / len(agg["errors"]), 3),
            "bias": round(bias, 3),
            "n": len(agg["errors"]),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "nota": "agrupado por unidad+fuente — respaldo para ciudades sin historia propia",
        }
        updated.append(f"{key}: sigma {cal[key]['sigma']:.2f} bias {cal[key]['bias']:+.2f} (n={cal[key]['n']})")

    atomic_write(CALIBRATION_FILE, json.dumps(cal, indent=2))
    if updated:
        print(f"  [CAL] {', '.join(updated)}")
    return cal

# =============================================================================
# FORECASTS
# =============================================================================

def get_ecmwf(city_slug, dates):
    """ECMWF via Open-Meteo with bias correction. For all cities."""
    loc = LOCATIONS[city_slug]
    unit = loc["unit"]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
        f"&forecast_days=7&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models=ecmwf_ifs025&bias_correction=true"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" in data:
                raise RuntimeError(data.get("reason", "unknown API error"))
            for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                if date in dates and temp is not None:
                    result[date] = round(temp, 1) if unit == "C" else round(temp)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [ECMWF] {city_slug}: {e}")
    return result

def get_regional(city_slug, dates):
    """Second forecast source via Open-Meteo, using whichever model has real regional
    coverage for this specific city (see LOCATIONS["regional_model"]) — e.g. UKMO for
    London, AROME for Paris, ICON-D2 for Munich, JMA-GSM for Tokyo/Seoul/Taipei, GEM
    for Toronto, GFS-seamless (HRRR blend) for US cities, ICON-EU for the rest of
    Europe, ICON-seamless for the Chinese cities. None if no model has a defensible
    regional edge over plain ECMWF for that city (same 'no invent a table without a
    real regional alternative' rule the standalone per-city scripts already follow).
    US cities keep the original short-range HRRR-blend window (forecast_days=3,
    D+0/D+1 only via the cutoff in take_forecast_snapshot); the other regional models
    are full multi-day products like ECMWF, so they get the same forecast_days=7."""
    loc = LOCATIONS[city_slug]
    model = loc.get("regional_model")
    if not model:
        return {}
    unit = loc["unit"]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    forecast_days = 3 if loc["region"] == "us" else 7
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
        f"&forecast_days={forecast_days}&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models={model}"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" in data:
                raise RuntimeError(data.get("reason", "unknown API error"))
            for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                if date in dates and temp is not None:
                    result[date] = round(temp, 1) if unit == "C" else round(temp)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [REGIONAL:{model}] {city_slug}: {e}")
    return result

def get_metar(city_slug):
    """Current observed temperature from METAR station. D+0 only."""
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json"
        data = requests.get(url, timeout=(5, 8)).json()
        if data and isinstance(data, list):
            temp_c = data[0].get("temp")
            if temp_c is not None:
                if unit == "F":
                    return round(float(temp_c) * 9/5 + 32)
                return round(float(temp_c), 1)
    except Exception as e:
        print(f"  [METAR] {city_slug}: {e}")
    return None

# Codigo de pais ISO-2 por ciudad - lo pide el endpoint de Wunderground como parte del
# location_id ("{ICAO}:9:{CC}"). La mayoria de los mercados de clima de Polymarket
# resuelven via Wunderground (lo confirmamos consultando el texto de resolucion de
# varios eventos), no via Visual Crossing - por eso esta es ahora la fuente primaria.
WU_COUNTRY = {
    "nyc": "US", "chicago": "US", "miami": "US", "dallas": "US", "seattle": "US", "atlanta": "US",
    "houston": "US", "los-angeles": "US", "denver": "US",
    "london": "GB", "paris": "FR", "munich": "DE", "ankara": "TR",
    "madrid": "ES", "amsterdam": "NL", "warsaw": "PL",
    "seoul": "KR", "tokyo": "JP", "shanghai": "CN", "singapore": "SG", "lucknow": "IN",
    "tel-aviv": "IL", "toronto": "CA", "sao-paulo": "BR", "buenos-aires": "AR",
    "wellington": "NZ", "chengdu": "CN", "beijing": "CN",
    "taipei": "TW", "kuala-lumpur": "MY",
}
# Clave publica embebida en el propio HTML de wunderground.com - la recibe cualquier
# visitante de la pagina, no es una clave privada ni protegida. Solo funciona con este
# endpoint (v1/location/.../observations/historical) para estaciones de aeropuerto/ICAO;
# el endpoint v2/pws/history/daily es solo para estaciones caseras (PWS), no sirve aca.
WU_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"

def get_actual_temp_wunderground(city_slug, date_str):
    """Maximo real del dia via Wunderground/Weather.com - la fuente que de verdad usa
    Polymarket para resolver la mayoria de estos mercados (confirmado revisando el texto
    de resolucion de varios eventos: Buenos Aires, NYC, Helsinki, Milan, Chengdu, etc.)."""
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    country = WU_COUNTRY.get(city_slug)
    if not country:
        return None
    unit = loc["unit"]
    wu_units = "e" if unit == "F" else "m"
    fecha_compacta = date_str.replace("-", "")
    url = (
        f"https://api.weather.com/v1/location/{station}:9:{country}/observations/historical.json"
        f"?apiKey={WU_API_KEY}&units={wu_units}&startDate={fecha_compacta}"
    )
    try:
        data = requests.get(url, timeout=(5, 8)).json()
        obs = data.get("observations", [])
        temps = [o["temp"] for o in obs if o.get("temp") is not None]
        if temps:
            return round(float(max(temps)), 1)
    except Exception as e:
        print(f"  [WU] {city_slug} {date_str}: {e}")
    return None

def get_actual_temp(city_slug, date_str):
    """Temperatura real del dia. Wunderground primero (es la fuente de liquidacion real
    de Polymarket para casi todas las ciudades); si falla, cae a Visual Crossing."""
    wu = get_actual_temp_wunderground(city_slug, date_str)
    if wu is not None:
        return wu

    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    vc_unit = "us" if unit == "F" else "metric"
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
        f"/{station}/{date_str}/{date_str}"
        f"?unitGroup={vc_unit}&key={VC_KEY}&include=days&elements=tempmax"
    )
    try:
        data = requests.get(url, timeout=(5, 8)).json()
        days = data.get("days", [])
        if days and days[0].get("tempmax") is not None:
            return round(float(days[0]["tempmax"]), 1)
    except Exception as e:
        print(f"  [VC] {city_slug} {date_str}: {e}")
    return None

def check_market_resolved(market_id):
    """
    Checks if the market closed on Polymarket and who won.
    Returns: None (still open), True (YES won), False (NO won)
    """
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(5, 8))
        data = r.json()
        closed = data.get("closed", False)
        if not closed:
            return None
        # Check YES price — if ~1.0 then WIN, if ~0.0 then LOSS
        prices = json.loads(data.get("outcomePrices", "[0.5,0.5]"))
        yes_price = float(prices[0])
        if yes_price >= 0.95:
            return True   # WIN
        elif yes_price <= 0.05:
            return False  # LOSS
        return None  # not yet determined
    except Exception as e:
        print(f"  [RESOLVE] {market_id}: {e}")
    return None

# =============================================================================
# POLYMARKET
# =============================================================================

def get_polymarket_event(city_slug, month, day, year):
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=(5, 8))
        data = r.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception:
        pass
    return None

def get_market_price(market_id):
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 5))
        prices = json.loads(r.json().get("outcomePrices", "[0.5,0.5]"))
        return float(prices[0])
    except Exception:
        return None

def parse_temp_range(question):
    if not question: return None
    num = r'(-?\d+(?:\.\d+)?)'
    if re.search(r'or below', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or below', question, re.IGNORECASE)
        if m: return (-999.0, float(m.group(1)))
    if re.search(r'or higher', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or higher', question, re.IGNORECASE)
        if m: return (float(m.group(1)), 999.0)
    m = re.search(r'between ' + num + r'-' + num + r'[°]?[FC]', question, re.IGNORECASE)
    if m: return (float(m.group(1)), float(m.group(2)))
    m = re.search(r'be ' + num + r'[°]?[FC] on', question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None

def hours_to_resolution(end_date_str):
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 3600)
    except Exception:
        return 999.0

def in_bucket(forecast, t_low, t_high):
    if t_low == t_high:
        return round(float(forecast)) == round(t_low)
    return t_low <= float(forecast) <= t_high

# =============================================================================
# MARKET DATA STORAGE
# Each market is stored in a separate file: data/markets/{city}_{date}.json
# =============================================================================

def market_path(city_slug, date_str):
    return MARKETS_DIR / f"{city_slug}_{date_str}.json"

def load_market(city_slug, date_str):
    p = market_path(city_slug, date_str)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [WARN] Corrupt market file {p.name}: {e} — quarantining and starting fresh")
            p.rename(p.with_suffix(".json.corrupt"))
            return None
    return None

def save_market(market):
    p = market_path(market["city"], market["date"])
    atomic_write(p, json.dumps(market, indent=2, ensure_ascii=False))

def load_all_markets():
    markets = []
    for f in MARKETS_DIR.glob("*.json"):
        try:
            markets.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return markets

def new_market(city_slug, date_str, event, hours):
    loc = LOCATIONS[city_slug]
    return {
        "city":               city_slug,
        "city_name":          loc["name"],
        "date":               date_str,
        "unit":               loc["unit"],
        "station":            loc["station"],
        "event_end_date":     event.get("endDate", ""),
        "hours_at_discovery": round(hours, 1),
        "status":             "open",           # open | closed | resolved
        "position":           None,             # filled when position opens
        "actual_temp":        None,             # filled after resolution
        "resolved_outcome":   None,             # win / loss / no_position
        "pnl":                None,
        "forecast_snapshots": [],               # list of forecast snapshots
        "market_snapshots":   [],               # list of market price snapshots
        "all_outcomes":       [],               # all market buckets
        "created_at":         datetime.now(timezone.utc).isoformat(),
    }

# =============================================================================
# STATE (balance and open positions)
# =============================================================================

def load_state():
    default = {
        "balance":          BALANCE,
        "starting_balance": BALANCE,
        "total_trades":     0,
        "wins":             0,
        "losses":           0,
        "peak_balance":     BALANCE,
    }
    if not STATE_FILE.exists():
        return default
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [WARN] {STATE_FILE.name} corrupto, se ignora (balance/historial real en riesgo, revisar a mano): {e}")
        return default

def save_state(state):
    atomic_write(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False))

# =============================================================================
# CORE LOGIC
# =============================================================================

def take_forecast_snapshot(city_slug, dates):
    """Fetches forecasts from all sources and returns a snapshot. dates[0] must be city-local today."""
    now_str  = datetime.now(timezone.utc).isoformat()
    ecmwf    = get_ecmwf(city_slug, dates)
    regional = get_regional(city_slug, dates)
    today    = dates[0]
    loc      = LOCATIONS[city_slug]
    # The short-range HRRR-blend (US cities) only has real skill out to ~48h — keep
    # that cutoff for them. The other regional models (UKMO/AROME/ICON-D2/ICON-EU/
    # JMA-GSM/GEM/ICON-seamless) are full multi-day products like ECMWF, so they're
    # valid for the whole requested window, no cutoff needed.
    regional_cutoff = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d")

    snapshots = {}
    for date in dates:
        regional_ok = date <= regional_cutoff if loc["region"] == "us" else True
        snap = {
            "ts":       now_str,
            "ecmwf":    ecmwf.get(date),
            "regional": regional.get(date) if regional_ok else None,
            "metar":    get_metar(city_slug) if date == today else None,
        }
        # Best forecast: inverse-variance blend of sources with real calibration data
        # (DEB-style — weight each model by 1/sigma^2 instead of a fixed source
        # preference), falling back to the region-appropriate model (see
        # LOCATIONS["regional_model"]) over plain ECMWF while a source is still
        # uncalibrated. Each source's own known bias (see run_calibration/BIAS_DECAY)
        # is subtracted out here, on the value used for trading only — the raw
        # ecmwf/regional fields stored in the snapshot stay untouched so future
        # calibration keeps measuring the raw model's real error, not an
        # already-corrected number chasing its own tail.
        calibrated = []
        for source in ("ecmwf", "regional"):
            value = snap[source]
            cal_key = f"{city_slug}_{source}"
            if value is not None and cal_key in _cal:
                corrected = value - get_bias(city_slug, source)
                calibrated.append((corrected, _cal[cal_key]["sigma"], source))

        if len(calibrated) >= 2:
            weights = [1.0 / (sigma ** 2) for _, sigma, _ in calibrated]
            total_w = sum(weights)
            blended = sum(v * w for (v, _, _), w in zip(calibrated, weights)) / total_w
            snap["best"] = round(blended, 1)
            snap["best_source"] = "+".join(s for _, _, s in calibrated)
            snap["best_sigma"] = round(total_w ** -0.5, 3)
        elif len(calibrated) == 1:
            snap["best"], _, snap["best_source"] = calibrated[0]
        elif snap["regional"] is not None:
            snap["best"] = snap["regional"]
            snap["best_source"] = "regional"
        elif snap["ecmwf"] is not None:
            snap["best"] = snap["ecmwf"]
            snap["best_source"] = "ecmwf"
        else:
            snap["best"] = None
            snap["best_source"] = None
        snapshots[date] = snap
    return snapshots

def scan_and_update():
    """Main function of one cycle: updates forecasts, opens/closes positions."""
    global _cal
    now      = datetime.now(timezone.utc)
    state    = load_state()
    balance  = state["balance"]
    new_pos  = 0
    closed   = 0
    scan_rows  = []   # one row per market with a clean bucket match, for the scan report
    trade_rows = []   # one row per position actually opened this cycle
    resolved = 0

    for city_slug, loc in LOCATIONS.items():
        unit = loc["unit"]
        unit_sym = "F" if unit == "F" else "C"
        print(f"  -> {loc['name']}...", end=" ", flush=True)

        try:
            local_now = now.astimezone(ZoneInfo(TIMEZONES.get(city_slug, "UTC")))
            dates = [(local_now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]
            snapshots = take_forecast_snapshot(city_slug, dates)
            time.sleep(0.3)
        except Exception as e:
            print(f"skipped ({e})")
            continue

        for i, date in enumerate(dates):
            dt    = datetime.strptime(date, "%Y-%m-%d")
            event = get_polymarket_event(city_slug, MONTHS[dt.month - 1], dt.day, dt.year)
            if not event:
                continue

            end_date = event.get("endDate", "")
            hours    = hours_to_resolution(end_date) if end_date else 0
            horizon  = f"D+{i}"

            # Load or create market record
            mkt = load_market(city_slug, date)
            if mkt is None:
                if hours < MIN_HOURS or hours > MAX_HOURS:
                    continue
                mkt = new_market(city_slug, date, event, hours)

            # Skip if market already resolved
            if mkt["status"] == "resolved":
                continue

            # Update outcomes list — prices taken directly from event
            outcomes = []
            for market in event.get("markets", []):
                question = market.get("question", "")
                mid      = str(market.get("id", ""))
                volume   = float(market.get("volume", 0))
                rng      = parse_temp_range(question)
                if not rng:
                    continue
                try:
                    prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                    price = float(prices[0])  # YES price — outcomePrices is [YES, NO], not a bid/ask spread
                except Exception:
                    continue
                outcomes.append({
                    "question":  question,
                    "market_id": mid,
                    "range":     rng,
                    "price":     round(price, 4),
                    "volume":    round(volume, 0),
                })

            outcomes.sort(key=lambda x: x["range"][0])
            mkt["all_outcomes"] = outcomes

            # Forecast snapshot
            snap = snapshots.get(date, {})
            forecast_snap = {
                "ts":          snap.get("ts"),
                "horizon":     horizon,
                "hours_left":  round(hours, 1),
                "ecmwf":       snap.get("ecmwf"),
                "regional":    snap.get("regional"),
                "metar":       snap.get("metar"),
                "best":        snap.get("best"),
                "best_source": snap.get("best_source"),
                "best_sigma":  snap.get("best_sigma"),
            }
            mkt["forecast_snapshots"].append(forecast_snap)

            # Market price snapshot
            top = max(outcomes, key=lambda x: x["price"]) if outcomes else None
            market_snap = {
                "ts":       snap.get("ts"),
                "top_bucket": f"{top['range'][0]}-{top['range'][1]}{unit_sym}" if top else None,
                "top_price":  top["price"] if top else None,
            }
            mkt["market_snapshots"].append(market_snap)

            forecast_temp = snap.get("best")
            best_source   = snap.get("best_source")

            # --- STOP-LOSS, TRAILING STOP AND TAKE-PROFIT ---
            if mkt.get("position") and mkt["position"].get("status") == "open":
                pos = mkt["position"]
                current_price = None
                for o in outcomes:
                    if o["market_id"] == pos["market_id"]:
                        current_price = o["price"]
                        break

                if current_price is not None:
                    entry = pos["entry_price"]
                    stop  = pos.get("stop_price", entry * 0.75)  # 25% stop by default

                    # Trailing: if up 20%+ — move stop to breakeven
                    if current_price >= entry * 1.20 and stop < entry:
                        pos["stop_price"] = entry
                        pos["trailing_activated"] = True

                    # Take-profit: lock in the gain once up 55%+ (mid-point of the
                    # 50-60% range) instead of holding to resolution and risking it
                    # reversing back down.
                    take_profit_hit = current_price >= entry * 1.55

                    # Check stop or take-profit
                    if current_price <= stop or take_profit_hit:
                        pnl = round((current_price - entry) * pos["shares"], 2)
                        balance += pos["cost"] + pnl
                        pos["closed_at"] = snap.get("ts")
                        if take_profit_hit:
                            pos["close_reason"] = "take_profit"
                        else:
                            pos["close_reason"] = "stop_loss" if current_price < entry else "trailing_stop"
                        pos["exit_price"]   = current_price
                        pos["pnl"]          = pnl
                        pos["status"]       = "closed"
                        mkt["pnl"]              = pnl
                        mkt["status"]           = "resolved"
                        mkt["resolved_outcome"] = "win" if pnl >= 0 else "loss"
                        state["wins" if pnl >= 0 else "losses"] += 1
                        closed += 1
                        reason = "TAKE PROFIT" if take_profit_hit else ("STOP" if current_price < entry else "TRAILING BE")
                        print(f"  [{reason}] {loc['name']} {date} | entry ${entry:.3f} exit ${current_price:.3f} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

            # --- CLOSE POSITION if today's actual observed temp already invalidated the
            # bucket ---. The daily max can only rise or stay flat, so once the real
            # observation (Wunderground/Visual Crossing, via get_actual_temp — same source
            # used for post-resolution ground truth) already cleared the bucket's upper edge
            # by more than a buffer, the position is mathematically lost regardless of what
            # the forecast or market price still say. Ported from favoritos_bot.py's
            # temp_maxima_wu_hoy/INVALIDACION_BUFFER, which had this check and bot_v2 didn't.
            # Only checked for i == 0 (today) since positions are only ever opened same-day.
            if mkt.get("position") and mkt["position"].get("status") == "open" and i == 0:
                pos = mkt["position"]
                bucket_high = pos["bucket_high"]
                actual_today = get_actual_temp(city_slug, date)
                if actual_today is not None:
                    buffer = 2.0 if unit == "F" else 1.0
                    # "or higher" buckets (bucket_high == 999) can never be invalidated by a
                    # rising max — the higher it gets, the more confirmed the bucket is.
                    invalidated = bucket_high != 999 and actual_today > bucket_high + buffer
                    if invalidated:
                        current_price = None
                        for o in outcomes:
                            if o["market_id"] == pos["market_id"]:
                                current_price = o["price"]
                                break
                        if current_price is not None:
                            pnl = round((current_price - pos["entry_price"]) * pos["shares"], 2)
                            balance += pos["cost"] + pnl
                            pos["closed_at"]    = snap.get("ts")
                            pos["close_reason"] = "invalidated_actual_temp"
                            pos["exit_price"]   = current_price
                            pos["pnl"]          = pnl
                            pos["status"]       = "closed"
                            mkt["pnl"]              = pnl
                            mkt["status"]           = "resolved"
                            mkt["resolved_outcome"] = "win" if pnl >= 0 else "loss"
                            state["wins" if pnl >= 0 else "losses"] += 1
                            closed += 1
                            print(f"  [INVALIDATED] {loc['name']} {date} — actual {actual_today} already past bucket ({bucket_high}) | PnL: {'+' if pnl>=0 else ''}{pnl:.2f}")

            # --- CLOSE POSITION if forecast shifted 2+ degrees ---
            if mkt.get("position") and mkt["position"].get("status") == "open" and forecast_temp is not None:
                pos = mkt["position"]
                old_bucket_low  = pos["bucket_low"]
                old_bucket_high = pos["bucket_high"]
                # 2-degree buffer — avoid closing on small forecast fluctuations
                unit = loc["unit"]
                buffer = 2.0 if unit == "F" else 1.0
                if old_bucket_low == -999:
                    forecast_far = forecast_temp > old_bucket_high + buffer
                elif old_bucket_high == 999:
                    forecast_far = forecast_temp < old_bucket_low - buffer
                else:
                    mid_bucket = (old_bucket_low + old_bucket_high) / 2
                    forecast_far = abs(forecast_temp - mid_bucket) > (abs(mid_bucket - old_bucket_low) + buffer)
                if not in_bucket(forecast_temp, old_bucket_low, old_bucket_high) and forecast_far:
                    current_price = None
                    for o in outcomes:
                        if o["market_id"] == pos["market_id"]:
                            current_price = o["price"]
                            break
                    if current_price is not None:
                        pnl = round((current_price - pos["entry_price"]) * pos["shares"], 2)
                        balance += pos["cost"] + pnl
                        mkt["position"]["closed_at"]    = snap.get("ts")
                        mkt["position"]["close_reason"] = "forecast_changed"
                        mkt["position"]["exit_price"]   = current_price
                        mkt["position"]["pnl"]          = pnl
                        mkt["position"]["status"]       = "closed"
                        mkt["pnl"]              = pnl
                        mkt["status"]           = "resolved"
                        mkt["resolved_outcome"] = "win" if pnl >= 0 else "loss"
                        state["wins" if pnl >= 0 else "losses"] += 1
                        closed += 1
                        print(f"  [CLOSE] {loc['name']} {date} — forecast changed | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

            # --- OPEN POSITION ---
            # Solo se abren posiciones nuevas en el dia mas inmediato a resolver (i==0,
            # "D+0" en la zona horaria local de la ciudad). D+1/D+2/D+3 se siguen
            # escaneando y guardando como snapshot (para tener el pronostico a mano y
            # para calibracion), pero nunca generan una compra real - solo se opera en
            # las horas previas a la resolucion del dia mas cercano.
            if not mkt.get("position") and forecast_temp is not None and hours >= MIN_HOURS and i == 0 and not state.get("pausado"):
                sigma = snap.get("best_sigma") or get_sigma(city_slug, best_source or "ecmwf")
                best_signal = None
                skip_position = False

                # Find exactly ONE bucket that matches the forecast
                # If forecast doesn't fit any bucket cleanly — skip this market
                matched_bucket = None
                for o in outcomes:
                    t_low, t_high = o["range"]
                    if in_bucket(forecast_temp, t_low, t_high):
                        matched_bucket = o
                        break

                report_p, report_ev, report_bucket = None, None, None
                if matched_bucket:
                    o = matched_bucket
                    t_low, t_high = o["range"]
                    volume = o["volume"]
                    price  = o["price"]
                    report_bucket = f"{t_low}-{t_high}{unit_sym}"

                    # All filters — if any fails, skip this market entirely
                    if volume >= MIN_VOLUME:
                        p  = bucket_prob(forecast_temp, t_low, t_high, sigma)
                        ev = calc_ev(p, price)
                        report_p, report_ev = p, ev
                        if ev >= MIN_EV:
                            kelly = calc_kelly(p, price)
                            size  = bet_size(kelly, balance)
                            if size >= 0.50:
                                best_signal = {
                                    "market_id":    o["market_id"],
                                    "question":     o["question"],
                                    "bucket_low":   t_low,
                                    "bucket_high":  t_high,
                                    "entry_price":  price,
                                    "bid_at_entry": price,
                                    "spread":       0.0,
                                    "shares":       round(size / price, 2),
                                    "cost":         size,
                                    "p":            round(p, 4),
                                    "ev":           round(ev, 4),
                                    "kelly":        round(kelly, 4),
                                    "forecast_temp":forecast_temp,
                                    "forecast_src": best_source,
                                    "sigma":        sigma,
                                    "opened_at":    snap.get("ts"),
                                    "status":       "open",
                                    "pnl":          None,
                                    "exit_price":   None,
                                    "close_reason": None,
                                    "closed_at":    None,
                                }

                if best_signal:
                    # Fetch real bestAsk from Polymarket API for accurate entry price
                    skip_position = False
                    try:
                        r = requests.get(f"https://gamma-api.polymarket.com/markets/{best_signal['market_id']}", timeout=(3, 5))
                        mdata = r.json()
                        real_ask = float(mdata.get("bestAsk", best_signal["entry_price"]))
                        real_bid = float(mdata.get("bestBid", best_signal["bid_at_entry"]))
                        real_spread = round(real_ask - real_bid, 4)
                        # Re-check slippage and price with real values
                        if real_spread > MAX_SLIPPAGE or real_ask >= MAX_PRICE:
                            print(f"  [SKIP] {loc['name']} {date} — real ask ${real_ask:.3f} spread ${real_spread:.3f}")
                            skip_position = True
                        else:
                            real_ev = round(calc_ev(best_signal["p"], real_ask), 4)
                            if real_ev < MIN_EV:
                                print(f"  [SKIP] {loc['name']} {date} — real ev {real_ev:+.2f} below MIN_EV")
                                skip_position = True
                            else:
                                best_signal["entry_price"]  = real_ask
                                best_signal["bid_at_entry"] = real_bid
                                best_signal["spread"]       = real_spread
                                best_signal["shares"]       = round(best_signal["cost"] / real_ask, 2)
                                best_signal["ev"]           = real_ev
                    except Exception as e:
                        print(f"  [WARN] Could not fetch real ask for {best_signal['market_id']}: {e}")

                    if not skip_position and best_signal["entry_price"] < MAX_PRICE:
                        balance -= best_signal["cost"]
                        mkt["position"] = best_signal
                        state["total_trades"] += 1
                        new_pos += 1
                        bucket_label = f"{best_signal['bucket_low']}-{best_signal['bucket_high']}{unit_sym}"
                        print(f"  [BUY]  {loc['name']} {horizon} {date} | {bucket_label} | "
                              f"${best_signal['entry_price']:.3f} | EV {best_signal['ev']:+.2f} | "
                              f"${best_signal['cost']:.2f} ({best_signal['forecast_src'].upper()})")
                        send_telegram(
                            f"🟢 BUY {loc['name']} {horizon} {date}\n"
                            f"{bucket_label} | ${best_signal['entry_price']:.3f} | "
                            f"EV {best_signal['ev']:+.2f} | ${best_signal['cost']:.2f} "
                            f"({best_signal['forecast_src'].upper()})"
                        )
                        trade_rows.append({
                            "city": loc["name"], "market": bucket_label,
                            "prob": best_signal["p"], "kelly": best_signal["kelly"],
                            "stake": best_signal["cost"], "ev": best_signal["ev"],
                            "decision": "Placing...",
                        })

                if report_bucket is not None:
                    if best_signal and not skip_position:
                        report_action = "TRADE"
                    elif report_ev is not None and report_ev >= MIN_EV:
                        report_action = "WATCH"   # real edge, blocked by size/slippage/spread
                    else:
                        report_action = "SKIP"
                    scan_rows.append({
                        "city": loc["name"], "date": date, "bucket": report_bucket,
                        "prob": report_p, "ev": report_ev, "action": report_action,
                    })

            # Market closed by time
            if hours < 0.5 and mkt["status"] == "open":
                mkt["status"] = "closed"

            save_market(mkt)
            time.sleep(0.1)

        print("ok")

    # --- AUTO-RESOLUTION ---
    hoy_utc = now.strftime("%Y-%m-%d")
    for mkt in load_all_markets():
        # Registrar la temperatura real de CUALQUIER mercado ya pasado que no la tenga,
        # tenga posicion o no y sin importar como se cerro. Sin esto la calibracion se
        # quedaba sin datos: cerrar por stop-loss/take-profit/cambio-de-pronostico ponia
        # status="resolved" sin consultar nunca get_actual_temp, y los mercados sin
        # posicion ni siquiera llegaban a este bucle. Resultado medido el 2026-07-28:
        # 179 mercados "resolved" y solo 14 con actual_temp, con calibration.json en {}.
        if mkt.get("actual_temp") is None and mkt["date"] < hoy_utc:
            t = get_actual_temp(mkt["city"], mkt["date"])
            if t is not None:
                mkt["actual_temp"] = t
                save_market(mkt)
                time.sleep(0.2)

        if mkt["status"] == "resolved":
            continue

        pos = mkt.get("position")
        if not pos or pos.get("status") != "open":
            continue

        market_id = pos.get("market_id")
        if not market_id:
            continue

        # Check if market closed on Polymarket
        won = check_market_resolved(market_id)
        if won is None:
            continue  # market still open

        # Market closed — record result
        price  = pos["entry_price"]
        size   = pos["cost"]
        shares = pos["shares"]
        pnl    = round(shares * (1 - price), 2) if won else round(-size, 2)

        mkt["actual_temp"] = get_actual_temp(mkt["city"], mkt["date"])

        balance += size + pnl
        pos["exit_price"]   = 1.0 if won else 0.0
        pos["pnl"]          = pnl
        pos["close_reason"] = "resolved"
        pos["closed_at"]    = now.isoformat()
        pos["status"]       = "closed"
        mkt["pnl"]          = pnl
        mkt["status"]       = "resolved"
        mkt["resolved_outcome"] = "win" if won else "loss"

        if won:
            state["wins"] += 1
        else:
            state["losses"] += 1

        result = "WIN" if won else "LOSS"
        print(f"  [{result}] {mkt['city_name']} {mkt['date']} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
        resolved += 1

        save_market(mkt)
        time.sleep(0.3)

    state["balance"]      = round(balance, 2)
    state["peak_balance"] = max(state.get("peak_balance", balance), balance)
    save_state(state)

    check_freno_emergencia(state)

    # Run calibration if enough data collected
    all_mkts = load_all_markets()
    resolved_count = len([m for m in all_mkts if m["status"] == "resolved"])
    if resolved_count >= CALIBRATION_MIN:
        global _cal
        _cal = run_calibration(all_mkts)

    send_scan_report(state["balance"], scan_rows, trade_rows)

    return new_pos, closed, resolved


def check_freno_emergencia(state):
    """Freno de emergencia a nivel de portfolio completo — mismo criterio que ya
    tiene favoritos_bot.py (pausar compras nuevas, no las salidas de proteccion,
    si el equity real cae -50% desde el capital inicial). bot_v2.py manejaba mas
    capital que favoritos_bot.py y no tenia este freno; agregado 2026-07-24 tras
    la revision de arquitectura antes de operar con dinero real.
    Equity = balance en caja + valor de mercado real (bestBid) de lo abierto, mismo
    criterio que usa render_pnl_report para no mostrar numeros distintos para lo
    mismo."""
    if state.get("pausado"):
        return
    markets = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    valor_abiertas = 0.0
    for m in open_pos:
        pos = m["position"]
        current_price = pos["entry_price"]
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/markets/{pos['market_id']}", timeout=(3, 5))
            bid = r.json().get("bestBid")
            if bid is not None:
                current_price = float(bid)
        except Exception:
            pass
        valor_abiertas += current_price * pos["shares"]

    equity = state["balance"] + valor_abiertas
    limite = state["starting_balance"] * (1 - FRENO_EMERGENCIA_DRAWDOWN)
    if equity <= limite:
        state["pausado"] = True
        save_state(state)
        alerta = (
            f"🚨 FRENO DE EMERGENCIA ACTIVADO — BOT PRINCIPAL\n"
            f"Equity: ${equity:.2f} de ${state['starting_balance']:.2f} "
            f"({(equity/state['starting_balance']-1)*100:+.1f}%)\n"
            f"Se detuvo la apertura de posiciones nuevas. Las posiciones abiertas siguen "
            f"protegidas por stop-loss/take-profit/invalidacion.\n"
            f"Revisar y correr 'py bot_v2.py reanudar' para retomar."
        )
        print(f"  {alerta}")
        send_telegram(alerta)

# =============================================================================
# REPORT
# =============================================================================

def send_telegram_photo(path, caption=""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(path, "rb") as f:
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                          files={"photo": f}, timeout=(5, 20))
    except Exception as e:
        print(f"  [WARN] Telegram photo: {e}")


def render_scan_report(balance, scan_rows, trade_rows):
    """Clean console-style snapshot of one scan cycle (markets found + trades placed),
    rendered as a monospace image — same idea as a terminal screenshot, no charts.
    Kept self-contained here (not shared with favoritos_bot.py's own version) per the
    project's one-script-per-bot convention."""
    C_BG, C_TEXT, C_DIM = "#0d1117", "#e6edf3", "#8b949e"
    C_TRADE, C_WATCH, C_SKIP = "#3fb950", "#d29922", "#f85149"
    action_color = {"TRADE": C_TRADE, "WATCH": C_WATCH, "SKIP": C_SKIP}

    # matplotlib's mathtext treats a MATCHED PAIR of literal "$" in one Text object
    # as a formula delimiter (it silently ate "Max bet" and mashed spacing together
    # the first time this ran) — and escaping with "\$" throws off fixed-width
    # column padding, because the backslash counts toward Python's string width but
    # not toward matplotlib's rendered width. Simplest reliable fix: no "$" in this
    # image at all, plain numbers under labeled columns (still USD throughout).
    max_rows = 25
    shown = scan_rows[:max_rows]
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows = []  # (text, color, size, bold, mono)
    rows.append(("BOT PRINCIPAL — SCAN", C_TEXT, 17, True, False))
    rows.append((now_str, C_DIM, 11, False, True))
    rows.append(("=" * 84, C_DIM, 10, False, True))
    rows.append((
        f"Balance: {balance:,.2f}   Max bet: {MAX_BET:.2f}   "
        f"Kelly: {KELLY_FRACTION}   Min EV: {MIN_EV*100:.0f}%   (USD)",
        C_TEXT, 11.5, False, True,
    ))
    rows.append(("", C_TEXT, 6, False, True))
    rows.append((f"{'City':<16}{'Date':<12}{'Bucket':<12}{'Prob':>6}{'EV%':>9}{'Action':>9}", C_DIM, 11, True, True))
    rows.append(("-" * 84, C_DIM, 10, False, True))
    if shown:
        for r in shown:
            prob_s = f"{r['prob']:.2f}" if r["prob"] is not None else "  —"
            ev_s   = f"{r['ev']*100:+.1f}%" if r["ev"] is not None else "     —"
            row = f"{r['city']:<16}{r['date']:<12}{r['bucket']:<12}{prob_s:>6}{ev_s:>9}{r['action']:>9}"
            rows.append((row, action_color.get(r["action"], C_TEXT), 11, False, True))
    else:
        rows.append(("(sin mercados con bucket unico esta corrida)", C_DIM, 11, False, True))
    if len(scan_rows) > max_rows:
        rows.append((f"... y {len(scan_rows) - max_rows} mercado(s) mas", C_DIM, 10, False, True))

    rows.append(("", C_TEXT, 6, False, True))
    if trade_rows:
        rows.append((f"{'City':<16}{'Market':<14}{'Prob':>6}{'Kelly':>7}{'Stake':>8}{'EV':>8}   Decision", C_DIM, 11, True, True))
        rows.append(("-" * 84, C_DIM, 10, False, True))
        for t in trade_rows:
            row = (f"{t['city']:<16}{t['market']:<14}{t['prob']:>6.2f}{t['kelly']:>7.2f}"
                   f"{t['stake']:>8.2f}{t['ev']:>+8.2f}   {t['decision']}")
            rows.append((row, C_TRADE, 11, False, True))
        rows.append(("-" * 84, C_DIM, 10, False, True))
        total_stake = sum(t["stake"] for t in trade_rows)
        total_ev    = sum(t["ev"] for t in trade_rows)
        rows.append((f"Total staked: {total_stake:.2f}   Total EV: {total_ev:+.2f}   (USD)", C_TEXT, 12, True, True))
    else:
        rows.append(("Sin compras nuevas esta corrida.", C_DIM, 11, False, True))

    n = len(rows)
    fig_h = max(3.5, 0.30 * n + 0.7)
    fig, ax = plt.subplots(figsize=(11, fig_h), facecolor=C_BG)
    ax.set_facecolor(C_BG)
    ax.axis("off")

    dy = 1.0 / (n + 1)
    y = 1.0 - dy * 0.6
    for text, color, size, bold, mono in rows:
        ax.text(0.02, y, text, transform=ax.transAxes, color=color, fontsize=size,
                fontweight="bold" if bold else "normal",
                family="monospace" if mono else "sans-serif", va="top")
        y -= dy

    img_dir = DATA_DIR / "images"
    img_dir.mkdir(exist_ok=True)
    path = img_dir / "bot_v2_scan.png"
    fig.savefig(path, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def send_scan_report(balance, scan_rows, trade_rows):
    try:
        path = render_scan_report(balance, scan_rows, trade_rows)
        send_telegram_photo(path, caption=f"Bot Principal — scan {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    except Exception as e:
        print(f"  [WARN] Scan report render/send failed: {e}")


def print_status():
    state    = load_state()
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    bal     = state["balance"]
    start   = state["starting_balance"]
    ret_pct = (bal - start) / start * 100
    wins    = state["wins"]
    losses  = state["losses"]
    total   = wins + losses

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — STATUS")
    print(f"{'='*55}")
    print(f"  Balance:     ${bal:,.2f}  (start ${start:,.2f}, {'+'if ret_pct>=0 else ''}{ret_pct:.1f}%)")
    print(f"  Trades:      {total} | W: {wins} | L: {losses} | WR: {wins/total:.0%}" if total else "  No trades yet")
    print(f"  Open:        {len(open_pos)}")
    print(f"  Resolved:    {len(resolved)}")
    if state.get("pausado"):
        print(f"  🚨 PAUSADO por freno de emergencia — no se compran posiciones nuevas. 'py bot_v2.py reanudar' para retomar.")

    if open_pos:
        print(f"\n  Open positions:")
        total_unrealized = 0.0
        for m in open_pos:
            pos      = m["position"]
            unit_sym = "F" if m["unit"] == "F" else "C"
            label    = f"{pos['bucket_low']}-{pos['bucket_high']}{unit_sym}"

            # Current price from latest market snapshot
            current_price = pos["entry_price"]
            snaps = m.get("market_snapshots", [])
            if snaps:
                # Find our bucket price in all_outcomes
                for o in m.get("all_outcomes", []):
                    if o["market_id"] == pos["market_id"]:
                        current_price = o["price"]
                        break

            unrealized = round((current_price - pos["entry_price"]) * pos["shares"], 2)
            total_unrealized += unrealized
            pnl_str = f"{'+'if unrealized>=0 else ''}{unrealized:.2f}"

            print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | "
                  f"entry ${pos['entry_price']:.3f} -> ${current_price:.3f} | "
                  f"PnL: {pnl_str} | {pos['forecast_src'].upper()}")

        sign = "+" if total_unrealized >= 0 else ""
        print(f"\n  Unrealized PnL: {sign}{total_unrealized:.2f}")

    print(f"{'='*55}\n")

def print_report():
    markets  = load_all_markets()
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — FULL REPORT")
    print(f"{'='*55}")

    if not resolved:
        print("  No resolved markets yet.")
        return

    total_pnl = sum(m["pnl"] for m in resolved)
    wins      = [m for m in resolved if m["resolved_outcome"] == "win"]
    losses    = [m for m in resolved if m["resolved_outcome"] == "loss"]

    print(f"\n  Total resolved: {len(resolved)}")
    print(f"  Wins:           {len(wins)} | Losses: {len(losses)}")
    print(f"  Win rate:       {len(wins)/len(resolved):.0%}")
    print(f"  Total PnL:      {'+'if total_pnl>=0 else ''}{total_pnl:.2f}")

    print(f"\n  By city:")
    for city in sorted(set(m["city"] for m in resolved)):
        group = [m for m in resolved if m["city"] == city]
        w     = len([m for m in group if m["resolved_outcome"] == "win"])
        pnl   = sum(m["pnl"] for m in group)
        name  = LOCATIONS[city]["name"]
        print(f"    {name:<16} {w}/{len(group)} ({w/len(group):.0%})  PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

    print(f"\n  Market details:")
    for m in sorted(resolved, key=lambda x: x["date"]):
        pos      = m.get("position", {})
        unit_sym = "F" if m["unit"] == "F" else "C"
        snaps    = m.get("forecast_snapshots", [])
        first_fc = snaps[0]["best"] if snaps else None
        last_fc  = snaps[-1]["best"] if snaps else None
        label    = f"{pos.get('bucket_low')}-{pos.get('bucket_high')}{unit_sym}" if pos else "no position"
        result   = m["resolved_outcome"].upper()
        pnl_str  = f"{'+'if m['pnl']>=0 else ''}{m['pnl']:.2f}" if m["pnl"] is not None else "-"
        fc_str   = f"forecast {first_fc}->{last_fc}{unit_sym}" if first_fc else "no forecast"
        actual   = f"actual {m['actual_temp']}{unit_sym}" if m["actual_temp"] else ""
        print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | {fc_str} | {actual} | {result} {pnl_str}")

    print(f"{'='*55}\n")


def render_pnl_report():
    """PnL snapshot (balance, win rate, PnL por ciudad) en el mismo estilo visual
    que render_scan_report — pensado para pedir un vistazo rapido a demanda, no solo
    en cada scan."""
    state    = load_state()
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    bal     = state["balance"]
    start   = state["starting_balance"]
    ret_pct = (bal - start) / start * 100
    wins    = state["wins"]
    losses  = state["losses"]
    total   = wins + losses

    C_BG, C_TEXT, C_DIM = "#0d1117", "#e6edf3", "#8b949e"
    C_POS, C_NEG = "#3fb950", "#f85149"

    rows = []  # (text, color, size, bold, mono)
    rows.append(("BOT PRINCIPAL — PNL", C_TEXT, 17, True, False))
    rows.append((datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), C_DIM, 11, False, True))
    rows.append(("=" * 84, C_DIM, 10, False, True))
    rows.append((f"Balance: {bal:,.2f}   Inicio: {start:,.2f}   Retorno: {ret_pct:+.1f}%   (USD)",
                  C_POS if ret_pct >= 0 else C_NEG, 13, True, True))
    if total:
        rows.append((f"Trades resueltos: {total}   Aciertos: {wins}   Fallos: {losses}   Win rate: {wins/total*100:.0f}%",
                      C_TEXT, 11.5, False, True))
    else:
        rows.append(("Sin trades resueltos todavia.", C_DIM, 11.5, False, True))
    rows.append((f"Posiciones abiertas: {len(open_pos)}", C_TEXT, 11.5, False, True))
    rows.append(("", C_TEXT, 6, False, True))

    # --- Detalle de posiciones abiertas: a diferencia del scan (que solo muestra lo
    # que paso en ESE ciclo), esto se pide a demanda y trae precio actual y temp real
    # EN VIVO (no el ultimo snapshot guardado, que puede tener hasta SCAN_INTERVAL de
    # antiguedad) para poder ver como viene cada posicion sin esperar al proximo scan.
    if open_pos:
        rows.append((f"{'City':<14} {'Bucket':<10} {'Entry':>6} {'Ahora':>6} {'Real':>7} {'Pronost':>12} {'PnL$':>8} {'Hs res':>6}",
                      C_DIM, 10.5, True, True))
        rows.append(("-" * 84, C_DIM, 10, False, True))
        for mkt in open_pos:
            pos = mkt["position"]
            mid = pos["market_id"]
            unit_sym = mkt["unit"]

            current_price = None
            try:
                r = requests.get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=(3, 5))
                best_bid = r.json().get("bestBid")
                if best_bid is not None:
                    current_price = float(best_bid)
            except Exception:
                pass
            if current_price is None:
                for o in mkt.get("all_outcomes", []):
                    if o["market_id"] == mid:
                        current_price = o["price"]
                        break

            real_temp = get_actual_temp(mkt["city"], mkt["date"])

            last_snap = mkt["forecast_snapshots"][-1] if mkt["forecast_snapshots"] else {}
            forecast  = last_snap.get("best")
            src       = (last_snap.get("best_source") or "?")[:3]

            hours_left = hours_to_resolution(mkt.get("event_end_date", "")) if mkt.get("event_end_date") else None

            entry_s   = f"{pos['entry_price']:.3f}"
            now_s     = f"{current_price:.3f}" if current_price is not None else "  ?"
            real_s    = f"{real_temp:.1f}{unit_sym}" if real_temp is not None else "  ?"
            fcst_s    = f"{forecast:.1f}{unit_sym}({src})" if forecast is not None else "  ?"
            pnl_unreal = (current_price - pos["entry_price"]) * pos["shares"] if current_price is not None else None
            pnl_s     = f"{pnl_unreal:+.2f}" if pnl_unreal is not None else "  ?"
            hs_s      = f"{hours_left:.0f}" if hours_left is not None else "  ?"

            bucket = f"{pos['bucket_low']:g}-{pos['bucket_high']:g}{unit_sym}"
            row = f"{mkt['city_name']:<14} {bucket:<10} {entry_s:>6} {now_s:>6} {real_s:>7} {fcst_s:>12} {pnl_s:>8} {hs_s:>6}"
            color = C_POS if (pnl_unreal or 0) >= 0 else C_NEG
            rows.append((row, color, 10.5, False, True))
        rows.append(("-" * 84, C_DIM, 10, False, True))
        rows.append(("", C_TEXT, 6, False, True))

    if resolved:
        rows.append((f"{'City':<18}{'Trades':>8}{'WR%':>7}{'PnL':>10}", C_DIM, 11, True, True))
        rows.append(("-" * 84, C_DIM, 10, False, True))
        by_city = {}
        for m in resolved:
            by_city.setdefault(m["city"], []).append(m)
        city_pnls = []
        for city, group in by_city.items():
            w   = len([m for m in group if m["resolved_outcome"] == "win"])
            pnl = sum(m["pnl"] for m in group)
            city_pnls.append((LOCATIONS[city]["name"], len(group), w, pnl))
        city_pnls.sort(key=lambda r: -r[3])
        for name, n_trades, w, pnl in city_pnls:
            wr = w / n_trades * 100
            row = f"{name:<18}{n_trades:>8}{wr:>6.0f}%{pnl:>+10.2f}"
            rows.append((row, C_POS if pnl >= 0 else C_NEG, 11, False, True))
        rows.append(("-" * 84, C_DIM, 10, False, True))
        best  = city_pnls[0]
        worst = city_pnls[-1]
        rows.append((f"Mejor ciudad: {best[0]} ({best[3]:+.2f})   Peor ciudad: {worst[0]} ({worst[3]:+.2f})",
                      C_TEXT, 11, True, True))
    else:
        rows.append(("(sin mercados resueltos todavia para desglosar por ciudad)", C_DIM, 11, False, True))

    # --- Brier score: que tan bien calibradas estan nuestras probabilidades, no solo
    # si acertamos o no. 0.0 = perfecto, 0.25 = igual que tirar una moneda, 1.0 = siempre
    # al reves. Complementa el win rate (que no distingue "gane por poco" de "gane muy
    # seguro") con una medida de calidad de la probabilidad calculada.
    briers = []
    for m in resolved:
        pos = m.get("position")
        if not pos or pos.get("p") is None:
            continue
        outcome = 1.0 if m["resolved_outcome"] == "win" else 0.0
        briers.append((pos["p"] - outcome) ** 2)
    rows.append(("", C_TEXT, 6, False, True))
    rows.append(("-" * 84, C_DIM, 10, False, True))
    if briers:
        brier = sum(briers) / len(briers)
        ref = "mejor que adivinar 50/50" if brier < 0.25 else "peor que adivinar 50/50"
        rows.append((f"Brier score: {brier:.3f}  (n={len(briers)}, mas bajo = mejor calibrado)  -  {ref}",
                      C_POS if brier < 0.25 else C_NEG, 11.5, True, True))
    else:
        rows.append(("Brier score: sin datos suficientes todavia.", C_DIM, 11, False, True))

    # --- Correlacion por region: si 2+ posiciones ABIERTAS caen en la misma region
    # geografica amplia, no son tan independientes como parecen — si el modelo se
    # equivoca en el patron climatico de esa zona, pueden perder juntas. Proxy simple
    # (ver REGIONES arriba), no deteccion real de sistema sinoptico.
    por_region = {}
    for m in open_pos:
        region = CITY_TO_REGION.get(m["city"], "Otra")
        por_region.setdefault(region, []).append(m["city_name"])
    clusters = {r: cs for r, cs in por_region.items() if len(cs) >= 2}
    rows.append(("", C_TEXT, 6, False, True))
    if clusters:
        rows.append(("Riesgo correlacionado (2+ posiciones abiertas en la misma region):",
                      C_NEG, 11, True, True))
        for region, ciudades in clusters.items():
            rows.append((f"  {region}: {len(ciudades)} posiciones -> {', '.join(ciudades)}", C_NEG, 10.5, False, True))
    elif open_pos:
        rows.append(("Sin riesgo correlacionado detectado — posiciones abiertas repartidas en regiones distintas.",
                      C_POS, 10.5, False, True))

    n = len(rows)
    fig_h = max(3.5, 0.30 * n + 0.7)
    fig, ax = plt.subplots(figsize=(11, fig_h), facecolor=C_BG)
    ax.set_facecolor(C_BG)
    ax.axis("off")
    dy = 1.0 / (n + 1)
    y  = 1.0 - dy * 0.6
    for text, color, size, bold, mono in rows:
        ax.text(0.02, y, text, transform=ax.transAxes, color=color, fontsize=size,
                fontweight="bold" if bold else "normal",
                family="monospace" if mono else "sans-serif", va="top")
        y -= dy

    img_dir = DATA_DIR / "images"
    img_dir.mkdir(exist_ok=True)
    path = img_dir / "bot_v2_pnl.png"
    fig.savefig(path, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def send_pnl_report():
    path = render_pnl_report()
    send_telegram_photo(path, caption=f"Bot Principal — PnL {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return path

# =============================================================================
# AQI WATCH — parentesis aparte del clima, solo avisa, no opera nada
# =============================================================================
# Mercados de calidad del aire (AQI) en Polymarket: mismo tipo de resolucion
# verificable que el clima (AirNow/EPA), pero esporadicos (se abren cuando hay
# un evento real, ej. humo de incendios) y no uno diario por ciudad como el
# clima, asi que no se automatiza la entrada. Esto solo detecta mercados nuevos
# y avisa por Telegram para investigar a mano en ese momento.

AQI_TAG_ID   = 105654  # tag "Air Quality" en la Gamma API de Polymarket
AQI_SEEN_FILE = DATA_DIR / "aqi_seen_markets.json"


def load_aqi_seen():
    if not AQI_SEEN_FILE.exists():
        return set()
    try:
        return set(json.loads(AQI_SEEN_FILE.read_text(encoding="utf-8")))
    except Exception as e:
        print(f"  [WARN] {AQI_SEEN_FILE.name} corrupto, se ignora: {e}")
        return set()


def save_aqi_seen(seen):
    atomic_write(AQI_SEEN_FILE, json.dumps(list(seen)))


def check_new_aqi_markets():
    """Consulta mercados AQI abiertos y avisa por Telegram si hay uno nuevo.
    No calcula EV ni compra nada — es solo una nota para investigar a mano."""
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/events",
            params={"tag_id": AQI_TAG_ID, "closed": "false", "limit": 50},
            timeout=(3, 8),
        )
        events = r.json()
        if not isinstance(events, list):
            return
    except Exception as e:
        print(f"  [WARN] AQI watch: no se pudo consultar Polymarket: {e}")
        return

    seen = load_aqi_seen()
    nuevos = [e for e in events if str(e.get("id", "")) not in seen]
    if not nuevos:
        return

    for e in nuevos:
        seen.add(str(e.get("id", "")))
        title = e.get("title") or e.get("slug", "")
        vol = float(e.get("volume") or 0)
        send_telegram(
            "AVISO (parentesis, no es clima) — nuevo mercado AQI en Polymarket\n"
            f"{title}\n"
            f"Sub-mercados: {len(e.get('markets', []))} | Volumen: ${vol:,.0f}\n"
            f"Slug: {e.get('slug', '')}\n"
            "Nicho aparte del bot, nada se compra solo — revisar a mano si vale la pena."
        )
        print(f"  [AQI WATCH] nuevo mercado detectado: {title}")

    save_aqi_seen(seen)


# =============================================================================
# MAIN LOOP
# =============================================================================

MONITOR_INTERVAL = 600  # monitor positions every 10 minutes

def monitor_positions():
    """Quick stop check on open positions without full scan."""
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    if not open_pos:
        return 0

    state   = load_state()
    balance = state["balance"]
    closed  = 0

    for mkt in open_pos:
        pos = mkt["position"]
        mid = pos["market_id"]

        # Fetch real bestBid from Polymarket API — actual sell price
        current_price = None
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=(3, 5))
            mdata = r.json()
            best_bid = mdata.get("bestBid")
            if best_bid is not None:
                current_price = float(best_bid)
        except Exception:
            pass

        # Fallback to cached price if API failed
        if current_price is None:
            for o in mkt.get("all_outcomes", []):
                if o["market_id"] == mid:
                    current_price = o["price"]
                    break

        if current_price is None:
            continue

        entry = pos["entry_price"]
        stop  = pos.get("stop_price", entry * 0.75)  # 25% stop
        city_name = LOCATIONS.get(mkt["city"], {}).get("name", mkt["city"])

        # Hours left to resolution
        end_date = mkt.get("event_end_date", "")
        hours_left = hours_to_resolution(end_date) if end_date else 999.0

        dirty = False

        # Trailing: if up 20%+ — move stop to breakeven
        if current_price >= entry * 1.20 and stop < entry:
            pos["stop_price"] = entry
            pos["trailing_activated"] = True
            dirty = True
            print(f"  [TRAILING] {city_name} {mkt['date']} — stop moved to breakeven ${entry:.3f}")

        # Check take-profit — 55% (mid-point of the 50-60% range), relative to entry,
        # same rule as scan_and_update's own stop/take-profit check.
        take_triggered = current_price >= entry * 1.55
        # Check stop
        stop_triggered = current_price <= stop

        if take_triggered or stop_triggered:
            pnl = round((current_price - entry) * pos["shares"], 2)
            balance += pos["cost"] + pnl
            pos["closed_at"]    = datetime.now(timezone.utc).isoformat()
            if take_triggered:
                pos["close_reason"] = "take_profit"
                reason = "TAKE"
            elif current_price < entry:
                pos["close_reason"] = "stop_loss"
                reason = "STOP"
            else:
                pos["close_reason"] = "trailing_stop"
                reason = "TRAILING BE"
            pos["exit_price"]   = current_price
            pos["pnl"]          = pnl
            pos["status"]       = "closed"
            mkt["pnl"]              = pnl
            mkt["status"]           = "resolved"
            mkt["resolved_outcome"] = "win" if pnl >= 0 else "loss"
            state["wins" if pnl >= 0 else "losses"] += 1
            closed += 1
            dirty = True
            print(f"  [{reason}] {city_name} {mkt['date']} | entry ${entry:.3f} exit ${current_price:.3f} | {hours_left:.0f}h left | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

        if dirty:
            save_market(mkt)

    if closed:
        state["balance"] = round(balance, 2)
        save_state(state)

    return closed


def run_loop():
    global _cal
    _cal = load_cal()

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — STARTING")
    print(f"{'='*55}")
    print(f"  Cities:     {len(LOCATIONS)}")
    print(f"  Balance:    ${BALANCE:,.0f} | Max bet: ${MAX_BET}")
    print(f"  Scan:       {SCAN_INTERVAL//60} min | Monitor: {MONITOR_INTERVAL//60} min")
    print(f"  Sources:    ECMWF + per-city regional model + METAR(D+0)")
    print(f"  Data:       {DATA_DIR.resolve()}")
    print(f"  Ctrl+C to stop\n")

    last_full_scan = 0

    while True:
        now_ts  = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            check_new_aqi_markets()
        except Exception as e:
            print(f"  AQI watch error: {e}")

        # Full scan once per hour
        if now_ts - last_full_scan >= SCAN_INTERVAL:
            print(f"[{now_str}] full scan...")
            try:
                new_pos, closed, resolved = scan_and_update()
                state = load_state()
                print(f"  balance: ${state['balance']:,.2f} | "
                      f"new: {new_pos} | closed: {closed} | resolved: {resolved}")
                last_full_scan = time.time()
            except KeyboardInterrupt:
                print(f"\n  Stopping — saving state...")
                save_state(load_state())
                print(f"  Done. Bye!")
                break
            except requests.exceptions.ConnectionError:
                print(f"  Connection lost — waiting 60 sec")
                time.sleep(60)
                continue
            except Exception as e:
                print(f"  Error: {e} — waiting 60 sec")
                time.sleep(60)
                continue
        else:
            # Quick stop monitoring
            print(f"[{now_str}] monitoring positions...")
            try:
                stopped = monitor_positions()
                if stopped:
                    state = load_state()
                    print(f"  balance: ${state['balance']:,.2f}")
            except Exception as e:
                print(f"  Monitor error: {e}")

        try:
            time.sleep(MONITOR_INTERVAL)
        except KeyboardInterrupt:
            print(f"\n  Stopping — saving state...")
            save_state(load_state())
            print(f"  Done. Bye!")
            break

# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run_loop()
    elif cmd == "status":
        _cal = load_cal()
        print_status()
    elif cmd == "report":
        _cal = load_cal()
        print_report()
    elif cmd == "pnl":
        path = send_pnl_report()
        print(f"Reporte de PnL enviado a Telegram. Imagen: {path}")
    elif cmd == "reanudar":
        state = load_state()
        state["pausado"] = False
        save_state(state)
        print("Freno de emergencia levantado - vuelve a comprar posiciones nuevas desde el proximo ciclo.")
        send_telegram("✅ BOT PRINCIPAL — freno de emergencia levantado manualmente, retomando compras.")
    else:
        print("Usage: python weatherbet.py [run|status|report|pnl|reanudar]")
