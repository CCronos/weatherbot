#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_chengdu.py — Chengdu: ECMWF e ICON SEPARADOS (sin promediar)
Real (METAR) | Pico ECMWF hoy/mañana | Pico ICON hoy/mañana | Lluvia/tormenta
Ciclo de actualización del modelo | Dashboard con mejor contraste | Tracking automático
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

LAT, LON = 30.5728, 103.9469
STATION = "ZUUU"
WU_COUNTRY = "CN"
WU_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"  # clave publica embebida en wunderground.com


def fetch_wu_max_hoy():
    """Maximo real registrado HOY hasta ahora segun Wunderground - la fuente que
    realmente liquida el mercado (Chengdu resuelve via Wunderground, confirmado)."""
    fecha = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
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
READINGS_FILE = Path("data/chengdu_readings.json")
TRACKING_FILE = Path("data/chengdu_tracking.json")
STATS_FILE = Path("data/chengdu_stats.json")
DASH_SNAPSHOT_FILE = Path("data/dashboard_snapshot_chengdu.json")
IMG_DIR = Path("data/images")
CHECK_INTERVAL = 3600  # 1h — Chengdu es la ciudad principal, reporte más frecuente
PEAK_HOURS = ("14:00", "15:00", "16:00", "17:00", "18:00")
MIN_EV_HIGHLIGHT = 0.05
SMOOTH_N = 3
MIN_MODEL_PROB = 0.01
RAIN_PROB_THRESHOLD = 40
HEAVY_MM_THRESHOLD = 2.0
PICK_HOUR = 8
PICK_SIZE_USD = 20.0
PICKS_PER_DAY = 3
SIGMA_DEFAULT = 1.3

BRAND = "SIGNAL // CDU"


def hours_since_last_model_cycle():
    now_utc = datetime.now(ZoneInfo("UTC"))
    cycle_hours = [0, 6, 12, 18]
    last_cycle = max([h for h in cycle_hours if h <= now_utc.hour], default=18)
    last_cycle_time = now_utc.replace(hour=last_cycle, minute=0, second=0, microsecond=0)
    if last_cycle_time > now_utc:
        last_cycle_time -= timedelta(days=1)
    delta = now_utc - last_cycle_time
    return delta.total_seconds() / 3600, f"{last_cycle:02d}z"


# Tema único de Chengdu — fondo/panel tintados con el acento naranja de la ciudad,
# derivados por mezcla computada (no a ojo) desde el navy base compartido por todas
# las ciudades. Colores de modelo (ECMWF/ICON) son fijos en TODOS los dashboards
# de todas las ciudades, para que el mismo modelo siempre se lea con el mismo color.
C_BG      = "#262638"
C_PANEL   = "#39364b"
C_PANEL2  = "#484259"   # tono más claro para tarjetas de valores, mejor contraste
C_ACCENT  = "#d95926"   # naranja — identidad única de Chengdu
C_ECMWF   = "#199e70"   # aqua — ECMWF, igual en todas las ciudades
C_ICON    = "#9085e9"   # violeta — familia ICON, igual en todas las ciudades
C_REAL    = "#ffffff"
C_TEXT    = "#ffffff"
C_TEXT_DIM= "#c3c2b7"
C_GRID    = "#4a4558"

BIAS_TABLE = {
    ("O",  "parcial"):    (+1.66, 100, 12),
    ("NE", "despejado"):  (+1.58, 100, 8),
    ("NE", "seco"):       (+1.12, 95, 20),
    ("S",  "parcial"):    (+1.46, 90, 21),
    ("NE", None):         (+1.13, 90, 31),
    ("S",  "seco"):       (+1.36, 89, 45),
    ("N",  "lluvia"):     (+0.00, 50, 24),
    ("N",  "nublado"):    (-0.20, 60, 10),
    ("O",  "despejado"):  (+0.92, 64, 11),
    ("N",  "parcial"):    (+0.54, 66, 32),
    ("NO", "despejado"):  (-0.02, 67, 9),
    ("NO", "lluvia"):     (+0.40, 68, 56),
    (None, "lluvia"):     (+0.40, 68, 56),
}

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

