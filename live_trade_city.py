#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
live_trade_city.py — Bot de trading REAL para un mercado de temperatura de Polymarket.

Generico y parametrizado: toda la configuracion de la operacion vive en el bloque
CONFIG de aca abajo (ciudad, fecha, tokens por bucket, plan de precios/presupuesto).
Reemplaza a los viejos live_trade_{ciudad}{dia}.py, que eran copias con los tokens
hardcodeados y se volvian basura en cuanto el mercado resolvia — el 2026-07-28
quedaban tres corriendo contra mercados ya resueltos.

Como opera:
- COMPRA por "goteo": ordenes limite CHICAS (DRIP_USD) que se reponen solas a medida
  que se llenan, en vez de mandar el tamano completo de una. Pedido explicito del
  usuario 2026-07-26: estos mercados no tienen volumen y una orden grande delata
  tamano, otros bots se adelantan y mueven el precio.
- VENDE con la regla escalonada compartida (live_trade_common.check_tiered_exit):
  stop-loss -50% desde el precio promedio de entrada vende todo; TP1 +200% vende 30%
  del tamano original; TP2 +400% vende otro 30%; el 40% restante corre libre sujeto
  solo al stop-loss. Se aplica POR BUCKET (cada bucket es una posicion independiente
  con su propio precio de entrada).
- Toda venta pasa por piso de precio + recorte a la profundidad real del book
  (ver live_trade_common.sell_partial) — el bug de Ankara del 2026-07-25, donde una
  venta sin piso barrio el book y convirtio +164% en perdida real.

Uso:
    python live_trade_city.py plan     # muestra el plan sin tocar nada (empezar SIEMPRE por aca)
    python live_trade_city.py status   # estado actual de la posicion
    python live_trade_city.py once     # un solo ciclo
    python live_trade_city.py run      # loop permanente (lo que levanta supervisor.py)
