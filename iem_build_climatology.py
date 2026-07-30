#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iem_build_climatology.py — Construye, desde el METAR crudo propio (data/iem_raw/*.csv,
bajado de IEM — ver iem_download.py), el MISMO tipo de analisis que traia el servicio
de pago: por cada estacion, temporada (corregida por hemisferio) y hora local, cuanto
sube en promedio la temperatura desde ahi y en que % de los dias aparece un maximo
nuevo despues — condicionado tambien por direccion de viento y tendencia (subiendo/
bajando/estable) cuando hay muestra suficiente.

Escribe data/iem/consolidated_full.json con el MISMO esquema que data/husky/
consolidated_full.json, para que husky_query.py y la pagina PASSPORT puedan usar esta
fuente en vez de (o cruzada con) la del servicio de pago sin cambiar nada mas.

Definiciones (replican lo que se veia en los datos de Husky, deducido de su forma):
  - "gain" a la hora H de un dia = maximo FINAL de ese dia menos el maximo ya
    observado hasta esa hora (no la lectura puntual — una lectura puede estar por
    debajo de un pico ya visto antes ese mismo dia).
  - "nuevo maximo despues de esta hora" = el maximo final del dia supera al maximo
    ya observado hasta esa hora.
  - tendencia en una lectura = comparada con la lectura previa (20-30 min antes):
    subiendo si +0.3C o mas, bajando si -0.3C o menos, estable en el medio.
  - temporada = por mes calendario, invertida en el hemisferio sur (mismo criterio
    que husky_query.estacion_del_anio).

Uso:
    python iem_build_climatology.py            # las 50 estaciones
    python iem_build_climatology.py LTAC        # una sola, para probar/revisar
