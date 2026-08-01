#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
husky_check_resolutions.py — Cierra el circulo pedido por el usuario 2026-07-29:
revisa cada prediccion ya registrada (data/live_predictions_log.json, escrito por
husky_live_snapshot.py) cuyo mercado de Polymarket ya resolvio, y guarda que bucket
gano de verdad + la temperatura real, para poder medir si el modelo (peak_final)
y las elecciones de EV/Kelly (mejor_bucket) sirven o no.

No opera nada — solo lee y anota. Pensado para correr en cada ciclo de la tarea
programada (junto a husky_live_snapshot.py + husky_daily_digest.py), es barato
(una llamada por registro pendiente).

Uso:
    python husky_check_resolutions.py
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

import bot_v2 as B

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LOG_FILE = Path("data/live_predictions_log.json")
EXTRA_LOCATIONS_FILE = Path("data/husky/extra_locations.json")

# Mismo parche que husky_live_snapshot.py — este script se corre como proceso
# SEPARADO (via subprocess, no import), asi que el bot_v2.LOCATIONS que ve aca es
# una importacion limpia sin las 21 ciudades extra. Sin esto, B.get_actual_temp /
# B.get_polymarket_event revientan con KeyError apenas hay que resolver una
# prediccion de una ciudad extra (ej. Helsinki) — y como el loop de mas abajo no
# tenia try/except por registro, UN solo KeyError tumbaba la corrida entera y no
# se guardaba nada, ni siquiera lo de las 30 ciudades reales que si funcionaban.
# Encontrado 2026-07-30 en una auditoria completa pedida por el usuario.
if EXTRA_LOCATIONS_FILE.exists():
    _extra = json.loads(EXTRA_LOCATIONS_FILE.read_text(encoding="utf-8"))
    for _slug, _loc in _extra.items():
        B.LOCATIONS[_slug] = {k: v for k, v in _loc.items() if k != "historical_station"}
        B.TIMEZONES[_slug] = _loc["timezone"]


def cargar_log():
    if not LOG_FILE.exists():
        return {"records": []}
    return json.loads(LOG_FILE.read_text(encoding="utf-8"))


def guardar_log(log):
    tmp = LOG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(LOG_FILE)


def evento_de(slug, fecha_str):
    y, m, d = fecha_str.split("-")
    return B.get_polymarket_event(slug, B.MONTHS[int(m) - 1], int(d), int(y))


def crps_gaussiano(mu, sigma, y):
    """CRPS de un pronostico gaussiano N(mu, sigma) contra el valor real y - formula
    cerrada de Gneiting & Raftery 2007. Mas bajo = mejor. A diferencia del Brier
    score (que solo evalua el bucket apostado ese dia), esto puntua la calidad de
    TODA la distribucion del pronostico contra el resultado real, tenga pick o no.
    None si falta cualquier dato (mu, sigma o y)."""
    if mu is None or sigma is None or y is None or sigma <= 0:
        return None
    import math
    z = (y - mu) / sigma
    phi = math.exp(-z * z / 2) / math.sqrt(2 * math.pi)
    valor = sigma * (z * (2 * B.norm_cdf(z) - 1) + 2 * phi - 1 / math.sqrt(math.pi))
    return round(valor, 4)


def bucket_ganador(event):
    """De todos los buckets del evento, cual resolvio YES (outcomePrices ~[1,0]).
    None si el evento todavia no cerro (ningun bucket a ~1.0 todavia)."""
    for m in event.get("markets", []):
        try:
            prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
            yes_price = float(prices[0])
        except Exception:
            continue
        if yes_price >= 0.95:
            rng = B.parse_temp_range(m.get("question", ""))
            return rng, m.get("question", "")
    return None, None


