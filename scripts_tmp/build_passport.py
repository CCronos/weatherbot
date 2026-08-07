#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Arma la pagina PASSPORT a partir del template + los datasets (historico
consolidado, ciudades operadas, snapshot de pico en vivo, track record).

Escribe DOS salidas identicas:
  - scripts_tmp/husky_passport.html  (flujo viejo: republicar via tool Artifact)
  - dist/index.html                  (flujo nuevo: deploy a Cloudflare Pages
                                      desde GitHub Actions — ver
                                      .github/workflows/passport-web.yml)

Se corre desde cualquier maquina: la raiz del repo se deduce de la ubicacion de
este archivo (antes estaba hardcodeada la ruta de Windows de la laptop, lo que
rompia la corrida en el runner Linux de Actions).
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

tpl = (ROOT / "scripts_tmp/passport_template.html").read_text(encoding="utf-8")
consolidated = (ROOT / "data/husky/consolidated_full.json").read_text(encoding="utf-8").replace("</script", "<\\/script")
tracked = (ROOT / "data/husky/tracked_stations.json").read_text(encoding="utf-8").replace("</script", "<\\/script")

live_path = ROOT / "data/live_peak_snapshot.json"
live_json = live_path.read_text(encoding="utf-8").replace("</script", "<\\/script") if live_path.exists() else "null"

res_path = ROOT / "data/husky/resolution_sources.json"
res_json = res_path.read_text(encoding="utf-8").replace("</script", "<\\/script") if res_path.exists() else "{}"

tr_path = ROOT / "data/live_predictions_log.json"
tr_json = tr_path.read_text(encoding="utf-8").replace("</script", "<\\/script") if tr_path.exists() else '{"records":[]}'

gato1_b64 = (ROOT / "scripts_tmp/gatos/gato1_cutout_small.b64").read_text(encoding="ascii")
gato2_b64 = (ROOT / "scripts_tmp/gatos/gato2_cutout_small.b64").read_text(encoding="ascii")
gato3_b64 = (ROOT / "scripts_tmp/gatos/gato3_crop.b64").read_text(encoding="ascii")

# Serie diaria historica (temp max / viento / direccion dominante) por estacion,
# generada por build_daily_series.py a partir de data/iem_raw/*.csv — un archivo
# compacto por estacion, se combinan todos en un solo dict keyed por ICAO para
# el grafico "evolucion historica" de la pestana Detalle por ciudad.
daily_series_dir = ROOT / "data/husky/daily_series"
daily_series = {}
if daily_series_dir.exists():
    for f in sorted(daily_series_dir.glob("*.json")):
        daily_series[f.stem] = json.loads(f.read_text(encoding="utf-8"))
daily_series_json = json.dumps(daily_series, separators=(",", ":")).replace("</script", "<\\/script")

out = (tpl
       .replace("__CONSOLIDATED_JSON__", consolidated)
       .replace("__TRACKED_JSON__", tracked)
       .replace("__LIVE_SNAPSHOT_JSON__", live_json)
       .replace("__RESOLUTION_JSON__", res_json)
       .replace("__TRACK_RECORD_JSON__", tr_json)
       .replace("__DAILY_SERIES_JSON__", daily_series_json)
       .replace("__GATO1_B64__", gato1_b64)
       .replace("__GATO2_B64__", gato2_b64)
       .replace("__GATO3_B64__", gato3_b64))

out_path = ROOT / "scripts_tmp/husky_passport.html"
out_path.write_text(out, encoding="utf-8")

dist_dir = ROOT / "dist"
dist_dir.mkdir(exist_ok=True)
(dist_dir / "index.html").write_text(out, encoding="utf-8")

print(f"generado: {out_path} y dist/index.html ({out_path.stat().st_size/1024:.0f} KB) — "
      f"vivo: {'si' if live_path.exists() else 'NO (sin snapshot)'}")
