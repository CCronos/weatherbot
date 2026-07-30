#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_milan.py — Milan (LIMC, Malpensa): ICON-D2 + AROME + ECMWF (separados, sin promediar)
TAF | Lluvia/tormenta | Real vs ECMWF (tendencia) | Ciclo del modelo | Tracking automático
Dashboard visual propio + envío a Telegram
"""

import sys
import json
import math
import re
import time
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Windows consoles often default to a legacy codepage (cp1252) that can't encode
# the em-dashes/symbols used in these prints — force UTF-8 so a print() never
# silently crashes the whole cycle before it reaches the Telegram send.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

LAT, LON = 45.6306, 8.7281  # Malpensa Intl Airport coordinates (resolution station), not Milan city center
STATION = "LIMC"
WU_COUNTRY = "IT"
WU_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"  # clave publica embebida en wunderground.com
TZ = "Europe/Rome"


def fetch_wu_max_hoy():
    """Maximo real registrado HOY hasta ahora segun Wunderground - la fuente que
    realmente liquida el mercado (Milan resuelve via Wunderground, confirmado)."""
    fecha = datetime.now(ZoneInfo(TZ)).strftime("%Y%m%d")
    url = (
        f"https://api.weather.com/v1/location/{STATION}:9:{WU_COUNTRY}/observations/historical.json"
        f"?apiKey={WU_API_KEY}&units=m&startDate={fecha}"
    )
    try:
        data = requests.get(url, timeout=(5, 8)).json()
        temps = [o["temp"] for o in data.get("observations", []) if o.get("temp") is not None]
        return max(temps) if temps else None
    except Exception as e:
        print(f"  [WARN] WU: {e}")
        return None
READINGS_FILE = Path("data/milan_readings.json")
TRACKING_FILE = Path("data/milan_tracking.json")
STATS_FILE = Path("data/milan_stats.json")
DASH_SNAPSHOT_FILE = Path("data/dashboard_snapshot_milan.json")
IMG_DIR = Path("data/images")
CHECK_INTERVAL = 14400  # 4h
PEAK_HOURS = ("12:00", "13:00", "14:00", "15:00", "16:00")
MIN_EV_HIGHLIGHT = 0.05
SMOOTH_N = 3
MIN_MODEL_PROB = 0.01
SIGMA_DEFAULT = 1.2
RAIN_PROB_THRESHOLD = 40
HEAVY_MM_THRESHOLD = 2.0
PICK_HOUR = 8
PICK_SIZE_USD = 20.0
PICKS_PER_DAY = 3

BRAND = "SIGNAL // MIL"
# Tema único de Milan — acento rojo. Colores de modelo (ECMWF/ICON) fijos
# en todos los dashboards de todas las ciudades, para reconocer el modelo por color
# sin importar la ciudad. AROME es exclusivo de Milan (Météo-France alta resolución).
C_BG, C_PANEL, C_ACCENT = "#27273e", "#3b3855", "#e66767"
C_ICON, C_AROME, C_ECMWF = "#9085e9", "#d55181", "#199e70"
C_REAL, C_TEXT, C_TEXT_DIM, C_GRID = "#ffffff", "#ffffff", "#c3c2b7", "#4a4560"

# icon_d2 y arome son modelos de alta resolución con horizonte corto (~48h, como HRRR
# en EE.UU.) — devuelven null más allá de eso; es el mismo manejo de nulls que ya usan
# los demás modelos de corto alcance en este repo.
MODELS = {"icon_d2": "icon_d2", "arome": "meteofrance_arome_france_hd", "ecmwf": "ecmwf_ifs025"}
MODEL_COLORS = {"icon_d2": C_ICON, "arome": C_AROME, "ecmwf": C_ECMWF}

# Nota: Milan todavía no tiene una tabla de sesgo (BIAS_TABLE) — a diferencia de
# Helsinki/Chengdu, que la construyeron a partir de semanas de tracking histórico de
# viento/cielo vs. error. No se inventa una; los finales se usan sin corrección de sesgo.

WEATHER_CODES = {
    0: "despejado", 1: "mayormente despejado", 2: "parcialmente nublado", 3: "nublado",
    45: "niebla", 48: "niebla escarchada",
    51: "llovizna ligera", 53: "llovizna moderada", 55: "llovizna intensa",
    61: "lluvia ligera", 63: "lluvia moderada", 65: "lluvia intensa",
    80: "chubascos ligeros", 81: "chubascos moderados", 82: "chubascos violentos",
    95: "TORMENTA ELÉCTRICA", 96: "TORMENTA con granizo ligero", 99: "TORMENTA con granizo fuerte",
}
RAIN_CODES = {51, 53, 55, 61, 63, 65, 80, 81, 82, 95, 96, 99}
STORM_CODES = {95, 96, 99}

WX_MAP = {"RA": "lluvia", "SHRA": "chubascos", "TSRA": "tormenta con lluvia", "TS": "tormenta",
          "DZ": "llovizna", "FG": "niebla", "BR": "neblina", "SN": "nieve"}

with open("config.json", encoding="utf-8") as f:
    cfg = json.load(f)
TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID_2 = (
    cfg.get("telegram_token", ""), cfg.get("telegram_chat_dashboards") or cfg.get("telegram_chat_id", ""), cfg.get("telegram_chat_id_2", "")
)
IMG_DIR.mkdir(parents=True, exist_ok=True)


def send_telegram(text):
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for cid in [TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID_2]:
        if cid:
            try:
                requests.post(url, data={"chat_id": cid, "text": text}, timeout=(5, 15))
            except Exception as e:
                print(f"  [WARN] Telegram: {e}")


def send_telegram_photo(path, caption=""):
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    for cid in [TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID_2]:
        if cid:
            try:
                with open(path, "rb") as f:
                    requests.post(url, data={"chat_id": cid, "caption": caption[:1024]},
                                  files={"photo": f}, timeout=(8, 25))
            except Exception as e:
                print(f"  [WARN] Telegram photo: {e}")


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def hours_since_last_model_cycle():
    now_utc = datetime.now(ZoneInfo("UTC"))
    cycle_hours = [0, 6, 12, 18]
    last_cycle = max([h for h in cycle_hours if h <= now_utc.hour], default=18)
    last_cycle_time = now_utc.replace(hour=last_cycle, minute=0, second=0, microsecond=0)
    if last_cycle_time > now_utc:
        last_cycle_time -= timedelta(days=1)
    delta = now_utc - last_cycle_time
    return delta.total_seconds() / 3600, f"{last_cycle:02d}z"


def fetch_hourly_peak(model_key, target_date):
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": LAT, "longitude": LON, "hourly": "temperature_2m",
            "timezone": TZ, "start_date": target_date, "end_date": target_date, "models": model_key,
        }, timeout=10)
        h = r.json()["hourly"]
        out = {t.split("T")[1]: v for t, v in zip(h["time"], h["temperature_2m"])}
        peak_t, peak_h = None, None
        for hh in PEAK_HOURS:
            v = out.get(hh)
            if v is not None and (peak_t is None or v > peak_t):
                peak_t, peak_h = v, hh
        return peak_t, peak_h
    except Exception as e:
        print(f"  [WARN] hourly {model_key}: {e}")
        return None, None


def fetch_daily_conditions(target_date):
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": LAT, "longitude": LON,
            "daily": "precipitation_probability_max,precipitation_sum,weathercode,windspeed_10m_max,winddirection_10m_dominant",
            "timezone": TZ, "start_date": target_date, "end_date": target_date, "models": "ecmwf_ifs025",
        }, timeout=10)
        d = r.json()["daily"]
        code = d["weathercode"][0]
        return {"precip_prob": d["precipitation_probability_max"][0],
                "weather_desc": WEATHER_CODES.get(code, f"código {code}"), "weathercode": code}
    except Exception:
        return None


def fetch_rain_windows(target_date):
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": LAT, "longitude": LON,
            "hourly": "precipitation,precipitation_probability,weathercode",
            "timezone": TZ, "start_date": target_date, "end_date": target_date, "models": "ecmwf_ifs025",
        }, timeout=10)
        d = r.json()["hourly"]
        times, precip, prob, codes = d["time"], d["precipitation"], d["precipitation_probability"], d["weathercode"]

        def classify(p_mm, p_prob, code):
            if code in STORM_CODES: return "storm"
            if p_mm >= HEAVY_MM_THRESHOLD: return "heavy"
            if p_prob >= RAIN_PROB_THRESHOLD or code in RAIN_CODES or p_mm >= 0.3: return "light"
            return None

        hourly_class = [(t.split("T")[1], classify(p, pr, c), p) for t, p, pr, c in zip(times, precip, prob, codes)]
        windows, current = [], None
        for hour, cls, p_mm in hourly_class:
            if cls is not None:
                if current is None or current["class"] != cls:
                    if current is not None: windows.append(current)
                    current = {"class": cls, "start": hour, "end": hour, "total_mm": p_mm}
                else:
                    current["end"] = hour; current["total_mm"] += p_mm
            else:
                if current is not None: windows.append(current); current = None
        if current is not None: windows.append(current)
        return windows
    except Exception as e:
        print(f"  [WARN] lluvia: {e}")
        return []


def fetch_taf_structured():
    try:
        r = requests.get("https://aviationweather.gov/api/data/taf",
                          params={"ids": STATION, "format": "json"}, timeout=10)
        data = r.json()
        if not data: return []
        periods = []
        for p in data[0].get("fcsts", []):
            periods.append({"time_from": p.get("timeFrom"), "time_to": p.get("timeTo"),
                            "wind_dir": p.get("wdir"), "wind_speed": p.get("wspd"),
                            "wind_gust": p.get("wgst"), "visib": p.get("visib"),
                            "wx": p.get("wxString"), "type": p.get("fcstChange") or "BASE"})
        return periods
    except Exception as e:
        print(f"  [WARN] TAF: {e}")
        return []


def wind_deg_to_octant(deg):
    """TAF/METAR a veces reportan viento variable ('VRB') en vez de un rumbo numérico."""
    if not isinstance(deg, (int, float)):
        return "VRB"
    dirs = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]
    return dirs[int((deg + 22.5) // 45) % 8]


def fmt_epoch(epoch):
    if not epoch: return "?"
    try:
        dt = datetime.fromtimestamp(int(epoch), tz=ZoneInfo("UTC")).astimezone(ZoneInfo(TZ))
        return dt.strftime("%d/%H:%M")
    except Exception:
        return "?"


def describe_wx(wx):
    if not wx: return None
    tokens = wx.replace("-", "").replace("+", "").split()
    descs = []
    for tok in tokens:
        for code, label in WX_MAP.items():
            if code in tok: descs.append(label); break
    return ", ".join(dict.fromkeys(descs)) if descs else wx


def format_taf_summary(periods):
    lines = []
    for p in periods:
        wind = ""
        if p.get("wind_dir") is not None and p.get("wind_speed") is not None:
            wind = f"viento {wind_deg_to_octant(p['wind_dir'])} {p['wind_speed']}kt"
            if p.get("wind_gust"): wind += f" (ráfagas {p['wind_gust']}kt)"
        wx = describe_wx(p.get("wx"))
        visib = f"vis. {p['visib']}sm" if p.get("visib") else ""
        parts = [x for x in [wind, wx, visib] if x]
        detail = " · ".join(parts) if parts else "sin cambios significativos"
        lines.append(f"[{p['type']}] {fmt_epoch(p['time_from'])}→{fmt_epoch(p['time_to'])}: {detail}")
    return lines


def fetch_metar_condition():
    try:
        r = requests.get("https://aviationweather.gov/api/data/metar",
                          params={"ids": STATION, "format": "raw"}, timeout=10)
        raw = r.text.strip()
        if not raw: return None, None, None
        wm = re.search(r"\b(\d{3})(\d{2,3})(MPS|KT)\b", raw)
        wind_dir = wind_deg_to_octant(int(wm.group(1))) if wm else None
        sky = None
        if "CAVOK" in raw or "SKC" in raw or "NSC" in raw: sky = "despejado"
        elif re.search(r"\bRA\b|SHRA|TSRA", raw): sky = "lluvia"
        elif re.search(r"\bBKN|OVC\b", raw): sky = "nublado"
        elif re.search(r"\bFEW|SCT\b", raw): sky = "parcial"
        return wind_dir, sky, raw
    except Exception as e:
        print(f"  [WARN] METAR: {e}")
        return None, None, None


def fetch_metar_history(hours_back=25):
    try:
        r = requests.get("https://aviationweather.gov/api/data/metar",
                          params={"ids": STATION, "format": "json", "hours": hours_back}, timeout=10)
        out = []
        for e in r.json():
            t, ot = e.get("temp"), e.get("obsTime")
            if t is None or ot is None: continue
            dt = datetime.fromtimestamp(ot, tz=ZoneInfo("UTC")).astimezone(ZoneInfo(TZ))
            out.append((dt, float(t)))
        out.sort(key=lambda x: x[0])
        return out
    except Exception as e:
        print(f"  [WARN] METAR history: {e}")
        return []


def compare_real_vs_ecmwf(target_date, ecmwf_hourly):
    history = fetch_metar_history(hours_back=14)
    if not history: return [], None
    comparisons = []
    for dt, real_t in history:
        if dt.strftime("%Y-%m-%d") != target_date: continue
        model_t = ecmwf_hourly.get(dt.strftime("%H:00"))
        if model_t is None: continue
        comparisons.append((dt.strftime("%H:%M"), real_t, model_t, real_t - model_t))
    if not comparisons: return [], None
    return comparisons, sum(c[3] for c in comparisons) / len(comparisons)


def fetch_ecmwf_hourly_full(target_date):
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": LAT, "longitude": LON, "hourly": "temperature_2m",
            "timezone": TZ, "start_date": target_date, "end_date": target_date, "models": "ecmwf_ifs025",
        }, timeout=10)
        h = r.json()["hourly"]
        return {t.split("T")[1]: v for t, v in zip(h["time"], h["temperature_2m"])}
    except Exception:
        return {}


def load_readings():
    if not READINGS_FILE.exists():
        return {}
    try:
        return json.loads(READINGS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [WARN] {READINGS_FILE.name} corrupto, se ignora: {e}")
        return {}


def save_readings(r):
    READINGS_FILE.parent.mkdir(exist_ok=True)
    tmp = READINGS_FILE.with_suffix(READINGS_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(r, indent=2), encoding="utf-8")
    tmp.replace(READINGS_FILE)


def save_dashboard_snapshot(date, now_str, real_t, models, wind_dir, sky):
    """Resumen chico para dashboard_combinado.py — no reemplaza el envio individual
    de este script, solo deja el ultimo estado calculado disponible para la grilla."""
    snap = {
        "city": "Milan", "brand": BRAND, "date": date, "updated_at": now_str,
        "updated_at_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
        "unit": "C", "real_temp": real_t, "wind_dir": wind_dir, "sky": sky,
        "models": models,
    }
    DASH_SNAPSHOT_FILE.parent.mkdir(exist_ok=True)
    tmp = DASH_SNAPSHOT_FILE.with_suffix(DASH_SNAPSHOT_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DASH_SNAPSHOT_FILE)


def append_and_smooth(date, model_name, value):
    r = load_readings()
    key = f"{date}_{model_name}"
    lst = r.get(key, [])
    lst.append(value); lst = lst[-20:]
    r[key] = lst; save_readings(r)
    recent = lst[-SMOOTH_N:]
    return sum(recent) / len(recent), len(recent)


def bucket_probs(center, sigma, pad=12):
    """Buckets within +/-pad degrees of the forecast center — avoids silently dropping
    out-of-range forecasts (e.g. summer heatwaves) instead of clamping to a fixed window."""
    low, high = int(math.floor(center - pad)), int(math.ceil(center + pad)) + 1
    return {b: norm_cdf((b + 0.5 - center) / sigma) - norm_cdf((b - 0.5 - center) / sigma)
            for b in range(low, high)}


MESES_EN = ["january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december"]


def _pregunta_es_del_dia(q, target_date):
    """La pregunta de Polymarket trae la fecha en texto ("... on July 28"); target_date
    viene como YYYY-MM-DD. Chequeo agregado 2026-07-28: `target_date` era un parametro
    que la funcion recibia y NUNCA usaba, asi que se devolvian los mercados de la ciudad
    de cualquier fecha y colisionaban entre si por numero de bucket."""
    try:
        _, m, d = target_date.split("-")
        return f"on {MESES_EN[int(m) - 1]} {int(d)}" in q
    except Exception:
        return True   # ante una fecha rara, mejor no filtrar de mas


def fetch_milan_market_meta(target_date):
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets",
                          params={"active": "true", "closed": "false", "limit": 200}, timeout=(3, 8))
        out = []
        for m in r.json():
            q = (m.get("question") or "").lower()
            if "milan" not in q: continue
            # Polymarket publica TAMBIEN un mercado de temperatura MINIMA para esta
            # ciudad, con la misma forma de pregunta y numeros de bucket que se solapan
            # con los del maximo. Es el mismo bug que se encontro el 2026-07-24 en los
            # analyze_*.py (reportaba "ganador 27C" cuando el maximo real fue 33C); el
            # arreglo se aplico alli pero nunca volvio a los check_*.py.
            if "highest temperature" not in q: continue
            if not _pregunta_es_del_dia(q, target_date): continue
            match = re.search(r"be (\d+)°?c", q)
            if not match: continue
            out.append({"bucket": int(match.group(1)), "market_id": m.get("id"), "question": m.get("question")})
        return out
    except Exception as e:
        print(f"  [WARN] mercados: {e}")
        return []


def fetch_live_price(market_id):
    """Price to BUY at — bestAsk, not bestBid (bestBid is what you'd get selling)."""
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 6))
        ask = r.json().get("bestAsk")
        return float(ask) if ask is not None else None
    except Exception:
        return None


