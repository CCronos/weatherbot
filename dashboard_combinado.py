#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dashboard_combinado.py — Resumen visual de las 6 ciudades (Buenos Aires, Helsinki,
Hong Kong, Milan, New York, Chengdu) en una sola imagen tipo grilla, leyendo los
snapshots que cada check_*.py ya deja en data/dashboard_snapshot_{city}.json al
final de su propio run_check(). No importa ni modifica esos scripts, y no hace
ninguna llamada a las APIs de clima/mercado — solo lee JSON chicos y arma una
imagen. Los 6 envios individuales de cada script siguen llegando igual; este es
un resumen adicional al mismo chat de Telegram (telegram_chat_dashboards).
"""

import sys
import json
import time
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

with open("config.json", encoding="utf-8") as f:
    cfg = json.load(f)
TELEGRAM_TOKEN = cfg.get("telegram_token", "")
TELEGRAM_CHAT_ID = cfg.get("telegram_chat_dashboards") or cfg.get("telegram_chat_id", "")

IMG_DIR = Path("data/images")
IMG_DIR.mkdir(parents=True, exist_ok=True)
CHECK_INTERVAL = 3600  # 1h — solo lee 6 JSON chicos, barato correr seguido

# key -> (nombre ciudad, brand, color de acento del panel)
CITIES = [
    ("buenos_aires", "Buenos Aires", "SIGNAL // BUE", "#c98500"),
    ("helsinki",     "Helsinki",     "SIGNAL // HEL", "#3fa7d6"),
    ("hong_kong",    "Hong Kong",    "SIGNAL // HKG", "#e0554f"),
    ("milan",        "Milan",        "SIGNAL // MIL", "#5ec962"),
    ("new_york",     "New York",     "SIGNAL // NYC", "#7e7ce0"),
    ("chengdu",      "Chengdu",      "SIGNAL // CDU", "#e0a83f"),
]

# Colores fijos por nombre de modelo, consistentes con la convencion de los
# dashboards individuales (mismo modelo = mismo color en todas las ciudades).
MODEL_COLORS = {
    "gfs": "#3987e5", "hrrr": "#3987e5", "jma_gsm": "#3987e5",
    "icon_global": "#9085e9", "icon_eu": "#9085e9", "icon_d2": "#9085e9", "icon": "#9085e9",
    "ecmwf": "#199e70",
    "ukmo": "#e07b9d",
    "gem": "#e0a83f", "arome": "#e0a83f",
}

C_BG, C_PANEL, C_TEXT, C_DIM = "#0d1117", "#161b22", "#e6edf3", "#8b949e"
C_STALE = "#f85149"
STALE_HOURS = 6  # panel se atenua si el snapshot es mas viejo que esto


def send_telegram_photo(path, caption=""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(path, "rb") as fh:
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                          files={"photo": fh}, timeout=(5, 20))
    except Exception as e:
        print(f"  [WARN] Telegram: {e}")


def load_snapshot(key):
    path = Path(f"data/dashboard_snapshot_{key}.json")
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [WARN] {path.name} corrupto, se ignora: {e}")
        return None


def hours_since_utc(iso_ts):
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts)
    except Exception:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def render_grid(rows):
    fig, axes = plt.subplots(2, 3, figsize=(15, 9), facecolor=C_BG)
    fig.suptitle(f"SIGNAL — Dashboard combinado · {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                 color=C_TEXT, fontsize=17, fontweight="bold", y=0.98)

    for ax, (key, name, brand, accent, snap) in zip(axes.flat, rows):
        ax.set_facecolor(C_PANEL)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.set_xticks([]); ax.set_yticks([])
        ax.set_xlim(0, 1); ax.set_ylim(0, 1)

        if snap is None:
            ax.text(0.5, 0.5, f"{name}\nsin datos todavía", ha="center", va="center",
                    color=C_DIM, fontsize=13, transform=ax.transAxes)
            continue

        age_h = hours_since_utc(snap.get("updated_at_utc"))
        stale = age_h is not None and age_h > STALE_HOURS
        fade = 0.4 if stale else 1.0

        ax.text(0.04, 0.90, brand, color=accent, fontsize=14, fontweight="bold",
                alpha=fade, transform=ax.transAxes)

        unit = snap.get("unit", "C")
        real_t = snap.get("real_temp")
        real_txt = f"{real_t:.1f}°{unit}" if real_t is not None else "s/d"
        ax.text(0.04, 0.66, real_txt, color=C_TEXT, fontsize=30, fontweight="bold",
                alpha=fade, transform=ax.transAxes)

        y = 0.46
        for mname, mval in (snap.get("models") or {}).items():
            mtxt = f"{mname.upper()}: {mval:.1f}°{unit}" if mval is not None else f"{mname.upper()}: s/d"
            color = MODEL_COLORS.get(mname, C_DIM)
            ax.text(0.06, y, mtxt, color=color, fontsize=11.5, alpha=fade, transform=ax.transAxes)
            y -= 0.13

        wind_dir, sky = snap.get("wind_dir"), snap.get("sky")
        if wind_dir or sky:
            ax.text(0.04, 0.10, f"{wind_dir or '—'} · {sky or '—'}", color=C_DIM,
                    fontsize=10.5, alpha=fade, transform=ax.transAxes)

        age_txt = f"hace {age_h:.1f}h" if age_h is not None else "?"
        stale_flag = "  (VIEJO)" if stale else ""
        ax.text(0.96, 0.04, f"act. {age_txt}{stale_flag}",
                color=C_STALE if stale else C_DIM, fontsize=9.5, ha="right", transform=ax.transAxes)

    path = IMG_DIR / "dashboard_combinado.png"
    fig.savefig(path, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def run_check():
    rows = [(key, name, brand, accent, load_snapshot(key)) for key, name, brand, accent in CITIES]
    n_ok = sum(1 for r in rows if r[4] is not None)
    print(f"  [DASHBOARD COMBINADO] {n_ok}/{len(rows)} ciudades con datos")

    path = render_grid(rows)
    caption = f"SIGNAL — Dashboard combinado · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    send_telegram_photo(path, caption=caption)
    print("  [TELEGRAM] Dashboard combinado enviado.")


def main():
    print("SIGNAL — Dashboard combinado — iniciado")
    print(f"Revisa cada {CHECK_INTERVAL // 60} min\nCtrl+C para detener\n")
    while True:
        try:
            run_check()
        except Exception as e:
            print(f"  [ERROR] {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