"""

import sys
import csv
import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

IEM_DIR = Path("data/iem_raw")
PASSPORT_DIR = Path("data/husky/passport")  # solo para timezone/nombre/pais, no el analisis
OUT_DIR = Path("data/iem")
OUT_DIR.mkdir(parents=True, exist_ok=True)

COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
SOUTH = {"AR", "BR", "ZA", "NZ", "ID"}
MES_A_ESTACION_NORTE = {12: "winter", 1: "winter", 2: "winter",
                         3: "spring", 4: "spring", 5: "spring",
                         6: "summer", 7: "summer", 8: "summer",
                         9: "autumn", 10: "autumn", 11: "autumn"}
OPUESTA = {"winter": "summer", "summer": "winter", "spring": "autumn", "autumn": "spring"}

TREND_UMBRAL = 0.3  # grados C entre lecturas consecutivas para llamarlo subiendo/bajando
MIN_N_CELDA = 8      # por debajo de esto no se guarda la celda (muestra insuficiente para confiar)


def estacion_del_anio(country_code, mes):
    base = MES_A_ESTACION_NORTE[mes]
    return OPUESTA[base] if country_code in SOUTH else base


def wind_to_compass8(deg_str):
    if not deg_str or deg_str in ("M", ""):
        return None
    try:
        deg = float(deg_str)
    except ValueError:
        return None
    return COMPASS[int((deg / 45.0) + 0.5) % 8]


def cargar_metadata(code):
    p = PASSPORT_DIR / f"{code}.json"
    if not p.exists():
        return {"code": code, "name": code, "timezone": "UTC", "country_code": None}
    return json.loads(p.read_text(encoding="utf-8"))["station"]


def leer_lecturas(code, tz_name):
    """Devuelve lecturas ordenadas: (dt_local, hora_local, temp_c, viento_compass)."""
    p = IEM_DIR / f"{code}.csv"
    if not p.exists():
        return []
    tz = ZoneInfo(tz_name)
    lecturas = []
    with open(p, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            temp_s = row.get("tmpc", "").strip()
            if not temp_s or temp_s == "M":
                continue
            try:
                temp_c = float(temp_s)
                dt_utc = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("UTC"))
            except (ValueError, KeyError):
                continue
            dt_local = dt_utc.astimezone(tz)
            lecturas.append({
                "dt": dt_local, "date": dt_local.date(), "hour": dt_local.hour,
                "temp": temp_c, "wind": wind_to_compass8(row.get("drct")),
            })
    lecturas.sort(key=lambda r: r["dt"])
    return lecturas


def calcular_dias(lecturas):
    """Agrupa por dia local y calcula, para cada lectura: cuanto maximo ya se habia
    visto ESE dia hasta esa lectura (inclusive), el maximo FINAL del dia, la tendencia
    vs la lectura anterior, y la temporada del dia."""
    por_dia = defaultdict(list)
    for r in lecturas:
        por_dia[r["date"]].append(r)

    filas = []
    for fecha, rs in por_dia.items():
        if len(rs) < 4:  # dia con muy pocas lecturas, no vale la pena
            continue
        max_final = max(r["temp"] for r in rs)
        running_max = -999.0
        prev_temp = None
        horas_ya_tomadas = set()  # una muestra por (dia, hora) — no una por cada
                                  # lectura de 20-30 min, que inflaba "n" 2-3x contra
                                  # el conteo real de dias (bug encontrado 2026-07-29
                                  # comparando Ankara: n=679 propio vs n=284 del
                                  # servicio de pago para la misma celda exacta)
        for r in rs:
            running_max = max(running_max, r["temp"])
            if prev_temp is None:
                trend = None
            elif r["temp"] - prev_temp >= TREND_UMBRAL:
                trend = "climbing"
            elif r["temp"] - prev_temp <= -TREND_UMBRAL:
                trend = "cooling"
            else:
                trend = "plateau"
            prev_temp = r["temp"]
            if r["hour"] in horas_ya_tomadas:
                continue  # ya se registro una muestra de esta hora, este dia
            horas_ya_tomadas.add(r["hour"])
            filas.append({
                "hour": r["hour"], "wind": r["wind"], "trend": trend,
                "gain": round(max_final - running_max, 2),
                "nuevo_max_despues": max_final > running_max + 1e-6,
                "mes": fecha.month,
            })
    return filas


def agregar(filas, country_code):
    """Arma el mismo esquema curve[hora][clave] = {n, g, m, p} que consolidated_full.json,
    separado por temporada (invierno/primavera/verano/otono, corregido por hemisferio)."""
    por_temporada = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for f in filas:
        temporada = estacion_del_anio(country_code, f["mes"])
        hora = str(f["hour"])
        claves = ["any"]
        if f["wind"]:
            claves.append(f["wind"])
        if f["trend"]:
            claves.append(f"any|{f['trend']}")
        if f["wind"] and f["trend"]:
            claves.append(f"{f['wind']}|{f['trend']}")
        for clave in claves:
            por_temporada[temporada][hora][clave].append((f["gain"], f["nuevo_max_despues"]))

    resultado = {}
    for temporada, horas in por_temporada.items():
        curve = {}
        for hora, claves in horas.items():
            celdas = {}
            for clave, muestras in claves.items():
                if len(muestras) < MIN_N_CELDA:
                    continue
                gains = sorted(g for g, _ in muestras)
                n = len(gains)
                avg_gain = round(sum(gains) / n, 2)
                median_gain = round(gains[n // 2] if n % 2 else (gains[n // 2 - 1] + gains[n // 2]) / 2, 2)
                pct_nuevo_max = round(100.0 * sum(1 for _, nm in muestras if nm) / n, 1)
                celdas[clave] = {"n": n, "g": avg_gain, "m": median_gain, "p": pct_nuevo_max}
            if celdas:
                curve[hora] = celdas
        if curve:
            dias_unicos = len(set((f["mes"],) for f in filas))  # aprox, no exacto pero informativo
            resultado[temporada] = {
                "n_days": sum(c.get("any", {}).get("n", 0) for c in [curve.get(h, {}) for h in curve][:1]) or None,
                "curve": curve,
            }
    return resultado


def procesar_estacion(code):
    meta = cargar_metadata(code)
    lecturas = leer_lecturas(code, meta.get("timezone") or "UTC")
    if not lecturas:
        return None
    filas = calcular_dias(lecturas)
    if not filas:
        return None
    seasons = agregar(filas, meta.get("country_code"))
    if not seasons:
        return None
    return {
        "code": meta["code"], "name": meta["name"], "country": meta.get("country_code"),
        "timezone": meta.get("timezone"), "seasons": seasons,
        "fuente": "IEM (mesonet.agron.iastate.edu) — propio, no Husky",
    }


def main():
    solo = sys.argv[1].upper() if len(sys.argv) > 1 else None
    codigos = sorted(p.stem for p in IEM_DIR.glob("*.csv") if not p.stem.startswith("_"))
    if solo:
        codigos = [c for c in codigos if c == solo]

    stations = []
    for i, code in enumerate(codigos, 1):
        print(f"  [{i}/{len(codigos)}] {code}...", end=" ", flush=True)
        try:
            r = procesar_estacion(code)
            if r:
                stations.append(r)
                n_lecturas = sum(len(h) for s in r["seasons"].values() for h in [s["curve"]])
                print(f"OK ({len(r['seasons'])} temporadas)")
            else:
                print("sin datos suficientes")
        except Exception as e:
            print(f"error: {e}")

    out = {"generated": datetime.now().strftime("%Y-%m-%d"), "stations": stations}
    out_path = OUT_DIR / "consolidated_full.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"\n{len(stations)} estaciones -> {out_path} ({out_path.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
