#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resume los CSV crudos de data/iem_raw/{ICAO}.csv (lecturas horarias METAR,
2023-01-01 en adelante, via IEM/Iowa State) a una serie diaria compacta por
estacion: temperatura maxima, velocidad de viento y direccion dominante.

Motivo: el CSV crudo tiene ~37.700 filas por estacion (una cada 20-60 min)
- demasiado para embeber en la pagina (103MB las 50 estaciones juntas).
Resumido a un punto por dia queda en ~1.260 filas por estacion, liviano
para el grafico "evolucion historica" de la pestana Detalle por ciudad.

Agrupa por fecha UTC (no local) por simplicidad — esto es para un grafico
de tendencia de 3+ anios, no para logica de trading, un dia de diferencia
por huso horario no cambia el patron visible. bot_v2.py y el track record
si usan fecha local donde importa.

Salida: data/husky/daily_series/{ICAO}.json — lista de dias como arrays
[offset_dias_desde_start, temp_max_c, viento_nudos, dir_index(0-7 o null)]
en vez de objetos, para ahorrar espacio.
"""
import csv
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data/iem_raw"
OUT_DIR = ROOT / "data/husky/daily_series"
OUT_DIR.mkdir(parents=True, exist_ok=True)

DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def dir_index(deg):
    if deg is None:
        return None
    try:
        deg = float(deg) % 360
    except ValueError:
        return None
    return int((deg + 22.5) // 45) % 8


def build_one(path):
    by_day = defaultdict(lambda: {"temps": [], "winds": [], "dirs": []})
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            valid = row.get("valid", "")
            if not valid:
                continue
            day = valid[:10]  # "YYYY-MM-DD" — agrupa por fecha UTC, ver docstring
            temp = row.get("tmpc", "")
            wind = row.get("sknt", "")
            drct = row.get("drct", "")
            d = by_day[day]
            if temp not in ("", "M"):
                try:
                    d["temps"].append(float(temp))
                except ValueError:
                    pass
            if wind not in ("", "M"):
                try:
                    d["winds"].append(float(wind))
                except ValueError:
                    pass
            idx = dir_index(drct) if drct not in ("", "M") else None
            if idx is not None:
                d["dirs"].append(idx)

    days_sorted = sorted(by_day.keys())
    if not days_sorted:
        return None
    start = datetime.strptime(days_sorted[0], "%Y-%m-%d").date()

    out = []
    for day in days_sorted:
        d = by_day[day]
        if not d["temps"]:
            continue
        offset = (datetime.strptime(day, "%Y-%m-%d").date() - start).days
        tmax = round(max(d["temps"]), 1)
        wind_kt = round(statistics.mean(d["winds"]), 1) if d["winds"] else None
        dom_dir = Counter(d["dirs"]).most_common(1)[0][0] if d["dirs"] else None
        out.append([offset, tmax, wind_kt, dom_dir])

    return {"start": days_sorted[0], "days": out}


def main():
    files = sorted(RAW_DIR.glob("*.csv"))
    print(f"{len(files)} estaciones encontradas en {RAW_DIR}")
    total_bytes = 0
    for path in files:
        code = path.stem
        result = build_one(path)
        if result is None:
            print(f"  {code}: sin datos, saltando")
            continue
        out_path = OUT_DIR / f"{code}.json"
        text = json.dumps(result, separators=(",", ":"))
        out_path.write_text(text, encoding="utf-8")
        total_bytes += len(text.encode("utf-8"))
        print(f"  {code}: {len(result['days'])} dias -> {out_path.name} ({len(text)/1024:.0f} KB)")
    print(f"total: {total_bytes/1024/1024:.1f} MB en {OUT_DIR}")


if __name__ == "__main__":
    main()
