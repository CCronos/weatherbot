#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
supervisor.py — Vigila los procesos que deben quedar corriendo siempre en este
proyecto, y reinicia el que encuentre caido (crash, cierre de consola, reinicio de
Windows, lo que sea), avisando por Telegram cada vez que reinicia algo. Agregado
2026-07-24 tras la revision de arquitectura: antes de esto, un proceso caido podia
quedar sin nadie enterarse — con dinero real eso es plata parada o sin vigilancia.

Limitacion conocida: este script no tiene quien lo vigile a el. Si arranca solo al
prender la maquina via la Tarea Programada de Windows "WeatherbotSupervisor"
(-> start_supervisor.bat), asi que lo que este en PROCESSES revive sin intervencion:
por eso la lista tiene que reflejar SOLO operaciones vivas.
"""

import sys
import json
import time
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

import psutil
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SUPERVISOR_INTERVAL = 180  # 3 min
DIGEST_STATE_FILE = Path("data/supervisor_digest_state.json")
DIGEST_EVERY_HOURS = 23  # un poco menos de 24h para tolerar drift del loop de 3 min

with open("config.json", encoding="utf-8") as f:
    _cfg = json.load(f)
TELEGRAM_TOKEN   = _cfg.get("telegram_token", "")
TELEGRAM_CHAT_ID = _cfg.get("telegram_chat_bot_principal") or _cfg.get("telegram_chat_id", "")

# (nombre, [args para el script]) — nombre tambien se usa para los archivos de log
# en data/{nombre}.log / data/{nombre}.err.log
#
# 2026-07-28: la lista se vacio en la limpieza. Antes tenia 3 bots de dinero real
# apuntando a mercados YA RESUELTOS (Munich y Helsinki del 26-jul): el supervisor los
# reiniciaba en loop contra tokens muertos. Y arrastraba 17 lineas comentadas de bots
# de papel, que en esta laptop (4GB) son el motivo real de que se ponga lenta.
#
# Regla: aca va SOLO lo que tiene que estar vivo AHORA. Un bot de una operacion
# terminada se saca el mismo dia — no se comenta, se saca.
PROCESSES = [
    ("live_trade_city",         ["live_trade_city.py", "run"]),
]


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=(5, 15))
    except Exception as e:
        print(f"  [WARN] Telegram: {e}")


def esta_corriendo(args):
    """True si algun proceso vivo tiene el script (primer elemento de args) como
    argumento EXACTO en su cmdline (comparando nombre de archivo, no substring) -
    con substring alcanzaba con que otro proceso (una consola corriendo un comando
    que solo MENCIONA el nombre del archivo en un texto largo, por ejemplo un
    comando de diagnostico) hiciera falso positivo. No exige que coincidan los args
    extra (ej. "run" de wallet_watch) - alcanza con confirmar que el script se esta
    ejecutando de alguna forma."""
    script = args[0]
    for p in psutil.process_iter(["cmdline"]):
        try:
            cmdline = p.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if any(Path(part).name == script for part in cmdline):
            return True
    return False


# Un proceso que arranca y se muere enseguida (config incompleta, token invalido, falta
# una dependencia) haria que el supervisor lo relance cada 3 min PARA SIEMPRE, con un
# aviso de Telegram cada vez. Tras MAX_REINTENTOS seguidos se da por vencido, avisa una
# sola vez y lo deja quieto hasta que alguien arregle la causa y reinicie el supervisor.
MAX_REINTENTOS = 5
_reintentos = {}
_rendidos = set()


def reiniciar(nombre, args):
    log_dir = Path("data")
    log_dir.mkdir(exist_ok=True)
    out_path = log_dir / f"{nombre}.log"
    err_path = log_dir / f"{nombre}.err.log"
    with open(out_path, "a", encoding="utf-8") as out, open(err_path, "a", encoding="utf-8") as err:
        out.write(f"\n=== supervisor reinicio {nombre} — {datetime.now(timezone.utc).isoformat()} ===\n")
        out.flush()
        subprocess.Popen(
            [sys.executable, "-u", *args],
            stdout=out, stderr=err,
            cwd=str(Path(__file__).resolve().parent),
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    print(f"  [REINICIADO] {nombre}")
    send_telegram(f"⚠️ Supervisor reinicio {nombre} — no estaba corriendo.")


def revisar_una_vez(reiniciar_caidos=True):
    arriba, caidos = [], []
    for nombre, args in PROCESSES:
        if esta_corriendo(args):
            arriba.append(nombre)
            _reintentos[nombre] = 0        # sobrevivio un ciclo entero: cuenta limpia
            _rendidos.discard(nombre)
            continue

        caidos.append(nombre)
        if not reiniciar_caidos or nombre in _rendidos:
            continue

        _reintentos[nombre] = _reintentos.get(nombre, 0) + 1
        if _reintentos[nombre] > MAX_REINTENTOS:
            _rendidos.add(nombre)
            msg = (f"🛑 Supervisor: {nombre} se murio {MAX_REINTENTOS} veces seguidas al arrancar. "
                   f"Dejo de reintentar — revisar data/{nombre}.err.log. "
                   f"No se reintenta hasta reiniciar el supervisor.")
            print(f"  [RENDIDO] {nombre}")
            send_telegram(msg)
            continue

        reiniciar(nombre, args)
    return arriba, caidos


def load_digest_state():
    if not DIGEST_STATE_FILE.exists():
        return {"ultimo_envio": None, "log_sizes": {}}
    try:
        return json.loads(DIGEST_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"ultimo_envio": None, "log_sizes": {}}


def save_digest_state(state):
    DIGEST_STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = DIGEST_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(DIGEST_STATE_FILE)


def debe_mandar_digest(digest_state):
    ultimo = digest_state.get("ultimo_envio")
    if not ultimo:
        return True
    try:
        dt = datetime.fromisoformat(ultimo)
    except Exception:
        return True
    return (datetime.now(timezone.utc) - dt) >= timedelta(hours=DIGEST_EVERY_HOURS)


def nuevas_lineas_de_error(log_sizes):
    """Compara el tamano de cada *.err.log contra la ultima vez que se reviso — si
    crecio, hay contenido nuevo que nadie vio todavia (un [WARN] que no llego a
    disparar ningun otro aviso, por ejemplo). La primera vez que se ve un archivo
    no cuenta como "nuevo" (evita un falso positivo con todo el historial viejo la
    primera vez que corre el digest)."""
    nuevos_sizes = {}
    hallazgos = []
    for f in sorted(Path("data").glob("*.err.log")):
        size = f.stat().st_size
        prev = log_sizes.get(f.name, size)
        nuevos_sizes[f.name] = size
        if size > prev:
            ultima_linea = ""
            try:
                with open(f, "rb") as fh:
                    fh.seek(prev)
                    nuevo_texto = fh.read().decode("utf-8", errors="replace").strip()
                if nuevo_texto:
                    ultima_linea = nuevo_texto.splitlines()[-1][:120]
            except Exception:
                pass
            hallazgos.append(f"  {f.name}: +{size - prev} bytes" + (f" (ultima linea: {ultima_linea})" if ultima_linea else ""))
    return hallazgos, nuevos_sizes


def generar_resumen_diario():
    """Resumen de "todo sigue bien" (o no) — pensado para leerse en 10 segundos.
    Solo reporta, no toca codigo ni parametros de riesgo (eso sigue siendo decision
    del usuario)."""
    arriba, caidos = revisar_una_vez(reiniciar_caidos=False)
    lineas = [f"🗓️ SUPERVISOR — resumen diario ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})", ""]
    lineas.append(f"Procesos: {len(arriba)}/{len(PROCESSES)} arriba" +
                  (f" — CAIDOS AHORA: {', '.join(caidos)}" if caidos else ""))

    for nombre, path in (("Bot Principal", Path("data/state.json")), ("Favoritos", Path("data/favoritos_state.json"))):
        if not path.exists():
            continue
        try:
            s = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        bal = s.get("balance")
        start = s.get("starting_balance") or s.get("starting_capital")
        pct = (bal / start - 1) * 100 if (bal is not None and start) else None
        estado = "🚨 PAUSADO (freno de emergencia)" if s.get("pausado") else "OK"
        bal_txt = f"${bal:,.2f}" if bal is not None else "s/d"
        lineas.append(f"{nombre}: {bal_txt}" + (f" ({pct:+.1f}%)" if pct is not None else "") + f" — {estado}")

    digest_state = load_digest_state()
    hallazgos, nuevos_sizes = nuevas_lineas_de_error(digest_state.get("log_sizes", {}))
    lineas.append("")
    if hallazgos:
        lineas.append("Errores nuevos en logs desde el ultimo resumen:")
        lineas.extend(hallazgos)
    else:
        lineas.append("Sin errores nuevos en los logs desde el ultimo resumen.")

    digest_state["log_sizes"] = nuevos_sizes
    digest_state["ultimo_envio"] = datetime.now(timezone.utc).isoformat()
    save_digest_state(digest_state)

    return "\n".join(lineas)


def main():
    print("SUPERVISOR — iniciado")
    print(f"Vigilando {len(PROCESSES)} procesos, revisa cada {SUPERVISOR_INTERVAL//60} min")
    print("Ctrl+C para detener\n")
    while True:
        try:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            arriba, caidos = revisar_una_vez()
            print(f"[{now_str}] arriba: {len(arriba)}/{len(PROCESSES)}" +
                  (f" | reiniciados: {', '.join(caidos)}" if caidos else ""))

            if debe_mandar_digest(load_digest_state()):
                resumen = generar_resumen_diario()
                send_telegram(resumen)
                print("  [DIGEST] resumen diario enviado")
        except Exception as e:
            print(f"  [ERROR] {e}")
        time.sleep(SUPERVISOR_INTERVAL)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        main()
    elif cmd == "status":
        arriba, caidos = revisar_una_vez(reiniciar_caidos=False)
        print(f"Arriba ({len(arriba)}): {', '.join(arriba) or '-'}")
        print(f"Caidos ({len(caidos)}): {', '.join(caidos) or '-'}")
    elif cmd == "digest":
        resumen = generar_resumen_diario()
        print(resumen)
        send_telegram(resumen)
        print("\n[TELEGRAM] Resumen enviado.")
    else:
        print("Usage: python supervisor.py [run|status|digest]")
