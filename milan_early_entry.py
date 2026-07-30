#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
milan_early_entry.py — Detecta mercados NUEVOS de Milan apenas se crean
y simula una entrada temprana usando la proyección ICON-D2+AROME+ECMWF ya calculada.
NO ejecuta compras reales — solo registra y avisa.
"""

import sys
import json
import re
import time
import math
import requests
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

# Windows consoles often default to a legacy codepage (cp1252) that can't encode
# the em-dashes/emoji used in these prints — force UTF-8 so a print() never crashes.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

LAT, LON = 45.6306, 8.7281  # Malpensa Intl Airport coordinates (resolution station), not Milan city center
TZ = "Europe/Rome"
CHECK_INTERVAL = 60          # segundos entre revisiones — lo más rápido práctico sin abusar de la API
SIGMA_DEFAULT = 1.2
ENTRY_MAX_PRICE = 0.15       # solo entra si el precio está por debajo de esto
MIN_EV_HIGHLIGHT = 0.05
POSITION_SIZE_USD = 20.0
MODELS = {"icon_d2": "icon_d2", "arome": "meteofrance_arome_france_hd", "ecmwf": "ecmwf_ifs025"}
SEEN_FILE = Path("data/milan_seen_markets.json")
EARLY_TRACKING_FILE = Path("data/milan_early_tracking.json")

with open("config.json", encoding="utf-8") as f:
    cfg = json.load(f)
TELEGRAM_TOKEN     = cfg.get("telegram_token", "")
TELEGRAM_CHAT_ID   = cfg.get("telegram_chat_early_entry") or cfg.get("telegram_chat_id", "")
TELEGRAM_CHAT_ID_2 = cfg.get("telegram_chat_id_2", "")


def send_telegram(text):
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chat_id in [TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID_2]:
        if not chat_id:
            continue
        try:
            requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=(5, 15))
        except Exception as e:
            print(f"  [WARN] Telegram send failed: {e}")


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bucket_probs(center, sigma, pad=12):
    """Buckets dentro de +/-pad grados del centro del pronóstico — no un rango fijo,
    para no perder silenciosamente pronósticos fuera de temporada."""
    low, high = int(math.floor(center - pad)), int(math.ceil(center + pad)) + 1
    probs = {}
    for bucket in range(low, high):
        lo, hi = bucket - 0.5, bucket + 0.5
        probs[bucket] = norm_cdf((hi - center) / sigma) - norm_cdf((lo - center) / sigma)
    return probs


def load_seen():
    if not SEEN_FILE.exists():
        return set()
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    except Exception as e:
        print(f"  [WARN] {SEEN_FILE.name} corrupto, se ignora: {e}")
        return set()


def save_seen(seen):
    SEEN_FILE.parent.mkdir(exist_ok=True)
    tmp = SEEN_FILE.with_suffix(SEEN_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(list(seen)), encoding="utf-8")
    tmp.replace(SEEN_FILE)


def load_early_tracking():
    if not EARLY_TRACKING_FILE.exists():
        return []
    try:
        return json.loads(EARLY_TRACKING_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [WARN] {EARLY_TRACKING_FILE.name} corrupto, se ignora: {e}")
        return []


def save_early_tracking(data):
    EARLY_TRACKING_FILE.parent.mkdir(exist_ok=True)
    tmp = EARLY_TRACKING_FILE.with_suffix(EARLY_TRACKING_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(EARLY_TRACKING_FILE)


def extract_date_from_question(question):
    """Busca 'on <Month> <day>' en la pregunta del mercado y devuelve YYYY-MM-DD."""
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    }
    match = re.search(r"on (\w+) (\d{1,2})", question.lower())
    if not match:
        return None
    month_name, day = match.group(1), int(match.group(2))
    month = months.get(month_name)
    if not month:
        return None
    now = datetime.now(ZoneInfo(TZ))
    year = now.year
    if month < now.month - 6:  # e.g. parsing "January" while it's currently December
        year += 1
    try:
        return f"{year}-{month:02d}-{day:02d}"
    except Exception:
        return None


def fetch_forecast_for_date(model_key, target_date):
    """Intenta traer la proyección de un modelo para una fecha específica (hasta ~10-15 días)."""
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": LAT, "longitude": LON,
                "daily": "temperature_2m_max",
                "timezone": TZ,
                "start_date": target_date, "end_date": target_date,
                "models": model_key,
            },
            timeout=10,
        )
        vals = r.json()["daily"]["temperature_2m_max"]
        return vals[0] if vals else None
    except Exception as e:
        print(f"  [WARN] No se pudo obtener {model_key} para {target_date}: {e}")
        return None


def fetch_new_milan_markets():
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false", "limit": 200, "order": "id", "ascending": "false"},
            timeout=(3, 8),
        )
        markets = r.json()
    except Exception as e:
        print(f"  [WARN] No se pudo consultar mercados: {e}")
        return []

    return [m for m in markets if "milan" in (m.get("question") or "").lower()]


def calc_ev(p, price):
    if price is None or price <= 0 or price >= 1:
        return None
    return p * (1.0 / price - 1.0) - (1.0 - p)


def process_new_market(m, seen):
    market_id = str(m.get("id", ""))
    question = m.get("question", "")

    if market_id in seen:
        return
    seen.add(market_id)

    target_date = extract_date_from_question(question)
    if not target_date:
        print(f"  [WARN] No se pudo extraer fecha de: {question}")
        return

    try:
        price = float(json.loads(m.get("outcomePrices", "[0.5,0.5]"))[0])
    except Exception:
        price = None

    # Extraer el bucket numérico de la pregunta (ej. "be 24°C" -> 24)
    bucket_match = re.search(r"be (\d+)°?C", question, re.IGNORECASE)
    bucket = int(bucket_match.group(1)) if bucket_match else None

    print(f"\n  🆕 MERCADO NUEVO — {question}")
    print(f"     Fecha objetivo: {target_date} | Bucket: {bucket} | Precio: ${price}")

    forecasts = {name: fetch_forecast_for_date(model_id, target_date) for name, model_id in MODELS.items()}
    probs = {
        name: (bucket_probs(v, SIGMA_DEFAULT).get(bucket, 0.0) if (v is not None and bucket is not None) else None)
        for name, v in forecasts.items()
    }

    lines = [
        f"🆕 NUEVO MERCADO MILAN — {target_date}",
        question,
        f"Precio apertura: ${price:.3f}" if price is not None else "Precio: s/d",
    ]
    for name, v in forecasts.items():
        if v is not None:
            p = probs.get(name)
            lines.append(f"{name.upper()} proyecta: {v:.1f}°C" + (f" → prob. bucket {bucket}°C: {p*100:.0f}%" if p is not None else ""))

    entered = False
    valid_probs = [p for p in probs.values() if p is not None]
    if price is not None and price <= ENTRY_MAX_PRICE and bucket is not None and valid_probs:
        best_p = max(valid_probs)
        ev = calc_ev(best_p, price)
        if ev is not None and ev >= MIN_EV_HIGHLIGHT:
            # Re-validar contra el precio REAL de compra (bestAsk) - "price" viene de
            # outcomePrices, un precio de referencia/mid, no necesariamente lo que se
            # pagaria al comprar. Agregado 2026-07-24 tras confirmar en vivo que
            # bestAsk puede ser el DOBLE de outcomePrices en buckets baratos/finos.
            real_ask = None
            try:
                r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 5))
                real_ask = float(r.json().get("bestAsk", price))
            except Exception as e:
                print(f"  [WARN] no se pudo confirmar bestAsk real de {market_id}: {e}")
            real_ev = calc_ev(best_p, real_ask) if real_ask is not None else None
            if real_ask is not None and real_ask <= ENTRY_MAX_PRICE and real_ev is not None and real_ev >= MIN_EV_HIGHLIGHT:
                shares = round(POSITION_SIZE_USD / real_ask, 2)
                early = load_early_tracking()
                early.append({
                    "market_id": market_id,
                    "question": question,
                    "target_date": target_date,
                    "bucket": bucket,
                    "entry_price": real_ask,
                    "forecasts": forecasts,
                    "probs": probs,
                    "ev": real_ev,
                    "cost": POSITION_SIZE_USD,
                    "shares": shares,
                    "status": "pending",
                    "entered_at": datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M:%S"),
                })
                save_early_tracking(early)
                entered = True
                lines.append(f"\n✅ ENTRADA SIMULADA — ${POSITION_SIZE_USD:.0f} a ${real_ask:.3f} (EV {real_ev:+.2f})")

    if not entered:
        lines.append("\n— Sin entrada (precio fuera de rango o EV insuficiente) —")

    send_telegram("\n".join(lines))
    print("  [TELEGRAM] Alerta enviada." + (" [ENTRADA SIMULADA]" if entered else ""))


def main():
    print("MILAN EARLY ENTRY WATCHER — iniciado")
    print(f"Revisa cada {CHECK_INTERVAL}s | Entra si precio <= ${ENTRY_MAX_PRICE} y EV >= {MIN_EV_HIGHLIGHT}")
    print("Modo: SIMULACIÓN — no ejecuta compras reales")
    print("Ctrl+C para detener\n")

    seen = load_seen()
    print(f"  Mercados de Milan ya conocidos: {len(seen)}")

    while True:
        try:
            markets = fetch_new_milan_markets()
            new_count = 0
            for m in markets:
                mid = str(m.get("id", ""))
                if mid not in seen:
                    new_count += 1
                    process_new_market(m, seen)
            save_seen(seen)
            if new_count == 0:
                print(f"  [{datetime.now(ZoneInfo(TZ)).strftime('%H:%M:%S')}] sin mercados nuevos")
        except Exception as e:
            print(f"  [ERROR] {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