with open("config.json", encoding="utf-8") as f:
    cfg = json.load(f)
TELEGRAM_TOKEN     = cfg.get("telegram_token", "")
TELEGRAM_CHAT_ID   = cfg.get("telegram_chat_dashboards") or cfg.get("telegram_chat_id", "")
TELEGRAM_CHAT_ID_2 = cfg.get("telegram_chat_id_2", "")

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


def fetch_daily_max(model_key, target_date, next_date):
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": LAT, "longitude": LON, "daily": "temperature_2m_max",
            "timezone": "Asia/Shanghai", "start_date": target_date, "end_date": next_date,
            "models": model_key,
        }, timeout=10)
        vals = r.json()["daily"]["temperature_2m_max"]
        return (vals[0] if len(vals) > 0 else None), (vals[1] if len(vals) > 1 else None)
    except Exception as e:
        print(f"  [WARN] {model_key}: {e}")
        return None, None


def fetch_hourly_peak(model_key, target_date):
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": LAT, "longitude": LON, "hourly": "temperature_2m",
            "timezone": "Asia/Shanghai", "start_date": target_date, "end_date": target_date,
            "models": model_key,
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
        print(f"  [WARN] horario {model_key}: {e}")
        return None, None


def fetch_daily_conditions(target_date):
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": LAT, "longitude": LON,
            "daily": "precipitation_probability_max,precipitation_sum,weathercode,windspeed_10m_max,winddirection_10m_dominant",
            "timezone": "Asia/Shanghai", "start_date": target_date, "end_date": target_date,
            "models": "ecmwf_ifs025",
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
            "timezone": "Asia/Shanghai", "start_date": target_date, "end_date": target_date,
            "models": "ecmwf_ifs025",
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


