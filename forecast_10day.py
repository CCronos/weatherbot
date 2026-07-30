#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
forecast_10day.py — Proyección ECMWF vs ICON para Chengdu a 10 días, en imagen.
Sombrea los primeros 5 días (zona de mayor confianza meteorológica).
"""

import sys
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json

# Windows consoles often default to a legacy codepage (cp1252) that can't encode
# the em-dashes/symbols used in these prints — force UTF-8 so a print() never crashes.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

LAT, LON = 30.5728, 103.9469
IMG_DIR = Path("data/images")
IMG_DIR.mkdir(parents=True, exist_ok=True)
HIGH_CONFIDENCE_DAYS = 5

BRAND = "SIGNAL // CDU — PROYECCIÓN 10 DÍAS"
C_BG      = "#12203a"
C_PANEL   = "#1b2f52"
C_ACCENT  = "#f4a536"
C_ECMWF   = "#3ecfc0"
C_ICON    = "#c77dff"
C_HILITE  = "#2a4a6a"   # zona sombreada de alta confianza
C_TEXT    = "#e8edf5"
C_TEXT_DIM= "#8ea0c2"
C_GRID    = "#2a4066"

with open("config.json", encoding="utf-8") as f:
    cfg = json.load(f)
TELEGRAM_TOKEN     = cfg.get("telegram_token", "")
TELEGRAM_CHAT_ID   = cfg.get("telegram_chat_id", "")
TELEGRAM_CHAT_ID_2 = cfg.get("telegram_chat_id_2", "")


def send_telegram_photo(image_path, caption=""):
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    for chat_id in [TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID_2]:
        if not chat_id:
            continue
        try:
            with open(image_path, "rb") as f:
                requests.post(url, data={"chat_id": chat_id, "caption": caption[:1024]},
                              files={"photo": f}, timeout=(8, 25))
        except Exception as e:
            print(f"  [WARN] Telegram photo send failed: {e}")


def fetch_series(model_key, days=10):
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": LAT, "longitude": LON,
                "daily": "temperature_2m_max,precipitation_probability_max",
                "timezone": "Asia/Shanghai",
                "forecast_days": days,
                "models": model_key,
            },
            timeout=15,
        )
        d = r.json()["daily"]
        return list(zip(d["time"], d["temperature_2m_max"], d["precipitation_probability_max"]))
    except Exception as e:
        print(f"  [WARN] Error trayendo {model_key}: {e}")
        return []


def hours_since_last_model_cycle():
    """ECMWF/ICON publican en ciclos 00z/06z/12z/18z UTC. Calcula hace cuánto salió el último."""
    now_utc = datetime.now(ZoneInfo("UTC"))
    cycle_hours = [0, 6, 12, 18]
    last_cycle = max([h for h in cycle_hours if h <= now_utc.hour], default=18)
    last_cycle_time = now_utc.replace(hour=last_cycle, minute=0, second=0, microsecond=0)
    if last_cycle_time > now_utc:
        last_cycle_time -= timedelta(days=1)
    delta = now_utc - last_cycle_time
    return delta.total_seconds() / 3600, f"{last_cycle:02d}z"


def render_forecast_dashboard(ecmwf, icon, now_str, hrs_since_cycle, cycle_label):
    fig = plt.figure(figsize=(10, 12), facecolor=C_BG)
    gs = fig.add_gridspec(4, 2, height_ratios=[0.5, 0.7, 2.6, 1.2],
                           hspace=0.5, wspace=0.25, left=0.07, right=0.96, top=0.97, bottom=0.03)

    # Header
    ax_head = fig.add_subplot(gs[0, :]); ax_head.axis("off")
    ax_head.text(0, 0.7, BRAND, fontsize=19, color=C_ACCENT, fontweight="bold", transform=ax_head.transAxes)
    ax_head.text(0, 0.15, f"Chengdu (ZUUU) · {now_str}", fontsize=9.5, color=C_TEXT_DIM, transform=ax_head.transAxes)

    # --- Tarjeta: velocidad vs precisión (ciclo del modelo) ---
    ax_cycle = fig.add_subplot(gs[1, :])
    ax_cycle.set_facecolor(C_PANEL)
    for s in ax_cycle.spines.values(): s.set_visible(False)
    ax_cycle.set_title("VELOCIDAD VS PRECISIÓN — CICLO DE ACTUALIZACIÓN DEL MODELO", color=C_ACCENT,
                        fontsize=10.5, fontweight="bold", loc="left", pad=6)
    ax_cycle.axis("off")
    fresh = hrs_since_cycle <= 2
    freshness_color = C_ECMWF if fresh else C_TEXT_DIM
    ax_cycle.text(0.03, 0.35,
                  f"Último ciclo del modelo: {cycle_label} UTC · hace {hrs_since_cycle:.1f}h"
                  + ("  →  dato FRESCO, ventaja informativa alta" if fresh else "  →  el mercado ya tuvo tiempo de ajustarse"),
                  fontsize=9.5, color=freshness_color, fontweight="bold" if fresh else "normal",
                  transform=ax_cycle.transAxes)

    # --- Gráfico de líneas ECMWF vs ICON, 10 días, con sombreado 5 días ---
    ax_chart = fig.add_subplot(gs[2, :])
    ax_chart.set_facecolor(C_PANEL)
    for s in ax_chart.spines.values(): s.set_visible(False)
    ax_chart.set_title("PROYECCIÓN DE TEMPERATURA MÁXIMA — 10 DÍAS", color=C_ACCENT,
                        fontsize=11, fontweight="bold", loc="left", pad=8)

    dates_e = [d[0][5:] for d in ecmwf]
    temps_e = [d[1] for d in ecmwf]
    temps_i = [d[1] for d in icon] if icon else [None] * len(dates_e)

    n = len(dates_e)
    valid_temps_e = [t for t in temps_e if t is not None]
    if n >= HIGH_CONFIDENCE_DAYS and valid_temps_e:
        label_y = max(valid_temps_e) + 1
        ax_chart.axvspan(-0.5, HIGH_CONFIDENCE_DAYS - 0.5, color=C_HILITE, alpha=0.6, zorder=0)
        ax_chart.text(HIGH_CONFIDENCE_DAYS/2 - 0.5, label_y,
                       "ALTA CONFIANZA\n(1-5 días)", ha="center", fontsize=8.5, color=C_ECMWF,
                       fontweight="bold")
        ax_chart.text((HIGH_CONFIDENCE_DAYS + n)/2 - 0.5, label_y,
                       "BAJA CONFIANZA\n(6-10 días)", ha="center", fontsize=8.5, color=C_TEXT_DIM,
                       fontweight="bold")

    ax_chart.plot(range(n), temps_e, color=C_ECMWF, linewidth=2.4, marker="o", markersize=6, label="ECMWF")
    if any(t is not None for t in temps_i):
        ax_chart.plot(range(len(temps_i)), temps_i, color=C_ICON, linewidth=2.4, marker="s", markersize=6, label="ICON")

    ax_chart.axvline(HIGH_CONFIDENCE_DAYS - 0.5, color=C_ACCENT, linestyle="--", linewidth=1, alpha=0.7)
    ax_chart.set_xticks(range(n))
    ax_chart.set_xticklabels(dates_e, color=C_TEXT_DIM, fontsize=8.5, rotation=0)
    ax_chart.tick_params(axis="y", colors=C_TEXT_DIM, labelsize=9)
    ax_chart.grid(axis="y", color=C_GRID, linewidth=0.5)
    ax_chart.legend(facecolor=C_PANEL, edgecolor=C_GRID, labelcolor=C_TEXT, fontsize=9, loc="lower right")
    ax_chart.set_ylabel("°C", color=C_TEXT_DIM)

    # --- Tabla ---
    ax_tbl = fig.add_subplot(gs[3, :])
    ax_tbl.set_facecolor(C_PANEL)
    for s in ax_tbl.spines.values(): s.set_visible(False)
    ax_tbl.set_title("DETALLE POR DÍA", color=C_ACCENT, fontsize=11, fontweight="bold", loc="left", pad=8)
    ax_tbl.axis("off")

    headers = ["FECHA", "ECMWF", "LLUVIA%", "ICON", "LLUVIA%", "DIF."]
    xpos = [0.02, 0.22, 0.36, 0.52, 0.66, 0.82]
    y = 0.90
    for h, x in zip(headers, xpos):
        ax_tbl.text(x, y, h, fontsize=8.5, color=C_ACCENT, fontweight="bold", transform=ax_tbl.transAxes)
    y -= 0.11

    for idx in range(n):
        date_e, temp_e, rain_e = ecmwf[idx]
        if idx < len(icon):
            date_i, temp_i, rain_i = icon[idx]
        else:
            temp_i, rain_i = None, None

        row_bg = idx < HIGH_CONFIDENCE_DAYS
        if row_bg:
            ax_tbl.add_patch(plt.Rectangle((0.0, y - 0.02), 1.0, 0.10, transform=ax_tbl.transAxes,
                                             color=C_HILITE, alpha=0.5, zorder=0))

        diff_str = f"{temp_e - temp_i:+.1f}°C" if (temp_e is not None and temp_i is not None) else "s/d"
        vals = [date_e, f"{temp_e:.1f}°C" if temp_e is not None else "s/d",
                f"{rain_e:.0f}%" if rain_e is not None else "s/d",
                f"{temp_i:.1f}°C" if temp_i is not None else "s/d",
                f"{rain_i:.0f}%" if rain_i is not None else "s/d", diff_str]
        for v, x in zip(vals, xpos):
            ax_tbl.text(x, y, v, fontsize=8.5, color=C_TEXT, transform=ax_tbl.transAxes)
        y -= 0.105

    path = IMG_DIR / "forecast_10day.png"
    fig.savefig(path, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def main():
    now_str = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    print("=" * 70)
    print(f"  CHENGDU — Proyección 10 días — {now_str}")
    print("=" * 70)

    ecmwf = fetch_series("ecmwf_ifs025", 10)
    icon = fetch_series("icon_seamless", 10)

    if not ecmwf:
        print("No se pudo obtener el pronóstico de ECMWF.")
        return

    print(f"\n  {'Fecha':<12}{'ECMWF':<10}{'lluvia%':<10}{'ICON':<10}{'lluvia%':<10}{'Diferencia'}")
    print("  " + "-" * 62)
    for idx, (date_e, temp_e, rain_e) in enumerate(ecmwf):
        if idx < len(icon):
            date_i, temp_i, rain_i = icon[idx]
        else:
            temp_i, rain_i = None, None
        diff_str = f"{temp_e - temp_i:+.1f}°C" if (temp_e is not None and temp_i is not None) else "s/d"
        rain_i_str = f"{rain_i:.0f}" if rain_i is not None else "s/d"
        temp_i_str = f"{temp_i:.1f}" if temp_i is not None else "s/d"
        temp_e_str = f"{temp_e:.1f}" if temp_e is not None else "s/d"
        rain_e_str = f"{rain_e:.0f}" if rain_e is not None else "s/d"
        marker = " <- alta confianza" if idx < HIGH_CONFIDENCE_DAYS else ""
        print(f"  {date_e:<12}{temp_e_str:<10}{rain_e_str:<10}{temp_i_str:<10}{rain_i_str:<10}{diff_str}{marker}")

    hrs_since_cycle, cycle_label = hours_since_last_model_cycle()
    print(f"\n  Último ciclo de modelo: {cycle_label} UTC, hace {hrs_since_cycle:.1f}h")

    dash_path = render_forecast_dashboard(ecmwf, icon, now_str, hrs_since_cycle, cycle_label)
    send_telegram_photo(dash_path, caption=f"📅 Proyección 10 días Chengdu — {now_str}")
    print("\n  [TELEGRAM] Imagen enviada.")
    print("=" * 70)


if __name__ == "__main__":
    main()