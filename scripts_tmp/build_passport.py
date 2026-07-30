#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Arma scripts_tmp/husky_passport.html a partir del template + los 3 datasets
(historico consolidado, ciudades operadas, snapshot de pico en vivo). Se corre
cada vez que hay que republicar la pagina con datos frescos."""
import json
from pathlib import Path

ROOT = Path(r"C:\Users\Usuario\Documents\weatherbot")

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

out = (tpl
       .replace("__CONSOLIDATED_JSON__", consolidated)
       .replace("__TRACKED_JSON__", tracked)
       .replace("__LIVE_SNAPSHOT_JSON__", live_json)
       .replace("__RESOLUTION_JSON__", res_json)
       .replace("__TRACK_RECORD_JSON__", tr_json)
       .replace("__GATO1_B64__", gato1_b64)
       .replace("__GATO2_B64__", gato2_b64)
       .replace("__GATO3_B64__", gato3_b64))

out_path = ROOT / "scripts_tmp/husky_passport.html"
out_path.write_text(out, encoding="utf-8")
print(f"generado: {out_path} ({out_path.stat().st_size/1024:.0f} KB) — vivo: {'si' if live_path.exists() else 'NO (sin snapshot)'}")
