#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
live_trade_common.py — Piezas compartidas de trading REAL entre los distintos
live_trade_{ciudad}.py, para que un bug de seguridad arreglado una vez quede
arreglado para siempre en todos los mercados nuevos, en vez de tener que
acordarse de copiarlo a mano cada vez (asi se colo originalmente el bug de venta
sin piso de precio, 2026-07-25 - ver safe_sell_all).

Es la UNICA excepcion a la regla del proyecto de "cada bot standalone, sin
modulos compartidos" (ver README/CLAUDE.md) — se justifica porque ejecuta ordenes
reales con dinero real, y la consistencia de seguridad pesa mas que la
legibilidad aislada en este caso puntual. El resto de la logica (entrada,
pronostico, condiciones de salida especificas de cada ciudad) sigue viviendo en
cada script por separado.
"""

import requests


def get_book(token_id):
    """Devuelve (asks, bids) ordenados. ([], []) si la API falla — un corte de red no
    debe tumbar el ciclo de un bot que tiene dinero real abierto: se salta el ciclo y
    se reintenta, que es mucho mas seguro que propagar la excepcion hacia arriba."""
    try:
        r = requests.get("https://clob.polymarket.com/book", params={"token_id": token_id}, timeout=(5, 10))
        d = r.json()
    except Exception as e:
        print(f"  [WARN] book {token_id[:12]}...: {e}")
        return [], []
    asks = sorted(d.get("asks", []), key=lambda x: float(x["price"]))
    bids = sorted(d.get("bids", []), key=lambda x: -float(x["price"]))
    return asks, bids


def best_bid(token_id):
    _, bids = get_book(token_id)
    return float(bids[0]["price"]) if bids else None


def book_depth_at_or_above(token_id, floor_price):
    """Cuantas shares se pueden vender DE VERDAD sin bajar de floor_price, sumando los
    niveles del book. Sirve para no pedir una venta que el book no aguanta (el bug de
    Ankara del 2026-07-25: se mando la posicion entera y barrio el book hasta $0.0687
    con el bid en $0.258)."""
    _, bids = get_book(token_id)
    return sum(float(b["size"]) for b in bids if float(b["price"]) >= floor_price)


SELL_SLIPPAGE_TOLERANCE = 0.15  # nunca vender mas de 15% por debajo del bid visible al momento de decidir


# ---------------------------------------------------------------------------
# Salida escalonada "para todo" — definida por el usuario 2026-07-25, aplica a
# TODAS las posiciones reales (presentes y futuras), una por bucket/token:
#   - Stop-loss: -50% desde el precio PROMEDIO de entrada -> vende TODO lo que
#     quede de esa posicion.
#   - TP tramo 1: +200% sobre la entrada -> vende 30% del tamaño ORIGINAL
#     comprado (no del remanente, para que el tramo sea siempre el mismo tamaño
#     sin importar si TP1 ya se disparo antes).
#   - TP tramo 2: +400% -> vende otro 30% del tamaño original.
#   - El 40% restante NO tiene techo ni piso propio — corre libre, sujeto solo
#     al stop-loss de arriba (confirmado explicitamente con el usuario, no es
#     un trailing-stop como en Ankara).
# ---------------------------------------------------------------------------
STOP_LOSS_PCT = -0.50
TP1_GAIN_PCT = 2.00
TP1_FRACTION = 0.30
TP2_GAIN_PCT = 4.00
TP2_FRACTION = 0.30

# Ninguna venta (ni el stop-loss, ni un tramo de TP) pide de una vez mas que este
# % de lo que falta por vender — aunque sell_partial ya tenga piso de precio, el
# usuario pidio explicitamente (2026-07-25) no vaciar la posicion en un solo tiro
# para no arriesgarse a que el book no tenga profundidad: se trocea en varios
# ciclos, igual que el "goteo" del lado de compra.
SELL_CHUNK_FRACTION = 0.40
SELL_CHUNK_MIN_SHARES = 5.0  # por debajo de esto no vale la pena trocear mas, se vende de una


def _chunk_size(shares_target):
    if shares_target <= SELL_CHUNK_MIN_SHARES:
        return shares_target
    return max(shares_target * SELL_CHUNK_FRACTION, SELL_CHUNK_MIN_SHARES)


def fresh_posicion():
    return {
        "bought_shares": 0.0, "bought_cost": 0.0,
        "shares": 0.0, "total_cost": 0.0, "avg_price": 0.0,
        "tp1_target": None, "tp1_sold": 0.0, "tp1_done": False,
        "tp2_target": None, "tp2_sold": 0.0, "tp2_done": False,
        "stop_triggered": False, "closed": False, "realized_pnl": 0.0,
        "fallos_venta_consecutivos": 0,
    }


def refresh_posiciones(state, key_fn):
    """Reconstruye/actualiza state['posiciones'] (dict por bucket/clave) a partir de
    state['niveles'] — cada nivel solo ACUMULA shares_filled/cost_filled (nunca
    baja) via check_drip_fill, asi que comparar contra el ultimo 'bought_shares'
    visto detecta compras nuevas sin perder el rastro de lo ya vendido por
    check_tiered_exit. key_fn(nivel) -> clave de bucket, ej. `lambda n: n['bucket']`
    o `lambda n: 'main'` en scripts sin buckets (un solo mercado)."""
    por_bucket = {}
    for n in state["niveles"]:
        b = str(key_fn(n))
        agg = por_bucket.setdefault(b, {"shares": 0.0, "cost": 0.0})
        agg["shares"] += n["shares_filled"]
        agg["cost"] += n["cost_filled"]

    posiciones = state.setdefault("posiciones", {})
    for b, agg in por_bucket.items():
        p = posiciones.setdefault(b, fresh_posicion())
        for k, v in fresh_posicion().items():  # migra esquemas viejos sin pisar progreso ya guardado
            p.setdefault(k, v)
        if agg["shares"] > p["bought_shares"] + 1e-9:
            delta_shares = agg["shares"] - p["bought_shares"]
            delta_cost = agg["cost"] - p["bought_cost"]
            p["bought_shares"] = round(agg["shares"], 6)
            p["bought_cost"] = round(agg["cost"], 6)
            p["shares"] = round(p["shares"] + delta_shares, 6)
            p["total_cost"] = round(p["total_cost"] + delta_cost, 6)
            p["avg_price"] = round(p["bought_cost"] / p["bought_shares"], 6) if p["bought_shares"] else 0.0
    return posiciones


# Cada cuantos fallos de venta SEGUIDOS se manda un aviso por Telegram (y se repite
# cada tantos fallos mas, no una sola vez) — un stop-loss o TP que no logra vender
# deja la posicion abierta y expuesta; a diferencia de una compra fallida (donde lo
# peor que pasa es perder una oportunidad), acá el riesgo es que nadie se entere de
# que la salida esta trabada mientras el precio se sigue moviendo en contra.
FALLOS_VENTA_ALERTA_CADA = 3


def sell_partial(client, token_id, shares_to_sell, pos, reason, log, send_telegram,
                  slippage_tolerance=SELL_SLIPPAGE_TOLERANCE):
    """Como safe_sell_all pero para una CANTIDAD especifica (no toda la posicion) —
    usado por los tramos de take-profit escalonado. Mismo piso de precio (nunca
    vender mas de `slippage_tolerance` por debajo del bid visible). Si el book no
    aguanta todo dentro del margen, vende lo que pueda; el resto queda pendiente,
    el llamador reintenta en el proximo ciclo porque tp1_done/tp2_done solo se
    marca True si algo se vendio de verdad."""

    def _fallo(motivo):
        # Antes esto solo se logueaba — encontrado 2026-07-29 en auditoria: una venta
        # (stop-loss o TP) que falla repetidamente no tenia NINGUN aviso escalado, a
        # diferencia de otros fallos del proyecto (ver PLACE_DRIP_MAX_FALLOS en
        # live_trade_city.py, agregado el mismo dia por el mismo motivo del lado compra).
        pos["fallos_venta_consecutivos"] = pos.get("fallos_venta_consecutivos", 0) + 1
        n = pos["fallos_venta_consecutivos"]
        log(f"  [ERROR] {reason}: {motivo} (fallo de venta #{n})")
        if n % FALLOS_VENTA_ALERTA_CADA == 0:
            send_telegram(
                f"🚨 {reason}: {n} intentos de venta seguidos fallaron ({motivo}) — "
                f"la posicion sigue ABIERTA y expuesta, revisar manualmente si persiste."
            )
        return 0.0, 0.0

    shares_to_sell = min(shares_to_sell, pos["shares"])
    if shares_to_sell <= 0.01:
        return 0.0, 0.0
    asks_bids = get_book(token_id)
    bids = asks_bids[1]
    if not bids:
        return _fallo("sin bids en el book")
    bid_ahora = float(bids[0]["price"])
    floor_price = round(max(0.01, bid_ahora * (1 - slippage_tolerance)), 2)

    # No pedir mas de lo que el book aguanta por encima del piso. El piso solo evita
    # que se EJECUTE por debajo; sin este recorte se sigue mandando una orden mas
    # grande que la liquidez real y el resto se cancela (FAK), lo que en la practica
    # deja la salida a medias sin que quede claro por que. Mejor pedir exactamente lo
    # que hay y volver el proximo ciclo por el resto.
    profundidad = sum(float(b["size"]) for b in bids if float(b["price"]) >= floor_price)
    if profundidad < shares_to_sell:
        log(f"  [book] solo hay {profundidad:.2f} shares de demanda >= ${floor_price} "
            f"(se querian vender {shares_to_sell:.2f}) — se vende eso y se reintenta")
        shares_to_sell = profundidad
    if shares_to_sell <= 0.01:
        return 0.0, 0.0
    try:
        resp = client.place_market_order(
            token_id=token_id, side="SELL", shares=round(shares_to_sell, 2),
            min_price=floor_price, order_type="FAK",
        )
        ok = getattr(resp, "ok", False)
    except Exception as e:
        return _fallo(f"excepcion: {e}")
    if not ok:
        return _fallo(f"rechazada: {getattr(resp, 'code', '')} {getattr(resp, 'message', '')}")

    sold = float(resp.making_amount) if resp.making_amount else 0.0
    proceeds = float(resp.taking_amount) if resp.taking_amount else 0.0
    if sold <= 0:
        return _fallo("orden aceptada pero no se vendio nada")

    pos["fallos_venta_consecutivos"] = 0
    cost_vendido = pos["total_cost"] * (sold / pos["shares"]) if pos["shares"] > 0 else 0.0
    pos["shares"] = round(pos["shares"] - sold, 6)
    pos["total_cost"] = round(pos["total_cost"] - cost_vendido, 6)
    pos["realized_pnl"] = round(pos.get("realized_pnl", 0.0) + (proceeds - cost_vendido), 6)
    avg_sell = proceeds / sold
    log(f"  {reason}: vendidas {sold:.2f}/{shares_to_sell:.2f} shares @ ${avg_sell:.4f} (piso ${floor_price})")
    send_telegram(f"💰 {reason}\nVendidas {sold:.2f} shares @ ${avg_sell:.4f} — proceeds ${proceeds:.2f}")
    return sold, proceeds


def check_tiered_exit(client, token_id, pos, log, send_telegram, label=""):
    """Aplica la regla de salida escalonada a UNA posicion (un bucket/token). Ver
    las constantes STOP_LOSS_PCT/TP1_.../TP2_... arriba. Cada venta (stop-loss o
    un tramo de TP) se trocea via _chunk_size — nunca se pide vender de una sola
    vez todo lo que falta, se reparte en varios ciclos (ver SELL_CHUNK_FRACTION).
    Modifica `pos` in-place. Devuelve True si esta posicion quedo totalmente
    cerrada este ciclo (para que el llamador pueda cancelar/parar cualquier
    compra pendiente de ese bucket)."""
    if pos.get("closed") or pos["shares"] <= 0.01:
        return pos.get("closed", False)

    bid = best_bid(token_id)
    if bid is None:
        return False

    entry = pos["avg_price"]
    if entry <= 0:
        return False
    gain_pct = (bid - entry) / entry

    # Una vez que el stop-loss se activa una vez, queda marcado y se sigue
    # troceando la salida cada ciclo hasta vaciar la posicion, aunque el precio
    # rebote momentaneamente por encima del umbral (evita quedar indeciso).
    if gain_pct <= STOP_LOSS_PCT:
        pos["stop_triggered"] = True
    if pos.get("stop_triggered"):
        chunk = _chunk_size(pos["shares"])
        sell_partial(client, token_id, chunk, pos, f"STOP-LOSS -50% ({label})", log, send_telegram)
        if pos["shares"] <= 0.5:
            pos["closed"] = True
            log(f"  [{label}] posición cerrada por stop-loss")
        return pos["closed"]

    if not pos.get("tp1_done") and gain_pct >= TP1_GAIN_PCT:
        if pos.get("tp1_target") is None:
            pos["tp1_target"] = round(pos["bought_shares"] * TP1_FRACTION, 6)
        falta = pos["tp1_target"] - pos["tp1_sold"]
        if falta > 0.01:
            chunk = _chunk_size(falta)
            sold, _ = sell_partial(client, token_id, chunk, pos, f"TP1 +200% ({label})", log, send_telegram)
            pos["tp1_sold"] = round(pos["tp1_sold"] + sold, 6)
        if pos["tp1_sold"] >= pos["tp1_target"] - 0.01:
            pos["tp1_done"] = True
            log(f"  [{label}] TP1 completo — {pos['tp1_sold']:.2f} shares vendidas (30% del original)")

    # `if`, no `elif`: si el precio pega un salto que se pasa de +400% de una, TP2 tiene
    # que poder arrancar en el MISMO ciclo en que TP1 termino, no esperar 4 minutos mas
    # a que el precio siga ahi. Sigue siendo secuencial (exige tp1_done), solo que ya no
    # pierde un ciclo por cada tramo.
    if pos.get("tp1_done") and not pos.get("tp2_done") and gain_pct >= TP2_GAIN_PCT:
        if pos.get("tp2_target") is None:
            pos["tp2_target"] = round(pos["bought_shares"] * TP2_FRACTION, 6)
        falta = pos["tp2_target"] - pos["tp2_sold"]
        if falta > 0.01:
            chunk = _chunk_size(falta)
            sold, _ = sell_partial(client, token_id, chunk, pos, f"TP2 +400% ({label})", log, send_telegram)
            pos["tp2_sold"] = round(pos["tp2_sold"] + sold, 6)
        if pos["tp2_sold"] >= pos["tp2_target"] - 0.01:
            pos["tp2_done"] = True
            log(f"  [{label}] TP2 completo — {pos['tp2_sold']:.2f} shares vendidas (otro 30% del original)")

    return False