"""

import sys
import json
import time
import requests
from pathlib import Path
from datetime import datetime, timezone

from live_trade_common import refresh_posiciones, check_tiered_exit

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# =============================================================================
# CONFIG — lo unico que se toca al abrir una operacion nueva
# =============================================================================

CIUDAD = ""            # ej. "Ankara" — solo para los mensajes de Telegram/log
FECHA_MERCADO = ""     # ej. "2026-07-29" — fecha que resuelve el mercado
UNIDAD = "C"           # "C" o "F", solo para las etiquetas

# bucket (numero de grados) -> token_id del outcome YES de ese bucket.
# Se sacan del mercado en Polymarket; verificar SIEMPRE contra la pregunta real
# ("Will the highest temperature in X be NN°C on ...") antes de poner dinero.
TOKENS = {}

# (bucket, precio_limite, presupuesto_usd_de_ese_nivel)
PLAN = []

# Poner en True SOLO si el usuario pide explicitamente correr sin la salida
# escalonada de live_trade_common (sin stop-loss, sin TP1/TP2) — usado por la
# operacion de Istanbul 25C del 2026-07-29/30 (cerrada, ver data/archive_real/).
# Default False: cualquier operacion nueva usa la salida escalonada compartida.
SIN_SALIDA_AUTOMATICA = False

DRIP_USD = 2.50        # tamano de cada orden chica del goteo
CHECK_INTERVAL = 240   # 4 min — mercados sin mucho volumen, no hace falta mas rapido

# =============================================================================

SLUG = f"{CIUDAD.lower().replace(' ', '_')}_{FECHA_MERCADO}" if CIUDAD else "sin_configurar"
STATE_FILE = Path(f"data/live_trade_{SLUG}_state.json")
LOCK_FILE = Path(f"data/live_trade_{SLUG}.lock")
SECRETS_FILE = Path("secrets_trading.json")
CONFIG_FILE = Path("config.json")

_CFG = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
TELEGRAM_TOKEN = _CFG.get("telegram_token", "")
TELEGRAM_CHAT_ID = _CFG.get("telegram_chat_bot_principal") or _CFG.get("telegram_chat_id", "")


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] {msg}")


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log(f"[Telegram desactivado] {text}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=(5, 15))
    except Exception as e:
        log(f"  [WARN] Telegram: {e}")


def get_client():
    # Import perezoso: `plan` y `status` no necesitan la wallet, y asi se pueden correr
    # sin tener el SDK instalado ni desbloquear la private key.
    from polymarket.clients.secure import SecureClient
    secrets = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    return SecureClient.create(private_key=secrets["private_key"])


def validar_config():
    """Se corre antes de cualquier cosa que toque dinero. Un plan a medio configurar
    (tokens vacios, buckets sin token, presupuesto en 0) tiene que fallar aca y no a
    mitad de una orden."""
    problemas = []
    if not CIUDAD or not FECHA_MERCADO:
        problemas.append("CIUDAD/FECHA_MERCADO sin configurar")
    if not TOKENS:
        problemas.append("TOKENS vacio")
    if not PLAN:
        problemas.append("PLAN vacio")
    for bucket, price, usd in PLAN:
        if bucket not in TOKENS:
            problemas.append(f"bucket {bucket}{UNIDAD} esta en PLAN pero no tiene token_id")
        if not (0 < price < 1):
            problemas.append(f"precio {price} fuera de rango (0,1) para el bucket {bucket}")
        if usd <= 0:
            problemas.append(f"presupuesto {usd} invalido para el bucket {bucket}")
    return problemas


def fresh_state():
    niveles = []
    for bucket, price, usd_total in PLAN:
        niveles.append({
            "bucket": bucket, "price": price, "usd_total": round(usd_total, 4),
            "usd_deployed": 0.0, "shares_filled": 0.0, "cost_filled": 0.0,
            "order_id": None, "order_status": "none",   # none | placed | done
            "order_counted_shares": 0.0,                # cuanto de la orden EN CURSO ya se conto
            "fallos_consecutivos": 0,
        })
    return {"ciudad": CIUDAD, "fecha": FECHA_MERCADO, "niveles": niveles, "cycles": 0}


def load_state():
    if not STATE_FILE.exists():
        return fresh_state()
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    for n in state.get("niveles", []):
        n.setdefault("order_counted_shares", 0.0)  # migra states de la version anterior
        n.setdefault("fallos_consecutivos", 0)
    return state


def save_state(state):
    atomic_write(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False))


PLACE_DRIP_MAX_FALLOS = 3  # tras esto, un solo aviso por Telegram y sigue reintentando en silencio


def place_drip(client, nivel):
    remaining = nivel["usd_total"] - nivel["usd_deployed"]
    if remaining < 0.30:  # polvo, ya no vale la pena
        nivel["order_status"] = "done"
        return
    usd = min(DRIP_USD, remaining)
    size = round(usd / nivel["price"], 2)
    if size <= 0:
        nivel["order_status"] = "done"
        return
    try:
        resp = client.place_limit_order(
            token_id=TOKENS[nivel["bucket"]], price=nivel["price"], size=size, side="BUY",
        )
        ok = getattr(resp, "ok", False)
        error_txt = None if ok else f"{getattr(resp, 'code', '')} {getattr(resp, 'message', '')}"
    except Exception as e:
        ok = False
        error_txt = str(e)

    if ok:
        nivel["order_id"] = resp.order_id
        nivel["order_status"] = "placed"
        nivel["order_counted_shares"] = 0.0
        nivel["fallos_consecutivos"] = 0
        log(f"  {nivel['bucket']}{UNIDAD} @ ${nivel['price']}: gota de ${usd:.2f} ({size} shares) -> {resp.order_id}")
        return

    # Antes de esto, un fallo de place_drip (ej. el rechazo por fee estimate que
    # encontramos 2026-07-29 comprando Istanbul a mano) solo se logueaba — el bot podia
    # quedar reintentando la misma gota fallida cada 4 min PARA SIEMPRE sin que nadie se
    # enterara, porque el loop principal solo manda Telegram si run_once() lanza una
    # excepcion, y esta se atajaba adentro sin propagarse. Ahora avisa una vez tras
    # PLACE_DRIP_MAX_FALLOS seguidos (no en cada intento, para no saturar Telegram) y
    # sigue reintentando igual — no marca el nivel como "done" porque un rechazo de
    # balance puede resolverse solo (ej. otra posicion se libera fondos al vender).
    nivel["fallos_consecutivos"] = nivel.get("fallos_consecutivos", 0) + 1
    log(f"  [ERROR] place_drip {nivel['bucket']}{UNIDAD}@${nivel['price']} "
        f"(fallo {nivel['fallos_consecutivos']}): {error_txt}")
    if nivel["fallos_consecutivos"] == PLACE_DRIP_MAX_FALLOS:
        send_telegram(
            f"⚠️ {CIUDAD} {nivel['bucket']}{UNIDAD}@${nivel['price']}: la gota de compra "
            f"fallo {PLACE_DRIP_MAX_FALLOS} veces seguidas — ${nivel['usd_total'] - nivel['usd_deployed']:.2f} "
            f"sin desplegar. Ultimo error: {error_txt}"
        )


def check_drip_fill(client, nivel):
    """Contabiliza lo que se lleno de la orden en curso, INCLUYENDO fills parciales.

    Bug que arregla (encontrado 2026-07-28 revisando live_trade_munich26.py): la version
    anterior solo contaba la orden si se habia llenado al 100% (`matched >= original`).
    Con un fill parcial que se quedaba ahi: las shares nunca entraban a shares_filled, el
    nivel quedaba 'placed' para siempre y place_drip no reponia nunca mas (goteo trabado
    en silencio), y —lo grave— como refresh_posiciones lee shares_filled, esas shares
    REALES quedaban fuera del stop-loss y del take-profit. Dinero comprado de verdad,
    invisible para el sistema de salida.

    Ahora se lleva `order_counted_shares` por orden y se contabiliza el DELTA en cada
    pasada, asi un fill parcial entra al sistema de salida en el mismo ciclo sin riesgo
    de contarlo dos veces.
    """
    try:
        order = client.get_order(order_id=nivel["order_id"])
    except Exception as e:
        log(f"  [WARN] get_order {nivel['bucket']}{UNIDAD}@${nivel['price']}: {e}")
        return

    matched = float(order.size_matched)
    original = float(order.original_size)
    price = float(order.price)

    delta = matched - nivel.get("order_counted_shares", 0.0)
    if delta > 1e-9:
        cost = delta * price
        nivel["usd_deployed"] = round(nivel["usd_deployed"] + cost, 6)
        nivel["shares_filled"] = round(nivel["shares_filled"] + delta, 6)
        nivel["cost_filled"] = round(nivel["cost_filled"] + cost, 6)
        nivel["order_counted_shares"] = matched
        completa = matched >= original - 1e-6
        log(f"  {nivel['bucket']}{UNIDAD} @ ${nivel['price']}: "
            f"{'gota llenada' if completa else 'FILL PARCIAL'} +{delta:.2f} shares "
            f"({matched:.2f}/{original:.2f}) — nivel ${nivel['usd_deployed']:.2f}/${nivel['usd_total']:.2f}")
        send_telegram(
            f"{'✅' if completa else '🟡'} {CIUDAD} {nivel['bucket']}{UNIDAD} — ${nivel['price']} "
            f"{'se llenó' if completa else 'fill PARCIAL'}\n"
            f"+{delta:.2f} shares ({matched:.2f}/{original:.2f}) — "
            f"nivel ${nivel['usd_deployed']:.2f}/${nivel['usd_total']:.2f}"
        )

    # La orden termino (llena, cancelada por el exchange, o cerrada con fill parcial):
    # se libera el nivel para la proxima gota. Lo ya llenado quedo contabilizado arriba
    # en cualquiera de los casos.
    #
    # Bug encontrado 2026-07-29 con dinero real (Istanbul 25C): Polymarket devuelve
    # status="MATCHED" para una orden que ya NO esta resting en el book aunque
    # size_matched < original_size (confirmado con list_open_orders() -> 0 abiertas
    # mientras el nivel seguia en 'placed' aca). O sea "MATCHED" es terminal en su
    # vocabulario incluso con fill parcial, no solo cuando matched>=original. Antes de
    # este fix, el goteo quedaba trabado para siempre en cuanto una gota se llenaba
    # parcial y el exchange la cerraba asi — plata sin desplegar y sin aviso.
    # El SDK define el vocabulario de status como LIVE | MATCHED | DELAYED | UNMATCHED |
    # CANCELED (polymarket/models/clob/user_events.py) — son mutuamente excluyentes para
    # UNA orden entera, no por fill. Solo "LIVE" y "DELAYED" implican que puede seguir
    # llegando mas fill; "UNMATCHED" se suma aca por la misma razon que "MATCHED": si no
    # es LIVE, no esta resting, y dejar el nivel trabado esperando un fill que no va a
    # llegar es el mismo bug de fondo (aunque con 0 shares compradas, sin riesgo de plata).
    estado_orden = (getattr(order, "status", "") or "").upper()
    if matched >= original - 1e-6 or estado_orden in ("MATCHED", "UNMATCHED", "CANCELED", "CANCELLED", "EXPIRED"):
        nivel["order_id"] = None
        nivel["order_status"] = "none"
        nivel["order_counted_shares"] = 0.0


def resumen_posicion(state):
    por_bucket = {}
    for n in state["niveles"]:
        agg = por_bucket.setdefault(n["bucket"], {"shares": 0.0, "cost": 0.0})
        agg["shares"] += n["shares_filled"]
        agg["cost"] += n["cost_filled"]
    for b, agg in por_bucket.items():
        agg["avg_price"] = round(agg["cost"] / agg["shares"], 6) if agg["shares"] else 0.0
    return por_bucket


def run_once():
    state = load_state()
    client = get_client()
    state["cycles"] = state.get("cycles", 0) + 1

    # 1) Salidas primero: si un bucket dispara stop-loss, hay que frenar cualquier compra
    #    pendiente de ESE bucket antes de intentar comprar mas en el mismo ciclo.
    posiciones = refresh_posiciones(state, key_fn=lambda n: n["bucket"])
    stopped_buckets = set()
    if not SIN_SALIDA_AUTOMATICA:
        for bucket_str, pos in posiciones.items():
            bucket = int(bucket_str)
            if bucket not in TOKENS or pos["shares"] <= 0.01:
                continue
            check_tiered_exit(client, TOKENS[bucket], pos, log, send_telegram,
                              label=f"{CIUDAD} {bucket}{UNIDAD}")
            if pos.get("stop_triggered") or pos.get("closed"):
                stopped_buckets.add(bucket)

    for nivel in state["niveles"]:
        if nivel["bucket"] in stopped_buckets and nivel["order_status"] != "done":
            if nivel.get("order_id"):
                try:
                    client.cancel_order(order_id=nivel["order_id"])
                except Exception as e:
                    log(f"  [WARN] cancel_order tras stop-loss {nivel['bucket']}{UNIDAD}: {e}")
            nivel["order_status"] = "done"
            nivel["order_id"] = None

    # 2) Entradas — solo en buckets que no dispararon stop-loss
    for nivel in state["niveles"]:
        if nivel["bucket"] in stopped_buckets or nivel["order_status"] == "done":
            continue
        if nivel["order_status"] == "placed":
            check_drip_fill(client, nivel)
        if nivel["order_status"] == "none":
            place_drip(client, nivel)

    if all(n["order_status"] == "done" for n in state["niveles"]):
        log("Todos los niveles completaron su presupuesto asignado.")

    save_state(state)
    return state


def print_plan():
    problemas = validar_config()
    print("=" * 60)
    print(f"  PLAN — {CIUDAD or '(sin ciudad)'} {FECHA_MERCADO or '(sin fecha)'}")
    print("=" * 60)
    if problemas:
        print("  ⚠️  CONFIG INCOMPLETA — no se puede operar:")
        for p in problemas:
            print(f"     - {p}")
        print("=" * 60)
        return False
    total = 0.0
    for bucket, price, usd in PLAN:
        shares = usd / price
        print(f"  {bucket}{UNIDAD:<2} @ ${price:<8.4f}  ${usd:>7.2f}  -> ~{shares:>10.2f} shares  "
              f"(en gotas de ${DRIP_USD:.2f})")
        total += usd
    print("-" * 60)
    print(f"  TOTAL A ARRIESGAR: ${total:.2f}")
    if SIN_SALIDA_AUTOMATICA:
        print(f"  Salida: NINGUNA — sin stop-loss ni TP, se mantiene hasta resolucion")
    else:
        print(f"  Salida: stop-loss -50% | TP1 +200% (30%) | TP2 +400% (30%) | resto libre")
    print(f"  State: {STATE_FILE}")
    print("=" * 60)
    return True


def acquire_lock():
    import os
    import psutil
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
            if psutil.pid_exists(old_pid):
                log(f"[FATAL] ya hay otra instancia corriendo (PID {old_pid}) — no arranco una segunda.")
                sys.exit(1)
        except (ValueError, OSError):
            pass
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")


def release_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def main():
    problemas = validar_config()
    if problemas:
        log("[FATAL] config incompleta — corre 'python live_trade_city.py plan' para ver que falta.")
        for p in problemas:
            log(f"  - {p}")
        sys.exit(1)

    acquire_lock()
    try:
        salida_txt = "SIN salida automatica (SIN_SALIDA_AUTOMATICA)" if SIN_SALIDA_AUTOMATICA else "salida escalonada activa"
        log(f"live_trade_city — {CIUDAD} {FECHA_MERCADO} — iniciado "
            f"(goteo de ${DRIP_USD:.2f} cada {CHECK_INTERVAL//60} min, {salida_txt})")
        while True:
            try:
                run_once()
            except Exception as e:
                log(f"[ERROR] {e}")
                send_telegram(f"⚠️ live_trade {CIUDAD} error: {e}")
            time.sleep(CHECK_INTERVAL)
    finally:
        release_lock()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if cmd == "run":
        main()
    elif cmd == "once":
        if validar_config():
            print("Config incompleta — corre 'plan' para ver que falta.")
            sys.exit(1)
        print(json.dumps(run_once(), indent=2, ensure_ascii=False))
    elif cmd == "status":
        s = load_state()
        print(json.dumps({"resumen": resumen_posicion(s), "posiciones": s.get("posiciones", {})},
                          indent=2, ensure_ascii=False))
    elif cmd == "plan":
        print_plan()
    else:
        print("Usage: python live_trade_city.py [plan|status|once|run]")
