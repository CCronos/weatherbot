#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
husky_live_snapshot.py — Pronostico de pico maximo EN VIVO para las 30 ciudades
operadas, cruzando 3 fuentes independientes:

  1. METAR en vivo (temperatura y viento actuales, Aviation Weather, sin key)
  2. ECMWF + modelo regional (Open-Meteo, sin key), corregidos por el sesgo YA
     CALIBRADO en data/calibration.json (ver bot_v2.get_bias/get_sigma)
  3. El historial hora-a-hora ya descargado (data/husky/passport/*.json) — cuanto
     sube en promedio DESDE la hora y el viento de AHORA MISMO, condicionado por
     temporada real (hemisferio) — via husky_query.consultar()

No es un proceso que corre solo — no hay capacidad de "internet en vivo" en
paginas Artifact publicadas (verificado 2026-07-29), asi que esto es un script que
se corre a pedido, guarda data/live_peak_snapshot.json, y (via publish_passport.py
o a mano) se re-publica la pagina con el dato fresco. "En vivo" = "tan fresco como
la ultima vez que se corrio", no tiempo real continuo.

Uso:
    python husky_live_snapshot.py            # las 30 ciudades, guarda snapshot
    python husky_live_snapshot.py LTAC        # solo una ciudad, para probar rapido
"""

import sys
import json
import time
from pathlib import Path
from datetime import datetime, timezone

import bot_v2 as B
import husky_query as H

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_FILE = Path("data/live_peak_snapshot.json")
EXTRA_LOCATIONS_FILE = Path("data/husky/extra_locations.json")

# Las 21 ciudades que Husky cubre y nosotros no operamos (de las 50 totales) — se
# suman aca en caliente al diccionario de bot_v2, con el MISMO criterio de modelo
# regional por zona que ya usa bot_v2.LOCATIONS (JMA-GSM para Corea, ICON-seamless
# para China, ICON-EU para Europa, sin modelo regional donde bot_v2 tampoco lo
# tiene establecido para esa region). Pedido del usuario 2026-07-29: "integra las
# 20 ciudades que faltan bajo los mismos parametros de las 30 primeras".
_EXTRA = json.loads(EXTRA_LOCATIONS_FILE.read_text(encoding="utf-8")) if EXTRA_LOCATIONS_FILE.exists() else {}
HISTORICAL_STATION_OVERRIDE = {}  # station_en_vivo -> station_del_historico, cuando difieren
for _slug, _loc in _EXTRA.items():
    B.LOCATIONS[_slug] = {k: v for k, v in _loc.items() if k != "historical_station"}
    B.TIMEZONES[_slug] = _loc["timezone"]
    if _loc.get("historical_station") and _loc["historical_station"] != _loc["station"]:
        HISTORICAL_STATION_OVERRIDE[_loc["station"]] = _loc["historical_station"]

# Nuestro codigo de estacion de resolucion no siempre coincide con el que tiene
# historial descargado. Confirmados (2026-07-29):
#   - Paris (nuestra, LFPG/Charles de Gaulle): el historial disponible es de LFPB
#     (Le Bourget) — aeropuerto DISTINTO de verdad, no un typo. Se salta el cruce
#     historico en vez de mezclarlos.
#   - Istanbul (extra, LTFM real): Husky guardo su historico bajo "LTMF" (typo de
#     ellos, las letras invertidas) — mismo aeropuerto, se resuelve via
#     HISTORICAL_STATION_OVERRIDE arriba, no se salta.
SIN_HISTORICO_COMPATIBLE = {"LFPG"}

COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def wind_to_compass8(deg):
    """METAR reporta 'VRB' (viento variable, tipicamente con calma o rachas
    inconsistentes) en vez de un numero de grados cuando no hay una direccion
    dominante — no es un dato faltante, es un dato real que no mapea a un punto
    cardinal. Se trata igual que None (sin condicionar por viento) en vez de
    reventar el cruce con el historico para toda la ciudad."""
    if deg is None:
        return None
    try:
        return COMPASS[int((float(deg) / 45.0) + 0.5) % 8]
    except (ValueError, TypeError):
        return None


def fetch_metar_full(station):
    """Como bot_v2.get_metar pero tambien trae direccion de viento — necesaria
    para cruzar contra el historial condicionado por viento."""
    try:
        import requests
        url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json"
        data = requests.get(url, timeout=(5, 8)).json()
        if data and isinstance(data, list):
            d = data[0]
            temp_c = d.get("temp")
            return {
                "temp_c": float(temp_c) if temp_c is not None else None,
                "wind_deg": d.get("wdir"),
                "wind_compass": wind_to_compass8(d.get("wdir")),
                "wind_kt": d.get("wspd"),
                "sky": d.get("cover"),
                "raw": d.get("rawOb"),
                "obs_time": d.get("reportTime"),
            }
    except Exception as e:
        print(f"  [METAR] {station}: {e}")
    return None


def pronosticar_pico(city_slug, loc):
    station = loc["station"]
    unit = loc["unit"]
    fc_unit = "F" if unit == "F" else "C"

    metar = fetch_metar_full(station)
    if metar is None or metar["temp_c"] is None:
        return None
    metar_temp = metar["temp_c"] * 9 / 5 + 32 if unit == "F" else metar["temp_c"]

    dates = [datetime.now(timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo(B.TIMEZONES.get(city_slug, "UTC"))).strftime("%Y-%m-%d")]
    ecmwf = B.get_ecmwf(city_slug, dates).get(dates[0])
    regional = B.get_regional(city_slug, dates).get(dates[0])

    # --- estimado por MODELO: el mismo blend calibrado que usa bot_v2 (inverse-variance
    # cuando hay calibracion de las dos fuentes, si no la que este calibrada, si no ECMWF) ---
    calibradas = []
    for src, val in (("ecmwf", ecmwf), ("regional", regional)):
        if val is None:
            continue
        key = f"{city_slug}_{src}"
        if key in B._cal or True:  # get_bias/get_sigma ya caen al agrupado si falta la ciudad
            corr = val - B.get_bias(city_slug, src)
            sigma = B.get_sigma(city_slug, src)
            calibradas.append((corr, sigma, src))
    if len(calibradas) >= 2:
        pesos = [1.0 / (s ** 2) for _, s, _ in calibradas]
        tw = sum(pesos)
        model_peak = sum(v * w for (v, _, _), w in zip(calibradas, pesos)) / tw
        model_src = "+".join(s for _, _, s in calibradas)
    elif calibradas:
        model_peak, _, model_src = calibradas[0]
    else:
        model_peak, model_src = None, None

    # Nombre real del modelo regional (ej. "ukmo_seamless" para Londres, "icon_d2" para
    # Munich) en vez de la palabra generica "regional" — para que la web diga que modelo
    # exacto se uso. Se calcula ACA (no como parche externo sobre el JSON ya generado,
    # que se perdia en cada corrida nueva — bug encontrado 2026-07-29, todas las corridas
    # despues del parche manual volvieron a mostrar "?" en la columna de modelo).
    model_src_display = None
    if model_src:
        nombre_regional = loc.get("regional_model") or "sin_regional"
        model_src_display = (model_src.replace("regional", nombre_regional)
                              .replace("ecmwf", "ECMWF").replace("_", " "))

    # --- estimado EMPIRICO: temp actual + cuanto sube historicamente desde esta hora,
    # condicionado por el viento de AHORA MISMO (si el historial de esta estacion existe
    # y no es un caso de aeropuerto incompatible como Paris) ---
    estacion_historico = HISTORICAL_STATION_OVERRIDE.get(station, station)
    empirico_peak, hist = None, None
    if station not in SIN_HISTORICO_COMPATIBLE:
        local_now = datetime.now(timezone.utc).astimezone(
            __import__("zoneinfo").ZoneInfo(B.TIMEZONES.get(city_slug, "UTC")))
        hist = H.consultar(estacion_historico, local_now.hour, wind_dir=metar["wind_compass"], mes=local_now.month)
        if hist and hist.get("avg_gain_c") is not None:
            gain = hist["avg_gain_c"] * 9 / 5 if unit == "F" else hist["avg_gain_c"]
            empirico_peak = round(metar_temp + gain, 1)

    # --- viento de hoy: es el "mejor" o "peor" setup historico de la temporada? ---
    viento_nota = None
    passport = H.cargar_passport(estacion_historico) if station not in SIN_HISTORICO_COMPATIBLE else None
    if passport and hist:
        season_block = passport.get("seasons", {}).get(hist["estacion_del_anio"], {})
        # best_setup/worst_setup traen "wind" en None (no {} ausente) cuando la variable
        # dominante de esa estacion/temporada es otra (ej. Madrid: nubosidad, no viento) —
        # el default de .get() no protege eso, la clave SI existe, solo que vale None.
        best = ((season_block.get("best_setup") or {}).get("wind") or {}).get("direction")
        worst = ((season_block.get("worst_setup") or {}).get("wind") or {}).get("direction")
        if metar["wind_compass"] == best:
            viento_nota = "viento HOY coincide con el setup que MAS impulsa el calentamiento esta temporada"
        elif metar["wind_compass"] == worst:
            viento_nota = "viento HOY coincide con el setup que MAS frena el calentamiento esta temporada"

    # --- blend final: promedio de modelo + empirico cuando ambos existen (concuerdan
    # o no — el desacuerdo entre ellos es en si mismo la senal de confianza) ---
    candidatos = [v for v in (model_peak, empirico_peak) if v is not None]
    peak_final = round(sum(candidatos) / len(candidatos), 1) if candidatos else None
    spread = round(abs(model_peak - empirico_peak), 1) if (model_peak is not None and empirico_peak is not None) else None

    # --- EV/Kelly contra el mercado de HOY de Polymarket, usando peak_final como
    # el pronostico y la sigma YA CALIBRADA (misma matematica que bot_v2.scan_and_update,
    # pero con nuestro pico combinado modelo+empirico en vez de solo modelo) ---
    mejor_bucket = None
    fecha_mercado = datetime.now(timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo(B.TIMEZONES.get(city_slug, "UTC"))).strftime("%Y-%m-%d")
    if peak_final is not None:
        dt_local = datetime.now(timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo(B.TIMEZONES.get(city_slug, "UTC")))
        event = B.get_polymarket_event(city_slug, B.MONTHS[dt_local.month - 1], dt_local.day, dt_local.year)
        if event:
            fuente_sigma = model_src.split("+")[0] if model_src else "ecmwf"
            sigma = B.get_sigma(city_slug, fuente_sigma)
            candidatos = []
            for m in event.get("markets", []):
                rng = B.parse_temp_range(m.get("question", ""))
                if not rng:
                    continue
                try:
                    price = float(json.loads(m.get("outcomePrices", "[0.5,0.5]"))[0])
                except Exception:
                    continue
                volume = float(m.get("volume") or 0)
                # Piso de $0.06: por debajo de eso el EV se infla matematicamente sin ser
                # ejecutable en tamano real (mismo patron que en Sao Paulo/favoritos_bot:
                # Ankara 26C a $0.055 daba EV +387%, numero real segun la formula pero
                # inutil para operar). Antes solo se marcaba con una alerta en la tabla;
                # ahora se descarta directo de "mejor bucket" en vez de mostrarlo como
                # si fuera una oportunidad real. Corregido 2026-07-29 a pedido del usuario.
                if volume < B.MIN_VOLUME or not (0.06 <= price <= 0.75):
                    continue
                p = B.bucket_prob(peak_final, rng[0], rng[1], sigma)
                ev = B.calc_ev(p, price)
                if ev <= 0:
                    continue
                # Nivel de confianza por banda de precio — de la auditoria de 1680 trades
                # reales de las wallets vigiladas (2026-07-28): $0.20-$0.40 es la UNICA
                # banda rentable Y escalable (37% acierto, +30% ROI real sobre $25.5K).
                # Por debajo de $0.10 el book no aguanta tamano aunque el EV se vea enorme
                # (+200% ROI historico pero sobre ~$3 por trade — es loteria, no edge).
                if price >= 0.20:
                    confianza = "escalable"
                elif price >= 0.10:
                    confianza = "moderado"
                else:
                    confianza = "cautela"
                candidatos.append({
                    "bucket": (f"<={rng[1]:g}{unit}" if rng[0] == -999 else
                               f">={rng[0]:g}{unit}" if rng[1] == 999 else
                               f"{rng[0]:g}{unit}" if rng[0] == rng[1] else
                               f"{rng[0]:g}-{rng[1]:g}{unit}"),
                    "price": price, "prob": round(p, 3), "ev": round(ev, 3),
                    "kelly": round(B.calc_kelly(p, price), 4), "volume": round(volume, 0),
                    "confianza": confianza,
                    "market_id": str(m.get("id", "")), "range": [rng[0], rng[1]],
                })
            if candidatos:
                # Se prioriza la banda de confianza (escalable > moderado > cautela) ANTES
                # que el EV crudo — un EV modesto en la banda ejecutable vale mas que un EV
                # gigante en una banda que la evidencia real dice que no se puede operar en
                # tamano. Dentro de la misma banda, el mejor EV desempata.
                orden_confianza = {"escalable": 0, "moderado": 1, "cautela": 2}
                candidatos.sort(key=lambda c: (orden_confianza[c["confianza"]], -c["ev"]))
                mejor_bucket = candidatos[0]

    return {
        "slug": city_slug, "name": loc["name"], "station": station, "unit": unit,
        "metar_temp": round(metar_temp, 1), "wind_compass": metar["wind_compass"],
        "wind_deg": metar["wind_deg"], "sky": metar["sky"], "metar_obs_time": metar["obs_time"],
        "model_peak": round(model_peak, 1) if model_peak is not None else None, "model_src": model_src,
        "model_src_display": model_src_display,
        "empirico_peak": empirico_peak,
        "empirico_n": hist["n"] if hist else None,
        "empirico_pct_nuevo_max": hist["new_high_pct"] if hist else None,
        "peak_final": peak_final, "spread_modelo_vs_empirico": spread,
        "viento_nota": viento_nota,
        "mejor_bucket": mejor_bucket,
        "fecha_mercado": fecha_mercado,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    B._cal = B.load_cal()
    solo = sys.argv[1].upper() if len(sys.argv) > 1 else None

    resultados = []
    for slug, loc in B.LOCATIONS.items():
        if solo and loc["station"] != solo:
            continue
        print(f"  -> {loc['name']} ({loc['station']})...", end=" ", flush=True)
        # Reintento simple: NYC se cayo en la corrida automatica de las 10:00 del
        # 2026-07-29 por un timeout/hiccup de red puntual (funcionaba perfecto al
        # tiro despues, a mano) — sin reintento, una ciudad se pierde del snapshot
        # entero para todo el dia hasta la proxima corrida programada.
        r, ultimo_error = None, None
        for intento in range(2):
            try:
                r = pronosticar_pico(slug, loc)
                ultimo_error = None
                break
            except Exception as e:
                ultimo_error = e
                if intento == 0:
                    time.sleep(3)
        if r:
            resultados.append(r)
            print(f"metar {r['metar_temp']}{r['unit']} | modelo {r['model_peak']} | "
                  f"empirico {r['empirico_peak']} | final {r['peak_final']}")
        elif ultimo_error:
            print(f"error tras reintento: {ultimo_error}")
        else:
            print("sin datos")
        time.sleep(0.3)

    snapshot = {"generated_at": datetime.now(timezone.utc).isoformat(), "cities": resultados}
    OUT_FILE.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(resultados)} ciudades -> {OUT_FILE}")

    registrar_predicciones(resultados)


LOG_FILE = Path("data/live_predictions_log.json")


def cargar_log():
    if not LOG_FILE.exists():
        return {"records": []}
    return json.loads(LOG_FILE.read_text(encoding="utf-8"))


def registrar_predicciones(resultados):
    """Guarda UNA foto por ciudad+fecha_mercado (la primera corrida del dia para esa
    ciudad; las corridas siguientes del mismo dia no duplican) — pedido del usuario
    2026-07-29 para poder despues comparar nuestra prediccion contra el resultado
    real de Polymarket una vez que el mercado resuelve. Antes esto no se guardaba en
    ningun lado: cada corrida pisaba a la anterior sin dejar rastro para calibrar."""
    log = cargar_log()
    existentes = {(r["slug"], r["fecha_mercado"]) for r in log["records"]}
    nuevos = 0
    for c in resultados:
        clave = (c["slug"], c["fecha_mercado"])
        if clave in existentes:
            continue
        log["records"].append({
            "slug": c["slug"], "name": c["name"], "station": c["station"], "unit": c["unit"],
            "fecha_mercado": c["fecha_mercado"], "logged_at": c["fetched_at"],
            "model_peak": c["model_peak"], "empirico_peak": c["empirico_peak"], "peak_final": c["peak_final"],
            "mejor_bucket": c["mejor_bucket"],
            "resolved": False, "actual_temp": None, "winning_bucket": None,
            "pick_won": None, "forecast_error": None, "resolved_at": None,
        })
        nuevos += 1
    if nuevos:
        LOG_FILE.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"registro de predicciones: {nuevos} nuevas, {len(log['records'])} totales")


if __name__ == "__main__":
    main()