def wind_deg_to_octant(deg):
    dirs = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]
    return dirs[int((deg + 22.5) // 45) % 8]


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
            dt = datetime.fromtimestamp(ot, tz=ZoneInfo("UTC")).astimezone(ZoneInfo("Asia/Shanghai"))
            out.append((dt, float(t)))
        out.sort(key=lambda x: x[0])
        return out
    except Exception as e:
        print(f"  [WARN] METAR history: {e}")
        return []


def get_bias(wind_dir, sky):
    for key in [(wind_dir, sky), (wind_dir, None), (None, sky)]:
        if key in BIAS_TABLE: return BIAS_TABLE[key]
    return None


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


def save_dashboard_snapshot(date, now_str, real_temp, ecmwf_final, icon_final, wind_dir, sky):
    """Resumen chico para dashboard_combinado.py — no reemplaza el envio individual
    de este script, solo deja el ultimo estado calculado disponible para la grilla."""
    snap = {
        "city": "Chengdu", "brand": BRAND, "date": date, "updated_at": now_str,
        "updated_at_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
        "unit": "C", "real_temp": real_temp, "wind_dir": wind_dir, "sky": sky,
        "models": {"ecmwf": ecmwf_final, "icon": icon_final},
    }
    DASH_SNAPSHOT_FILE.parent.mkdir(exist_ok=True)
    tmp = DASH_SNAPSHOT_FILE.with_suffix(DASH_SNAPSHOT_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DASH_SNAPSHOT_FILE)


def append_and_smooth(target_date, model_name, value):
    readings = load_readings()
    key = f"{target_date}_{model_name}"
    day_list = readings.get(key, [])
    day_list.append(value); day_list = day_list[-20:]
    readings[key] = day_list; save_readings(readings)
    recent = day_list[-SMOOTH_N:]
    return sum(recent) / len(recent), len(recent)


def bucket_probs(center, sigma, pad=12):
    """Buckets within +/-pad degrees of the forecast center — avoids silently dropping
    out-of-season forecasts that fall outside a hardcoded range."""
    low, high = int(math.floor(center - pad)), int(math.ceil(center + pad)) + 1
    return {b: norm_cdf((b + 0.5 - center) / sigma) - norm_cdf((b - 0.5 - center) / sigma) for b in range(low, high)}


def load_market_meta(target_date):
    path = Path(f"data/markets/chengdu_{target_date}.json")
    if not path.exists(): return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("all_outcomes", [])
    except Exception:
        return None


def fetch_live_market_price(market_id):
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


def maybe_record_daily_picks(target_date, ecmwf_probs, icon_probs, outcomes_meta):
    now_hour = datetime.now(ZoneInfo("Asia/Shanghai")).hour
    if now_hour < PICK_HOUR or not outcomes_meta: return None
    tracking = load_tracking()
    if target_date in tracking: return None

    all_buckets = set(ecmwf_probs) | set(icon_probs)
    scored = []
    for b in all_buckets:
        pe = ecmwf_probs.get(b, 0.0); pi = icon_probs.get(b, 0.0)
        scored.append((b, max(pe, pi), pe, pi))
    scored.sort(key=lambda x: -x[1])
    top = scored[:PICKS_PER_DAY]

    picks = []
    for bucket, best_p, pe, pi in top:
        match = next((o for o in outcomes_meta if o["range"][0] == o["range"][1] == float(bucket)), None)
        if not match: continue
        bid = fetch_live_market_price(match.get("market_id"))
        if bid is None or bid <= 0 or bid >= 1: continue
        shares = round(PICK_SIZE_USD / bid, 2)
        picks.append({"bucket": bucket, "market_id": match.get("market_id"), "entry_price": bid,
                      "ecmwf_prob": round(pe, 4), "icon_prob": round(pi, 4),
                      "cost": PICK_SIZE_USD, "shares": shares, "status": "pending", "result": None, "pnl": None})
    if not picks: return None

    tracking[target_date] = {"recorded_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M"), "picks": picks}
    save_tracking(tracking)
    msg = [f"📌 PICKS DEL DÍA — {target_date}"]
    for p in picks:
        msg.append(f"  {p['bucket']}°C @ ${p['entry_price']:.3f} (ECMWF {p['ecmwf_prob']*100:.0f}% · ICON {p['icon_prob']*100:.0f}%)")
    send_telegram("\n".join(msg))
    return picks


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
        send_telegram("\n".join(["🎯 RESULTADOS"] + lines + [f"\nAcumulado: {stats['wins']}W/{stats['losses']}L ({wr:.0f}%) PnL {stats['pnl']:+.2f}"]))


def get_weekly_winrate_series():
    tracking = load_tracking()
    series = []
    for d in sorted(tracking.keys())[-7:]:
        resolved = [p for p in tracking[d]["picks"] if p["status"] == "resolved"]
        if not resolved: continue
        wins = sum(1 for p in resolved if p["result"] == "WIN")
        series.append((d, wins / len(resolved) * 100))
    return series


def draw_signature(ax, fig):
    """Firma 'EndyReport' + silueta de gato (cabeza + orejas), blanco sobre contorno oscuro.
    transAxes no es isotrópico en un eje ancho y bajo — se corrige el radio horizontal
    por el aspecto físico real del eje para que la cabeza se vea circular, no ovalada."""
    pos = ax.get_position()
    fig_w, fig_h = fig.get_size_inches()
    scale = (pos.height * fig_h) / (pos.width * fig_w)  # y-por-x físico

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


def _value_card(ax, label, value, color):
    """Tarjeta de número grande con fondo resaltado para máximo contraste.
    El texto de viento/cielo puede ser mucho más largo que una temperatura
    ("— · despejado") — la fuente se reduce con el largo para que no se
    salga de la columna (1/3 del ancho de la figura)."""
    ax.set_facecolor(C_PANEL2)
    for s in ax.spines.values(): s.set_visible(False)
    ax.set_title(label, color=C_TEXT_DIM, fontsize=13, fontweight="bold", loc="left", pad=6)
    ax.axis("off")
    fs = 32 if len(value) <= 8 else (23 if len(value) <= 14 else 17)
    ax.text(0.5, 0.32, value, fontsize=fs, color=color, fontweight="bold",
            ha="center", va="center", family="sans-serif", transform=ax.transAxes,
            path_effects=None)


def render_dashboard(target_date, next_date, now_str, real_temp,
                      ecmwf_today, ecmwf_hour, icon_today, icon_hour,
                      ecmwf_tomorrow, icon_tomorrow,
                      wind_dir, sky, rain_lines, ecmwf_probs, icon_probs,
                      chart_rows, temp_series_24h, weekly_wr,
                      hrs_cycle, cycle_label, fresh):
    # Se quito el panel de "ciclo del modelo" (diagnostico interno, poco accionable
    # para una lectura rapida) para poder agrandar la letra de todo lo demas sin
    # necesitar un PNG mas alto de lo que Telegram muestra legible.
    fig = plt.figure(figsize=(10, 12), facecolor=C_BG)
    gs = fig.add_gridspec(7, 3, height_ratios=[0.6, 1.25, 1.3, 1.3, 1.3, 1.6, 1.7],
                           hspace=0.65, wspace=0.3, left=0.07, right=0.96, top=0.97, bottom=0.02)

    ax_head = fig.add_subplot(gs[0, :]); ax_head.axis("off")
    ax_head.text(0, 0.75, BRAND, fontsize=26, color=C_ACCENT, fontweight="bold", transform=ax_head.transAxes)
    ax_head.text(0, 0.20, f"Chengdu ({STATION}) · {target_date} · {now_str} hora local",
                 fontsize=13, color=C_TEXT_DIM, transform=ax_head.transAxes)
    draw_signature(ax_head, fig)

    cards = [("REAL AHORA", f"{real_temp:.1f}°C" if real_temp is not None else "s/d", C_REAL),
             (f"PICO ECMWF hoy ({ecmwf_hour or '?'})", f"{ecmwf_today:.1f}°C" if ecmwf_today is not None else "s/d", C_ECMWF),
             (f"PICO ICON hoy ({icon_hour or '?'})", f"{icon_today:.1f}°C" if icon_today is not None else "s/d", C_ICON)]
    for i, (label, value, color) in enumerate(cards):
        ax = fig.add_subplot(gs[1, i])
        _value_card(ax, label, value, color)

    ax_t1 = fig.add_subplot(gs[2, 0]); _value_card(ax_t1, f"ECMWF mañana ({next_date[-2:]})",
                                                     f"{ecmwf_tomorrow:.1f}°C" if ecmwf_tomorrow is not None else "s/d", C_ECMWF)
    ax_t2 = fig.add_subplot(gs[2, 1]); _value_card(ax_t2, f"ICON mañana ({next_date[-2:]})",
                                                     f"{icon_tomorrow:.1f}°C" if icon_tomorrow is not None else "s/d", C_ICON)
    ax_ws = fig.add_subplot(gs[2, 2]); _value_card(ax_ws, "VIENTO / CIELO", f"{wind_dir or '—'} · {sky or '—'}", C_TEXT)

    ax_spark = fig.add_subplot(gs[3, :]); _card(ax_spark, "TEMPERATURA REAL — ÚLTIMAS 24H (METAR)")
    if temp_series_24h and len(temp_series_24h) >= 2:
        hrs = [t[0] for t in temp_series_24h]; temps = [t[1] for t in temp_series_24h]
        ax_spark.plot(hrs, temps, color=C_REAL, linewidth=2.8, marker="o", markersize=4)
        ax_spark.fill_between(range(len(hrs)), temps, min(temps) - 0.5, color=C_REAL, alpha=0.12)
        step = max(1, len(hrs)//8)
        ax_spark.set_xticks(range(0, len(hrs), step))
        ax_spark.set_xticklabels([hrs[i] for i in range(0, len(hrs), step)], color=C_TEXT_DIM, fontsize=11)
        ax_spark.set_facecolor(C_PANEL); ax_spark.tick_params(axis="y", colors=C_TEXT_DIM, labelsize=11)
        for s in ax_spark.spines.values(): s.set_visible(False)
        ax_spark.grid(axis="y", color=C_GRID, linewidth=0.5)
    else:
        ax_spark.axis("off")
        ax_spark.text(0.03, 0.5, "Datos insuficientes.", fontsize=12, color=C_TEXT_DIM, transform=ax_spark.transAxes)

    ax_rain = fig.add_subplot(gs[4, :]); _card(ax_rain, "LLUVIA / TORMENTA PREVISTA (ECMWF)")
    ax_rain.axis("off")
    if rain_lines:
        y = 0.85
        for line in rain_lines[:4]:
            ax_rain.text(0.03, y, line, fontsize=13, color=C_TEXT, transform=ax_rain.transAxes); y -= 0.24
    else:
        ax_rain.text(0.03, 0.5, "Sin lluvia/tormenta relevante detectada.", fontsize=13, color=C_TEXT_DIM, transform=ax_rain.transAxes)

    ax_prob = fig.add_subplot(gs[5, :]); _card(ax_prob, "PROBABILIDAD POR BUCKET — ECMWF vs ICON")
    all_b = sorted(set(b for b, p in ecmwf_probs.items() if p >= MIN_MODEL_PROB) |
                    set(b for b, p in icon_probs.items() if p >= MIN_MODEL_PROB))
    if all_b:
        x = range(len(all_b)); width = 0.38
        ve = [ecmwf_probs.get(b, 0)*100 for b in all_b]
        vi = [icon_probs.get(b, 0)*100 for b in all_b]
        ax_prob.bar([i-width/2 for i in x], ve, width=width, color=C_ECMWF, label="ECMWF")
        ax_prob.bar([i+width/2 for i in x], vi, width=width, color=C_ICON, label="ICON")
        ax_prob.set_xticks(list(x)); ax_prob.set_xticklabels([str(b) for b in all_b], color=C_TEXT_DIM, fontsize=12)
        ax_prob.set_facecolor(C_PANEL); ax_prob.tick_params(axis="y", colors=C_TEXT_DIM, labelsize=11)
        for s in ax_prob.spines.values(): s.set_visible(False)
        ax_prob.grid(axis="y", color=C_GRID, linewidth=0.5)
        ax_prob.legend(facecolor=C_PANEL, edgecolor=C_GRID, labelcolor=C_TEXT, fontsize=11, loc="upper right")

    ax_mkt = fig.add_subplot(gs[6, :]); _card(ax_mkt, "MERCADO (POLYMARKET) VS. ECMWF VS. ICON — en vivo")
    if chart_rows:
        buckets = [f"{r[0]}°C" for r in chart_rows]
        mkt = [r[1]*100 for r in chart_rows]; ecv = [r[2]*100 for r in chart_rows]; icv = [r[3]*100 for r in chart_rows]
        x = range(len(buckets)); w = 0.27
        ax_mkt.bar([i-w for i in x], mkt, width=w, color=C_TEXT_DIM, label="Mercado")
        ax_mkt.bar([i for i in x], ecv, width=w, color=C_ECMWF, label="ECMWF")
        ax_mkt.bar([i+w for i in x], icv, width=w, color=C_ICON, label="ICON")
        ax_mkt.set_xticks(list(x)); ax_mkt.set_xticklabels(buckets, color=C_TEXT_DIM, fontsize=12)
        ax_mkt.set_facecolor(C_PANEL); ax_mkt.tick_params(axis="y", colors=C_TEXT_DIM, labelsize=11)
        for s in ax_mkt.spines.values(): s.set_visible(False)
        ax_mkt.grid(axis="y", color=C_GRID, linewidth=0.5)
        ax_mkt.legend(facecolor=C_PANEL, edgecolor=C_GRID, labelcolor=C_TEXT, fontsize=11, loc="upper right")
    else:
        ax_mkt.axis("off")
        ax_mkt.text(0.03, 0.5, "Sin datos de mercado esta corrida.", fontsize=12, color=C_TEXT_DIM, transform=ax_mkt.transAxes)

    path = IMG_DIR / "dashboard_latest.png"
    fig.savefig(path, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def run_check():
    target_date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    next_date = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)).strftime("%Y-%m-%d")
    now_str = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")

    print("=" * 60)
    print(f"  CHENGDU — ECMWF vs ICON (separados) — {target_date} ({now_str})")
    print("=" * 60)

    ecmwf_today_raw, ecmwf_tomorrow = fetch_daily_max("ecmwf_ifs025", target_date, next_date)
    icon_today_raw, icon_tomorrow = fetch_daily_max("icon_seamless", target_date, next_date)
    ecmwf_peak, ecmwf_hour = fetch_hourly_peak("ecmwf_ifs025", target_date)
    icon_peak, icon_hour = fetch_hourly_peak("icon_seamless", target_date)

    ecmwf_today = ecmwf_peak if ecmwf_peak is not None else ecmwf_today_raw
    icon_today = icon_peak if icon_peak is not None else icon_today_raw

    print(f"\n  ECMWF hoy: {ecmwf_today}  a las {ecmwf_hour}  |  mañana: {ecmwf_tomorrow}")
    print(f"  ICON  hoy: {icon_today}  a las {icon_hour}  |  mañana: {icon_tomorrow}")

    hrs_cycle, cycle_label = hours_since_last_model_cycle()
    fresh = hrs_cycle <= 2
    print(f"\n  [CICLO] Último modelo: {cycle_label} UTC, hace {hrs_cycle:.1f}h "
          f"{'⚡ FRESCO' if fresh else '(mercado ya pudo haber reaccionado)'}")

    if ecmwf_today is not None:
        ecmwf_smooth, ecmwf_n = append_and_smooth(target_date, "ecmwf", ecmwf_today)
    else:
        ecmwf_smooth, ecmwf_n = None, 0
    if icon_today is not None:
        icon_smooth, icon_n = append_and_smooth(target_date, "icon", icon_today)
    else:
        icon_smooth, icon_n = None, 0

    wind_dir, sky, raw_metar = fetch_metar_condition()
    print(f"  METAR: {raw_metar or 'sin dato'}")
    bias_info = get_bias(wind_dir, sky) if (wind_dir or sky) else None
    ecmwf_final, icon_final = ecmwf_smooth, icon_smooth
    if bias_info:
        bias, conf, n = bias_info
        if ecmwf_final is not None: ecmwf_final = ecmwf_final + bias
        if icon_final is not None: icon_final = icon_final + bias
        print(f"  [SESGO] {wind_dir or '-'} + {sky or '-'} -> {bias:+.2f}°C aplicado a AMBOS modelos por separado")

    # Persist the smoothed + bias-corrected value actually used for trading, so
    # analyze_ecmwf_accuracy.py can score against the real signal, not a raw reading.
    if ecmwf_final is not None:
        readings = load_readings()
        readings[f"{target_date}_ecmwf_final"] = [ecmwf_final]
        save_readings(readings)

    history = fetch_metar_history(hours_back=25)
    real_temp = history[-1][1] if history else None
    temp_series_24h = [(dt.strftime("%H:%M"), t) for dt, t in history]
    if real_temp is not None:
        print(f"  Temp. real ahora: {real_temp:.1f}°C")

    daily = fetch_daily_conditions(target_date)
    rain_windows = fetch_rain_windows(target_date)
    tag_map = {"storm": "TORMENTA", "heavy": "lluvia fuerte", "light": "lluvia ligera/moderada"}
    rain_lines = [f"{tag_map.get(w['class'],'lluvia')}  {w['start']}–{w['end']}  (~{w['total_mm']:.1f}mm)" for w in rain_windows]
    if rain_lines:
        print("\n  LLUVIA/TORMENTA:")
        for l in rain_lines: print(f"    {l}")
    else:
        print("\n  LLUVIA/TORMENTA: sin bloques relevantes.")

    ecmwf_probs = bucket_probs(ecmwf_final, SIGMA_DEFAULT) if ecmwf_final is not None else {}
    icon_probs = bucket_probs(icon_final, SIGMA_DEFAULT) if icon_final is not None else {}

    print(f"\n  Probabilidad ECMWF (centro {ecmwf_final}):" if ecmwf_final else "\n  ECMWF: sin datos")
    for b, p in sorted(ecmwf_probs.items()):
        if p >= MIN_MODEL_PROB: print(f"    {b}°C: {p*100:.1f}%")
    print(f"\n  Probabilidad ICON (centro {icon_final}):" if icon_final else "\n  ICON: sin datos")
    for b, p in sorted(icon_probs.items()):
        if p >= MIN_MODEL_PROB: print(f"    {b}°C: {p*100:.1f}%")

    outcomes_meta = load_market_meta(target_date)
    chart_rows = []
    cruce_lines = []
    relevant = sorted(set(ecmwf_probs) | set(icon_probs))
    if outcomes_meta:
        print("\n  CRUCE CON MERCADO (en vivo):")
        for o in outcomes_meta:
            lo, hi = o["range"]
            if lo != hi: continue
            bucket = int(lo)
            if bucket not in relevant: continue
            pe, pi = ecmwf_probs.get(bucket, 0), icon_probs.get(bucket, 0)
            if max(pe, pi) < MIN_MODEL_PROB: continue
            bid = fetch_live_market_price(o.get("market_id"))
            if bid is None or bid <= 0 or bid >= 1: continue
            ev_e, ev_i = calc_ev(pe, bid), calc_ev(pi, bid)
            print(f"    {bucket}°C  mkt ${bid:.3f}  ECMWF {pe*100:.1f}% (EV {ev_e:+.2f})  ICON {pi*100:.1f}% (EV {ev_i:+.2f})")
            cruce_lines.append(f"{bucket}°C: mkt {bid*100:.0f}% | ECMWF {pe*100:.0f}% (EV{ev_e:+.2f}) | ICON {pi*100:.0f}% (EV{ev_i:+.2f})")
            chart_rows.append((bucket, bid, pe, pi))

    weekly_wr = get_weekly_winrate_series()

    dash_path = render_dashboard(
        target_date, next_date, now_str, real_temp,
        ecmwf_today, ecmwf_hour, icon_today, icon_hour,
        ecmwf_tomorrow, icon_tomorrow,
        wind_dir, sky, rain_lines, ecmwf_probs, icon_probs,
        chart_rows, temp_series_24h, weekly_wr,
        hrs_cycle, cycle_label, fresh,
    )
    save_dashboard_snapshot(target_date, now_str, real_temp, ecmwf_final, icon_final, wind_dir, sky)
    print("\n  [DASHBOARD] Snapshot guardado (imagen individual ya no se manda — ver dashboard_combinado.py).")

    maybe_record_daily_picks(target_date, ecmwf_probs, icon_probs, outcomes_meta)
    resolve_pending_picks()

    wu_max = fetch_wu_max_hoy()

    real_txt = f"{real_temp:.1f}°C" if real_temp is not None else "s/d"
    wu_txt = f"  |  WU hoy: {wu_max:.1f}°C" if wu_max is not None else ""
    ecmwf_txt = f"ECMWF {ecmwf_today:.1f}°" if ecmwf_today is not None else "ECMWF s/d"
    icon_txt = f"ICON {icon_today:.1f}°" if icon_today is not None else "ICON s/d"
    lines = [
        f"🧭 {BRAND} · {target_date} {now_str}",
        f"Real: {real_txt}{wu_txt}",
        f"Modelos: {ecmwf_txt} · {icon_txt}",
    ]
    if rain_lines:
        lines.append(f"🌧 {len(rain_lines)} evento(s) de lluvia/tormenta hoy")
    print("  [RESUMEN] " + " | ".join(lines))
    print("=" * 60)


def main():
    print(f"{BRAND} — iniciado (mejor contraste + sin proyección de mañana en texto)")
    print(f"Revisa cada {CHECK_INTERVAL//60} min | Tracking automático picks {PICK_HOUR}am")
    print("Ctrl+C para detener\n")
    while True:
        try:
            run_check()
        except Exception as e:
            print(f"  [ERROR] {e}")
        print(f"\n  Próxima revisión en {CHECK_INTERVAL//60} min...\n")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()