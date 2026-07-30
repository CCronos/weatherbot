#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wallet_watch_global.py — Monitorea la actividad de clima en Polymarket de una wallet.
Agrupa compras/ventas del MISMO mercado en un solo mensaje consolidado
(en vez de una alerta por cada fill/acción individual).
"""

import json
import sys
import time
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

WALLETS = {
    "Wallet China":      "0xed229d040eac1bb1cdada3c7fc6009f5c0426450",
    "Wallet Husky":      "0xaf17116ae2b1476032785a67bd5b7c8c05905c20",
    "Wallet Multicolor": "0x6ff2cb14da8be7eb57541d250a0196c5f295f140",
    "OnlyLuckNoBrain":   "0x6a8d1709bfb718d8555d315a983c4816278350f9",
}
CHECK_INTERVAL = 120
RESOLVER_CADA_N_CICLOS = 5  # revisar resolucion cada 5 corridas (~10 min), no cada 2 min
DASHBOARD_INTERVAL = 5400  # 1.5h — un solo dash consolidado en vez de una alerta de texto por trade
SEEN_FILE = Path("data/wallet_seen_global.json")
TRADES_FILE = Path("data/wallet_trades_global.json")  # bets rastreados (solo lado BUY) para calcular aciertos/Brier
WEATHER_KEYWORDS = ["temperature", "highest temp", "°c", "°f"]

# --- Nuestro propio EV/Kelly sobre las compras grandes de las wallets --------------
# Agregado 2026-07-23: cuando una wallet compra una posicion grande, se calcula
# nuestra propia probabilidad (pronostico ECMWF + sigma) y se anota en la misma
# alerta de Telegram si nuestro modelo coincide o discrepa con el precio pagado.
# Duplicado localmente (coordenadas, bucket_prob, calc_ev, calc_kelly, sigma) en vez
# de importar de bot_v2 - regla del proyecto: cada bot queda legible standalone, la
# logica de decision/matematica no se comparte entre archivos.
UMBRAL_MONTO_GRANDE = 50.0   # USD - por debajo de esto no vale la pena calcular (dust)
PRECIO_PISO = 0.01           # precios <= esto disparan un EV artificial enorme (visto
                              # en favoritos_bot.py) sin significar una oportunidad real
SIGMA_C, SIGMA_F = 1.2, 2.0
MESES_EDGE = {"January":1,"February":2,"March":3,"April":4,"May":5,"June":6,
              "July":7,"August":8,"September":9,"October":10,"November":11,"December":12}

# Coordenadas de estacion (no centro de ciudad) para las ciudades que mas aparecen en
# la actividad de estas wallets. Si una ciudad no esta aca, simplemente no se calcula
# nuestro edge para esa compra (no se inventa un pronostico sin coordenadas reales).
CIUDADES_EDGE = {
    "New York City": {"lat": 40.7772,  "lon":  -73.8726, "tz": "America/New_York",  "unit": "F"},
    "Chicago":       {"lat": 41.9742,  "lon":  -87.9073, "tz": "America/Chicago",   "unit": "F"},
    "London":        {"lat": 51.5048,  "lon":    0.0495, "tz": "Europe/London",     "unit": "C"},
    "Paris":         {"lat": 48.9962,  "lon":    2.5979, "tz": "Europe/Paris",      "unit": "C"},
    "Munich":        {"lat": 48.3537,  "lon":   11.7750, "tz": "Europe/Berlin",     "unit": "C"},
    "Toronto":       {"lat": 43.6772,  "lon":  -79.6306, "tz": "America/Toronto",   "unit": "C"},
    "Beijing":       {"lat": 40.0801,  "lon":  116.5846, "tz": "Asia/Shanghai",     "unit": "C"},
    "Chengdu":       {"lat": 30.5728,  "lon":  103.9469, "tz": "Asia/Shanghai",     "unit": "C"},
    "Madrid":        {"lat": 40.4936,  "lon":   -3.5668, "tz": "Europe/Madrid",     "unit": "C"},
    "Milan":         {"lat": 45.6306,  "lon":    8.7281, "tz": "Europe/Rome",       "unit": "C"},
    "Amsterdam":     {"lat": 52.3086,  "lon":    4.7639, "tz": "Europe/Amsterdam",  "unit": "C"},
    "Warsaw":        {"lat": 52.1657,  "lon":   20.9671, "tz": "Europe/Warsaw",     "unit": "C"},
    "Istanbul":      {"lat": 41.2753,  "lon":   28.7519, "tz": "Europe/Istanbul",   "unit": "C"},
    "Shenzhen":      {"lat": 22.6393,  "lon":  113.8107, "tz": "Asia/Shanghai",     "unit": "C"},
    "Guangzhou":     {"lat": 23.3924,  "lon":  113.2988, "tz": "Asia/Shanghai",     "unit": "C"},
    "Hong Kong":     {"lat": 22.3020,  "lon":  114.1742, "tz": "Asia/Hong_Kong",    "unit": "C"},
    "Helsinki":      {"lat": 60.3183,  "lon":   24.9497, "tz": "Europe/Helsinki",  "unit": "C"},
}

with open("config.json", encoding="utf-8") as f:
    cfg = json.load(f)
TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID_2 = (
    cfg.get("telegram_token", ""),
    cfg.get("telegram_chat_wallet") or cfg.get("telegram_chat_id", ""),
    cfg.get("telegram_chat_id_2", ""),
)


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
                    requests.post(url, data={"chat_id": cid, "caption": caption},
                                  files={"photo": f}, timeout=(5, 20))
            except Exception as e:
                print(f"  [WARN] Telegram photo: {e}")


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


def load_trades():
    return json.loads(TRADES_FILE.read_text(encoding="utf-8")) if TRADES_FILE.exists() else []


def save_trades(trades):
    TRADES_FILE.parent.mkdir(exist_ok=True)
    tmp = TRADES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(trades, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(TRADES_FILE)


def fetch_full_activity(wallet_address, max_pages=30):
    """Trae TODO el historial de actividad de la wallet (no solo lo reciente), avanzando
    de a 100 registros hasta que una pagina viene vacia o mas corta que el limite pedido
    (senal de que ya no hay mas). Tope de 30 paginas (=3000 registros) como salvavidas
    para no quedar en un loop si la API se comporta raro."""
    todos = []
    offset = 0
    limit = 100
    for _ in range(max_pages):
        try:
            r = requests.get(
                "https://data-api.polymarket.com/activity",
                params={"user": wallet_address, "limit": limit, "offset": offset},
                timeout=(5, 15),
            )
            pagina = r.json()
        except Exception as e:
            print(f"  [WARN] fetch_full_activity offset={offset}: {e}")
            break
        if not pagina:
            break
        todos.extend(pagina)
        if len(pagina) < limit:
            break
        offset += limit
    return todos


def construir_historial(label, wallet_address):
    """Reconstruye TODOS los bets (lado BUY) de clima de esta wallet desde el inicio de
    su historial en Polymarket - no solo desde que este script empezo a vigilarla. Agrupa
    por (slug, outcome, side) porque el slug ya es especifico de una fecha/mercado exacto,
    asi que agrupar por slug no mezcla dias distintos. NO resuelve todavia (eso es lento,
    se hace aparte sobre la lista combinada de las 3 wallets - ver backfill_todas)."""
    activity = fetch_full_activity(wallet_address)
    grupos = {}
    for a in activity:
        if str(a.get("type", "")).upper() != "TRADE":
            continue
        title = get_title(a)
        if not title or not is_weather_market(title):
            continue
        if str(a.get("side", "")).upper() != "BUY":
            continue  # solo BUY se califica como bet (ver resolver_trades)
        slug = a.get("slug", "")
        outcome = a.get("outcome", "?")
        side = a.get("side", "?")
        if not slug:
            continue
        key = (slug, outcome, side)
        if key not in grupos:
            grupos[key] = {
                "size": 0.0, "usdc": 0.0, "fills": 0, "title": title,
                "outcome_index": a.get("outcomeIndex"), "ts_min": a.get("timestamp", 0),
                "event_slug": a.get("eventSlug", ""),
            }
        g = grupos[key]
        g["size"] += float(a.get("size", 0) or 0)
        g["usdc"] += float(a.get("usdcSize", 0) or 0)
        g["fills"] += 1
        g["ts_min"] = min(g["ts_min"], a.get("timestamp", 0))

    trades = []
    for (slug, outcome, side), g in grupos.items():
        avg_price = (g["usdc"] / g["size"]) if g["size"] else 0.0
        city = extract_city(g["title"])
        opened_at = datetime.fromtimestamp(g["ts_min"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        trades.append({
            "label": label, "slug": slug, "event_slug": g["event_slug"], "title": g["title"], "city": city,
            "outcome": outcome, "outcome_index": g["outcome_index"],
            "price": round(avg_price, 4), "size": round(g["size"], 2),
            "usdc": round(g["usdc"], 2), "opened_at": opened_at,
            "status": "open", "won": None, "pnl": None,
        })

    return trades


def backfill_todas():
    """Reconstruye el historial completo de las 3 wallets y REEMPLAZA data/wallet_trades_global.json
    con esa version completa - es el pedido explicito del usuario ("recopila todo desde
    que la estamos vigilando"), no un merge con lo poco que el tracking en vivo alcanzo
    a guardar desde que se agrego hace un rato.

    Primero arma la lista CRUDA (sin resolver) de las 3 wallets - esta parte es rapida,
    solo son fetches paginados de actividad, sin llamadas de resolucion individuales - y
    la guarda. Recien despues resuelve sobre la lista COMBINADA completa, con checkpoints
    periodicos (ver resolver_trades save_every) - asi un corte a mitad de camino nunca
    pierde ni la lista completa de bets ni las resoluciones ya conseguidas hasta ese punto."""
    todos = []
    for label, addr in WALLETS.items():
        print(f"  Reconstruyendo historial de {label}...")
        trades = construir_historial(label, addr)
        print(f"    {len(trades)} bets de clima encontrados")
        todos.extend(trades)
    save_trades(todos)
    print(f"  Lista completa guardada ({len(todos)} bets) - resolviendo contra Polymarket...")
    todos = resolver_trades(todos)
    return todos


def resolver_trades(trades, save_every=20):
    """Revisa los bets abiertos (solo lado BUY - una compra de una wallet ajena es la
    unica accion que podemos calificar como "acierto/fallo" de forma directa; una venta
    es solo informativa, no un bet que resolver) contra Polymarket, y marca ganado/perdido
    cuando ya cerro.

    IMPORTANTE: buscar el mercado individual por su propio slug (gamma-api /markets?slug=)
    solo funciona para mercados recientes - para mercados viejos/archivados esa busqueda
    devuelve una lista vacia aunque el mercado exista y ya haya resuelto (confirmado en
    vivo: un mercado de abril de 2026 daba [] por slug propio). La forma que SI funciona
    para historial viejo es pedir el EVENTO completo por su eventSlug (gamma-api /events?slug=)
    y buscar el sub-mercado especifico dentro de su lista "markets" - se usa esa primero.

    Guarda cada `save_every` resoluciones nuevas, no solo al final - con cientos de bets
    por resolver (cada uno es una llamada de red secuencial) esto puede tardar mas que el
    limite de tiempo del entorno donde corre; sin este checkpoint periodico, cualquier
    corte a mitad de camino perdia TODO el progreso de esa corrida (se vio en vivo dos
    veces seguidas). Ademas, como solo se procesan trades con status=="open", volver a
    llamar esta funcion con la misma lista es seguro - retoma donde quedo, no repite
    trabajo ya hecho."""
    cambiado = False
    recien_resueltos = 0
    for t in trades:
        if t.get("status") != "open":
            continue
        slug = t.get("slug")
        event_slug = t.get("event_slug")
        if not slug:
            continue
        try:
            m = None
            if event_slug:
                r = requests.get("https://gamma-api.polymarket.com/events", params={"slug": event_slug}, timeout=(5, 10))
                eventos = r.json()
                if isinstance(eventos, list) and eventos:
                    for cand in eventos[0].get("markets", []):
                        if cand.get("slug") == slug:
                            m = cand
                            break
            if m is None:
                # Sin event_slug guardado (registros viejos de antes de este fix) o el
                # evento no trajo el sub-mercado - se intenta la busqueda directa como
                # respaldo, que si funciona para mercados recientes/todavia no archivados.
                r = requests.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=(5, 8))
                data = r.json()
                if isinstance(data, list) and data:
                    m = data[0]
            if m is None or not m.get("closed", False):
                continue
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                prices = json.loads(prices)
            idx = t.get("outcome_index")
            if idx is None or prices is None or idx >= len(prices):
                continue
            p_final = float(prices[idx])
            gano = p_final >= 0.9
            t["status"] = "resolved"
            t["won"] = gano
            # PnL en USD: si gano, las acciones pagan $1 c/u (menos lo que costaron);
            # si perdio, se pierde todo lo apostado (usdc pagado).
            t["pnl"] = round(t["size"] - t["usdc"], 2) if gano else round(-t["usdc"], 2)
            cambiado = True
            recien_resueltos += 1
            if recien_resueltos >= save_every:
                save_trades(trades)
                recien_resueltos = 0
        except Exception as e:
            print(f"  [WARN] resolver {slug}: {e}")
    if cambiado:
        save_trades(trades)
    return trades


def fetch_activity(wallet_address):
    try:
        r = requests.get(
            "https://data-api.polymarket.com/activity",
            params={"user": wallet_address, "limit": 100},
            timeout=(5, 10),
        )
        return r.json()
    except Exception as e:
        print(f"  [WARN] No se pudo consultar actividad de {wallet_address}: {e}")
        return []


def get_title(a):
    for key in ["title", "eventTitle", "question", "marketQuestion", "slug"]:
        val = a.get(key)
        if val:
            return str(val)
    market = a.get("market")
    if isinstance(market, dict):
        for key in ["question", "title"]:
            if market.get(key):
                return str(market[key])
    return ""


def is_weather_market(title):
    title_low = title.lower()
    return any(kw in title_low for kw in WEATHER_KEYWORDS)


def extract_city(title):
    import re
    m = re.search(r"in ([A-Za-zÀ-ÿ\s]+?) be", title, re.IGNORECASE)
    return m.group(1).strip() if m else "?"


def norm_cdf(x):
    import math
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_prob(forecast, t_low, t_high, sigma):
    f = float(forecast)
    if t_low == -999:
        return norm_cdf((t_high - f) / sigma)
    if t_high == 999:
        return 1.0 - norm_cdf((t_low - f) / sigma)
    lo, hi = (t_low - 0.5, t_high + 0.5) if t_low == t_high else (t_low, t_high)
    return max(0.0, norm_cdf((hi - f) / sigma) - norm_cdf((lo - f) / sigma))


def calc_ev(p, price):
    if price <= 0 or price >= 1:
        return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)


def calc_kelly(p, price, kelly_fraction=0.25):
    if price <= 0 or price >= 1:
        return 0.0
    b = 1.0 / price - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * kelly_fraction, 1.0), 4)


def parse_temp_range(question):
    import re
    if not question:
        return None
    num = r'(-?\d+(?:\.\d+)?)'
    if re.search(r'or below', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or below', question, re.IGNORECASE)
        if m:
            return (-999.0, float(m.group(1)))
    if re.search(r'or higher', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or higher', question, re.IGNORECASE)
        if m:
            return (float(m.group(1)), 999.0)
    m = re.search(r'between ' + num + r'-' + num + r'[°]?[FC]', question, re.IGNORECASE)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(r'be ' + num + r'[°]?[FC] on', question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None


def extraer_fecha_iso(title):
    import re
    # \b antes de "on" es necesario: "London" termina en "on", sin el limite de
    # palabra "...London be 26C on July 23?" hace match falso adentro de "London".
    m = re.search(r"\bon (\w+) (\d{1,2})", title)
    if not m:
        return None
    mes = MESES_EDGE.get(m.group(1))
    if not mes:
        return None
    # Bug encontrado 2026-07-30 en auditoria: usaba siempre el anio actual, sin rollover
    # cerca de fin de anio (a diferencia de la logica ya correcta en
    # chengdu_early_entry.py:extract_date_from_question). Sin esto, un trade de wallet
    # que menciona un mercado de enero visto en diciembre calculaba una fecha en el
    # PASADO — fetch_forecast_edge no tira error, pero devuelve vacio/nada util, asi que
    # ese trade queda silenciosamente sin "nuestro edge" calculado.
    ahora = datetime.now(timezone.utc)
    anio = ahora.year
    if mes < ahora.month - 6:  # ej. parseando "enero" estando ya en diciembre
        anio += 1
    return f"{anio}-{mes:02d}-{int(m.group(2)):02d}"


def fetch_forecast_edge(lat, lon, date_iso, unit, tz):
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon, "daily": "temperature_2m_max",
                "temperature_unit": temp_unit, "start_date": date_iso, "end_date": date_iso,
                "timezone": tz, "models": "ecmwf_ifs025", "bias_correction": "true",
            },
            timeout=(5, 10),
        )
        vals = r.json().get("daily", {}).get("temperature_2m_max", [])
        return vals[0] if vals else None
    except Exception as e:
        print(f"  [WARN] fetch_forecast_edge {lat},{lon} {date_iso}: {e}")
        return None


def nuestro_edge_sobre_trade(title, city, outcome, price):
    """Nuestra propia prob/EV/Kelly sobre una compra de una wallet, para anotar en la
    alerta de Telegram. Devuelve None si no hay forma confiable de comparar (ciudad sin
    coordenadas, mercado de HOY -donde el mercado ya sabe el resultado real de la tarde
    y nuestro pronostico estatico no es comparable-, o precio en el piso de Polymarket
    donde el EV se dispara sin significar una oportunidad real)."""
    info = CIUDADES_EDGE.get(city)
    if not info:
        return None
    if price is None or price <= PRECIO_PISO or price >= 0.99:
        return None
    fecha_iso = extraer_fecha_iso(title)
    if not fecha_iso:
        return None
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if fecha_iso <= hoy:
        return None
    rng = parse_temp_range(title)
    if not rng:
        return None
    forecast = fetch_forecast_edge(info["lat"], info["lon"], fecha_iso, info["unit"], info["tz"])
    if forecast is None:
        return None
    sigma = SIGMA_F if info["unit"] == "F" else SIGMA_C
    prob_yes = bucket_prob(forecast, rng[0], rng[1], sigma)
    prob = prob_yes if outcome == "Yes" else (1.0 - prob_yes)
    ev = calc_ev(prob, price)
    kelly = calc_kelly(prob, price)
    return {"forecast": forecast, "prob": prob, "ev": ev, "kelly": kelly}


def fetch_current_price(slug, outcome_index):
    """Precio actual (outcomePrices[idx]) de un mercado todavia sin resolver, buscado
    por slug. Solo sirve para mercados recientes (igual que el fallback de
    resolver_trades) — las posiciones activas de esta dashboard son por definicion
    recientes (mercado todavia no cerro), asi que no hace falta el camino por
    eventSlug que resolver_trades necesita para historial viejo/archivado."""
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=(5, 8))
        data = r.json()
        if not isinstance(data, list) or not data:
            return None
        m = data[0]
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            prices = json.loads(prices)
        if not prices or outcome_index is None or outcome_index >= len(prices):
            return None
        return float(prices[outcome_index])
    except Exception as e:
        print(f"  [WARN] fetch_current_price {slug}: {e}")
        return None


def render_active_positions_report():
    """Dashboard consolidado de posiciones ACTIVAS (bets BUY todavia sin resolver) de
    las 3 wallets — reemplaza la alerta de texto individual por trade (demasiado
    volumen para leer, pedido del usuario 2026-07-24). Agrupa por (wallet, slug,
    outcome) porque main() puede guardar varios registros incrementales de la MISMA
    posicion si la wallet la fue armando en distintos ciclos de 2 min — sin agrupar,
    el precio promedio y la cantidad total saldrian mal repartidos entre filas.

    "Activa" acá significa "el mercado de Polymarket todavia no cerro" (mismo criterio
    que status=="open" en resolver_trades) — no sabemos si la wallet ya vendio antes
    de la resolucion, esta dashboard no restea ventas posteriores contra la compra
    original (side=SELL solo queda en el log de consola, no se linkea a la posicion).
    """
    trades = load_trades()
    abiertas = [t for t in trades if t.get("status") == "open"]

    posiciones = {}
    for t in abiertas:
        key = (t["label"], t["slug"], t["outcome"])
        p = posiciones.setdefault(key, {
            "label": t["label"], "slug": t["slug"], "title": t["title"], "city": t["city"],
            "outcome": t["outcome"], "outcome_index": t.get("outcome_index"),
            "size": 0.0, "usdc": 0.0, "opened_at": t["opened_at"],
        })
        p["size"] += t["size"]
        p["usdc"] += t["usdc"]
        p["opened_at"] = min(p["opened_at"], t["opened_at"])

    C_BG, C_TEXT, C_DIM = "#0d1117", "#e6edf3", "#8b949e"
    C_POS, C_NEG = "#3fb950", "#f85149"

    rows = []
    rows.append(("WALLET WATCH — POSICIONES ACTIVAS", C_TEXT, 17, True, False))
    rows.append((datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), C_DIM, 11, False, True))
    rows.append(("=" * 100, C_DIM, 10, False, True))
    rows.append((f"Posiciones abiertas: {len(posiciones)}   (mercado de Polymarket todavia sin resolver;",
                  C_TEXT, 11, False, True))
    rows.append(("no restea ventas posteriores de la wallet contra la compra original)", C_DIM, 9.5, False, True))
    rows.append(("\"Compraríamos\" en blanco = hoy mismo (D+0) o ciudad sin coordenadas propias — no se inventa comparacion.",
                  C_DIM, 9.5, False, True))
    rows.append(("", C_TEXT, 6, False, True))

    if posiciones:
        header = f"{'Wallet':<19} {'Ciudad':<13} {'Outcome':<8} {'Entry':>6} {'Ahora':>6} {'Qty':>8} {'PnL$':>8}   {'Compraríamos':<32}"
        rows.append((header, C_DIM, 10, True, True))
        rows.append(("-" * 100, C_DIM, 10, False, True))

        items = sorted(posiciones.values(), key=lambda p: -p["usdc"])
        for p in items:
            entry = p["usdc"] / p["size"] if p["size"] else 0.0
            current = fetch_current_price(p["slug"], p["outcome_index"])
            pnl_unreal = (current - entry) * p["size"] if current is not None else None

            edge = nuestro_edge_sobre_trade(p["title"], p["city"], p["outcome"], current) if current is not None else None
            if edge:
                veredicto = (f"{'SI' if edge['ev'] >= 0 else 'NO'} p={edge['prob']*100:.0f}% "
                              f"EV{edge['ev']:+.2f} K{edge['kelly']:.3f}")
            else:
                veredicto = "s/d"

            entry_s = f"{entry:.3f}"
            now_s   = f"{current:.3f}" if current is not None else "  ?"
            pnl_s   = f"{pnl_unreal:+.2f}" if pnl_unreal is not None else "  ?"

            row = (f"{p['label']:<19} {p['city']:<13} {p['outcome']:<8} {entry_s:>6} {now_s:>6} "
                   f"{p['size']:>8.1f} {pnl_s:>8}   {veredicto:<32}")
            color = C_POS if (pnl_unreal or 0) >= 0 else C_NEG
            rows.append((row, color, 10, False, True))
        rows.append(("-" * 100, C_DIM, 10, False, True))
    else:
        rows.append(("Sin posiciones activas registradas por ahora.", C_DIM, 11, False, True))

    n_rows = len(rows)
    fig_h = max(3.5, 0.30 * n_rows + 0.7)
    fig, ax = plt.subplots(figsize=(15, fig_h), facecolor=C_BG)
    ax.set_facecolor(C_BG)
    ax.axis("off")
    dy = 1.0 / (n_rows + 1)
    y = 1.0 - dy * 0.6
    for text, color, size, bold, mono in rows:
        ax.text(0.02, y, text, transform=ax.transAxes, color=color, fontsize=size,
                fontweight="bold" if bold else "normal",
                family="monospace" if mono else "sans-serif", va="top")
        y -= dy

    img_dir = Path("data/images")
    img_dir.mkdir(parents=True, exist_ok=True)
    path = img_dir / "wallet_watch_active.png"
    fig.savefig(path, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def send_active_positions_report():
    path = render_active_positions_report()
    send_telegram_photo(path, caption=f"Wallet Watch — posiciones activas {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return path


def main():
    print("WALLET WATCHER — GLOBAL (mensajes consolidados por mercado)")
    for label, addr in WALLETS.items():
        print(f"  {label}: {addr}")
    print(f"Revisa cada {CHECK_INTERVAL}s\nCtrl+C para detener\n")

    seen = load_seen()
    print(f"  Trades ya conocidos: {len(seen)}")

    ciclo = 0
    ultimo_dashboard = 0.0
    while True:
        ciclo += 1
        try:
            # Solo se alerta de operaciones de HOY en adelante (hora local) — las de
            # días anteriores se marcan como vistas pero nunca generan alerta, para
            # no barrer historial viejo como un aluvión de alertas al reiniciar.
            today_start = datetime.now().astimezone().replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp()

            for label, wallet_address in WALLETS.items():
                activity = fetch_activity(wallet_address)
                print(f"\n  [{datetime.now(ZoneInfo('UTC')).strftime('%H:%M:%S')} UTC] "
                      f"{label}: {len(activity)} actividades recibidas")

                # --- Agrupar por (título del mercado + outcome + side) ---
                groups = {}
                for a in activity:
                    a_type = str(a.get("type", "")).upper()
                    if a_type and a_type != "TRADE":
                        continue
                    title = get_title(a)
                    if not title or not is_weather_market(title):
                        continue

                    # El wallet va en el tx_id para que "seen" no cruce entre wallets.
                    tx_id = label + a.get("transactionHash", "") + str(a.get("timestamp", "")) + str(a.get("size", ""))
                    if tx_id in seen:
                        continue
                    seen.add(tx_id)

                    if a.get("timestamp", 0) < today_start:
                        continue

                    side = a.get("side", "?")
                    outcome = a.get("outcome", "?")
                    size = float(a.get("size", 0) or 0)
                    usdc = float(a.get("usdcSize", 0) or 0)
                    slug = a.get("slug", "")
                    outcome_index = a.get("outcomeIndex")
                    event_slug = a.get("eventSlug", "")

                    key = (title, outcome, side)
                    if key not in groups:
                        groups[key] = {"size": 0.0, "usdc": 0.0, "fills": 0, "slug": slug,
                                       "outcome_index": outcome_index, "event_slug": event_slug}
                    groups[key]["size"] += size
                    groups[key]["usdc"] += usdc
                    groups[key]["fills"] += 1

                # --- Registrar cada grupo (ya no se manda una alerta de texto por
                # trade — demasiado volumen para leer, pedido del usuario 2026-07-24;
                # ahora todo se consolida en el dashboard periodico, ver DASHBOARD_INTERVAL
                # mas abajo y render_active_positions_report()). ---
                trades = load_trades()
                trades_dirty = False
                for (title, outcome, side), data in groups.items():
                    total_size = data["size"]
                    total_usdc = data["usdc"]
                    avg_price = (total_usdc / total_size) if total_size else 0
                    city = extract_city(title)
                    now_str = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M UTC")
                    fills_note = f" ({data['fills']} operaciones agrupadas)" if data["fills"] > 1 else ""
                    print(f"  [DETECTADO] {label} — {city} — {side} {outcome}: {total_size:.1f} acciones @ ${avg_price:.3f}{fills_note}")

                    # Solo el lado BUY se registra como un "bet" a calificar despues
                    # (acierto/fallo, Brier) - una venta es informativa, no un bet nuevo.
                    if str(side).upper() == "BUY" and data.get("slug") and data.get("outcome_index") is not None:
                        trades.append({
                            "label": label, "slug": data["slug"], "event_slug": data.get("event_slug", ""),
                            "title": title, "city": city,
                            "outcome": outcome, "outcome_index": data["outcome_index"],
                            "price": round(avg_price, 4), "size": round(total_size, 2),
                            "usdc": round(total_usdc, 2), "opened_at": now_str,
                            "status": "open", "won": None, "pnl": None,
                        })
                        trades_dirty = True

                if not groups:
                    print(f"  {label}: sin trades nuevos de clima")

                if trades_dirty:
                    save_trades(trades)

            save_seen(seen)
            # Revisar resolucion contra Polymarket es una llamada de red por cada bet
            # todavia abierto (pueden ser cientos entre las 3 wallets) - un mercado de
            # clima tarda horas en cerrar, asi que repetir esto cada CHECK_INTERVAL
            # (2 min) es puro gasto: casi nunca hay nada nuevo que resolver en ese lapso.
            # Se espacia a 1 de cada RESOLVER_CADA_N_CICLOS corridas (10 min con el
            # intervalo actual) para aliviar la maquina sin perder frescura real.
            if ciclo % RESOLVER_CADA_N_CICLOS == 0:
                resolver_trades(load_trades())

            # Dashboard consolidado de posiciones activas cada DASHBOARD_INTERVAL —
            # arranca en el primer ciclo (ultimo_dashboard=0) para tener un vistazo
            # inicial sin esperar 1.5h despues de un reinicio.
            if time.time() - ultimo_dashboard >= DASHBOARD_INTERVAL:
                send_active_positions_report()
                ultimo_dashboard = time.time()
        except Exception as e:
            print(f"  [ERROR] {e}")
        time.sleep(CHECK_INTERVAL)


def render_wallet_report():
    """Trades rastreados por wallet (solo lado BUY), aciertos/fallos, win rate y Brier
    score - usando el PRECIO pagado como proxy de la probabilidad implicita que la
    wallet le puso a esa apuesta (no sabemos su confianza real, pero el precio que
    pago es la mejor aproximacion disponible). Mismo estilo visual que los reportes
    de bot_v2.py/favoritos_bot.py, implementado aca por separado."""
    resolver_trades(load_trades())
    trades = load_trades()

    C_BG, C_TEXT, C_DIM = "#0d1117", "#e6edf3", "#8b949e"
    C_POS, C_NEG = "#3fb950", "#f85149"

    # Columnas de la tabla: (encabezado, ancho, alineacion) - todo en monoespaciado
    # para que las columnas queden alineadas igual que un dashboard de terminal.
    COLS = [
        ("Wallet", 18, "<"), ("Bets", 6, ">"), ("Resuelt", 8, ">"),
        ("Acierto", 8, ">"), ("Fallo", 7, ">"), ("WR%", 6, ">"),
        ("Entry", 8, ">"), ("Exit", 8, ">"), ("PnL (USD)", 12, ">"), ("Brier", 8, ">"),
    ]
    table_width = sum(w for _, w, _ in COLS)

    def fmt_row(vals):
        return "".join(f"{v:{a}{w}}" for v, (_, w, a) in zip(vals, COLS))

    rows = []
    rows.append(("WALLET WATCH — PNL", C_TEXT, 17, True, False))
    rows.append((datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), C_DIM, 11, False, True))
    rows.append(("=" * table_width, C_DIM, 10, False, True))
    rows.append(("Bets = solo lado BUY observado. Brier score usa el precio pagado como",
                  C_DIM, 9.5, False, True))
    rows.append(("proxy de probabilidad implicita (no conocemos la confianza real de la wallet).",
                  C_DIM, 9.5, False, True))
    rows.append(("", C_TEXT, 6, False, True))

    rows.append((fmt_row([name for name, _, _ in COLS]), C_TEXT, 11, True, True))
    rows.append(("-" * table_width, C_DIM, 10, False, True))

    fechas_por_wallet = {}
    for label in WALLETS:
        propios = [t for t in trades if t["label"] == label]
        resueltos = [t for t in propios if t["status"] == "resolved"]
        wins = [t for t in resueltos if t["won"]]
        n = len(resueltos)
        wr = (len(wins) / n * 100) if n else 0.0
        pnl_total = sum(t.get("pnl") or 0.0 for t in resueltos)
        avg_entry = (sum(t["price"] for t in propios) / len(propios)) if propios else None
        avg_exit = (sum((1.0 if t["won"] else 0.0) for t in resueltos) / n) if n else None
        brier = (sum((t["price"] - (1.0 if t["won"] else 0.0)) ** 2 for t in resueltos) / n) if n else None

        vals = [
            label, str(len(propios)), str(n), str(len(wins)), str(n - len(wins)),
            f"{wr:.0f}%",
            f"{avg_entry:.3f}" if avg_entry is not None else "-",
            f"{avg_exit:.3f}" if avg_exit is not None else "-",
            f"{pnl_total:+.2f}" if n else "-",
            f"{brier:.3f}" if brier is not None else "-",
        ]
        color = C_POS if (n and pnl_total >= 0) else C_NEG if n else C_DIM
        rows.append((fmt_row(vals), color, 11, False, True))

        if propios:
            fechas = sorted(t["opened_at"] for t in propios)
            fechas_por_wallet[label] = (fechas[0], fechas[-1])

    rows.append(("-" * table_width, C_DIM, 10, False, True))
    rows.append(("", C_TEXT, 6, False, True))
    rows.append(("Vigilando cada wallet desde:", C_DIM, 9.5, False, True))
    for label, (desde, ultimo) in fechas_por_wallet.items():
        rows.append((f"  {label:<18} desde {desde}   ultimo bet {ultimo}", C_DIM, 9.5, False, True))

    n_rows = len(rows)
    fig_h = max(3.5, 0.30 * n_rows + 0.7)
    fig, ax = plt.subplots(figsize=(12.5, fig_h), facecolor=C_BG)
    ax.set_facecolor(C_BG)
    ax.axis("off")
    dy = 1.0 / (n_rows + 1)
    y = 1.0 - dy * 0.6
    for text, color, size, bold, mono in rows:
        ax.text(0.02, y, text, transform=ax.transAxes, color=color, fontsize=size,
                fontweight="bold" if bold else "normal",
                family="monospace" if mono else "sans-serif", va="top")
        y -= dy

    img_dir = Path("data/images")
    img_dir.mkdir(parents=True, exist_ok=True)
    path = img_dir / "wallet_watch_pnl.png"
    fig.savefig(path, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def send_wallet_report():
    path = render_wallet_report()
    send_telegram_photo(path, caption=f"Wallet Watch — PnL {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return path


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        main()
    elif cmd == "pnl":
        path = send_wallet_report()
        print(f"Reporte de PnL enviado a Telegram. Imagen: {path}")
    elif cmd == "activas":
        path = send_active_positions_report()
        print(f"Dashboard de posiciones activas enviado a Telegram. Imagen: {path}")
    elif cmd == "backfill":
        trades = backfill_todas()
        print(f"Backfill completo: {len(trades)} bets reconstruidos en total.")
        path = send_wallet_report()
        print(f"Reporte de PnL enviado a Telegram. Imagen: {path}")
    elif cmd == "resolver":
        # Retoma SOLO la resolucion sobre lo que ya esta guardado - util si "backfill"
        # se corto a mitad de camino (ya no hace falta re-descargar toda la actividad,
        # solo seguir resolviendo los que quedaron con status="open").
        trades = load_trades()
        pendientes = len([t for t in trades if t.get("status") == "open"])
        print(f"  {pendientes} bets pendientes de resolver de {len(trades)} totales...")
        trades = resolver_trades(trades)
        resueltos = len([t for t in trades if t.get("status") == "resolved"])
        print(f"Listo: {resueltos}/{len(trades)} resueltos en total.")
        path = send_wallet_report()
        print(f"Reporte de PnL enviado a Telegram. Imagen: {path}")