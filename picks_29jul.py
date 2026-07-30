#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
picks_29jul.py — Analisis puntual de los mercados del 2026-07-29 con la sigma y el
bias YA CALIBRADOS (data/calibration.json), cruzado con lo que estan haciendo las
wallets que seguimos. Manda el resultado a Telegram.

Por que existe y no se usa bot_v2 directo: bot_v2 solo abre posiciones en D+0, y esto
es un vistazo a D+1 pedido a mano. Ademas aplica dos filtros que bot_v2 no tiene y que
salieron de auditar 1.680 trades reales de las wallets vigiladas (2026-07-28):

  - banda de precio $0.20-$0.40 -> 37% de acierto y +30% ROI sobre $25.5K reales:
    la unica zona rentable Y escalable.
  - banda $0.06-$0.10 -> -36% ROI. Se descarta entera.
  - banda $0.00-$0.03 -> +200% ROI pero sobre $757 en 243 trades (~$3 cada uno):
    funciona como loteria de ticket chico, NO admite tamano.

Es de un solo uso (fecha fija). Borrar cuando pase el 29.
"""

import sys
import json
import math
import time
import requests

import bot_v2 as B

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FECHA = "2026-07-29"
DIA, MES, ANIO = 29, "july", 2026

# Bandas de precio, de la auditoria de las wallets (ver docstring)
BANDA_ESCALABLE = (0.20, 0.40)
BANDA_MUERTA = (0.06, 0.10)
BANDA_LOTERIA = (0.005, 0.03)

MIN_VOL = 1000


def analizar():
    B._cal = B.load_cal()
    filas = []
    for slug, loc in B.LOCATIONS.items():
        ev = B.get_polymarket_event(slug, MES, DIA, ANIO)
        if not ev:
            continue
        e = B.get_ecmwf(slug, [FECHA]).get(FECHA)
        r = B.get_regional(slug, [FECHA]).get(FECHA)
        if e is None and r is None:
            continue

        # Pronostico usado: el regional si existe, corregido por su bias conocido.
        crudo, fuente = (r, "regional") if r is not None else (e, "ecmwf")
        best = crudo - B.get_bias(slug, fuente)

        # Piso de sigma: solo 41 mercados tienen snapshot del modelo regional, muy poco
        # para calibrarlo, asi que get_sigma("regional") suele caer a la constante fija
        # —que es justamente la que estaba demasiado optimista—. Nunca declararse mas
        # seguro que la fuente que SI tenemos bien medida (ECMWF agrupado, n=174).
        sigma = B.get_sigma(slug, fuente)
        piso = B._cal.get(f"_pooled_{loc['unit']}_ecmwf", {}).get("sigma")
        if piso:
            sigma = max(sigma, piso)

        # Si los dos modelos discrepan, la incertidumbre real es mayor que la sigma
        # historica de uno solo: se suma la discrepancia en cuadratura.
        spread = abs(e - r) if (e is not None and r is not None) else 0.0
        sigma_eff = math.sqrt(sigma ** 2 + (spread / 2) ** 2)

        for m in ev.get("markets", []):
            rng = B.parse_temp_range(m.get("question", ""))
            if not rng:
                continue
            try:
                price = float(json.loads(m.get("outcomePrices", "[0.5,0.5]"))[0])
            except Exception:
                continue
            p = B.bucket_prob(best, rng[0], rng[1], sigma_eff)
            filas.append({
                "city": loc["name"], "unit": loc["unit"], "rng": rng, "price": price,
                "vol": float(m.get("volume") or 0), "p": p,
                "ev": B.calc_ev(p, price), "kelly": B.calc_kelly(p, price),
                "ecmwf": e, "reg": r, "spread": spread, "sigma": round(sigma_eff, 2),
                "best": round(best, 1), "fuente": fuente,
            })
        time.sleep(0.15)
    return filas


def etiqueta(f):
    lo, hi = f["rng"]
    u = f["unit"]
    if lo == -999:
        return f"<={hi:g}{u}"
    if hi == 999:
        return f">={lo:g}{u}"
    return f"{lo:g}{u}" if lo == hi else f"{lo:g}-{hi:g}{u}"


def main():
    filas = analizar()
    json.dump(filas, open("data/_picks_29jul.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    esc = sorted([f for f in filas
                  if BANDA_ESCALABLE[0] <= f["price"] <= BANDA_ESCALABLE[1]
                  and f["vol"] >= MIN_VOL and f["ev"] > 0],
                 key=lambda f: -f["ev"])
    lot = sorted([f for f in filas
                  if BANDA_LOTERIA[0] <= f["price"] <= BANDA_LOTERIA[1]
                  and f["vol"] >= MIN_VOL and f["ev"] > 0],
                 key=lambda f: -f["ev"])

    L = [f"🎯 OPCIONES PARA EL {FECHA}", ""]
    L.append("Calculado con sigma y bias YA CALIBRADOS (antes de hoy la calibración")
    L.append("nunca había corrido: calibration.json estaba vacío).")
    L.append("")
    L.append("── APOSTABLE CON TAMAÑO ($0.20-$0.40) ──")
    L.append("La única banda rentable y escalable en las wallets: 37% acierto, +30% ROI.")
    if esc:
        for f in esc[:6]:
            L.append(f"• {f['city']} {etiqueta(f)} @ ${f['price']:.3f}")
            L.append(f"    prob {f['p']*100:.0f}% | EV {f['ev']*100:+.0f}% | Kelly {f['kelly']*100:.1f}% | vol ${f['vol']:,.0f}")
            L.append(f"    ECMWF {f['ecmwf']} / regional {f['reg']} → usado {f['best']}{f['unit']} (σ {f['sigma']})")
    else:
        L.append("• Ninguna con EV positivo. No forzar: es señal de que el mercado")
        L.append("  está bien cotizado hoy en la banda que sabemos operar.")
    L.append("")
    L.append("── LOTERÍA (ticket chico, $2-5 máx) ──")
    L.append("+200% ROI histórico pero sobre ~$3 por trade. NO admite tamaño:")
    L.append("el book no existe. Es la trampa que infló nuestro papel un +61% falso.")
    if lot:
        for f in lot[:5]:
            L.append(f"• {f['city']} {etiqueta(f)} @ ${f['price']:.4f} — prob {f['p']*100:.0f}%, EV {f['ev']*100:+.0f}%")
    else:
        L.append("• Ninguna con EV positivo hoy.")
    L.append("")
    L.append(f"⛔ Evitar la banda $0.06-$0.10 entera (−36% ROI histórico).")
    L.append("")
    L.append("── WALLETS VIGILADAS ──")
    L.append("Multicolor (+25% ROI/$48.9K, la más escalable) está cargada en Europa")
    L.append("para el 29: París $960, Milán $930, Múnich $796, Londres $533.")
    L.append("Husky (+41% ROI) juega tickets chicos en Asia/Sao Paulo.")
    L.append("Wallet China pierde (−5%): no seguirla.")

    texto = "\n".join(L)
    print(texto)
    B.send_telegram(texto)
    print("\n[TELEGRAM] enviado.")


if __name__ == "__main__":
    main()
