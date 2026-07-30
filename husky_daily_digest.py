#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
husky_daily_digest.py — UN solo mensaje de Telegram con las mejores opciones del
momento, sacadas de data/live_peak_snapshot.json (el mismo dato que alimenta la web
PASSPORT) — reemplaza los reportes sueltos de bot_v2/favoritos/early_entry por un
digest unico, pedido por el usuario 2026-07-29 para no recibir tantos mensajes
separados.

No corre nada solo — se invoca despues de husky_live_snapshot.py (o se encadenan
ambos en una tarea programada, ver nota de horario mas abajo).

Filtro de "mejor opcion": banda de precio $0.06-$0.75 (excluye la trampa de EV
inflado en contratos de centavos, ver picks_29jul.py / el hallazgo de favoritos_bot),
volumen >= MIN_VOLUME (ya filtrado en husky_live_snapshot), EV > 0.10, ordenado por
Kelly (no por EV, para no promover tamanos irreales).

Uso:
    python husky_daily_digest.py         # arma y envia el digest
    python husky_daily_digest.py print   # solo imprime, no envia (para probar)
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timezone

import bot_v2 as B

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SNAPSHOT_FILE = Path("data/live_peak_snapshot.json")
MIN_EV_DIGEST = 0.10
SPREAD_ALERTA = 3.0  # grados de diferencia modelo vs empirico a partir de los cuales
                      # se marca como poco confiable (encontrado 2026-07-29: Sao Paulo
                      # tenia +501% EV con modelo=30.0 vs empirico=19.2, ±10.8 de spread,
                      # y el digest lo mostraba igual que una senal solida sin avisar)
ORDEN_CONFIANZA = {"escalable": 0, "moderado": 1, "cautela": 2}
CONFIANZA_LABEL = {"escalable": "✅ escalable", "moderado": "🟡 moderado", "cautela": "🔶 cautela (precio bajo)"}


def cargar_snapshot():
    if not SNAPSHOT_FILE.exists():
        return None
    return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))


def armar_digest():
    snap = cargar_snapshot()
    if not snap:
        return "🎯 PASSPORT — sin snapshot todavia. Corre husky_live_snapshot.py primero."

    gen = datetime.fromisoformat(snap["generated_at"])
    edad_min = round((datetime.now(timezone.utc) - gen).total_seconds() / 60)

    candidatas = []
    for c in snap["cities"]:
        mb = c.get("mejor_bucket")
        if not mb or mb["ev"] < MIN_EV_DIGEST:
            continue
        candidatas.append((c, mb))
    # Prioriza la banda de confianza (escalable > moderado > cautela) antes que el EV
    # crudo — mismo criterio que la seleccion de mejor_bucket en husky_live_snapshot.py,
    # asi el digest y la web nunca se contradicen sobre cual es "la mejor" opcion.
    candidatas.sort(key=lambda x: (ORDEN_CONFIANZA.get(x[1].get("confianza"), 9), -x[1]["kelly"]))

    lineas = [
        f"🎯 PASSPORT — mejores opciones ({edad_min} min de antigüedad)",
        f"Filtro: EV ≥ {MIN_EV_DIGEST*100:.0f}%, volumen ≥ ${B.MIN_VOLUME:.0f}. "
        f"Banda: ✅ escalable ≥$0.20 (la unica con ROI real probado) · 🟡 moderado $0.10-0.20 · 🔶 cautela <$0.10 (no escala en tamaño)",
        "",
    ]
    if not candidatas:
        lineas.append("Ninguna ciudad pasó el filtro esta vez. No forzar posiciones.")
    else:
        for c, mb in candidatas[:8]:
            etiqueta = CONFIANZA_LABEL.get(mb.get("confianza"), "")
            lineas.append(
                f"• {c['name']} {mb['bucket']} @ ${mb['price']:.3f} | {etiqueta}\n"
                f"  prob {mb['prob']*100:.0f}% | EV {mb['ev']*100:+.0f}% | Kelly {mb['kelly']*100:.1f}% | "
                f"vol ${mb['volume']:,.0f}"
            )
            spread = c.get("spread_modelo_vs_empirico")
            if spread is not None and spread >= SPREAD_ALERTA:
                lineas.append(f"   ⚠ modelo y empírico difieren ±{spread:.1f}° — baja confianza, revisar antes de entrar")
            if c.get("viento_nota"):
                lineas.append(f"   ↳ {c['viento_nota']}")
        if len(candidatas) > 8:
            lineas.append(f"... y {len(candidatas) - 8} más — ver la web para el resto.")

    lineas.append("")
    lineas.append("Detalle completo, gráficos e historial: ver PASSPORT (link de siempre).")
    return "\n".join(lineas)


def main():
    texto = armar_digest()
    print(texto)
    if len(sys.argv) > 1 and sys.argv[1] == "print":
        return
    B.send_telegram(texto)
    print("\n[TELEGRAM] digest enviado.")


if __name__ == "__main__":
    main()