def fetch_market_resolution(market_id):
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 6))
        d = r.json()
        if not d.get("closed", False): return None
        prices = d.get("outcomePrices")
        if prices is None: return None
        if isinstance(prices, str): prices = json.loads(prices)
        yp = float(prices[0])
        if yp >= 0.9: return True
        if yp <= 0.1: return False
        return None
    except Exception:
        return None


def calc_ev(p, price):
    if price is None or price <= 0 or price >= 1: return None
    return p * (1.0 / price - 1.0) - (1.0 - p)


def load_tracking():
    if not TRACKING_FILE.exists():
        return {}
    try:
        return json.loads(TRACKING_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [WARN] {TRACKING_FILE.name} corrupto, se ignora: {e}")
        return {}


def save_tracking(t):
    TRACKING_FILE.parent.mkdir(exist_ok=True)
    tmp = TRACKING_FILE.with_suffix(TRACKING_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(t, indent=2), encoding="utf-8")
    tmp.replace(TRACKING_FILE)


def load_stats():
    if not STATS_FILE.exists():
        return {"wins": 0, "losses": 0, "pnl": 0.0}
    try:
        return json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [WARN] {STATS_FILE.name} corrupto, se ignora: {e}")
        return {"wins": 0, "losses": 0, "pnl": 0.0}


def save_stats(s):
    STATS_FILE.parent.mkdir(exist_ok=True)
    tmp = STATS_FILE.with_suffix(STATS_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(s, indent=2), encoding="utf-8")
    tmp.replace(STATS_FILE)


def maybe_record_daily_picks(date, probs_by_model, market_meta):
    now_hour = datetime.now(ZoneInfo(TZ)).hour
    if now_hour < PICK_HOUR or not market_meta: return
    tracking = load_tracking()
    if date in tracking: return

    all_buckets = set().union(*[set(p) for p in probs_by_model.values()]) if probs_by_model else set()
    scored = []
    for b in all_buckets:
        best = max((p.get(b, 0) for p in probs_by_model.values()), default=0)
        scored.append((b, best))
    scored.sort(key=lambda x: -x[1])
    top = scored[:PICKS_PER_DAY]

    picks = []
    for bucket, best_p in top:
        match = next((m for m in market_meta if m["bucket"] == bucket), None)
        if not match: continue
        bid = fetch_live_price(match["market_id"])
        if bid is None or bid <= 0 or bid >= 1: continue
        shares = round(PICK_SIZE_USD / bid, 2)
        picks.append({"bucket": bucket, "market_id": match["market_id"], "entry_price": bid,
                      "cost": PICK_SIZE_USD, "shares": shares, "status": "pending", "result": None, "pnl": None})
    if not picks: return
    tracking[date] = {"recorded_at": datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M"), "picks": picks}
    save_tracking(tracking)
    msg = [f"📌 MILAN — PICKS DEL DÍA — {date}"]
    for p in picks:
        msg.append(f"  {p['bucket']}°C @ ${p['entry_price']:.3f}")
    send_telegram("\n".join(msg))


def resolve_pending_picks():
    tracking = load_tracking()
    stats = load_stats()
    updated, lines = False, []
    for date_key, day in tracking.items():
        for pick in day["picks"]:
            if pick["status"] != "pending": continue
            won = fetch_market_resolution(pick["market_id"])
            if won is None: continue
            pick["status"] = "resolved"; pick["result"] = "WIN" if won else "LOSS"
            pick["pnl"] = round(pick["shares"] - pick["cost"], 2) if won else round(-pick["cost"], 2)
            stats["wins" if won else "losses"] += 1
            stats["pnl"] = round(stats.get("pnl", 0.0) + pick["pnl"], 2)
            updated = True
            lines.append(f"{date_key} — {pick['bucket']}°C: {pick['result']} ({pick['pnl']:+.2f})")
    if updated:
        save_tracking(tracking); save_stats(stats)
        total = stats["wins"] + stats["losses"]
        wr = stats["wins"] / total * 100 if total else 0
        send_telegram("\n".join(["🎯 MILAN RESULTADOS"] + lines + [f"\nAcumulado: {stats['wins']}W/{stats['losses']}L ({wr:.0f}%) PnL {stats['pnl']:+.2f}"]))


def draw_signature(ax, fig):
    """Firma 'EndyReport' + silueta de gato (cabeza + orejas), blanco sobre contorno oscuro.
    transAxes no es isotrópico en un eje ancho y bajo — se corrige el radio horizontal
    por el aspecto físico real del eje para que la cabeza se vea circular, no ovalada."""
    pos = ax.get_position()
    fig_w, fig_h = fig.get_size_inches()
    scale = (pos.height * fig_h) / (pos.width * fig_w)

    cx, cy = 0.965, 0.55
    ry = 0.30
    rx = ry * scale

    ax.add_patch(Ellipse(
        (cx, cy), width=2*rx, height=2*ry, transform=ax.transAxes,
        facecolor="#ffffff", edgecolor="#111111", linewidth=1.1, zorder=60, clip_on=False))
    for side in (-1, 1):
        ear = plt.Polygon([
            (cx + side*0.70*rx, cy + 0.55*ry),
            (cx + side*1.55*rx, cy + 1.75*ry),
            (cx + side*0.10*rx, cy + 0.95*ry),
        ], transform=ax.transAxes, closed=True, facecolor="#ffffff",
           edgecolor="#111111", linewidth=1.1, zorder=60, clip_on=False)
        ax.add_patch(ear)
    ax.text(cx - rx*3.2, cy, "EndyReport", transform=ax.transAxes, fontsize=7.5,
            color=C_TEXT_DIM, fontweight="bold", ha="right", va="center", zorder=61,
            family="sans-serif", clip_on=False)


def _card(ax, title):
    ax.set_facecolor(C_PANEL)
    for s in ax.spines.values(): s.set_visible(False)
    ax.set_title(title, color=C_ACCENT, fontsize=14, fontweight="bold", loc="left", pad=8)


def render_dashboard(date, now_str, real_t, finals, peak_hours, wind_dir, sky,
                      hrs_cycle, cycle_label, fresh, rain_lines, taf_lines,
                      probs_by_model, chart_rows, temp_series, avg_diff, n_obs):
    # Se quitaron los paneles de "ciclo del modelo" y "TAF" (texto pequeño y poco
    # accionable para una lectura rapida) para poder agrandar la letra de todo lo
    # demas sin necesitar un PNG mas alto de lo que Telegram muestra legible.
    fig = plt.figure(figsize=(10, 13), facecolor=C_BG)
    gs = fig.add_gridspec(7, 3, height_ratios=[0.6, 1.15, 1.15, 1.3, 1.3, 1.5, 1.7],
                           hspace=0.65, wspace=0.3, left=0.07, right=0.96, top=0.97, bottom=0.02)

    axh = fig.add_subplot(gs[0, :]); axh.axis("off")
    axh.text(0, 0.75, BRAND, fontsize=26, color=C_ACCENT, fontweight="bold", transform=axh.transAxes)
    axh.text(0, 0.2, f"Milan ({STATION}) · {date} · {now_str}", fontsize=13, color=C_TEXT_DIM, transform=axh.transAxes)
    draw_signature(axh, fig)

    cards = [("REAL AHORA", real_t, C_REAL)] + [
        (f"{n.upper()} ({peak_hours.get(n) or '?'})", finals.get(n), MODEL_COLORS[n]) for n in MODELS
    ]
    for i, (label, val, color) in enumerate(cards[:3]):
        ax = fig.add_subplot(gs[1, i]); _card(ax, label); ax.axis("off")
        ax.text(0.05, 0.30, f"{val:.1f}°C" if val is not None else "s/d", fontsize=24, color=color,
                fontweight="bold", transform=ax.transAxes)

    ax_4 = fig.add_subplot(gs[2, 0]); _card(ax_4, cards[3][0]); ax_4.axis("off")
    ax_4.text(0.05, 0.30, f"{cards[3][1]:.1f}°C" if cards[3][1] is not None else "s/d", fontsize=21,
              color=cards[3][2], fontweight="bold", transform=ax_4.transAxes)
    ax_ws = fig.add_subplot(gs[2, 1:]); _card(ax_ws, "VIENTO / CIELO / TENDENCIA REAL"); ax_ws.axis("off")
    wtxt = f"{wind_dir or '—'} · {sky or '—'}"
    if avg_diff is not None:
        sign = "+" if avg_diff >= 0 else ""
        wtxt += f"   |   real vs ECMWF: {sign}{avg_diff:.1f}°C ({n_obs} obs)"
    ax_ws.text(0.03, 0.35, wtxt, fontsize=13.5, color=C_TEXT, transform=ax_ws.transAxes)

    ax_spark = fig.add_subplot(gs[3, :]); _card(ax_spark, "TEMPERATURA REAL — 24H (METAR)")
    if temp_series and len(temp_series) >= 2:
        hrs = [t[0] for t in temp_series]; temps = [t[1] for t in temp_series]
        ax_spark.plot(hrs, temps, color=C_REAL, linewidth=2.5, marker="o", markersize=4)
        ax_spark.fill_between(range(len(hrs)), temps, min(temps)-0.5, color=C_REAL, alpha=0.1)
        step = max(1, len(hrs)//8)
        ax_spark.set_xticks(range(0, len(hrs), step))
        ax_spark.set_xticklabels([hrs[i] for i in range(0, len(hrs), step)], color=C_TEXT_DIM, fontsize=11)
        ax_spark.set_facecolor(C_PANEL); ax_spark.tick_params(axis="y", colors=C_TEXT_DIM, labelsize=11)
        for s in ax_spark.spines.values(): s.set_visible(False)
        ax_spark.grid(axis="y", color=C_GRID, linewidth=0.5)
    else:
        ax_spark.axis("off")
        ax_spark.text(0.03, 0.5, "Datos insuficientes.", fontsize=12, color=C_TEXT_DIM, transform=ax_spark.transAxes)

    ax_rain = fig.add_subplot(gs[4, :]); _card(ax_rain, "LLUVIA / TORMENTA PREVISTA")
    ax_rain.axis("off")
    if rain_lines:
        y = 0.85
        for line in rain_lines[:4]:
            ax_rain.text(0.03, y, line, fontsize=13, color=C_TEXT, transform=ax_rain.transAxes); y -= 0.24
    else:
        ax_rain.text(0.03, 0.5, "Sin lluvia/tormenta relevante.", fontsize=13, color=C_TEXT_DIM, transform=ax_rain.transAxes)

    ax_prob = fig.add_subplot(gs[5, :]); _card(ax_prob, "PROBABILIDAD POR BUCKET — 3 MODELOS SEPARADOS")
    all_b = sorted(set().union(*[set(p) for p in probs_by_model.values()])) if probs_by_model else []
    all_b = [b for b in all_b if max(p.get(b, 0) for p in probs_by_model.values()) >= MIN_MODEL_PROB]
    if all_b:
        x = range(len(all_b)); w = 0.25
        for i, name in enumerate(MODELS):
            probs = probs_by_model.get(name, {})
            vals = [probs.get(b, 0)*100 for b in all_b]
            ax_prob.bar([xi + (i-1)*w for xi in x], vals, width=w, color=MODEL_COLORS[name], label=name.upper())
        ax_prob.set_xticks(list(x)); ax_prob.set_xticklabels([str(b) for b in all_b], color=C_TEXT_DIM, fontsize=12)
        ax_prob.set_facecolor(C_PANEL); ax_prob.tick_params(axis="y", colors=C_TEXT_DIM, labelsize=11)
        for s in ax_prob.spines.values(): s.set_visible(False)
        ax_prob.grid(axis="y", color=C_GRID, linewidth=0.5)
        ax_prob.legend(facecolor=C_PANEL, edgecolor=C_GRID, labelcolor=C_TEXT, fontsize=11)

    ax_mkt = fig.add_subplot(gs[6, :]); _card(ax_mkt, "MERCADO VS MODELOS — en vivo")
    if chart_rows:
        buckets = [f"{r[0]}°C" for r in chart_rows]
        mkt = [r[1]*100 for r in chart_rows]
        x = range(len(buckets)); w = 0.2
        ax_mkt.bar([i - 1.5*w for i in x], mkt, width=w, color=C_TEXT_DIM, label="Mercado")
        for j, name in enumerate(MODELS):
            vals = [r[2].get(name, 0)*100 for r in chart_rows]
            ax_mkt.bar([i + (j-0.5)*w for i in x], vals, width=w, color=MODEL_COLORS[name], label=name.upper())
        ax_mkt.set_xticks(list(x)); ax_mkt.set_xticklabels(buckets, color=C_TEXT_DIM, fontsize=12)
        ax_mkt.set_facecolor(C_PANEL); ax_mkt.tick_params(axis="y", colors=C_TEXT_DIM, labelsize=11)
        for s in ax_mkt.spines.values(): s.set_visible(False)
        ax_mkt.grid(axis="y", color=C_GRID, linewidth=0.5)
        ax_mkt.legend(facecolor=C_PANEL, edgecolor=C_GRID, labelcolor=C_TEXT, fontsize=10)
    else:
        ax_mkt.axis("off")
        ax_mkt.text(0.03, 0.5, "Sin mercado activo esta corrida.", fontsize=12, color=C_TEXT_DIM, transform=ax_mkt.transAxes)

    path = IMG_DIR / "milan_dashboard.png"
    fig.savefig(path, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def run_check():
    date = datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d")
    now_str = datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M")

    print("=" * 60)
    print(f"  MILAN — ICON-D2 / AROME / ECMWF — {date} ({now_str})")
    print("=" * 60)

    hrs_cycle, cycle_label = hours_since_last_model_cycle()
    fresh = hrs_cycle <= 2
    print(f"\n  [CICLO] {cycle_label} UTC, hace {hrs_cycle:.1f}h {'⚡ FRESCO' if fresh else ''}")

    peaks, peak_hours = {}, {}
    for name, model_id in MODELS.items():
        peak_t, peak_h = fetch_hourly_peak(model_id, date)
        peaks[name] = peak_t; peak_hours[name] = peak_h
        print(f"  {name.upper():8} {peak_t}  a las {peak_h}" if peak_t else f"  {name.upper():8} sin dato")

    wind_dir, sky, raw_metar = fetch_metar_condition()
    print(f"  METAR: {raw_metar or 'sin dato'}")

    finals = {}
    for name, peak_t in peaks.items():
        if peak_t is None: finals[name] = None; continue
        smooth, n = append_and_smooth(date, name, peak_t)
        finals[name] = smooth

    # Persist the smoothed ECMWF value actually used for trading (no bias correction —
    # Milan doesn't have a BIAS_TABLE yet), so analyze_milan_accuracy.py can score
    # against the real signal, not a raw reading.
    if finals.get("ecmwf") is not None:
        readings = load_readings()
        readings[f"{date}_ecmwf_final"] = [finals["ecmwf"]]
        save_readings(readings)

    daily = fetch_daily_conditions(date)
    rain_windows = fetch_rain_windows(date)
    tag_map = {"storm": "TORMENTA", "heavy": "lluvia fuerte", "light": "lluvia ligera/moderada"}
    rain_lines = [f"{tag_map.get(w['class'],'lluvia')}  {w['start']}–{w['end']}  (~{w['total_mm']:.1f}mm)" for w in rain_windows]

    taf_periods = fetch_taf_structured()
    taf_lines = format_taf_summary(taf_periods)

    ecmwf_hourly = fetch_ecmwf_hourly_full(date)
    history = fetch_metar_history()
    real_t = history[-1][1] if history else None
    temp_series = [(dt.strftime("%H:%M"), t) for dt, t in history]
    comparisons, avg_diff = compare_real_vs_ecmwf(date, ecmwf_hourly)
    if real_t is not None:
        print(f"  Temp. real ahora: {real_t:.1f}°C")

    probs_by_model = {name: bucket_probs(v, SIGMA_DEFAULT) for name, v in finals.items() if v is not None}

    market_meta = fetch_milan_market_meta(date)
    chart_rows = []
    if market_meta:
        print("\n  MERCADO EN VIVO:")
        for m in market_meta:
            bucket, mid = m["bucket"], m["market_id"]
            price = fetch_live_price(mid)
            if price is None or price <= 0 or price >= 1: continue
            probs_here = {n: p.get(bucket, 0.0) for n, p in probs_by_model.items()}
            evs = {n: calc_ev(p, price) for n, p in probs_here.items()}
            print(f"    {bucket}°C  mkt ${price:.3f}  " + " | ".join(f"{n.upper()} {p*100:.0f}% (EV{evs[n]:+.2f})" for n, p in probs_here.items()))
            chart_rows.append((bucket, price, probs_here))

    dash_path = render_dashboard(
        date, now_str, real_t, finals, peak_hours, wind_dir, sky,
        hrs_cycle, cycle_label, fresh, rain_lines, taf_lines,
        probs_by_model, chart_rows, temp_series, avg_diff, len(comparisons),
    )
    save_dashboard_snapshot(date, now_str, real_t, finals, wind_dir, sky)
    print("\n  [DASHBOARD] Snapshot guardado (imagen individual ya no se manda — ver dashboard_combinado.py).")

    maybe_record_daily_picks(date, probs_by_model, market_meta)
    resolve_pending_picks()

    wu_max = fetch_wu_max_hoy()

    real_txt = f"{real_t:.1f}°C" if real_t is not None else "s/d"
    wu_txt = f"  |  WU hoy: {wu_max:.1f}°C" if wu_max is not None else ""
    modelos_txt = " · ".join(
        f"{n.upper()} {finals[n]:.1f}°" if finals.get(n) is not None else f"{n.upper()} s/d"
        for n in MODELS
    )
    lines = [
        f"🧭 {BRAND} · {date} {now_str}",
        f"Real: {real_txt}{wu_txt}",
        f"Modelos: {modelos_txt}",
    ]
    if rain_lines:
        lines.append(f"🌧 {len(rain_lines)} evento(s) de lluvia/tormenta hoy")
    print("  [RESUMEN] " + " | ".join(lines))
    print("=" * 60)


def main():
    print(f"{BRAND} — iniciado (paridad completa con Helsinki/Chengdu)")
    print(f"Revisa cada {CHECK_INTERVAL//60} min\nCtrl+C para detener\n")
    while True:
        try:
            run_check()
        except Exception as e:
            print(f"  [ERROR] {e}")
        print(f"\n  Próxima revisión en {CHECK_INTERVAL//60} min...\n")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
