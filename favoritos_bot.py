#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
favoritos_bot.py — Experimento separado del bot principal (bot_v2.py), con su propio
capital ficticio de $200, en las 6 ciudades que ya seguiamos con dashboards (Chengdu,
Buenos Aires, New York City, Helsinki, Hong Kong, Milan).

Estrategia actual (fue evolucionando, esto es lo que hace HOY, no la version original):
1. Cada dia compra $20 de cada una de las 2 opciones donde NUESTRO propio pronostico
   (ECMWF, bias_correction) da mayor probabilidad Y que ademas tienen EV >= MIN_EV
   (10%, mismo umbral que bot_v2.py) contra el precio de mercado — no la que el
   mercado pone mas cara, y tampoco cualquier opcion "probable" sin chequear si el
   precio ya la reflejaba bien. Si ninguna opcion de la ciudad llega al EV minimo ese
   dia, se salta la ciudad (cambiado desde "favorita del mercado" el 2026-07-20; el
   filtro de EV se agrego el 2026-07-22 despues de confirmar que sin el, del 20 al 22
   de julio el criterio de probabilidad propia dio 5% de aciertos en 21 trades con
   -$66.81 de perdida — compraba la mas probable aunque ya estuviera cara. Ver
   state["criterio_edge_desde"] para el primer cambio).
2. Cuando hay 2 patas abiertas de la misma ciudad/fecha, las compara contra el maximo
   real registrado hoy en Wunderground (la fuente que de verdad liquida el mercado en
   5 de las 6 ciudades - Hong Kong usa HKO directo, queda afuera de esta comparacion):
   la mas cercana al dato real se deja correr con un techo alto (TAKE_PROFIT_BOOST,
   no el +30% normal); la mas lejana se cierra ya, tome lo que tome.
3. Si Wunderground ya registra un maximo por encima del bucket de una posicion, esa
   posicion se cierra de inmediato (esta matematicamente perdida) sin esperar al precio.
4. Stop-loss en -20%, take-profit normal en +30% (o el techo alto si fue marcada favorita).
5. Freno de emergencia: si el equity real cae -50% desde los $200 iniciales, se pausan
   las compras nuevas (las salidas de proteccion siguen activas) hasta revision manual.
6. No vuelve a comprar un market_id que ya compro antes (evita duplicar la misma apuesta
   si el mismo mercado aparece como D+1 un dia y D+0 al siguiente).

Objetivo: medir en varias semanas si esta estrategia es rentable, antes de decidir si
se prueba con dinero real (35 trades resueltos por ciudad como minimo, ver
reporte_consolidado.py).

