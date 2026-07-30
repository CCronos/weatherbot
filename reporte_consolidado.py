#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reporte_consolidado.py — Compara el rendimiento (en %, no en dolares, porque cada
iniciativa arranca con capital distinto) de las lineas separadas que corren en
paralelo: Bot Principal, Favoritos, Early Entry, y Picks Internos de Dashboards
(el sistema de picks que corre DENTRO de cada check_*.py — maybe_record_daily_picks/
resolve_pending_picks — separado y sin coordinar con las otras 3 lineas, aunque
puede operar las mismas 6 ciudades el mismo dia; agregado 2026-07-24 tras detectar
que corria invisible, sin entrar en ningun reporte). Wallet watch y el dashboard
visual combinado (dashboard_combinado.py) son solo de observacion/informacion (no
manejan capital propio), asi que no entran en la comparacion de rentabilidad, pero
se listan como referencia.

Corre una vez por semana (o a demanda con "py reporte_consolidado.py") y manda el
resultado a un chat de Telegram separado (telegram_chat_bot_principal por defecto,
o el que se configure) para no mezclarse con los reportes diarios de cada bot.
"""

import sys
import json
import time
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import bot_v2  # reutiliza send_telegram, atomic_write

CHECK_INTERVAL = 7 * 24 * 3600  # una vez por semana
STATE_FILE = Path("data/reporte_consolidado_state.json")

# Minimo de trades resueltos POR CIUDAD antes de considerarla con base estadistica
# solida para dinero real. Con ~35 trades y un win rate real cercano al 30-35%, el
# margen de error de un intervalo de confianza del 95% baja a unos +/-8-9 puntos
# porcentuales - todavia no es mucho, pero ya deja de ser puro ruido como con 5-7
# trades (que hoy dan margenes de error de +/-30+ puntos). Acordado en conversacion
# con el usuario como el minimo para "entrar solo por datos y eliminar sesgos".
TARGET_TRADES_PER_CITY = 35

with open("config.json", encoding="utf-8") as f:
    _cfg = json.load(f)
TELEGRAM_TOKEN   = _cfg.get("telegram_token", "")
TELEGRAM_CHAT_ID = (
    _cfg.get("telegram_chat_bot_principal")
    or _cfg.get("telegram_chat_id", "")
)


def send_telegram(text):
    import requests
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=(3, 5))
    except Exception as e:
        print(f"  [WARN] Telegram: {e}")


def rendimiento_bot_principal():
    """Lee data/state.json (bot_v2.py) - $10,000 iniciales."""
    path = Path("data/state.json")
    if not path.exists():
        return None
    s = json.loads(path.read_text(encoding="utf-8"))
    inicio = s.get("starting_balance", 10000.0)
    actual = s.get("balance", inicio)
    return {
        "nombre": "Bot Principal (21 ciudades, EV/Kelly)",
        "capital_inicial": inicio,
        "capital_actual": actual,
        "rendimiento_pct": (actual / inicio - 1) * 100 if inicio else 0.0,
        "trades": s.get("total_trades", 0),
        "wins": s.get("wins", 0),
        "losses": s.get("losses", 0),
    }


def _valor_mercado_abiertas(abiertas):
    """Valor de mercado real (no el costo) de las posiciones abiertas de favoritos_bot -
    duplicado aca a proposito (no importado de favoritos_bot.py) para que este reporte
    siga funcionando igual aunque favoritos_bot.py cambie de forma independiente."""
    import requests
    total = 0.0
    for p in abiertas:
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/markets/{p['market_id']}", timeout=(3, 6))
            bid = r.json().get("bestBid")
        except Exception:
            bid = None
        total += (bid if bid is not None else p["entry_price"]) * p["shares"]
    return total


def rendimiento_favoritos():
    """Lee data/favoritos_state.json (favoritos_bot.py) - $200 iniciales.
    El equity usa el valor de mercado real de lo abierto (no el costo), mismo criterio
    que usa favoritos_bot.py para su propio freno de emergencia - evita que este reporte
    y el de favoritos_bot.py muestren numeros distintos para la misma cosa."""
    path = Path("data/favoritos_state.json")
    if not path.exists():
        return None
    s = json.loads(path.read_text(encoding="utf-8"))
    inicio = s.get("starting_capital", 200.0)
    abiertas = [p for p in s.get("positions", []) if p["status"] == "open"]
    cerradas = [p for p in s.get("positions", []) if p["status"] == "closed"]
    ganadas = [p for p in cerradas if p.get("pnl") is not None and p["pnl"] >= 0]
    equity = s.get("balance", inicio) + _valor_mercado_abiertas(abiertas)
    return {
        "nombre": "Favoritos (6 ciudades, probabilidad propia + filtro EV)",
        "capital_inicial": inicio,
        "capital_actual": equity,
        "rendimiento_pct": (equity / inicio - 1) * 100 if inicio else 0.0,
        "trades": len(cerradas),
        "wins": len(ganadas),
        "losses": len(cerradas) - len(ganadas),
    }


def fetch_market_resolution(market_id):
    """Los scripts *_early_entry.py originales nunca resuelven sus propios picks
    (los dejan en status 'pending' para siempre) - esta funcion completa esa parte
    consultando Polymarket directamente antes de calcular metricas."""
    import requests
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 6))
        d = r.json()
        if not d.get("closed", False):
            return None
        prices = d.get("outcomePrices")
        if prices is None:
            return None
        if isinstance(prices, str):
            prices = json.loads(prices)
        yp = float(prices[0])
        if yp >= 0.9:
            return True
        if yp <= 0.1:
            return False
        return None
    except Exception:
        return None


def resolver_picks_early_entry():
    """Recorre data/*_early_tracking.json, resuelve los picks 'pending' cuyo
    mercado ya cerro en Polymarket, y persiste el resultado (pnl, status)."""
    for archivo in Path("data").glob("*_early_tracking.json"):
        try:
            picks = json.loads(archivo.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(picks, list):
            continue
        cambiado = False
        for p in picks:
            if p.get("status") != "pending":
                continue
            gano = fetch_market_resolution(p.get("market_id", ""))
            if gano is None:
                continue
            cost = p.get("cost", 0.0)
            shares = p.get("shares", 0.0)
            pnl = round(shares - cost, 2) if gano else round(-cost, 2)
            p["status"] = "resolved"
            p["resolved_outcome"] = "win" if gano else "loss"
            p["pnl"] = pnl
            cambiado = True
        if cambiado:
            bot_v2.atomic_write(archivo, json.dumps(picks, indent=2))


def rendimiento_early_entry():
    """Junta los archivos de tracking de los 6 scripts de entrada temprana
    (cada uno guarda su propio data/{ciudad}_early_tracking.json), si existen."""
    resolver_picks_early_entry()
    archivos = list(Path("data").glob("*_early_tracking.json"))
    if not archivos:
        return None
    total_costo = 0.0
    total_pnl = 0.0
    trades = 0
    wins = 0
    for f in archivos:
        try:
            picks = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for p in picks if isinstance(picks, list) else []:
            if p.get("status") != "resolved":
                continue
            trades += 1
            total_costo += p.get("cost", 0.0)
            pnl = p.get("pnl", 0.0) or 0.0
            total_pnl += pnl
            if pnl >= 0:
                wins += 1
    if trades == 0:
        return {
            "nombre": "Early Entry (6 ciudades)",
            "capital_inicial": None,
            "capital_actual": None,
            "rendimiento_pct": None,
            "trades": 0, "wins": 0, "losses": 0,
            "nota": "Corriendo, sin operaciones resueltas todavia.",
        }
    return {
        "nombre": "Early Entry (6 ciudades)",
        "capital_inicial": total_costo,
        "capital_actual": total_costo + total_pnl,
        "rendimiento_pct": (total_pnl / total_costo) * 100 if total_costo else 0.0,
        "trades": trades, "wins": wins, "losses": trades - wins,
    }


def rendimiento_picks_dashboards():
    """Suma el 3er/4to sistema de picks: el que corre DENTRO de cada check_*.py
    (Buenos Aires, Helsinki, Hong Kong, Milan, New York, Chengdu) via
    maybe_record_daily_picks/resolve_pending_picks - completamente separado de
    bot_v2.py/favoritos_bot.py/los *_early_entry.py, aunque puede terminar operando
    la misma ciudad+fecha+bucket el mismo dia sin que ninguno sepa del otro.
    Lee data/{city}_tracking.json - OJO, no confundir con data/{city}_early_tracking.json
    (eso es el sistema de early_entry, ya contado en rendimiento_early_entry)."""
    archivos = [f for f in Path("data").glob("*_tracking.json") if "early" not in f.name]
    if not archivos:
        return None
    total_costo = 0.0
    total_pnl = 0.0
    trades = 0
    wins = 0
    for f in archivos:
        try:
            dias = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for dia in (dias.values() if isinstance(dias, dict) else []):
            for p in dia.get("picks", []):
                if p.get("status") != "resolved":
                    continue
                trades += 1
                total_costo += p.get("cost", 0.0)
                pnl = p.get("pnl", 0.0) or 0.0
                total_pnl += pnl
                if pnl >= 0:
                    wins += 1
    if trades == 0:
        return {
            "nombre": "Picks Internos de Dashboards (6 ciudades)",
            "capital_inicial": None, "capital_actual": None, "rendimiento_pct": None,
            "trades": 0, "wins": 0, "losses": 0,
            "nota": "Corriendo, sin operaciones resueltas todavia.",
        }
    return {
        "nombre": "Picks Internos de Dashboards (6 ciudades)",
        "capital_inicial": total_costo,
        "capital_actual": total_costo + total_pnl,
        "rendimiento_pct": (total_pnl / total_costo) * 100 if total_costo else 0.0,
        "trades": trades, "wins": wins, "losses": trades - wins,
    }


def readiness_picks_dashboards():
    """Trades resueltos por ciudad del sistema de picks internos de los check_*.py,
    mismo formato que readiness_bot_principal/readiness_favoritos para que las 3
    lineas con desglose por ciudad (Early Entry no tiene coordenadas de ciudad
    limpias en su tracking, queda fuera de este desglose) entren juntas al ranking."""
    archivos = [f for f in Path("data").glob("*_tracking.json") if "early" not in f.name]
    filas = []
    for f in archivos:
        city_slug = f.name.replace("_tracking.json", "")
        nombre = bot_v2.LOCATIONS.get(city_slug, {}).get("name", city_slug.replace("-", " ").title())
        try:
            dias = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        resueltos = []
        for dia in (dias.values() if isinstance(dias, dict) else []):
            for p in dia.get("picks", []):
                if p.get("status") == "resolved":
                    resueltos.append(p)
        if not resueltos:
            continue
        wins = len([p for p in resueltos if (p.get("pnl") or 0) >= 0])
        n = len(resueltos)
        pnl = sum(p.get("pnl", 0.0) or 0.0 for p in resueltos)
        filas.append({
            "bot": "Picks Dashboard", "city": nombre, "trades": n, "wins": wins,
            "wr": (wins / n * 100) if n else 0.0, "pnl": pnl,
            "progreso": min(100.0, n / TARGET_TRADES_PER_CITY * 100),
            "listo": n >= TARGET_TRADES_PER_CITY,
        })
    return filas


def readiness_bot_principal():
    """Trades resueltos por ciudad en bot_v2.py, para medir cuan cerca esta cada
    una de tener base estadistica solida (TARGET_TRADES_PER_CITY)."""
    markets = bot_v2.load_all_markets()
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]
    por_ciudad = {}
    for m in resolved:
        por_ciudad.setdefault(m["city"], []).append(m)
    filas = []
    for city_slug, grupo in por_ciudad.items():
        nombre = bot_v2.LOCATIONS.get(city_slug, {}).get("name", city_slug)
        wins = len([m for m in grupo if m["resolved_outcome"] == "win"])
        n = len(grupo)
        pnl = sum(m["pnl"] for m in grupo)
        filas.append({
            "bot": "Bot Principal", "city": nombre, "trades": n, "wins": wins,
            "wr": (wins / n * 100) if n else 0.0, "pnl": pnl,
            "progreso": min(100.0, n / TARGET_TRADES_PER_CITY * 100),
            "listo": n >= TARGET_TRADES_PER_CITY,
        })
    return filas


def readiness_favoritos():
    """Trades cerrados por ciudad en favoritos_bot.py, contados SOLO desde que el bot
    cambio de "favorita del mercado" a "probabilidad propia" (state["criterio_edge_desde"],
    agregado el 2026-07-20) - los trades de antes del cambio no validan la estrategia
    que eventualmente iria a dinero real, aunque siguen en el historial del bot como
    referencia/comparacion."""
    path = Path("data/favoritos_state.json")
    if not path.exists():
        return []
    s = json.loads(path.read_text(encoding="utf-8"))
    desde = s.get("criterio_edge_desde")
    cerradas = [p for p in s.get("positions", []) if p["status"] == "closed"]
    if desde:
        cerradas = [p for p in cerradas if p.get("opened_at", "") >= desde]
    por_ciudad = {}
    for p in cerradas:
        por_ciudad.setdefault(p["city_name"], []).append(p)
    filas = []
    for nombre, grupo in por_ciudad.items():
        wins = len([p for p in grupo if p.get("pnl") is not None and p["pnl"] >= 0])
        n = len(grupo)
        pnl = sum(p["pnl"] for p in grupo if p.get("pnl") is not None)
        filas.append({
            "bot": "Favoritos", "city": nombre, "trades": n, "wins": wins,
            "wr": (wins / n * 100) if n else 0.0, "pnl": pnl,
            "progreso": min(100.0, n / TARGET_TRADES_PER_CITY * 100),
            "listo": n >= TARGET_TRADES_PER_CITY,
        })
    return filas


def send_telegram_photo(path, caption=""):
    import requests
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(path, "rb") as f:
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                          files={"photo": f}, timeout=(5, 20))
    except Exception as e:
        print(f"  [WARN] Telegram photo: {e}")


def render_readiness_report():
    """Progreso por ciudad (ambos bots) hacia el minimo de TARGET_TRADES_PER_CITY
    trades resueltos, y un ranking de las mejores candidatas para dinero real -
    solo entre las que YA llegaron al minimo estadistico, ordenadas por PnL.
    Mismo estilo visual que los reportes de bot_v2.py/favoritos_bot.py, pero
    implementado aca por separado (no importado) - este script es el unico que
    necesita ver datos de ambos bots a la vez, los otros dos no se tocan."""
    filas = readiness_bot_principal() + readiness_favoritos() + readiness_picks_dashboards()
    filas.sort(key=lambda r: -r["progreso"])

    C_BG, C_TEXT, C_DIM = "#0d1117", "#e6edf3", "#8b949e"
    C_POS, C_NEG, C_READY = "#3fb950", "#f85149", "#58a6ff"

    rows = []
    rows.append(("BASE ESTADISTICA POR CIUDAD — hacia dinero real", C_TEXT, 16, True, False))
    rows.append((datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), C_DIM, 11, False, True))
    rows.append((f"Minimo objetivo: {TARGET_TRADES_PER_CITY} trades resueltos por ciudad "
                  f"(margen de error ~+/-8-9 puntos en el win rate)", C_DIM, 10.5, False, True))
    rows.append(("=" * 90, C_DIM, 10, False, True))
    rows.append(("", C_TEXT, 6, False, True))

    rows.append((f"{'Bot':<17} {'City':<16}{'Trades':>8}{'Meta':>7}{'Progreso':>11}{'WR%':>7}{'PnL':>10}",
                  C_DIM, 11, True, True))
    rows.append(("-" * 90, C_DIM, 10, False, True))
    for r in filas:
        barra_len = 10
        llenas = round(r["progreso"] / 100 * barra_len)
        barra = "#" * llenas + "." * (barra_len - llenas)
        color = C_READY if r["listo"] else (C_POS if r["pnl"] >= 0 else C_NEG)
        row = (f"{r['bot']:<17} {r['city']:<16}{r['trades']:>8}{TARGET_TRADES_PER_CITY:>7}"
               f"  {barra} {r['progreso']:>4.0f}%{r['wr']:>7.0f}%{r['pnl']:>+10.2f}")
        rows.append((row, color, 10.5, False, True))

    rows.append(("-" * 90, C_DIM, 10, False, True))
    rows.append(("", C_TEXT, 6, False, True))

    listas = [r for r in filas if r["listo"]]
    if listas:
        listas.sort(key=lambda r: -r["pnl"])
        rows.append(("MEJORES CANDIDATAS PARA DINERO REAL (ya con base estadistica, por PnL)",
                      C_READY, 12, True, True))
        for r in listas[:5]:
            rows.append((f"  {r['bot']} — {r['city']}: {r['trades']} trades, {r['wr']:.0f}% WR, PnL {r['pnl']:+.2f}",
                          C_POS if r["pnl"] >= 0 else C_NEG, 11, False, True))
    else:
        rows.append(("Ninguna ciudad llego todavia al minimo estadistico — ninguna es candidata real todavia.",
                      C_NEG, 12, True, True))
        cerca = sorted(filas, key=lambda r: -r["progreso"])[:5]
        rows.append(("Las mas cerca de llegar:", C_DIM, 11, False, True))
        for r in cerca:
            faltan = TARGET_TRADES_PER_CITY - r["trades"]
            rows.append((f"  {r['bot']} — {r['city']}: {r['trades']}/{TARGET_TRADES_PER_CITY} "
                          f"(faltan {faltan})", C_TEXT, 11, False, True))

    n = len(rows)
    fig_h = max(4.5, 0.30 * n + 0.8)
    fig, ax = plt.subplots(figsize=(12, fig_h), facecolor=C_BG)
    ax.set_facecolor(C_BG)
    ax.axis("off")
    dy = 1.0 / (n + 1)
    y = 1.0 - dy * 0.6
    for text, color, size, bold, mono in rows:
        ax.text(0.02, y, text, transform=ax.transAxes, color=color, fontsize=size,
                fontweight="bold" if bold else "normal",
                family="monospace" if mono else "sans-serif", va="top")
        y -= dy

    img_dir = Path("data/images")
    img_dir.mkdir(parents=True, exist_ok=True)
    path = img_dir / "readiness_report.png"
    fig.savefig(path, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def send_readiness_report():
    path = render_readiness_report()
    send_telegram_photo(path, caption=f"Base estadistica por ciudad — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return path


def generar_reporte():
    lineas = ["🏆 REPORTE CONSOLIDADO — comparacion de rentabilidad", ""]
    resultados = []
    for fn in (rendimiento_bot_principal, rendimiento_favoritos, rendimiento_early_entry, rendimiento_picks_dashboards):
        r = fn()
        if r:
            resultados.append(r)

    for r in resultados:
        lineas.append(f"📈 {r['nombre']}")
        if r.get("rendimiento_pct") is None:
            lineas.append(f"   {r.get('nota', 'Sin datos todavia.')}")
        else:
            lineas.append(
                f"   ${r['capital_inicial']:.2f} -> ${r['capital_actual']:.2f}  "
                f"({'+' if r['rendimiento_pct'] >= 0 else ''}{r['rendimiento_pct']:.1f}%)"
            )
            if r["trades"]:
                wr = r["wins"] / r["trades"] * 100
                lineas.append(f"   {r['wins']}/{r['trades']} aciertos ({wr:.0f}%)")
        lineas.append("")

    con_datos = [r for r in resultados if r.get("rendimiento_pct") is not None]
    if con_datos:
        mejor = max(con_datos, key=lambda r: r["rendimiento_pct"])
        lineas.append(f"👉 Mejor rendimiento hasta ahora: {mejor['nombre']} ({mejor['rendimiento_pct']:+.1f}%)")

    lineas.append("")
    lineas.append("(Wallet watch y Dashboards no manejan capital propio, quedan fuera de esta comparacion.)")
    return "\n".join(lineas)


def main():
    print("REPORTE CONSOLIDADO — corre una vez por semana")
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {"ultimo": None}
    while True:
        ultimo = state.get("ultimo")
        necesita = (
            ultimo is None
            or (datetime.now(timezone.utc) - datetime.fromisoformat(ultimo)) >= timedelta(seconds=CHECK_INTERVAL)
        )
        if necesita:
            texto = generar_reporte()
            print(texto)
            send_telegram(texto)
            state["ultimo"] = datetime.now(timezone.utc).isoformat()
            STATE_FILE.parent.mkdir(exist_ok=True)
            bot_v2.atomic_write(STATE_FILE, json.dumps(state, indent=2))
        time.sleep(3600)  # revisa cada hora si ya toca la semana


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        main()
    elif cmd == "ahora":
        texto = generar_reporte()
        print(texto)
        send_telegram(texto)
    elif cmd == "readiness":
        path = send_readiness_report()
        print(f"Reporte de base estadistica enviado a Telegram. Imagen: {path}")
