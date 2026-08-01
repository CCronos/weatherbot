"""Recalibra el sesgo propio de peak_final (modelo+empirico ya combinados) usando el
track record de PASSPORT (data/live_predictions_log.json), no el de bot_v2.

Por que existe separado de calibration.json/run_calibration de bot_v2: ese archivo
corrige el sesgo de CADA fuente cruda (ecmwf, regional) antes de mezclarlas -
husky_live_snapshot.py ya usa esas correcciones al construir model_peak. Pero
empirico_peak no pasa por ninguna calibracion, y el promedio final (peak_final)
puede tener su propio sesgo neto aunque cada fuente ya este corregida. Mezclar esa
correccion en las mismas claves _pooled_{unit}_{source} de calibration.json
contaminaria la calibracion que SI usa bot_v2/live_trade para el modelo puro -
por eso esto vive en su propio archivo (data/husky_peak_bias.json) y solo lo lee
husky_live_snapshot.py, nunca bot_v2.py ni el trading real.

Corre cada hora en el pipeline de Actions, ANTES del snapshot en vivo, para que
el sesgo mas fresco (con el track record ya calificado del ciclo anterior) se
aplique al peak_final de este mismo ciclo.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

LOG_FILE = Path("data/live_predictions_log.json")
BIAS_FILE = Path("data/husky_peak_bias.json")
MIN_SAMPLES = 20  # por unidad - por debajo de esto no se aplica correccion (ver bias=0 default)


def atomic_write(path: Path, content: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def main():
    if not LOG_FILE.exists():
        print("sin live_predictions_log.json todavia, nada que recalibrar")
        return

    data = json.loads(LOG_FILE.read_text(encoding="utf-8"))
    recs = data.get("records", [])
    usable = [r for r in recs
              if r.get("resolved") and r.get("peak_final") is not None and r.get("actual_temp") is not None]

    por_unidad = {}
    for r in usable:
        por_unidad.setdefault(r["unit"], []).append(r["peak_final"] - r["actual_temp"])

    resultado = {}
    for unit, errores in por_unidad.items():
        n = len(errores)
        if n < MIN_SAMPLES:
            print(f"  {unit}: n={n} < {MIN_SAMPLES}, se salta (sin suficiente historia todavia)")
            continue
        # Media simple, no exponencial - mismo razonamiento que el pooled de bot_v2:
        # agrupa muchas ciudades/fechas distintas, el objetivo es un centro estable,
        # no seguir la tendencia de las ultimas observaciones de una sola ciudad.
        bias = sum(errores) / n
        mae = sum(abs(e) for e in errores) / n
        resultado[unit] = {
            "bias": round(bias, 3),
            "sigma": round(mae, 3),
            "n": n,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        print(f"  {unit}: bias={bias:+.3f} sigma={mae:.3f} (n={n})")

    if not resultado:
        print("ninguna unidad junto suficientes muestras, no se escribe nada")
        return

    atomic_write(BIAS_FILE, json.dumps(resultado, indent=2, ensure_ascii=False))
    print(f"escrito {BIAS_FILE}")


if __name__ == "__main__":
    main()