No ejecuta ordenes reales — es papel, igual que bot_v2.py.
"""

import sys
import json
import math
import time
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Reutiliza funciones ya probadas de bot_v2.py en vez de reescribirlas.
import bot_v2

# Canal de Telegram propio de este bot (no el del bot principal) - separado a proposito
# para que cada iniciativa tenga su propio feed, no todo mezclado en un solo chat.
_cfg = json.load(open("config.json", encoding="utf-8"))
TELEGRAM_TOKEN   = _cfg.get("telegram_token", "")
TELEGRAM_CHAT_ID = _cfg.get("telegram_chat_favoritos") or _cfg.get("telegram_chat_id", "")


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=(3, 5))
    except Exception as e:
        print(f"  [WARN] Telegram send failed: {e}")


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


def render_picks_report(trade_rows, balance, equity):
    """Reporte visual tipo consola de los picks del dia — mismo estilo (monoespaciado,
    oscuro, filas coloreadas) que el de bot_v2.py, pero implementado aca por separado
    a proposito (no importado) para que cada bot siga siendo independiente. No se usa
    "$" en el texto: matplotlib interpreta un par de "$" como formula matematica y
    escaparlo descuadra el ancho fijo de las columnas — mas simple dejar numeros
    limpios bajo columnas ya rotuladas."""
    C_BG, C_TEXT, C_DIM = "#0d1117", "#e6edf3", "#8b949e"
    C_BUY = "#3fb950"

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = []  # (text, color, size, bold, mono)
    rows.append(("FAVORITOS — PICKS DEL DÍA", C_TEXT, 17, True, False))
    rows.append((now_str, C_DIM, 11, False, True))
    rows.append(("=" * 78, C_DIM, 10, False, True))
    rows.append(("", C_TEXT, 6, False, True))
    rows.append((f"{'City':<16}{'Date':<12}{'Bucket':<12}{'Price':>7}{'Stake':>8}   Decision", C_DIM, 11, True, True))
    rows.append(("-" * 78, C_DIM, 10, False, True))
    for t in trade_rows:
        row = f"{t['city']:<16}{t['date']:<12}{t['bucket']:<12}{t['price']:>7.3f}{t['stake']:>8.2f}   {t['decision']}"
        rows.append((row, C_BUY, 11, False, True))
    rows.append(("-" * 78, C_DIM, 10, False, True))
    rows.append(("", C_TEXT, 6, False, True))
    rows.append((f"Balance: {balance:,.2f}   Equity (mark-to-market): {equity:,.2f}   (USD)", C_TEXT, 12, True, True))

    n = len(rows)
    fig_h = max(3.2, 0.30 * n + 0.7)
    fig, ax = plt.subplots(figsize=(10, fig_h), facecolor=C_BG)
    ax.set_facecolor(C_BG)
    ax.axis("off")

    dy = 1.0 / (n + 1)
    y = 1.0 - dy * 0.6
    for text, color, size, bold, mono in rows:
        ax.text(0.02, y, text, transform=ax.transAxes, color=color, fontsize=size,
                fontweight="bold" if bold else "normal",
                family="monospace" if mono else "sans-serif", va="top")
        y -= dy

    img_dir = Path("data/images")
    img_dir.mkdir(parents=True, exist_ok=True)
    path = img_dir / "favoritos_picks.png"
    fig.savefig(path, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


STARTING_CAPITAL = 200.0
BET_PER_LEG       = 20.0     # $20 por cada una de las 2 opciones favoritas
TAKE_PROFIT       = 0.30     # vender apenas la posicion sube +30% desde el precio de entrada
TAKE_PROFIT_BOOST = 0.55     # techo alto para la pata "favorita en vivo" (dejarla correr,
                              # pero no sin limite - Helsinki nos enseño que puede revertirse)
STOP_LOSS         = 0.20     # vender apenas la posicion baja -20% desde el precio de entrada
FRENO_EMERGENCIA_DRAWDOWN = 0.50  # pausar compras nuevas si el equity cae -50% desde el capital inicial
MAX_ENTRY_PRICE   = 0.75     # si la favorita ya cuesta mas que esto, no queda margen real para +30% sin pasar de $1 - se ignora
# Piso de precio de entrada y de volumen — agregados 2026-07-28 tras auditar el
# historial. El bot marcaba +61% ($200 -> $322), pero DOS trades explicaban todo:
# Milan 32C comprado a $0.0005 ($20 = 40.000 shares, +$220) y Hong Kong 30C a $0.0010
# ($20 = 20.000 shares, +$140). Sin esos dos, el resultado real es -$238. Ninguno de
# los dos es ejecutable: no existe book para comprar 40.000 shares a medio milesimo
# ni para vender $240 en un bucket de $0.006 — `shares = BET_PER_LEG / real_ask`
# asume fill completo sin mirar la profundidad. Es el mismo defecto que en dinero real
# costo la operacion de Ankara. Con estos pisos el papel deja de premiar fantasias de
# liquidez y el track record vuelve a servir para decidir.
MIN_ENTRY_PRICE   = 0.02     # por debajo de esto el tamano en shares se vuelve irreal
MIN_VOLUME        = 500      # mismo umbral que bot_v2.MIN_VOLUME
MAX_SHARES_POR_PATA = 2000   # tope duro de tamano, por si precio y volumen pasan igual
MIN_HOURS         = 1.0      # no comprar si el mercado resuelve en menos de 1h
CHECK_INTERVAL    = 900      # 15 min entre chequeos de toma de ganancia / resolucion
MIN_EV            = 0.10     # EV minimo (mismo umbral que bot_v2.py) para comprar una opcion,
                              # aunque sea la de mayor probabilidad segun nuestro pronostico -
                              # agregado el 2026-07-22: sin este filtro, del 2026-07-20 (cambio
                              # a probabilidad propia) hasta ahora el bot comprada las 2 opciones
                              # mas probables sin chequear si el precio ya las tenia bien cotizadas,
                              # y eso dio 5% de aciertos en 21 trades con -$66.81 de perdida.
MAX_SLIPPAGE      = 0.03     # spread ask-bid maximo tolerado (mismo umbral que bot_v2.py)

STATE_FILE = Path("data/favoritos_state.json")

CIUDADES = {
    # lat/lon son las coordenadas de la ESTACION de resolucion (aeropuerto u observatorio
    # segun corresponda), copiadas de los dashboards individuales (check_chengdu.py,
    # check_buenos_aires.py, etc.) - no el centro de la ciudad.
    "chengdu":      {"name": "Chengdu",       "tz": "Asia/Shanghai",                    "unit": "C", "wu_station": "ZUUU", "wu_country": "CN", "lat": 30.5728,  "lon": 103.9469},
    # Buenos Aires se saco el 2026-07-21: 10 trades seguidos sin acertar con el
    # criterio de probabilidad propia - suficiente para sospechar que algo especifico
    # de esa ciudad/estacion no esta calibrando bien, no solo mala suerte. Sus trades
    # historicos se quedan en data/favoritos_state.json como referencia, pero no se
    # abren posiciones nuevas ahi (ya no esta en este diccionario).
    "sao-paulo":    {"name": "Sao Paulo",     "tz": "America/Sao_Paulo",                "unit": "C", "wu_station": "SBGR", "wu_country": "BR", "lat": -23.4356, "lon": -46.4731},
    "nyc":          {"name": "New York City", "tz": "America/New_York",                 "unit": "F", "wu_station": "KLGA", "wu_country": "US", "lat": 40.7772,  "lon": -73.8726},
    "helsinki":     {"name": "Helsinki",      "tz": "Europe/Helsinki",                  "unit": "C", "wu_station": "EFHK", "wu_country": "FI", "lat": 60.3183,  "lon": 24.9497},
    # Hong Kong resuelve via HKO directo, no via Wunderground (confirmado en el texto
    # de resolucion del mercado) - se deja sin estacion WU a proposito, no aplica la
    # invalidacion por observacion real de este bloque. lat/lon = HKO King's Park (la
    # fuente de resolucion), no el aeropuerto.
    "hong-kong":    {"name": "Hong Kong",     "tz": "Asia/Hong_Kong",                   "unit": "C", "wu_station": None, "wu_country": None, "lat": 22.3020, "lon": 114.1742},
    "milan":        {"name": "Milan",         "tz": "Europe/Rome",                      "unit": "C", "wu_station": "LIMC", "wu_country": "IT", "lat": 45.6306,  "lon": 8.7281},
}

INVALIDACION_BUFFER = 1.0  # grados de margen antes de considerar el bucket matematicamente perdido

# Agrupacion geografica amplia — mismo proxy simple que bot_v2.REGIONES (duplicado
# aca, no importado, con solo las 6 ciudades de este bot). No es deteccion real de
# sistema sinoptico, solo un punto de partida barato para ver riesgo correlacionado.
REGIONES = {
    "US-Este":    ["nyc"],
    "Europa":     ["helsinki", "milan"],
    "Asia-Este":  ["chengdu", "hong-kong"],
    "Sudamerica": ["sao-paulo"],
}
CITY_TO_REGION = {c: r for r, cs in REGIONES.items() for c in cs}


def temp_maxima_wu_hoy(city_slug):
    """Maximo real registrado HOY hasta ahora, via Wunderground - la misma fuente que
    liquida el mercado (confirmado para 5 de las 6 ciudades). None si no aplica o falla."""
    info = CIUDADES[city_slug]
    station, country = info.get("wu_station"), info.get("wu_country")
    if not station or not country:
        return None
    fecha_local = datetime.now(timezone.utc).astimezone(ZoneInfo(info["tz"])).strftime("%Y%m%d")
    wu_units = "e" if info["unit"] == "F" else "m"
    url = (
        f"https://api.weather.com/v1/location/{station}:9:{country}/observations/historical.json"
        f"?apiKey={bot_v2.WU_API_KEY}&units={wu_units}&startDate={fecha_local}"
    )
    try:
        data = requests.get(url, timeout=(5, 8)).json()
        temps = [o["temp"] for o in data.get("observations", []) if o.get("temp") is not None]
        return max(temps) if temps else None
    except Exception as e:
        print(f"  [WU] {city_slug}: {e}")
        return None


def fetch_pronostico(city_slug, fecha):
    """Pronostico ECMWF (Open-Meteo, con bias_correction) para la fecha dada, en la
    estacion de resolucion de esa ciudad. Duplicado del patron de bot_v2.get_ecmwf en
    vez de importarlo porque favoritos_bot.py usa sus propias coordenadas (Helsinki,
    Hong Kong y Milan no estan en bot_v2.LOCATIONS)."""
    info = CIUDADES[city_slug]
    temp_unit = "fahrenheit" if info["unit"] == "F" else "celsius"
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={info['lat']}&longitude={info['lon']}"
        f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
        f"&forecast_days=3&timezone={info['tz']}"
        f"&models=ecmwf_ifs025&bias_correction=true"
    )
    try:
        data = requests.get(url, timeout=(5, 10)).json()
        for d, t in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
            if d == fecha and t is not None:
                return round(float(t), 1)
    except Exception as e:
        print(f"  [ECMWF] {city_slug} {fecha}: {e}")
    return None


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_prob(forecast, t_low, t_high, sigma):
    """Misma formula que bot_v2.bucket_prob, duplicada aca (es matematica de unas
    pocas lineas, no logica de decision - la regla del proyecto es no compartir
    logica de decision entre bots, esto se queda igual de legible duplicado)."""
    f = float(forecast)
    if t_low == -999:
        return norm_cdf((t_high - f) / sigma)
    if t_high == 999:
        return 1.0 - norm_cdf((t_low - f) / sigma)
    lo, hi = (t_low - 0.5, t_high + 0.5) if t_low == t_high else (t_low, t_high)
    return max(0.0, norm_cdf((hi - f) / sigma) - norm_cdf((lo - f) / sigma))


def calc_ev(p, price):
    """Misma formula que bot_v2.calc_ev, duplicada aca por la misma razon que bucket_prob."""
    if price <= 0 or price >= 1:
        return 0.0
    return p * (1.0 / price - 1.0) - (1.0 - p)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {
        "balance": STARTING_CAPITAL,
        "starting_capital": STARTING_CAPITAL,
        "positions": [],
        "picks_log": {},
        "ultimo_resumen": None,
        "pausado": False,
    }


def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    bot_v2.atomic_write(STATE_FILE, json.dumps(state, indent=2))


def elegir_mercado_del_dia(city_slug, tz_name):
    """Busca entre D+0 y D+1 el evento activo mas cercano a resolver (pero con mas de
    MIN_HOURS por delante) — asi no importa el corrimiento de fecha que usa Polymarket
    (ya vimos con Buenos Aires que el mercado 'de hoy' a veces aparece rotulado con la
    fecha de manana)."""
    now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))
    candidatos = []
    for i in (0, 1):
        d = now_local + timedelta(days=i)
        event = bot_v2.get_polymarket_event(city_slug, bot_v2.MONTHS[d.month - 1], d.day, d.year)
        if not event or event.get("closed"):
            continue
        hours = bot_v2.hours_to_resolution(event.get("endDate", ""))
        if hours < MIN_HOURS:
            continue
        candidatos.append((hours, event, d.strftime("%Y-%m-%d")))
    if not candidatos:
        return None, None
    candidatos.sort(key=lambda x: x[0])
    _, event, fecha = candidatos[0]
    return event, fecha


def outcomes_de_evento(event):
    outcomes = []
    for market in event.get("markets", []):
        question = market.get("question", "")
        rng = bot_v2.parse_temp_range(question)
        if not rng:
            continue
        try:
            prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
            price = float(prices[0])
        except Exception:
            continue
        outcomes.append({
            "question": question,
            "market_id": str(market.get("id", "")),
            "range": rng,
            "price": price,
            "volume": float(market.get("volume", 0)),
        })
    return outcomes


def hacer_picks_del_dia():
    state = load_state()
    if "criterio_edge_desde" not in state:
        # Marca desde cuando el bot compra por probabilidad propia en vez de por precio
        # de mercado - los trades de antes de esta fecha no validan la estrategia nueva,
        # reporte_consolidado.py los excluye del conteo hacia los 35 trades/ciudad.
        state["criterio_edge_desde"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
    if state.get("pausado"):
        # Freno de emergencia activo (equity cayo -50% en algun momento) - no se abren
        # posiciones nuevas hasta que se revise manualmente y se reanude a mano.
        print("  [PAUSADO] Freno de emergencia activo - no se compran posiciones nuevas. Corre 'py favoritos_bot.py reanudar' despues de revisar.")
        return
    hoy_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ya_elegidas = set(state["picks_log"].get(hoy_utc, []))

    # El mismo market_id puede aparecer como D+1 un dia y como D+0 al dia siguiente
    # (elegir_mercado_del_dia busca "el mas cercano a resolver" entre D+0/D+1) - sin este
    # chequeo, se termina comprando el mismo mercado dos veces en dias distintos. Se arma
    # una sola vez por ciclo, no por ciudad, para no recorrer todas las posiciones N veces.
    market_ids_ya_comprados = {p["market_id"] for p in state["positions"]}

    resumen = []
    trade_rows = []
    for city_slug, info in CIUDADES.items():
        if city_slug in ya_elegidas:
            continue

        event, fecha_mercado = elegir_mercado_del_dia(city_slug, info["tz"])
        if not event:
            print(f"  [{info['name']}] sin mercado activo hoy, se salta.")
            continue

        outcomes = outcomes_de_evento(event)
        pronostico = fetch_pronostico(city_slug, fecha_mercado)
        sigma = bot_v2.SIGMA_F if info["unit"] == "F" else bot_v2.SIGMA_C

        # Las 2 opciones con mayor probabilidad segun NUESTRO pronostico (no el precio
        # de mercado) - si el mercado sobrevalora un bucket que nuestro modelo no
        # favorece, no se entra ahi solo porque el mercado lo prefiera. MAX_ENTRY_PRICE
        # se mantiene como tope de precio de entrada (no se paga cualquier cosa por la
        # opcion elegida), pero ya no decide CUAL comprar.
        candidatas = [
            o for o in outcomes
            if MIN_ENTRY_PRICE <= o["price"] <= MAX_ENTRY_PRICE
            and o["volume"] >= MIN_VOLUME
            and o["market_id"] not in market_ids_ya_comprados
        ]
        if pronostico is None:
            # Sin pronostico esta corrida - no hay forma de calcular probabilidad
            # propia, se salta la ciudad en vez de caer de nuevo al precio de mercado.
            print(f"  [{info['name']}] sin pronostico ECMWF esta corrida, se salta.")
            continue
        for o in candidatas:
            o["prob"] = bucket_prob(pronostico, o["range"][0], o["range"][1], sigma)
            o["ev"] = calc_ev(o["prob"], o["price"])

        # Filtro de EV minimo - agregado el 2026-07-22 tras confirmar que sin esto el
        # bot compraba la opcion mas probable aunque el mercado ya la tuviera bien (o
        # mejor) cotizada, sin ningun margen real. Ahora la probabilidad decide CUAL
        # bucket es candidato, pero el EV decide si de verdad vale la pena comprarlo.
        candidatas = [o for o in candidatas if o["ev"] >= MIN_EV]
        candidatas.sort(key=lambda o: -o["prob"])
        top2 = candidatas[:2]

        if not top2:
            print(f"  [{info['name']}] ninguna opcion con EV >= {MIN_EV*100:.0f}% esta corrida, se salta.")
            ya_elegidas.add(city_slug)
            continue

        comprada_alguna_pata = False
        for o in top2:
            if state["balance"] < BET_PER_LEG:
                print(f"  [{info['name']}] balance insuficiente (${state['balance']:.2f}), se salta esta pata por ahora.")
                continue

            # Re-validar contra el precio REAL de compra (bestAsk) antes de comprometer
            # la posicion - o["price"] viene de outcomePrices, que es un precio de
            # referencia/mid, no necesariamente lo que de verdad se paga al comprar.
            # Mismo chequeo que ya tiene bot_v2.py (agregado aca el 2026-07-24 tras
            # confirmar en vivo, en un bucket de Milan, que bestAsk podia ser el DOBLE
            # de outcomePrices en buckets baratos/finos).
            try:
                r = requests.get(f"https://gamma-api.polymarket.com/markets/{o['market_id']}", timeout=(3, 5))
                mdata = r.json()
                real_ask = float(mdata.get("bestAsk", o["price"]))
                real_bid = float(mdata.get("bestBid", o["price"]))
                real_spread = round(real_ask - real_bid, 4)
            except Exception as e:
                print(f"  [{info['name']}] no se pudo confirmar bestAsk real, se salta esta pata: {e}")
                continue
            if real_spread > MAX_SLIPPAGE or real_ask >= MAX_ENTRY_PRICE or real_ask < MIN_ENTRY_PRICE:
                print(f"  [{info['name']}] real ask ${real_ask:.4f} (spread ${real_spread:.4f}) — fuera de rango, se salta esta pata.")
                continue
            real_prob = o.get("prob")
            real_ev = calc_ev(real_prob, real_ask) if real_prob is not None else o.get("ev")
            if real_ev is not None and real_ev < MIN_EV:
                print(f"  [{info['name']}] real EV {real_ev:+.2f} con ask ${real_ask:.3f} — por debajo del minimo, se salta esta pata.")
                continue

            shares = round(BET_PER_LEG / real_ask, 2)
            if shares > MAX_SHARES_POR_PATA:
                print(f"  [{info['name']}] {shares:.0f} shares a ${real_ask:.4f} supera el tope "
                      f"de {MAX_SHARES_POR_PATA} — tamano irreal para este book, se salta esta pata.")
                continue

            comprada_alguna_pata = True
            market_ids_ya_comprados.add(o["market_id"])
            state["balance"] -= BET_PER_LEG
            pos = {
                "city": city_slug,
                "city_name": info["name"],
                "date": fecha_mercado,
                "market_id": o["market_id"],
                "question": o["question"],
                "bucket": f"{o['range'][0]}-{o['range'][1]}{info['unit']}",
                "bucket_low": o["range"][0],
                "bucket_high": o["range"][1],
                "entry_price": real_ask,
                "bid_at_entry": real_bid,
                "spread": real_spread,
                "prob": o.get("prob"),  # nuestra probabilidad calculada al comprar -
                                         # None en trades de antes del cambio de criterio,
                                         # usado para el Brier score en render_pnl_report.
                "ev": real_ev,           # EV recalculado con el ask real (referencia/diagnostico,
                                         # None en trades de antes del filtro de EV).
                "shares": shares,
                "cost": BET_PER_LEG,
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "status": "open",
                "closed_at": None,
                "exit_price": None,
                "exit_reason": None,
                "pnl": None,
            }
            state["positions"].append(pos)
            linea = f"  [BUY] {info['name']} {fecha_mercado} | {pos['bucket']} | ${real_ask:.3f} | ${BET_PER_LEG:.0f}"
            print(linea)
            resumen.append(linea)
            trade_rows.append({
                "city": info["name"], "date": fecha_mercado, "bucket": pos["bucket"],
                "price": real_ask, "stake": BET_PER_LEG, "decision": "BUY",
            })

        if comprada_alguna_pata:
            ya_elegidas.add(city_slug)
        else:
            print(f"  [{info['name']}] no se compro nada por falta de capital - se reintenta en el proximo ciclo (no se marca como 'ya hecha hoy').")

    state["picks_log"][hoy_utc] = sorted(ya_elegidas)
    save_state(state)

    if resumen:
        send_telegram("📌 FAVORITOS — picks del día\n" + "\n".join(resumen) +
                              f"\nBalance: ${state['balance']:.2f}")
        try:
            abiertas = [p for p in state["positions"] if p["status"] == "open"]
            equity = state["balance"] + valor_mercado_abiertas(abiertas)
            path = render_picks_report(trade_rows, state["balance"], equity)
            send_telegram_photo(path, caption=f"Favoritos — picks {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
        except Exception as e:
            print(f"  [WARN] Picks report render/send failed: {e}")


def precio_actual(market_id):
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 6))
        d = r.json()
        return d.get("bestBid"), d.get("closed", False), d.get("outcomePrices")
    except Exception:
        return None, False, None


def valor_mercado_abiertas(abiertas):
    """Valor de mercado real (precio actual x acciones) de las posiciones abiertas, no
    solo su costo - para que el equity que se reporta (resumen 24h, status) sea el mismo
    numero que usa el freno de emergencia, no uno mas optimista/pesimista por ignorar
    ganancias o perdidas no realizadas."""
    total = 0.0
    for p in abiertas:
        bid, _closed, _outcome_prices = precio_actual(p["market_id"])
        total += (bid if bid is not None else p["entry_price"]) * p["shares"]
    return total


def bucket_low_high(pos):
    """.get() con fallback: posiciones abiertas antes de que estos campos existieran
    (compradas en una version anterior del bot) no los tienen - sin el fallback, esto
    tira KeyError y corta el chequeo de venta para TODAS las posiciones del ciclo, no
    solo la vieja. Si faltan, se parsean del string "bucket"."""
    low, high = pos.get("bucket_low"), pos.get("bucket_high")
    if low is not None and high is not None:
        return low, high
    try:
        partes = pos["bucket"].rstrip("CF").split("-")
        return float(partes[0]), float(partes[1])
    except Exception:
        return None, None


def distancia_a_bucket(temp, low, high):
    """0 si temp cae dentro del rango, si no la distancia al borde mas cercano."""
    if temp is None or low is None or high is None:
        return None
    if low <= temp <= high:
        return 0.0
    return min(abs(temp - low), abs(temp - high))


def comparar_patas_hermanas(abiertas):
    """Compara, para cada (ciudad, fecha) con 2+ patas abiertas, cual esta mas cerca
    del maximo real registrado hoy. La mas cercana se marca para dejar correr sin techo
    de toma de ganancia (es la candidata que de verdad puede llegar a resolver ganando);
    la mas lejana se marca para cerrar ya, tome lo que tome, porque ya perdio terreno
    frente a su hermana en base al dato real, no solo al precio de mercado.
    Devuelve (set de market_id a cerrar por rezago, dict market_id -> max_hoy usado)."""
    grupos = {}
    for p in abiertas:
        grupos.setdefault((p["city"], p["date"]), []).append(p)
        # La marca se recalcula de cero en cada ciclo. Antes solo se ponia y nunca se
        # sacaba: una pata que dejaba de ser la mas cercana al dato real se quedaba con
        # el techo alto (55%) para siempre — 36 de 102 posiciones historicas terminaron
        # marcadas asi. El dato real cambia durante el dia, la marca tambien tiene que.
        p.pop("take_profit_boost", None)

    cerrar_por_rezago = set()
    max_hoy_cache = {}
    for (city, _fecha), patas in grupos.items():
        if len(patas) < 2:
            continue
        if city not in max_hoy_cache:
            max_hoy_cache[city] = temp_maxima_wu_hoy(city)
        max_hoy = max_hoy_cache[city]
        if max_hoy is None:
            continue

        distancias = []
        for p in patas:
            low, high = bucket_low_high(p)
            distancias.append((p, distancia_a_bucket(max_hoy, low, high)))
        distancias = [(p, d) for p, d in distancias if d is not None]
        if len(distancias) < 2:
            continue

        distancias.sort(key=lambda x: x[1])
        mejor, mejor_dist = distancias[0]
        peor, peor_dist = distancias[-1]
        if peor_dist > mejor_dist:
            # Hay una favorita clara en base al dato real - dejarla correr sin techo,
            # y cerrar la rezagada ya (siguiente ciclo, con el precio de ese momento).
            mejor["take_profit_boost"] = True
            cerrar_por_rezago.add(peor["market_id"])

    return cerrar_por_rezago, max_hoy_cache


def monitorear_posiciones():
    state = load_state()
    abiertas = [p for p in state["positions"] if p["status"] == "open"]
    if not abiertas:
        return

    cerrar_por_rezago, max_hoy_cache = comparar_patas_hermanas(abiertas)

    cambios = []
    bid_cache = {}
    for pos in abiertas:
        bid, closed, outcome_prices = precio_actual(pos["market_id"])
        bid_cache[pos["market_id"]] = bid

        if closed:
            # Mercado ya resolvio: liquida al valor final (gano = $1/share, perdio = $0).
            try:
                prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                yes_price = float(prices[0]) if prices else 0.0
            except Exception:
                yes_price = 0.0
            gano = yes_price >= 0.9
            exit_price = 1.0 if gano else 0.0
            pnl = round((exit_price - pos["entry_price"]) * pos["shares"], 2)
            state["balance"] += pos["cost"] + pnl
            pos.update(status="closed", closed_at=datetime.now(timezone.utc).isoformat(),
                       exit_price=exit_price, exit_reason="resuelto", pnl=pnl)
            linea = f"  [RESUELTO] {pos['city_name']} {pos['date']} {pos['bucket']} -> {'GANO' if gano else 'PERDIO'} | PnL {pnl:+.2f}"
            print(linea); cambios.append(linea)
            continue

        if bid is None:
            continue

        if pos["market_id"] in cerrar_por_rezago:
            # Perdio la comparacion contra su pata hermana segun el dato real de hoy -
            # se cierra ya, tome lo que tome el precio en este momento.
            pnl = round((bid - pos["entry_price"]) * pos["shares"], 2)
            state["balance"] += pos["cost"] + pnl
            pos.update(status="closed", closed_at=datetime.now(timezone.utc).isoformat(),
                       exit_price=bid, exit_reason="rezago_vs_hermana", pnl=pnl)
            linea = (f"  [CIERRE POR REZAGO] {pos['city_name']} {pos['date']} {pos['bucket']} "
                     f"| entry ${pos['entry_price']:.3f} -> ${bid:.3f} | PnL {pnl:+.2f}")
            print(linea); cambios.append(linea)
            continue

        # Invalidacion por observacion real: si Wunderground (la fuente que liquida el
        # mercado) ya registro hoy un maximo por encima de nuestro bucket, el bucket esta
        # matematicamente perdido -el maximo del dia solo puede subir o quedarse igual-,
        # sin importar lo que diga el precio todavia. Salimos ya, no esperamos a que el
        # precio (o la resolucion oficial) lo reflejen.
        low, bucket_high = bucket_low_high(pos)
        # dict.get(clave, default) evalua el default SIEMPRE, asi que la version anterior
        # ("...get(city, temp_maxima_wu_hoy(city))") pegaba a Wunderground una vez por
        # posicion por ciclo aunque el cache ya tuviera el dato — el cache no servia de
        # nada. Ahora se consulta solo si de verdad falta, y se guarda para las hermanas.
        if pos["city"] not in max_hoy_cache:
            max_hoy_cache[pos["city"]] = temp_maxima_wu_hoy(pos["city"])
        max_hoy = max_hoy_cache[pos["city"]]
        if max_hoy is not None and bucket_high is not None and max_hoy > bucket_high + INVALIDACION_BUFFER:
            pnl = round((bid - pos["entry_price"]) * pos["shares"], 2)
            state["balance"] += pos["cost"] + pnl
            pos.update(status="closed", closed_at=datetime.now(timezone.utc).isoformat(),
                       exit_price=bid, exit_reason="invalidado_observacion_real", pnl=pnl)
            linea = (f"  [INVALIDADO] {pos['city_name']} {pos['date']} {pos['bucket']} "
                     f"| WU ya registro {max_hoy}° | entry ${pos['entry_price']:.3f} -> ${bid:.3f} | PnL {pnl:+.2f}")
            print(linea); cambios.append(linea)
            continue

        # A la pata marcada como "favorita en vivo" (comparar_patas_hermanas) se le
        # sube el techo de toma de ganancia a un nivel alto (no se deja del todo sin
        # limite - Helsinki nos enseño que puede revertirse y volver a stop-loss).
        take_profit_activo = TAKE_PROFIT_BOOST if pos.get("take_profit_boost") else TAKE_PROFIT

        ganancia_pct = (bid / pos["entry_price"]) - 1.0
        if ganancia_pct >= take_profit_activo or ganancia_pct <= -STOP_LOSS:
            razon = "stop_loss" if ganancia_pct <= -STOP_LOSS else "take_profit"
            etiqueta = "STOP" if razon == "stop_loss" else "SELL"
            pnl = round((bid - pos["entry_price"]) * pos["shares"], 2)
            state["balance"] += pos["cost"] + pnl
            pos.update(status="closed", closed_at=datetime.now(timezone.utc).isoformat(),
                       exit_price=bid, exit_reason=razon, pnl=pnl)
            linea = (f"  [{etiqueta} {ganancia_pct*100:+.0f}%] {pos['city_name']} {pos['date']} {pos['bucket']} "
                     f"| entry ${pos['entry_price']:.3f} -> ${bid:.3f} | PnL {pnl:+.2f}")
            print(linea); cambios.append(linea)
        elif pos.get("take_profit_boost") and pos["market_id"] not in cerrar_por_rezago:
            print(f"  [FAVORITA, TECHO {TAKE_PROFIT_BOOST*100:.0f}% ({ganancia_pct*100:+.0f}% ahora)] {pos['city_name']} {pos['date']} {pos['bucket']}")

    # Freno de emergencia: equity real (caja + valor de mercado de lo que sigue abierto,
    # no solo el costo) contra el capital inicial. Si cae -50%, se pausa la apertura de
    # posiciones nuevas hasta revision manual - las salidas de proteccion (stop-loss,
    # invalidacion, take-profit) siguen activas, solo se corta la toma de riesgo nueva.
    abiertas_ahora = [p for p in state["positions"] if p["status"] == "open"]
    # Ojo con el nombre: esta suma NO puede llamarse `valor_mercado_abiertas`, que es
    # una funcion de este mismo modulo — la version anterior la pisaba con un float y
    # cualquier llamada posterior en esta funcion habria reventado con
    # "TypeError: 'float' object is not callable".
    valor_abiertas = sum(
        (bid_cache.get(p["market_id"]) or p["entry_price"]) * p["shares"]
        for p in abiertas_ahora
    )
    equity = state["balance"] + valor_abiertas
    if not state.get("pausado") and equity <= state["starting_capital"] * (1 - FRENO_EMERGENCIA_DRAWDOWN):
        state["pausado"] = True
        alerta = (
            f"🚨 FRENO DE EMERGENCIA ACTIVADO\n"
            f"Equity: ${equity:.2f} de ${state['starting_capital']:.2f} "
            f"({(equity/state['starting_capital']-1)*100:+.1f}%)\n"
            f"Se detuvo la apertura de posiciones nuevas. Las posiciones abiertas siguen "
            f"protegidas por stop-loss/invalidacion.\n"
            f"Revisar y correr 'py favoritos_bot.py reanudar' para retomar."
        )
        print(alerta)
        send_telegram(alerta)

    save_state(state)
    if cambios:
        send_telegram("💰 FAVORITOS — movimientos\n" + "\n".join(cambios) +
                              f"\nBalance: ${state['balance']:.2f}")


def resumen_texto(state):
    abiertas  = [p for p in state["positions"] if p["status"] == "open"]
    cerradas  = [p for p in state["positions"] if p["status"] == "closed"]
    ganadas   = [p for p in cerradas if p["pnl"] is not None and p["pnl"] >= 0]
    pnl_total = sum(p["pnl"] for p in cerradas if p["pnl"] is not None)
    # Valor de mercado real, no el costo - mismo criterio que usa el freno de emergencia,
    # para que este numero y el que decide pausar el bot nunca se contradigan.
    equity = state["balance"] + valor_mercado_abiertas(abiertas)

    lineas = [
        "📊 FAVORITOS — corte de 24h",
        f"Capital inicial: ${state['starting_capital']:.2f}",
        f"Balance en caja: ${state['balance']:.2f}",
        f"Equity real (caja + valor de mercado de lo abierto): ${equity:.2f}",
        f"Rendimiento: {((equity/state['starting_capital'])-1)*100:+.1f}%",
        f"Posiciones abiertas: {len(abiertas)} | cerradas: {len(cerradas)}",
    ]
    if cerradas:
        lineas.append(f"Aciertos: {len(ganadas)}/{len(cerradas)} ({len(ganadas)/len(cerradas):.0%}) | PnL realizado: {pnl_total:+.2f}")
    for p in abiertas:
        lineas.append(f"  · {p['city_name']} {p['date']} {p['bucket']} entry ${p['entry_price']:.3f}")
    return "\n".join(lineas)


def enviar_resumen(state=None):
    state = state or load_state()
    texto = resumen_texto(state)
    print(texto)
    send_telegram(texto)
    state["ultimo_resumen"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


def print_status():
    state = load_state()
    abiertas = [p for p in state["positions"] if p["status"] == "open"]
    cerradas = [p for p in state["positions"] if p["status"] == "closed"]
    ganadas  = [p for p in cerradas if p["pnl"] is not None and p["pnl"] >= 0]
    pnl_total = sum(p["pnl"] for p in cerradas if p["pnl"] is not None)

    print("=" * 55)
    print("  FAVORITOS BOT — ESTADO")
    print("=" * 55)
    equity = state["balance"] + valor_mercado_abiertas(abiertas)
    print(f"  Capital inicial: ${state['starting_capital']:.2f}")
    print(f"  Balance en caja: ${state['balance']:.2f}")
    print(f"  Equity real (caja + valor de mercado de lo abierto): ${equity:.2f}")
    if state.get("pausado"):
        print("  ⚠️  PAUSADO por freno de emergencia — no se compran posiciones nuevas.")
    print(f"  Posiciones abiertas: {len(abiertas)} | cerradas: {len(cerradas)}")
    if cerradas:
        print(f"  Aciertos: {len(ganadas)}/{len(cerradas)} ({len(ganadas)/len(cerradas):.0%}) | PnL realizado: {pnl_total:+.2f}")
    for p in abiertas:
        print(f"    {p['city_name']:<14} {p['date']} {p['bucket']:<10} entry ${p['entry_price']:.3f}")
    print("=" * 55)


def render_pnl_report():
    """PnL snapshot (equity, win rate, PnL por ciudad) en el mismo estilo visual que
    render_picks_report — implementado por separado, no importado de bot_v2.py."""
    state    = load_state()
    abiertas = [p for p in state["positions"] if p["status"] == "open"]
    cerradas = [p for p in state["positions"] if p["status"] == "closed"]
    ganadas  = [p for p in cerradas if p["pnl"] is not None and p["pnl"] >= 0]
    pnl_total = sum(p["pnl"] for p in cerradas if p["pnl"] is not None)
    equity   = state["balance"] + valor_mercado_abiertas(abiertas)
    ret_pct  = (equity / state["starting_capital"] - 1) * 100

    C_BG, C_TEXT, C_DIM = "#0d1117", "#e6edf3", "#8b949e"
    C_POS, C_NEG = "#3fb950", "#f85149"

    rows = []
    rows.append(("FAVORITOS — PNL", C_TEXT, 17, True, False))
    rows.append((datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), C_DIM, 11, False, True))
    rows.append(("=" * 78, C_DIM, 10, False, True))
    rows.append((f"Balance: {state['balance']:,.2f}   Equity: {equity:,.2f}   "
                  f"Inicio: {state['starting_capital']:,.2f}   Retorno: {ret_pct:+.1f}%   (USD)",
                  C_POS if ret_pct >= 0 else C_NEG, 12.5, True, True))
    if state.get("pausado"):
        rows.append(("PAUSADO por freno de emergencia — no se compran posiciones nuevas.", C_NEG, 11, True, True))
    if cerradas:
        rows.append((f"Cerradas: {len(cerradas)}   Aciertos: {len(ganadas)} ({len(ganadas)/len(cerradas)*100:.0f}%)   "
                      f"PnL realizado: {pnl_total:+.2f}   Abiertas: {len(abiertas)}", C_TEXT, 11.5, False, True))
    else:
        rows.append((f"Sin posiciones cerradas todavia.   Abiertas: {len(abiertas)}", C_DIM, 11.5, False, True))
    rows.append(("", C_TEXT, 6, False, True))

    if cerradas:
        rows.append((f"{'City':<16}{'Trades':>8}{'WR%':>7}{'PnL':>10}", C_DIM, 11, True, True))
        rows.append(("-" * 78, C_DIM, 10, False, True))
        by_city = {}
        for p in cerradas:
            by_city.setdefault(p["city_name"], []).append(p)
        city_pnls = []
        for name, group in by_city.items():
            w   = len([p for p in group if p["pnl"] is not None and p["pnl"] >= 0])
            pnl = sum(p["pnl"] for p in group if p["pnl"] is not None)
            city_pnls.append((name, len(group), w, pnl))
        city_pnls.sort(key=lambda r: -r[3])
        for name, n_trades, w, pnl in city_pnls:
            wr = w / n_trades * 100
            row = f"{name:<16}{n_trades:>8}{wr:>6.0f}%{pnl:>+10.2f}"
            rows.append((row, C_POS if pnl >= 0 else C_NEG, 11, False, True))
        rows.append(("-" * 78, C_DIM, 10, False, True))
        best, worst = city_pnls[0], city_pnls[-1]
        rows.append((f"Mejor ciudad: {best[0]} ({best[3]:+.2f})   Peor ciudad: {worst[0]} ({worst[3]:+.2f})",
                      C_TEXT, 11, True, True))
    else:
        rows.append(("(sin posiciones cerradas todavia para desglosar por ciudad)", C_DIM, 11, False, True))

    # --- Brier score: solo sobre trades con probabilidad propia guardada (desde el
    # cambio de criterio) - los de antes (favorita del mercado) no tienen "prob" y
    # quedan afuera de esta cuenta a proposito, no serian comparables.
    briers = [(p["prob"] - (1.0 if p["pnl"] is not None and p["pnl"] >= 0 else 0.0)) ** 2
              for p in cerradas if p.get("prob") is not None]
    rows.append(("", C_TEXT, 6, False, True))
    rows.append(("-" * 78, C_DIM, 10, False, True))
    if briers:
        brier = sum(briers) / len(briers)
        ref = "mejor que adivinar 50/50" if brier < 0.25 else "peor que adivinar 50/50"
        rows.append((f"Brier score: {brier:.3f}  (n={len(briers)}, solo criterio nuevo)  -  {ref}",
                      C_POS if brier < 0.25 else C_NEG, 11.5, True, True))
    else:
        rows.append(("Brier score: sin trades con criterio nuevo resueltos todavia.", C_DIM, 11, False, True))

    # --- Correlacion por region (mismo proxy que bot_v2.py, ver REGIONES arriba).
    por_region = {}
    for p in abiertas:
        region = CITY_TO_REGION.get(p["city"], "Otra")
        por_region.setdefault(region, []).append(p["city_name"])
    clusters = {r: cs for r, cs in por_region.items() if len(cs) >= 2}
    rows.append(("", C_TEXT, 6, False, True))
    if clusters:
        rows.append(("Riesgo correlacionado (2+ posiciones abiertas en la misma region):",
                      C_NEG, 11, True, True))
        for region, ciudades in clusters.items():
            rows.append((f"  {region}: {len(ciudades)} posiciones -> {', '.join(ciudades)}", C_NEG, 10.5, False, True))
    elif abiertas:
        rows.append(("Sin riesgo correlacionado detectado — posiciones abiertas repartidas en regiones distintas.",
                      C_POS, 10.5, False, True))

    n = len(rows)
    fig_h = max(3.2, 0.30 * n + 0.7)
    fig, ax = plt.subplots(figsize=(10, fig_h), facecolor=C_BG)
    ax.set_facecolor(C_BG)
    ax.axis("off")
    dy = 1.0 / (n + 1)
    y  = 1.0 - dy * 0.6
    for text, color, size, bold, mono in rows:
        ax.text(0.02, y, text, transform=ax.transAxes, color=color, fontsize=size,
                fontweight="bold" if bold else "normal",
                family="monospace" if mono else "sans-serif", va="top")
        y -= dy

    img_dir = Path("data/images")
    img_dir.mkdir(parents=True, exist_ok=True)
    path = img_dir / "favoritos_pnl.png"
    fig.savefig(path, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def send_pnl_report():
    path = render_pnl_report()
    send_telegram_photo(path, caption=f"Favoritos — PnL {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return path


def main():
    print("FAVORITOS BOT — capital ficticio $200, favoritas del mercado")
    print(f"TP {TAKE_PROFIT*100:.0f}% (o {TAKE_PROFIT_BOOST*100:.0f}% si es la favorita en vivo) | "
          f"SL {STOP_LOSS*100:.0f}% | invalidacion por Wunderground | "
          f"freno de emergencia a -{FRENO_EMERGENCIA_DRAWDOWN*100:.0f}%")
    print(f"Ciudades: {', '.join(c['name'] for c in CIUDADES.values())}")
    print("Ctrl+C para detener\n")
    while True:
        try:
            hacer_picks_del_dia()
            monitorear_posiciones()

            state = load_state()
            ultimo = state.get("ultimo_resumen")
            necesita_resumen = (
                ultimo is None
                or (datetime.now(timezone.utc) - datetime.fromisoformat(ultimo)) >= timedelta(hours=24)
            )
            if necesita_resumen:
                enviar_resumen(state)
        except Exception as e:
            print(f"  [ERROR] {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        main()
    elif cmd == "status":
        print_status()
    elif cmd == "resumen":
        enviar_resumen()
    elif cmd == "reanudar":
        state = load_state()
        state["pausado"] = False
        save_state(state)
        print("Freno de emergencia levantado - vuelve a comprar posiciones nuevas desde el proximo ciclo.")
        send_telegram("✅ FAVORITOS — freno de emergencia levantado manualmente, retomando compras.")
    elif cmd == "pnl":
        path = send_pnl_report()
        print(f"Reporte de PnL enviado a Telegram. Imagen: {path}")
