#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_ecmwf_accuracy.py — Desde el 9 de julio en adelante: pronóstico ECMWF
vs. resultado real en Polymarket. Fuente de pronóstico: chengdu_readings.json
(mantenido por check_chengdu.py) + archivos viejos de bot_v2.py como respaldo.
Búsqueda ampliada con varias consultas para no perder mercados históricos.
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

READINGS_FILE = Path("data/chengdu_readings.json")   # fuente principal (check_chengdu.py)
HISTORY_FILE = Path("data/chengdu_forecast_history.json")  # respaldo viejo
MARKETS_DIR = Path("data/markets")                    # respaldo bot_v2.py
IMG_DIR = Path("data/images")
EARLY_RESOLVE_HOUR = 19
START_DATE = "2026-07-09"
SMOOTH_N = 3  # must match check_chengdu.py's smoothing window
SEARCH_QUERIES = ["chengdu temperature", "chengdu", "highest temperature chengdu", "Chengdu°C"]

BRAND = "PRECISION REPORT — CDU"
# Mismo tema de Chengdu (acento naranja) que check_chengdu.py, para que ambos reportes
# se lean como parte del mismo "canal". Aciertos/fallos usan los colores de estado
# (good/critical) en vez de una paleta categórica — es literalmente un estado bueno/malo.
C_BG, C_PANEL, C_ACCENT = "#262638", "#39364b", "#d95926"
C_WIN, C_LOSS = "#0ca30c", "#d03b3b"
C_TEXT, C_TEXT_DIM, C_GRID = "#ffffff", "#c3c2b7", "#4a4558"

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
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
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


def fetch_all_chengdu_markets():
    found = {}
    seen_market_ids = set()
    total_events = 0

    for query in SEARCH_QUERIES:
        events = search_once(query)
        total_events += len(events)
        for ev in events:
            for m in ev.get("markets", []):
                q = m.get("question", "")
                if "chengdu" not in q.lower():
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
          f"{len(seen_market_ids)} mercados únicos de Chengdu | fechas encontradas: {sorted(found.keys())}")

    # Respaldo: endpoint /markets viejo, por si algún día sigue faltando
    if len(found) < 3:
        print("  [DEBUG] pocos resultados, probando /markets como respaldo adicional...")
        for closed_flag in ["false", "true"]:
            try:
                r = requests.get("https://gamma-api.polymarket.com/markets",
                                  params={"closed": closed_flag, "limit": 500}, timeout=(5, 15))
                for m in r.json():
                    q = m.get("question", "")
                    if "chengdu" not in q.lower():
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
    """Prioridad: 1) valor final (suavizado + bias-corrected, lo que realmente se usa para
    operar en check_chengdu.py) 2) fallback: promedio suavizado del crudo, sin bias-correction
    (days antiguos que no tienen el valor final persistido) 3) archivo viejo de bot_v2.py
    4) chengdu_forecast_history.json (respaldo antiguo)"""
    if READINGS_FILE.exists():
        try:
            readings = json.loads(READINGS_FILE.read_text())
            final_key = f"{date_str}_ecmwf_final"
            if final_key in readings and readings[final_key]:
                return readings[final_key][-1], "check_chengdu(final,bias-corrected)"
            key = f"{date_str}_ecmwf"
            if key in readings and readings[key]:
                recent = readings[key][-SMOOTH_N:]
                return sum(recent) / len(recent), "check_chengdu(smoothed,no-bias)"
        except Exception:
            pass

    bot_v2_file = MARKETS_DIR / f"chengdu_{date_str}.json"
    if bot_v2_file.exists():
        try:
            mkt = json.loads(bot_v2_file.read_text(encoding="utf-8"))
            for snap in reversed(mkt.get("forecast_snapshots", [])):
                if snap.get("ecmwf") is not None:
                    return snap["ecmwf"], "bot_v2"
        except Exception:
            pass

    if HISTORY_FILE.exists():
        try:
            hist = json.loads(HISTORY_FILE.read_text())
            val = hist.get(date_str, {}).get("ecmwf")
            if val is not None:
                return val, "history_antiguo"
        except Exception:
            pass

    return None, "sin_fuente"


def is_date_fully_past(date_str):
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return False
    return day < now.date()


def is_date_past_resolve_hour(date_str):
    tz = ZoneInfo("Asia/Shanghai")
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
    all_markets = fetch_all_chengdu_markets()
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


def _card(ax, title):
    ax.set_facecolor(C_PANEL)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title(title, color=C_ACCENT, fontsize=11, fontweight="bold", loc="left", pad=8)


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
    axh.text(0, 0.15, f"Chengdu · ECMWF vs resultado real · muestra: {len(decided)} · {now_str}",
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

    ax_days = fig.add_subplot(gs[2, :]); _card(ax_days, "ECMWF PREDICHO VS RESULTADO — POR DÍA (desde 9 jul)")
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

    ax_tbl = fig.add_subplot(gs[3, :]); _card(ax_tbl, "DETALLE COMPLETO — DESDE EL 9 DE JULIO")
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

    path = IMG_DIR / "accuracy_dashboard.png"
    fig.savefig(path, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path, wr, len(hits), len(misses)


def main():
    now_str = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    print("=" * 80)
    print(f"  ANÁLISIS — ECMWF vs RESULTADO REAL, desde {START_DATE}")
    print("=" * 80)

    rows = collect_rows()
    if not rows:
        print("\n  ⚠️ No se encontró NINGÚN mercado de Chengdu.")
        return

    print(f"\n  {'Fecha':<12}{'ECMWF':<9}{'Fuente':<22}{'Predicho':<11}{'Ganador':<11}{'Resultado':<11}{'Modo'}")
    print("  " + "-" * 96)
    for r in rows:
        ecmwf_str = f"{r['last_ecmwf']:.1f}°C" if r["last_ecmwf"] is not None else "s/d"
        pred_str = f"{r['predicted_bucket']}°C" if r["predicted_bucket"] is not None else "—"
        win_str = f"{r['winning_bucket']}°C" if r["winning_bucket"] is not None else "-"
        result_str = "ACIERTO" if r["hit"] is True else "FALLO" if r["hit"] is False else "-"
        print(f"  {r['date']:<12}{ecmwf_str:<9}{r['source']:<22}{pred_str:<11}{win_str:<11}{result_str:<11}{r['mode']}")

    dash_path, wr, n_hits, n_misses = render_accuracy_dashboard(rows, now_str)
    send_telegram_photo(dash_path, caption=f"📈 Reporte de precisión ECMWF (desde {START_DATE}) — {n_hits} aciertos / {n_misses} fallos ({wr:.0f}%)")

    print(f"\n  RESUMEN: {n_hits} aciertos | {n_misses} fallos | Tasa: {wr:.1f}%")
    print("  [TELEGRAM] Dashboard enviado.")
    print("=" * 80)


if __name__ == "__main__":
    main()