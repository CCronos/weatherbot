#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_helsinki_accuracy.py — Desde el inicio del tracking en adelante: pronóstico
ECMWF vs. resultado real en Polymarket para Helsinki. Fuente de pronóstico:
helsinki_readings.json (mantenido por check_helsinki.py).
"""

import sys
import json
import re
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

# Windows consoles often default to a legacy codepage (cp1252) that can't encode
# the em-dashes/accents used in these prints — force UTF-8 so a print() never crashes.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

READINGS_FILE = Path("data/helsinki_readings.json")
IMG_DIR = Path("data/images")
EARLY_RESOLVE_HOUR = 19
START_DATE = "2026-07-13"
SEARCH_QUERIES = ["helsinki temperature", "helsinki", "highest temperature helsinki", "Helsinki°C"]
SMOOTH_N = 3  # must match check_helsinki.py's smoothing window

BRAND = "PRECISION REPORT — HEL"
# Mismo tema de Helsinki (acento azul) que check_helsinki.py.
C_BG, C_PANEL, C_ACCENT = "#162a4b", "#203d6a", "#3987e5"
C_WIN, C_LOSS = "#0ca30c", "#d03b3b"
C_TEXT, C_TEXT_DIM, C_GRID = "#ffffff", "#c3c2b7", "#2c4066"

with open("config.json", encoding="utf-8") as f:
    cfg = json.load(f)
TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID_2 = (
    cfg.get("telegram_token", ""), cfg.get("telegram_chat_id", ""), cfg.get("telegram_chat_id_2", "")
)
IMG_DIR.mkdir(parents=True, exist_ok=True)


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


def parse_date_bucket(question):
    months = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
              "july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
    q = question.lower()
    if "highest temperature" not in q:
        # Polymarket tambien puede publicar un mercado de temperatura MINIMA con la
        # misma forma de pregunta y numeros de bucket solapados - sin este filtro se
        # mezclan bajo la misma fecha+bucket (encontrado y corregido primero en
        # analyze_hong_kong_accuracy.py el 2026-07-24).
        return None, None
    date_match = re.search(r"on (\w+) (\d{1,2})", q)
    bucket_match = re.search(r"be (\d+)°?c", q)
    if not date_match or not bucket_match:
        return None, None
    month = months.get(date_match.group(1))
    if not month:
        return None, None
    day = int(date_match.group(2))
    now = datetime.now(ZoneInfo("Europe/Helsinki"))
    year = now.year
    if month < now.month - 6:  # e.g. parsing "January" while it's currently December
        year += 1
    return f"{year}-{month:02d}-{day:02d}", int(bucket_match.group(1))


def search_once(query):
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/public-search",
            params={"q": query, "limit_per_type": 200, "events_status": "all"},
            timeout=(5, 15),
        )
        data = r.json()
        return data.get("events", []) if isinstance(data, dict) else []
    except Exception as e:
        print(f"  [WARN] búsqueda '{query}' falló: {e}")
        return []


def fetch_all_helsinki_markets():
    found = {}
    seen_market_ids = set()
    total_events = 0

    for query in SEARCH_QUERIES:
        events = search_once(query)
        total_events += len(events)
        for ev in events:
            for m in ev.get("markets", []):
                q = m.get("question", "")
                if "helsinki" not in q.lower():
                    continue
                mid = m.get("id")
                if mid in seen_market_ids:
                    continue
                date_str, bucket = parse_date_bucket(q)
                if not date_str or bucket is None:
                    continue
                seen_market_ids.add(mid)
                found.setdefault(date_str, []).append({
                    "bucket": bucket, "market_id": mid, "closed": m.get("closed", False),
                })

    print(f"  [DEBUG] {total_events} eventos revisados en {len(SEARCH_QUERIES)} búsquedas | "
          f"{len(seen_market_ids)} mercados únicos de Helsinki | fechas encontradas: {sorted(found.keys())}")

    if len(found) < 3:
        print("  [DEBUG] pocos resultados, probando /markets como respaldo adicional...")
        for closed_flag in ["false", "true"]:
            try:
                r = requests.get("https://gamma-api.polymarket.com/markets",
                                  params={"closed": closed_flag, "limit": 500}, timeout=(5, 15))
                for m in r.json():
                    q = m.get("question", "")
                    if "helsinki" not in q.lower():
                        continue
                    date_str, bucket = parse_date_bucket(q)
                    if not date_str or bucket is None:
                        continue
                    mid = m.get("id")
                    if mid in seen_market_ids:
                        continue
                    seen_market_ids.add(mid)
                    found.setdefault(date_str, []).append({
                        "bucket": bucket, "market_id": mid, "closed": m.get("closed", False),
                    })
            except Exception as e:
                print(f"  [WARN] respaldo /markets falló: {e}")

    return found


def fetch_market_price(market_id):
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 6))
        d = r.json()
        prices = d.get("outcomePrices")
        if prices is None:
            return None, False
        if isinstance(prices, str):
            prices = json.loads(prices)
        return float(prices[0]), d.get("closed", False)
    except Exception:
        return None, False


def get_ecmwf_forecast_for_date(date_str):
    """Prioridad: 1) valor final (suavizado + bias-corrected, lo que realmente se usa
    para operar en check_helsinki.py) 2) fallback: promedio suavizado del crudo, sin
    bias-correction (días antiguos que no tienen el valor final persistido)."""
    if READINGS_FILE.exists():
        try:
            readings = json.loads(READINGS_FILE.read_text())
            final_key = f"{date_str}_ecmwf_final"
            if final_key in readings and readings[final_key]:
                return readings[final_key][-1], "check_helsinki(final,bias-corrected)"
            key = f"{date_str}_ecmwf"
            if key in readings and readings[key]:
                recent = readings[key][-SMOOTH_N:]
                return sum(recent) / len(recent), "check_helsinki(smoothed,no-bias)"
        except Exception:
            pass
    return None, "sin_fuente"


def is_date_fully_past(date_str):
    tz = ZoneInfo("Europe/Helsinki")
    now = datetime.now(tz)
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return False
    return day < now.date()


def is_date_past_resolve_hour(date_str):
    tz = ZoneInfo("Europe/Helsinki")
    now = datetime.now(tz)
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return False
    if day < now.date():
        return True
    if day == now.date():
        return now.hour >= EARLY_RESOLVE_HOUR
    return False


def find_winning_bucket(markets_for_date, date_str):
    prices = {}
    for m in markets_for_date:
        price, closed = fetch_market_price(m["market_id"])
        if price is not None:
            prices[m["bucket"]] = {"price": price, "closed": closed}

    for bucket, info in prices.items():
        if info["closed"] and info["price"] >= 0.9:
            return bucket, "closed_oficial"
    if is_date_past_resolve_hour(date_str):
        for bucket, info in prices.items():
            if info["price"] >= 0.99:
                return bucket, "de_facto_19h"
    if is_date_fully_past(date_str) and prices:
        best = max(prices.items(), key=lambda x: x[1]["price"])
        return best[0], "forzado_fecha_pasada"
    return None, "sin_resolver"


def collect_rows():
    all_markets = fetch_all_helsinki_markets()
    rows = []
    for date_str in sorted(all_markets.keys()):
        if date_str < START_DATE:
            continue
        last_ecmwf, source = get_ecmwf_forecast_for_date(date_str)
        predicted_bucket = round(last_ecmwf) if last_ecmwf is not None else None
        winning_bucket, mode = find_winning_bucket(all_markets[date_str], date_str)
        resolved = winning_bucket is not None
        hit = (predicted_bucket == winning_bucket) if (resolved and predicted_bucket is not None) else None
        rows.append({"date": date_str, "last_ecmwf": last_ecmwf, "predicted_bucket": predicted_bucket,
                    "winning_bucket": winning_bucket, "resolved": resolved, "hit": hit,
                    "mode": mode, "source": source})
    return rows


def _card(ax, title):
    ax.set_facecolor(C_PANEL)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title(title, color=C_ACCENT, fontsize=11, fontweight="bold", loc="left", pad=8)


def draw_signature(ax, fig):
    """Firma 'EndyReport' + silueta de gato (cabeza + orejas), blanco sobre contorno oscuro."""
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


def render_accuracy_dashboard(rows, now_str):
    decided = [r for r in rows if r["resolved"] and r["hit"] is not None]
    hits = [r for r in decided if r["hit"]]
    misses = [r for r in decided if not r["hit"]]
    wr = (len(hits) / len(decided) * 100) if decided else 0

    fig = plt.figure(figsize=(10, 11), facecolor=C_BG)
    gs = fig.add_gridspec(4, 3, height_ratios=[0.55, 1.0, 1.5, 1.8],
                           hspace=0.65, wspace=0.3, left=0.08, right=0.95, top=0.97, bottom=0.03)

    axh = fig.add_subplot(gs[0, :]); axh.axis("off")
    axh.text(0, 0.7, BRAND, fontsize=19, color=C_ACCENT, fontweight="bold", transform=axh.transAxes)
    axh.text(0, 0.15, f"Helsinki · ECMWF vs resultado real · muestra: {len(decided)} · {now_str}",
             fontsize=9.5, color=C_TEXT_DIM, transform=axh.transAxes)
    draw_signature(axh, fig)

    ax_wr = fig.add_subplot(gs[1, 0]); _card(ax_wr, "TASA DE ACIERTO"); ax_wr.axis("off")
    wr_color = C_WIN if wr >= 50 else C_LOSS
    ax_wr.text(0.06, 0.25, f"{wr:.0f}%", fontsize=36, color=wr_color, fontweight="bold", transform=ax_wr.transAxes)

    ax_hm = fig.add_subplot(gs[1, 1]); _card(ax_hm, "ACIERTOS / FALLOS"); ax_hm.axis("off")
    ax_hm.text(0.06, 0.30, f"{len(hits)}✓", fontsize=24, color=C_WIN, fontweight="bold", transform=ax_hm.transAxes)
    ax_hm.text(0.55, 0.30, f"{len(misses)}✗", fontsize=24, color=C_LOSS, fontweight="bold", transform=ax_hm.transAxes)

    ax_donut = fig.add_subplot(gs[1, 2]); _card(ax_donut, "DISTRIBUCIÓN")
    if decided:
        ax_donut.pie([len(hits), len(misses)], colors=[C_WIN, C_LOSS], startangle=90,
                     wedgeprops=dict(width=0.42, edgecolor=C_PANEL, linewidth=2))
        ax_donut.text(0, 0, f"{wr:.0f}%", ha="center", va="center", fontsize=16, color=C_TEXT, fontweight="bold")
    else:
        ax_donut.axis("off")
        ax_donut.text(0.1, 0.5, "Sin datos aún", fontsize=9, color=C_TEXT_DIM, transform=ax_donut.transAxes)

    ax_days = fig.add_subplot(gs[2, :]); _card(ax_days, f"ECMWF PREDICHO VS RESULTADO — POR DÍA (desde {START_DATE[5:]})")
    valid_rows = [r for r in rows if r["predicted_bucket"] is not None]
    if valid_rows:
        dates = [r["date"][5:] for r in valid_rows]
        bar_colors, heights = [], []
        for r in valid_rows:
            if r["hit"] is True: bar_colors.append(C_WIN); heights.append(1)
            elif r["hit"] is False: bar_colors.append(C_LOSS); heights.append(1)
            else: bar_colors.append(C_TEXT_DIM); heights.append(0.5)
        bars = ax_days.bar(dates, heights, color=bar_colors, width=0.55)
        ax_days.set_ylim(0, 1.3); ax_days.set_yticks([])
        ax_days.set_facecolor(C_PANEL)
        ax_days.tick_params(axis="x", colors=C_TEXT_DIM, labelsize=8)
        for s in ax_days.spines.values(): s.set_visible(False)
        for b, r in zip(bars, valid_rows):
            label = "✓" if r["hit"] is True else "✗" if r["hit"] is False else "?"
            ax_days.text(b.get_x() + b.get_width()/2, b.get_height() + 0.05, label,
                        ha="center", fontsize=10, color=C_TEXT, fontweight="bold")
    else:
        ax_days.axis("off")
        ax_days.text(0.03, 0.5, "Sin pronósticos registrados.", fontsize=9, color=C_TEXT_DIM, transform=ax_days.transAxes)

    ax_tbl = fig.add_subplot(gs[3, :]); _card(ax_tbl, f"DETALLE COMPLETO — DESDE {START_DATE}")
    ax_tbl.axis("off")
    y = 0.90
    for label, x in zip(["FECHA", "ECMWF", "PREDICHO", "GANADOR", "RESULTADO"], [0.02, 0.20, 0.40, 0.58, 0.80]):
        ax_tbl.text(x, y, label, fontsize=8.5, color=C_ACCENT, fontweight="bold", transform=ax_tbl.transAxes)
    y -= 0.09
    for r in rows[-12:]:
        ecmwf_str = f"{r['last_ecmwf']:.1f}°C" if r["last_ecmwf"] is not None else "s/d"
        pred_str = f"{r['predicted_bucket']}°C" if r["predicted_bucket"] is not None else "—"
        win_str = f"{r['winning_bucket']}°C" if r["winning_bucket"] is not None else ("pendiente" if not r["resolved"] else "?")
        result_str = "ACIERTO" if r["hit"] is True else "FALLO" if r["hit"] is False else "-"
        result_color = C_WIN if r["hit"] is True else C_LOSS if r["hit"] is False else C_TEXT_DIM
        for val, x, color in zip([r["date"], ecmwf_str, pred_str, win_str, result_str],
                                   [0.02, 0.20, 0.40, 0.58, 0.80],
                                   [C_TEXT, C_TEXT, C_TEXT, C_TEXT, result_color]):
            ax_tbl.text(x, y, val, fontsize=8.3, color=color,
                       fontweight="bold" if x == 0.80 else "normal", transform=ax_tbl.transAxes)
        y -= 0.078

    path = IMG_DIR / "helsinki_accuracy_dashboard.png"
    fig.savefig(path, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path, wr, len(hits), len(misses)


def main():
    now_str = datetime.now(ZoneInfo("Europe/Helsinki")).strftime("%Y-%m-%d %H:%M")
    print("=" * 80)
    print(f"  ANÁLISIS — ECMWF vs RESULTADO REAL (Helsinki), desde {START_DATE}")
    print("=" * 80)

    rows = collect_rows()
    if not rows:
        print("\n  ⚠️ No se encontró NINGÚN mercado de Helsinki.")
        return

    print(f"\n  {'Fecha':<12}{'ECMWF':<9}{'Fuente':<28}{'Predicho':<11}{'Ganador':<11}{'Resultado':<11}{'Modo'}")
    print("  " + "-" * 100)
    for r in rows:
        ecmwf_str = f"{r['last_ecmwf']:.1f}°C" if r["last_ecmwf"] is not None else "s/d"
        pred_str = f"{r['predicted_bucket']}°C" if r["predicted_bucket"] is not None else "—"
        win_str = f"{r['winning_bucket']}°C" if r["winning_bucket"] is not None else "-"
        result_str = "ACIERTO" if r["hit"] is True else "FALLO" if r["hit"] is False else "-"
        print(f"  {r['date']:<12}{ecmwf_str:<9}{r['source']:<28}{pred_str:<11}{win_str:<11}{result_str:<11}{r['mode']}")

    dash_path, wr, n_hits, n_misses = render_accuracy_dashboard(rows, now_str)
    send_telegram_photo(dash_path, caption=f"📈 Reporte de precisión ECMWF Helsinki (desde {START_DATE}) — {n_hits} aciertos / {n_misses} fallos ({wr:.0f}%)")

    print(f"\n  RESUMEN: {n_hits} aciertos | {n_misses} fallos | Tasa: {wr:.1f}%")
    print("  [TELEGRAM] Dashboard enviado.")
    print("=" * 80)


if __name__ == "__main__":
    main()
