#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iem_download.py — Baja el historico METAR crudo (temperatura, viento, cielo) de las
30 estaciones que operamos, directo del Iowa Environmental Mesonet (IEM, Universidad
Estatal de Iowa) — la misma red ASOS/AWOS que ya usa bot_v2.get_metar, solo que aca
se pide el archivo historico completo en vez del dato puntual de ahora.

Publico, gratis, sin API key (verificado 2026-07-29). Reemplaza la dependencia del
servicio de pago para la parte de "historial" — el pronostico en vivo (METAR de HOY +
ECMWF/modelo regional) sigue igual, eso ya era propio.

report_type=3,4 trae tanto los reportes de rutina (cada hora, a veces cada 20-30 min)
como los "especiales" (SPECI, disparados por un cambio brusco de condicion) — sin el
4, el conteo queda casi a la mitad del que reporta el servicio de pago (confirmado:
~31k vs ~61k lecturas para Ankara con vs sin especiales).

Uso:
    python iem_download.py            # las 30 estaciones
    python iem_download.py LTAC       # una sola, para probar
"""

import sys
import time
import requests
from pathlib import Path
from datetime import date

import bot_v2 as B

OUT_DIR = Path("data/iem_raw")
OUT_DIR.mkdir(parents=True, exist_ok=True)

START = "2023-01-01"
END = date.today().isoformat()

BASE_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


def descargar_estacion(station, reintentos=3):
    y1, m1, d1 = START.split("-")
    y2, m2, d2 = END.split("-")
    params = {
        "station": station, "data": "tmpc,drct,sknt,skyc1",
        "year1": y1, "month1": m1, "day1": d1,
        "year2": y2, "month2": m2, "day2": d2,
        "tz": "Etc/UTC", "format": "onlycomma",
        "latlon": "no", "elev": "no", "missing": "M", "trace": "T",
        "direct": "no", "report_type": "3,4",
    }
    for intento in range(reintentos):
        try:
            r = requests.get(BASE_URL, params=params, timeout=(10, 60))
            if r.status_code == 429:
                espera = 5 * (intento + 1)
                print(f"    [429] rate limit, espero {espera}s...")
                time.sleep(espera)
                continue
            r.raise_for_status()
            return r.text
        except requests.exceptions.RequestException as e:
            print(f"    [WARN] intento {intento + 1}/{reintentos}: {e}")
            time.sleep(3)
    return None


def main():
    import json
    solo = sys.argv[1].upper() if len(sys.argv) > 1 else None
    # las 50 estaciones (30 que operamos + 20 de expansion futura), no solo LOCATIONS
    codes_path = Path("data/husky/all_50_codes.json")
    if codes_path.exists():
        estaciones = sorted(json.loads(codes_path.read_text(encoding="utf-8")))
    else:
        estaciones = sorted(set(loc["station"] for loc in B.LOCATIONS.values()))
    if solo:
        estaciones = [s for s in estaciones if s == solo]

    print(f"Descargando {len(estaciones)} estaciones, {START} a {END}, desde IEM (mesonet.agron.iastate.edu)\n")
    ok, fail = [], []
    for i, station in enumerate(estaciones, 1):
        out_path = OUT_DIR / f"{station}.csv"
        if out_path.exists() and out_path.stat().st_size > 1000:
            print(f"  [{i}/{len(estaciones)}] {station}... ya existe, se salta")
            ok.append(station)
            continue
        print(f"  [{i}/{len(estaciones)}] {station}...", end=" ", flush=True)
        texto = descargar_estacion(station)
        if texto is None:
            print("FALLO")
            fail.append(station)
            continue
        lineas = texto.strip().count("\n")
        out_path.write_text(texto, encoding="utf-8")
        print(f"{lineas} filas -> {out_path}")
        ok.append(station)
        time.sleep(2.0)  # pausa entre pedidos, no golpear el servidor publico

    print(f"\n{len(ok)} OK, {len(fail)} fallidas")
    if fail:
        print("Fallidas:", ", ".join(fail))


if __name__ == "__main__":
    main()
