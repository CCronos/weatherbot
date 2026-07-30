#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
husky_query.py — Lee el historico ya descargado de Husky (data/husky/passport/*.json,
data/husky/edge/*.json — 50 estaciones, extraido 2026-07-29 via /api/station/{code}/passport
y /api/station/{code}/edge, con el token de sesion del usuario) y responde la pregunta
concreta que motivo la extraccion: "a partir de esta hora, la temperatura sube tantos
grados en tanto % de los dias", corregido por TEMPORADA (no la mezcla de las 4 que
trae el bloque "all") y opcionalmente por direccion de viento + tendencia si se conoce.

No hace llamadas de red — es una capa de lectura sobre los JSON ya bajados. Si mas
adelante se re-extrae el historico (habria que repetir el proceso de login manual +
"copy as cURL" descrito en la conversacion, el token expira 2026-08-28), solo hay que
volver a correr la descarga; esta capa no cambia.

Uso rapido:
    python husky_query.py LTAC 10          # Ankara, historico a partir de las 10:00 (temporada actual)
    python husky_query.py LTAC 10 --wind N --trend climbing
    python husky_query.py --list           # codigos de estacion disponibles
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import date

BASE = Path("data/husky")
PASSPORT_DIR = BASE / "passport"
EDGE_DIR = BASE / "edge"

# Paises al sur del ecuador entre las 50 estaciones de Husky — las estaciones ahi
# tienen las 4 temporadas AL REVES respecto al hemisferio norte (jul-ago-sep es
# invierno, no verano). Confirmado por pais, no por latitud exacta, porque el JSON
# de Husky no trae lat/lon en el bloque "station" (si en /radar, pero no vale la
# pena una llamada de red extra solo para esto).
HEMISFERIO_SUR = {"AR", "BR", "ZA", "NZ", "ID"}  # Buenos Aires, Sao Paulo, Cape Town, Wellington, Jakarta

_MESES_A_ESTACION_NORTE = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}
_OPUESTA = {"winter": "summer", "summer": "winter", "spring": "autumn", "autumn": "spring"}


def estacion_del_anio(country_code, mes=None):
    mes = mes or date.today().month
    base = _MESES_A_ESTACION_NORTE[mes]
    return _OPUESTA[base] if country_code in HEMISFERIO_SUR else base


def _cargar(dir_, code):
    p = dir_ / f"{code.upper()}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def cargar_passport(code):
    return _cargar(PASSPORT_DIR, code)


def cargar_edge(code):
    return _cargar(EDGE_DIR, code)


def estaciones_disponibles():
    return sorted(p.stem for p in PASSPORT_DIR.glob("*.json"))


def _hora_mas_cercana(disponibles, hora):
    """after_hour_by_wind tiene una entrada por hora entera 8-20 (o el rango que
    active_hours_local indique) — si piden una hora fuera de ese rango (de noche,
    por ejemplo) se devuelve la mas cercana en vez de fallar, con una nota."""
    horas = sorted((int(h) for h in disponibles), key=lambda h: abs(h - hora))
    return horas[0]


def consultar(code, hora_local, wind_dir=None, trend=None, mes=None):
    """Devuelve un dict con la mejor estimacion historica disponible para
    "a partir de esta hora, cuanto sube y en que % de los dias", corregida por
    temporada. Prioridad de especificidad (la primera que tenga datos, de mas a
    menos especifica):
      1. after_hour_by_wind de la TEMPORADA correcta, cruzado por viento+tendencia
      2. after_hour_by_wind de la TEMPORADA correcta, solo "any" (sin condicionar viento)
      3. daily_temperature_behavior.heating_after_local_hour de la temporada (checkpoints fijos)
      4. lo mismo pero del bloque "all" (las 4 temporadas mezcladas) — ultimo recurso
    Devuelve None si la estacion no existe en el historico descargado.
    """
    passport = cargar_passport(code)
    if not passport:
        return None

    country = passport["station"].get("country_code")
    estacion = estacion_del_anio(country, mes)
    bloque_temporada = passport.get("seasons", {}).get(estacion, {})
    bloque_all = passport.get("all", {})

    resultado = {
        "station": passport["station"], "estacion_del_anio": estacion,
        "hora_pedida": hora_local, "wind_dir": wind_dir, "trend": trend,
        "nivel": None, "hora_usada": None, "n": None,
        "avg_gain_c": None, "median_gain_c": None, "new_high_pct": None,
        "nota": None,
    }

    for nivel, bloque in (("temporada", bloque_temporada), ("agregado_4_temporadas", bloque_all)):
        ahw = bloque.get("after_hour_by_wind") or {}
        if not ahw:
            continue
        hora_usada = _hora_mas_cercana(ahw.keys(), hora_local)
        celdas = ahw[str(hora_usada)]

        clave = None
        if wind_dir and trend:
            clave = f"{wind_dir}|{trend}" if f"{wind_dir}|{trend}" in celdas else None
        if clave is None and wind_dir and wind_dir in celdas:
            clave = wind_dir
        if clave is None and trend:
            clave = f"any|{trend}" if f"any|{trend}" in celdas else None
        if clave is None:
            clave = "any"
        if clave not in celdas:
            continue

        c = celdas[clave]
        resultado.update(
            nivel=f"{nivel} ({'viento '+clave if clave!='any' else 'sin condicionar viento'})",
            hora_usada=hora_usada, n=c.get("n"),
            avg_gain_c=c.get("avg_gain_c"), median_gain_c=c.get("median_gain_c"),
            new_high_pct=c.get("new_high_pct"),
        )
        if hora_usada != hora_local:
            resultado["nota"] = f"sin dato exacto para las {hora_local}:00, se uso la hora mas cercana con datos ({hora_usada}:00)"
        return resultado

    # Ultimo recurso: los checkpoints fijos (10/12/15/18) de daily_temperature_behavior
    for nivel, bloque in (("temporada_checkpoint", bloque_temporada), ("agregado_checkpoint", bloque_all)):
        dtb = (bloque.get("daily_temperature_behavior") or {}).get("heating_after_local_hour") or {}
        if not dtb:
            continue
        hora_usada = _hora_mas_cercana(dtb.keys(), hora_local)
        c = dtb[str(hora_usada)]
        resultado.update(
            nivel=nivel, hora_usada=hora_usada, n=c.get("samples"),
            avg_gain_c=c.get("avg_remaining_heat_c"), median_gain_c=None,
            new_high_pct=c.get("chance_new_high_after_hour_pct"),
        )
        if hora_usada != hora_local:
            resultado["nota"] = f"sin dato exacto para las {hora_local}:00, se uso el checkpoint mas cercano ({hora_usada}:00)"
        return resultado

    return resultado  # niveles todos None si no hay nada — lo maneja el llamador


def frase(resultado):
    """La frase exacta que se pidio: 'a partir de x hora, la temperatura sube tantos
    grados en el tanto por ciento de los dias' — lista para meter en un mensaje de
    Telegram junto a una senal del bot."""
    if resultado is None:
        return "Sin historico de Husky para esta estacion."
    if resultado["avg_gain_c"] is None:
        return f"Sin datos historicos de Husky para las {resultado['hora_pedida']}:00 en {resultado['station']['name']}."
    r = resultado
    base = (
        f"{r['station']['name']} ({r['station']['code']}) — historico {r['estacion_del_anio']} "
        f"(n={r['n']} dias): a partir de las {r['hora_usada']}:00, la temperatura sube en promedio "
        f"+{r['avg_gain_c']:.1f}C, con nuevo maximo del dia en el {r['new_high_pct']:.0f}% de los dias."
    )
    if r.get("nota"):
        base += f" [{r['nota']}]"
    return base


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("code", nargs="?", help="codigo ICAO de la estacion, ej. LTAC")
    ap.add_argument("hora", nargs="?", type=int, help="hora local (0-23)")
    ap.add_argument("--wind", default=None, help="direccion de viento actual, ej. N, SW")
    ap.add_argument("--trend", default=None, choices=["climbing", "cooling", "plateau"])
    ap.add_argument("--list", action="store_true", help="lista los codigos disponibles")
    args = ap.parse_args()

    if args.list or not args.code:
        print("\n".join(estaciones_disponibles()))
        return

    r = consultar(args.code, args.hora, wind_dir=args.wind, trend=args.trend)
    print(frase(r))
    if r:
        print(json.dumps(r, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
