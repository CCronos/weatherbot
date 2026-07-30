(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("data-consolidated").textContent);
  var TRACKED = JSON.parse(document.getElementById("data-tracked").textContent); // { ICAO: {slug,name,unit} }
  var LIVE = JSON.parse(document.getElementById("data-live").textContent); // { generated_at, cities: [...] } o null
  var RESOLUTION = JSON.parse(document.getElementById("data-resolution").textContent); // { ICAO: {source, source_confirmed, source_fallback, full_station_name} }

  // "hace X min" tiene que ir avanzando solo mientras la pagina esta abierta, no
  // quedarse fijo en lo que decia al cargar — sino parece "en vivo" sin serlo.
  function updateLiveAge() {
    if (!LIVE || !LIVE.generated_at) return;
    var updEl = document.getElementById("live-updated");
    var gen = new Date(LIVE.generated_at);
    var mins = Math.round((Date.now() - gen.getTime()) / 60000);
    var edad = mins < 1 ? "hace instantes" : mins < 60 ? "hace " + mins + " min" : "hace " + Math.round(mins / 60) + "h";
    var stale = mins >= 30;
    updEl.textContent = "última lectura: " + edad + (stale ? " — puede estar desactualizado, pedí que la refresquen" : "") +
      " (" + gen.toISOString().slice(0, 16).replace("T", " ") + " UTC)";
  }

  function renderLive() {
    var updEl = document.getElementById("live-updated");
    var emptyEl = document.getElementById("live-empty-msg");
    var tableEl = document.getElementById("live-table");
    var body = document.getElementById("live-body");

    if (!LIVE || !LIVE.cities || !LIVE.cities.length) {
      updEl.textContent = "sin datos todavía";
      emptyEl.style.display = "";
      tableEl.style.display = "none";
      return;
    }
    emptyEl.style.display = "none";
    tableEl.style.display = "";
    updateLiveAge();

    body.innerHTML = LIVE.cities.map(function (c) {
      var peakClass = c.peak_final == null ? "" : (c.spread_modelo_vs_empirico != null && c.spread_modelo_vs_empirico >= 3 ? "warn" : "good");
      var spreadFlag = c.spread_modelo_vs_empirico != null && c.spread_modelo_vs_empirico >= 3
        ? '<span class="spread-flag high">±' + c.spread_modelo_vs_empirico.toFixed(1) + "</span>" : "";
      var windFlag = "";
      if (c.viento_nota) {
        var boost = c.viento_nota.indexOf("MAS impulsa") >= 0;
        windFlag = '<span class="wind-flag ' + (boost ? "boost" : "kill") + '">' + (boost ? "▲ viento favorable" : "▼ viento adverso") + "</span>";
      }
      var res = RESOLUTION[c.station];
      var fuenteTxt = res
        ? "WU" + (res.source_confirmed ? " ✓" : " ?")
        : "s/d";
      var fuenteTitle = res
        ? (res.source_confirmed
            ? "Verificado contra el texto de resolución del mercado."
            : "No verificado ciudad-por-ciudad — es la fuente que el bot intenta primero por defecto.") +
          " Fallback: " + res.source_fallback
        : "";
      var bucketCell = "s/d";
      if (c.mejor_bucket) {
        var mb = c.mejor_bucket;
        // Precio muy bajo infla el EV matematicamente sin que sea ejecutable en tamano
        // real (la misma trampa que hizo parecer +61% rentable a favoritos_bot cuando
        // en realidad perdia plata) — se marca en vez de mostrar el numero pelado.
        var cautela = mb.price < 0.06;
        bucketCell = mb.bucket + " @ " + mb.price.toFixed(3) +
          " · EV " + (mb.ev * 100).toFixed(0) + "%" + (cautela ? " ⚠" : "") +
          " · Kelly " + (mb.kelly * 100).toFixed(1) + "%" +
          '<div class="sub" style="margin-top:2px;">vol $' + mb.volume.toLocaleString() +
          (cautela ? " — precio muy bajo, EV no es fiable en tamaño" : "") + "</div>";
      }
      return "<tr>" +
        '<td class="city">' + c.name + " (" + c.station + ")</td>" +
        "<td>" + (c.metar_temp != null ? c.metar_temp.toFixed(1) + "°" + c.unit : "s/d") + "</td>" +
        "<td>" + (c.wind_compass || "—") + (c.sky ? " · " + c.sky : "") + "</td>" +
        '<td title="' + (c.model_src_display || "") + '">' + (c.model_peak != null ? c.model_peak.toFixed(1) + "° (" + (c.model_src_display || "?") + ")" : "s/d") + "</td>" +
        "<td>" + (c.empirico_peak != null ? c.empirico_peak.toFixed(1) + "°" : "s/d") + "</td>" +
        '<td class="peak ' + peakClass + '">' + (c.peak_final != null ? c.peak_final.toFixed(1) + "°" + c.unit : "s/d") + spreadFlag + "</td>" +
        "<td>" + windFlag + "</td>" +
        '<td title="' + fuenteTitle + '">' + fuenteTxt + "</td>" +
        "<td>" + bucketCell + "</td>" +
        "</tr>";
    }).join("");
  }
  var STATIONS = DATA.stations; // array
  var byCode = {};
  STATIONS.forEach(function (s) { byCode[s.code] = s; });

  var SOUTH = { AR: 1, BR: 1, ZA: 1, NZ: 1, ID: 1 };
  var MONTH_SEASON_N = {12:"winter",1:"winter",2:"winter",3:"spring",4:"spring",5:"spring",
                         6:"summer",7:"summer",8:"summer",9:"autumn",10:"autumn",11:"autumn"};
  var OPPOSITE = { winter: "summer", summer: "winter", spring: "autumn", autumn: "spring" };
  var SEASON_ES = { winter: "invierno", spring: "primavera", summer: "verano", autumn: "otoño" };
  var WINDS = ["N","NE","E","SE","S","SW","W","NW"];

  function seasonFor(countryCode, month) {
    var base = MONTH_SEASON_N[month];
    return SOUTH[countryCode] ? OPPOSITE[base] : base;
  }

  // ---- state ----
  var state = { code: null, hour: 12, wind: "", trend: "", seasonOverride: null };

  // ---- station picker ----
  var input = document.getElementById("station-input");
  var listEl = document.getElementById("station-list");

  function renderStationList(filter) {
    var f = (filter || "").toLowerCase();
    var items = STATIONS.filter(function (s) {
      return !f || s.code.toLowerCase().indexOf(f) >= 0 || s.name.toLowerCase().indexOf(f) >= 0;
    }).slice(0, 40);
    listEl.innerHTML = items.map(function (s) {
      var tracked = TRACKED[s.code];
      var badge = tracked ? "●" : "";
      return '<div class="station-opt" data-code="' + s.code + '"><span>' + badge + ' ' + s.code + '</span><span class="name">' + s.name + '</span></div>';
    }).join("");
    listEl.classList.toggle("open", items.length > 0);
  }
  // Al elegir una estacion el campo queda con "CODIGO - Nombre" — sin esto, si el
  // usuario hace foco y escribe sin borrar primero, el texto nuevo se pega al final
  // ("LTAC - AnkaraHelsinki") y el filtro no matchea nada (bug reportado 2026-07-29:
  // "escribo una ciudad y no filtra"). Seleccionando todo al hacer foco, escribir
  // directamente reemplaza el valor anterior, como en cualquier buscador.
  input.addEventListener("focus", function () {
    input.select();
    renderStationList(input.value);
  });
  input.addEventListener("input", function () { renderStationList(input.value); });
  document.addEventListener("click", function (e) {
    if (!listEl.contains(e.target) && e.target !== input) listEl.classList.remove("open");
  });
  listEl.addEventListener("click", function (e) {
    var opt = e.target.closest(".station-opt");
    if (!opt) return;
    selectStation(opt.getAttribute("data-code"));
    listEl.classList.remove("open");
  });

  function localHourNow(tz) {
    try {
      var s = new Date().toLocaleString("en-US", { timeZone: tz, hour: "2-digit", hour12: false });
      var h = parseInt(s, 10);
      return isNaN(h) ? 12 : (h === 24 ? 0 : h);
    } catch (e) { return 12; }
  }

  function selectStation(code) {
    var st = byCode[code];
    if (!st) return;
    state.code = code;
    state.hour = localHourNow(st.timezone);
    input.value = st.code + " — " + st.name;
    render();
    tickClock();
  }

  // ---- live clock: hora REAL ahora mismo en la estacion elegida, separada del
  // stepper (que es "la hora que estoy inspeccionando", pueden diferir a proposito
  // si el usuario mueve el stepper para explorar otra hora del dia) ----
  function tickClock() {
    var st = state.code && byCode[state.code];
    var el = document.getElementById("now-clock-time");
    if (!st) { el.textContent = "--:--"; return; }
    try {
      var full = new Date().toLocaleString("en-GB", {
        timeZone: st.timezone, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
      });
      el.textContent = full;
    } catch (e) { el.textContent = "--:--"; }
  }
  document.getElementById("now-clock-jump").addEventListener("click", function () {
    var st = state.code && byCode[state.code];
    if (!st) return;
    state.hour = localHourNow(st.timezone);
    render();
  });
  setInterval(tickClock, 1000);
  setInterval(updateLiveAge, 30000); // cada 30s alcanza, es solo texto de "hace X min"

  // ---- hour stepper ----
  document.getElementById("hour-minus").addEventListener("click", function () {
    state.hour = (state.hour + 23) % 24; render();
  });
  document.getElementById("hour-plus").addEventListener("click", function () {
    state.hour = (state.hour + 1) % 24; render();
  });

  // ---- wind chips ----
  var windWrap = document.getElementById("wind-chips");
  windWrap.innerHTML = '<span class="chip active" data-wind="">cualquiera</span>' +
    WINDS.map(function (w) { return '<span class="chip" data-wind="' + w + '">' + w + "</span>"; }).join("");
  windWrap.addEventListener("click", function (e) {
    var chip = e.target.closest(".chip"); if (!chip) return;
    windWrap.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("active"); });
    chip.classList.add("active");
    state.wind = chip.getAttribute("data-wind") || "";
    render();
  });

  // ---- trend chips ----
  var trendWrap = document.getElementById("trend-chips");
  trendWrap.addEventListener("click", function (e) {
    var chip = e.target.closest(".chip"); if (!chip) return;
    trendWrap.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("active"); });
    chip.classList.add("active");
    state.trend = chip.getAttribute("data-trend") || "";
    render();
  });

  // ---- matching logic (mirrors husky_query.py) ----
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

    // A esta hora puede no existir suficiente muestra para el viento/tendencia pedidos
    // (ej. "subiendo" no tiene datos a las 20h porque a esa hora casi nunca sigue
    // subiendo) y la funcion cae a una version menos especifica en silencio. Sin avisar
    // esto, elegir un filtro que no tiene datos parecia "no hacer nada" (reportado
    // 2026-07-29). Se devuelve que fue lo pedido vs lo realmente usado para poder
    // mostrar la diferencia en la nota.
    var pedia_algo = !!(wind || trend);
    var consiguio_lo_pedido = key !== "any" &&
      (!wind || key.split("|")[0] === wind) &&
      (!trend || key.indexOf("|" + trend) >= 0 || key === trend + "|" + trend);
    return {
      hourUsed: hourUsed, key: key, n: cell.n, gain: cell.g, median: cell.m, pct: cell.p,
      exact: hourUsed === hour,
      sin_dato_para_filtro: pedia_algo && !consiguio_lo_pedido,
    };
  }

  // ---- chart ----
  function drawChart(st, season, hour) {
    var canvas = document.getElementById("curve");
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth || 900, h = 190;
    canvas.width = w * dpr; canvas.height = h * dpr;
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    var block = st.seasons[season];
    var curve = (block && block.curve) || {};
    var hours = Object.keys(curve).map(Number).sort(function (a, b) { return a - b; });
    if (!hours.length) {
      ctx.fillStyle = "#5b626b"; ctx.font = "12px ui-monospace, monospace";
      ctx.fillText("Sin datos de curva para esta temporada.", 10, h / 2);
      return;
    }
    var pad = { l: 34, r: 34, t: 12, b: 22 };
    var gw = w - pad.l - pad.r, gh = h - pad.t - pad.b;
    var gains = hours.map(function (hh) { return curve[hh].any.g; });
    var pcts = hours.map(function (hh) { return curve[hh].any.p; });
    var maxGain = Math.max.apply(null, gains.concat([0.5]));

    function x(i) { return pad.l + (gw * i) / (hours.length - 1 || 1); }
    function yGain(v) { return pad.t + gh - (gh * v) / maxGain; }
    function yPct(v) { return pad.t + gh - (gh * v) / 100; }

    // grid
    ctx.strokeStyle = "#1a1f27"; ctx.lineWidth = 1;
    for (var g = 0; g <= 4; g++) {
      var gy = pad.t + (gh * g) / 4;
      ctx.beginPath(); ctx.moveTo(pad.l, gy); ctx.lineTo(w - pad.r, gy); ctx.stroke();
    }

    // area fill (gain)
    ctx.beginPath();
    ctx.moveTo(x(0), yGain(0));
    hours.forEach(function (hh, i) { ctx.lineTo(x(i), yGain(curve[hh].any.g)); });
    ctx.lineTo(x(hours.length - 1), yGain(0));
    ctx.closePath();
    var grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + gh);
    grad.addColorStop(0, "rgba(224,168,63,0.28)");
    grad.addColorStop(1, "rgba(224,168,63,0.02)");
    ctx.fillStyle = grad; ctx.fill();

    // gain line
    ctx.beginPath();
    hours.forEach(function (hh, i) {
      var px = x(i), py = yGain(curve[hh].any.g);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.strokeStyle = "#e0a83f"; ctx.lineWidth = 2; ctx.stroke();

    // pct line (dashed green)
    ctx.beginPath();
    ctx.setLineDash([4, 3]);
    hours.forEach(function (hh, i) {
      var px = x(i), py = yPct(curve[hh].any.p);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.strokeStyle = "#3fb950"; ctx.lineWidth = 1.6; ctx.stroke();
    ctx.setLineDash([]);

    // current-hour marker
    var nh = nearestHour(hours, hour);
    var idx = hours.indexOf(nh);
    var mx = x(idx);
    ctx.strokeStyle = "rgba(233,230,221,0.25)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(mx, pad.t); ctx.lineTo(mx, pad.t + gh); ctx.stroke();
    ctx.beginPath(); ctx.arc(mx, yGain(curve[nh].any.g), 4, 0, Math.PI * 2);
    ctx.fillStyle = "#e0a83f"; ctx.fill();

    // axis labels (hours)
    ctx.fillStyle = "#5b626b"; ctx.font = "10px ui-monospace, monospace"; ctx.textAlign = "center";
    hours.forEach(function (hh, i) {
      if (hours.length > 10 && i % 2 !== 0) return;
      ctx.fillText(hh + "h", x(i), h - 6);
    });
  }

  // ---- render ----
  function render() {
    var st = state.code && byCode[state.code];
    document.getElementById("hour-val").textContent = st
      ? String(state.hour).padStart(2, "0") + ":00"
      : "--:00";

    if (!st) {
      document.getElementById("ro-station").textContent = "—";
      document.getElementById("ro-sub").textContent = "Elegí una estación arriba.";
      document.getElementById("season-auto").innerHTML = "—";
      return;
    }

    var now = new Date();
    var season = state.seasonOverride || seasonFor(st.country, now.getMonth() + 1);
    var seasonAutoEl = document.getElementById("season-auto");
    seasonAutoEl.innerHTML = SEASON_ES[season] + '<span class="hint">automática por hemisferio</span>';

    document.getElementById("ro-station").innerHTML =
      '<span class="code">' + st.code + "</span> · " + st.name;
    var tracked = TRACKED[st.code];
    var res = RESOLUTION[st.code];
    var resTxt = res
      ? " Resuelve vía <b>" + res.source + "</b>" + (res.source_confirmed ? " (verificado contra el texto del mercado)" : " (asumido, sin verificar esta ciudad puntual)") +
        " · estación: <b>" + res.full_station_name + "</b>."
      : "";
    document.getElementById("ro-sub").innerHTML =
      (tracked ? "Rastreada como <b>" + tracked.name + "</b> en bot_v2 (" + tracked.slug + ")." : "No está entre nuestras 30 ciudades operadas todavía.") +
      " Temporada de datos: <b>" + SEASON_ES[season] + "</b>." + resTxt;

    var r = query(st, state.hour, state.wind, state.trend, season);
    var gainEl = document.getElementById("ro-gain");
    var pctEl = document.getElementById("ro-pct");
    var medEl = document.getElementById("ro-median");
    var nEl = document.getElementById("ro-n");
    var note = document.getElementById("ro-note");

    if (!r) {
      gainEl.textContent = "s/d"; pctEl.textContent = "s/d"; medEl.textContent = "s/d"; nEl.textContent = "s/d";
      note.textContent = "Sin historial suficiente para este cruce.";
      note.className = "match-note";
    } else {
      gainEl.textContent = "+" + r.gain.toFixed(1);
      gainEl.className = "num " + (r.gain >= 2 ? "good" : r.gain >= 0.5 ? "warn" : "bad");
      medEl.textContent = "+" + r.median.toFixed(1);
      pctEl.textContent = r.pct.toFixed(0) + "%";
      pctEl.className = "num " + (r.pct >= 66 ? "good" : r.pct >= 33 ? "warn" : "bad");
      nEl.textContent = r.n;

      var whatKey = r.key === "any" ? "sin condicionar viento" : "viento " + r.key;
      var whatHour = r.exact ? "las " + r.hourUsed + ":00 exactas" : "las " + r.hourUsed + ":00 (más cercana con datos a las " + state.hour + ":00)";
      note.innerHTML = "A partir de " + whatHour + " — " + whatKey + " · " + r.n + " días en el histórico de " + SEASON_ES[season] + ".";
      // Si se pidio viento y/o tendencia pero esta estacion/hora no tiene suficiente
      // muestra de esa combinacion, se cae a una version menos especifica EN SILENCIO
      // — sin este aviso, elegir un filtro sin datos parecia "no hacer nada" (reportado
      // 2026-07-29: a las 20h en Ankara casi nunca sigue "subiendo" la temperatura en
      // verano, esa combinacion puntual no tiene muestra suficiente ahi).
      if (r.sin_dato_para_filtro) {
        note.innerHTML += " <b>Ojo:</b> no había suficiente muestra para " +
          (state.wind ? "viento " + state.wind : "") + (state.wind && state.trend ? " + " : "") +
          (state.trend ? "tendencia \"" + document.querySelector('[data-trend="' + state.trend + '"]').textContent + "\"" : "") +
          " a esta hora en esta ciudad — se muestra el dato sin ese filtro.";
      }
      note.className = "match-note " + (r.exact && r.key !== "any" ? "exact" : r.exact ? "" : "fallback");
    }

    var wb = (st.seasons[season] || {}).best_wind || {};
    var ww = (st.seasons[season] || {}).worst_wind || {};
    document.getElementById("wind-best-dir").textContent = wb.dir || "—";
    document.getElementById("wind-best-eff").textContent = wb.max_c != null
      ? "máx. típico " + wb.max_c.toFixed(1) + "°C (" + (wb.effect || "") + ")" : "sin datos suficientes";
    document.getElementById("wind-worst-dir").textContent = ww.dir || "—";
    document.getElementById("wind-worst-eff").textContent = ww.max_c != null
      ? "máx. típico " + ww.max_c.toFixed(1) + "°C (" + (ww.effect || "") + ")" : "sin datos suficientes";

    drawChart(st, season, state.hour);
  }

  // ---- coverage table ----
  var covBody = document.getElementById("cov-body");
  var MISMATCH = { LFPB: "LFPG (CDG)" };
  function renderCoverage(filter) {
    var f = (filter || "").toLowerCase();
    var rows = STATIONS.filter(function (s) {
      return !f || s.code.toLowerCase().indexOf(f) >= 0 || s.name.toLowerCase().indexOf(f) >= 0;
    }).map(function (s) {
      var tracked = TRACKED[s.code];
      var tag = MISMATCH[s.code]
        ? '<span class="tag mismatch">≠ ' + MISMATCH[s.code] + "</span>"
        : tracked ? '<span class="tag tracked">operada</span>'
        : '<span class="tag untracked">sin operar</span>';
      var sel = s.code === state.code ? " selected" : "";
      return '<tr class="row-clickable' + sel + '" data-code="' + s.code + '"><td>' + s.code + "</td><td>" + s.name + "</td><td>" + tag + "</td></tr>";
    });
    covBody.innerHTML = rows.join("");
  }
  document.getElementById("cov-search").addEventListener("input", function (e) { renderCoverage(e.target.value); });
  covBody.addEventListener("click", function (e) {
    var tr = e.target.closest("tr[data-code]"); if (!tr) return;
    selectStation(tr.getAttribute("data-code"));
    renderCoverage(document.getElementById("cov-search").value);
  });

  renderCoverage("");
  document.getElementById("prov-count").textContent = STATIONS.length;
  document.getElementById("station-count-badge").textContent = STATIONS.length + " estaciones";

  // default: Ankara if present, else first station
  renderLive();
  selectStation(byCode["LTAC"] ? "LTAC" : STATIONS[0].code);
  window.addEventListener("resize", function () { if (state.code) drawChart(byCode[state.code], state.seasonOverride || seasonFor(byCode[state.code].country, new Date().getMonth()+1), state.hour); });
})();