def main():
    log = cargar_log()
    hoy = datetime.now(timezone.utc)
    pendientes = [r for r in log["records"] if not r["resolved"]]
    print(f"{len(pendientes)} predicciones pendientes de resolucion")

    revisados = resueltos = fallidos = 0
    for r in pendientes:
        try:
            # Los mercados de "highest temperature" resuelven el dia siguiente al que
            # miden (necesitan el dato oficial del cierre de ese dia) — no tiene sentido
            # ni gastar una llamada en preguntar antes de esa ventana.
            fecha_mercado = datetime.strptime(r["fecha_mercado"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if hoy < fecha_mercado + timedelta(hours=20):
                continue
            revisados += 1

            event = evento_de(r["slug"], r["fecha_mercado"])
            if not event or not event.get("closed"):
                continue
            rng_ganador, pregunta_ganadora = bucket_ganador(event)
            if rng_ganador is None:
                continue  # cerrado pero todavia sin outcome claro, se reintenta el proximo ciclo

            actual_temp = B.get_actual_temp(r["slug"], r["fecha_mercado"])

            mb = r.get("mejor_bucket")
            pick_won = None
            if mb and mb.get("range"):
                pick_won = (round(mb["range"][0], 3) == round(rng_ganador[0], 3) and
                            round(mb["range"][1], 3) == round(rng_ganador[1], 3))

            r["winning_bucket"] = pregunta_ganadora
            r["winning_range"] = list(rng_ganador)
            r["pick_won"] = pick_won
            r["forecast_error"] = round(r["peak_final"] - actual_temp, 2) if (r["peak_final"] is not None and actual_temp is not None) else None
            # CRPS (formula cerrada para pronostico gaussiano N(peak_final, sigma) vs el
            # valor real) - evalua la distribucion COMPLETA del pronostico, no solo el
            # bucket que se apostaria ese dia. Solo disponible para registros que ya
            # guardan "sigma" (agregado 2026-07-31 a husky_live_snapshot.py) - los
            # anteriores se quedan en None, no se recalculan con un sigma inventado.
            r["crps"] = crps_gaussiano(r["peak_final"], r.get("sigma"), actual_temp)

            if actual_temp is None:
                # Bug encontrado 2026-07-30 en auditoria: antes esto igual marcaba
                # resolved=True con actual_temp=None, y el print de abajo (que asumia
                # forecast_error siempre numerico) tiraba TypeError sobre None — el
                # except de mas abajo lo atajaba y contaba como "fallido, se reintenta",
                # pero como los campos ya se habian escrito en `r` y resolved quedaba en
                # True, guardar_log() persistia el registro varado PARA SIEMPRE (el
                # filtro de pendientes es solo `not r["resolved"]`): el panel de Track
                # Record mostraba ese registro sin dato real de por vida, mientras el log
                # decia falsamente que se iba a reintentar. Ahora NO se marca resolved
                # hasta tener actual_temp de verdad; el bucket ganador/pick_won ya
                # calculado arriba se re-guarda igual cada ciclo (barato, no hace daño).
                fallidos += 1
                print(f"  [WARN] {r['name']} {r['fecha_mercado']}: bucket ganador OK "
                      f"({pregunta_ganadora}) pero sin temperatura real todavia — se reintenta")
                continue

            r["resolved"] = True
            r["actual_temp"] = actual_temp
            r["resolved_at"] = datetime.now(timezone.utc).isoformat()
            resueltos += 1

            resultado_txt = ("SIN PICK" if mb is None else ("GANO" if pick_won else "PERDIO"))
            print(f"  {r['name']:16} {r['fecha_mercado']} -> real {actual_temp} | "
                  f"pico_final {r['peak_final']} (err {r['forecast_error']:+.1f}) | pick: {resultado_txt}")
        except Exception as e:
            # Un registro puntual que falla (ciudad rara, API caida un rato, etc.) no
            # tiene que tumbar el resto — se reintenta solo el proximo ciclo. Antes de
            # esto, UN error mataba el proceso entero y no se guardaba nada de nada
            # (encontrado 2026-07-30 en auditoria: pasaba siempre con las 21 ciudades
            # extra porque bot_v2.LOCATIONS no las tenia — ya arreglado arriba, pero
            # esta proteccion queda para cualquier otro caso futuro).
            fallidos += 1
            print(f"  [WARN] {r.get('name', r.get('slug'))} {r.get('fecha_mercado')}: {e}")

    guardar_log(log)
    print(f"\n{revisados} revisados, {resueltos} recien resueltos, {fallidos} fallidos (se reintentan)")


if __name__ == "__main__":
    main()
