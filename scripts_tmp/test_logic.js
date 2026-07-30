const fs = require("fs");

const DATA = JSON.parse(fs.readFileSync("data/husky/consolidated_full.json", "utf-8"));
const STATIONS = DATA.stations;
const byCode = {};
STATIONS.forEach(function (s) { byCode[s.code] = s; });

// --- funciones copiadas TAL CUAL del artefacto, para probar la logica real ---
var SOUTH = { AR: 1, BR: 1, ZA: 1, NZ: 1, ID: 1 };
var MONTH_SEASON_N = {12:"winter",1:"winter",2:"winter",3:"spring",4:"spring",5:"spring",
                       6:"summer",7:"summer",8:"summer",9:"autumn",10:"autumn",11:"autumn"};
var OPPOSITE = { winter: "summer", summer: "winter", spring: "autumn", autumn: "spring" };

function seasonFor(countryCode, month) {
  var base = MONTH_SEASON_N[month];
  return SOUTH[countryCode] ? OPPOSITE[base] : base;
}

function nearestHour(hoursAvail, hour) {
  var best = hoursAvail[0], bd = Math.abs(hoursAvail[0] - hour);
  hoursAvail.forEach(function (h) {
    var d = Math.abs(h - hour);
    if (d < bd) { best = h; bd = d; }
  });
  return best;
}

function query(st, hour, wind, trend, season) {
  var seasonBlock = st.seasons[season];
  if (!seasonBlock) return null;
  var curve = seasonBlock.curve || {};
  var hoursAvail = Object.keys(curve).map(Number);
  if (!hoursAvail.length) return null;
  var hourUsed = nearestHour(hoursAvail, hour);
  var cells = curve[String(hourUsed)];

  var key = null;
  if (wind && trend && cells[wind + "|" + trend]) key = wind + "|" + trend;
  if (!key && wind && cells[wind]) key = wind;
  if (!key && trend && cells["any|" + trend]) key = "any|" + trend;
  if (!key) key = "any";
  var cell = cells[key];
  if (!cell) return null;

  return {
    hourUsed: hourUsed, key: key, n: cell.n, gain: cell.g, median: cell.m, pct: cell.p,
    exact: hourUsed === hour,
  };
}

// --- casos de prueba: mismos que se corrieron en python (husky_query.py) ---
const cases = [
  ["LTAC", 10, null, null, "summer"],
  ["LTAC", 10, "N", "climbing", "summer"],
  ["EFHK", 14, null, null, "summer"],
  ["SAEZ", 12, null, null, "winter"],
  ["LTAC", 4, null, null, "summer"],   // hora sin dato exacto, cae a la mas cercana
  ["EDDM", 3, null, null, "summer"],
  ["EFHK", 4, null, null, "summer"],
  // casos borde
  ["XXXX", 10, null, null, "summer"],  // estacion inexistente
  ["LTAC", 10, "S", "climbing", "winter"], // combinacion viento+tendencia rara, puede no existir
];

for (const [code, hour, wind, trend, season] of cases) {
  const st = byCode[code];
  if (!st) { console.log(`${code} @ ${hour}h -> ESTACION NO EXISTE (esperado si es XXXX)`); continue; }
  const r = query(st, hour, wind, trend, season);
  if (!r) { console.log(`${code} @ ${hour}h wind=${wind} trend=${trend} season=${season} -> SIN DATOS (null)`); continue; }
  console.log(`${code} @ ${hour}h wind=${wind} trend=${trend} season=${season} -> hora_usada=${r.hourUsed} key=${r.key} n=${r.n} gain=${r.gain} median=${r.median} pct=${r.pct} exact=${r.exact}`);
}

// --- chequeo estructural: las 50 estaciones tienen las 4 temporadas y curva no vacia? ---
let problemas = [];
STATIONS.forEach(function (s) {
  ["winter","spring","summer","autumn"].forEach(function (season) {
    const block = s.seasons[season];
    if (!block) { problemas.push(`${s.code}: falta temporada ${season}`); return; }
    const hours = Object.keys(block.curve || {});
    if (hours.length === 0) problemas.push(`${s.code}/${season}: curve vacia`);
  });
});
console.log(`\nEstaciones chequeadas: ${STATIONS.length}`);
console.log(`Problemas estructurales encontrados: ${problemas.length}`);
problemas.forEach(function (p) { console.log("  " + p); });
