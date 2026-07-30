#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
husky_scheduled_run.py — Corre el snapshot en vivo + el digest consolidado y lo
manda a Telegram, uno detras del otro. Pensado para una Tarea Programada de Windows
(ver setup_husky_schedule.bat / registrar_tarea_husky.ps1), 2 veces al dia — pedido
del usuario 2026-07-29 para reemplazar los reportes sueltos de los otros bots por
UN solo mensaje consolidado.

No actualiza la pagina web (PASSPORT) — eso requiere republicar via el tool Artifact,
que solo se puede llamar desde una sesion de Claude activa, no desde una tarea de
Windows corriendo sola. Este script solo cubre la parte de Telegram.
"""
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "data" / "husky_scheduled_run.log"


def log(msg):
    linea = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(linea)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(linea + "\n")


def correr(args):
    r = subprocess.run([sys.executable, *args], cwd=str(ROOT),
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout, r.stderr


def main():
    log("=== inicio corrida programada ===")

    rc, out, err = correr(["husky_live_snapshot.py"])
    log(f"husky_live_snapshot.py -> rc={rc}")
    if rc != 0:
        log(f"  stderr: {err[-2000:]}")
        log("snapshot fallo, no se manda digest con datos viejos/incompletos")
        return

    rc, out, err = correr(["husky_daily_digest.py"])  # sin "print" -> S envia a Telegram
    log(f"husky_daily_digest.py -> rc={rc}")
    if rc != 0:
        log(f"  stderr: {err[-2000:]}")
    else:
        log("digest enviado")

    # Cierra el circulo pedido 2026-07-29: revisa que predicciones ya registradas
    # (por husky_live_snapshot.py, dentro de su propio main()) tienen mercado resuelto
    # en Polymarket, y anota real vs. predicho — sin esto el track record nunca se
    # actualizaba solo, habia que acordarse de correrlo a mano.
    rc, out, err = correr(["husky_check_resolutions.py"])
    log(f"husky_check_resolutions.py -> rc={rc}")
    if rc != 0:
        log(f"  stderr: {err[-2000:]}")
    else:
        log(out.strip().splitlines()[-1] if out.strip() else "sin salida")

    log("=== fin corrida programada ===\n")


if __name__ == "__main__":
    main()
