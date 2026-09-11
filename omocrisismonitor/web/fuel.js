/* Fuel layer — a left-docked panel with Brent/WTI price tiles + a sparkline,
   and a retail pump-price world choropleth on the map (gasoline | diesel).
   Attaches via window.OCM + the `hub` EventTarget. */
(function () {
  const { hub } = window.OCM;
  const $ = (s) => document.querySelector(s);

  let map = null;
  let enabled = true;
  let world = null;                    // vendored countries FeatureCollection
  let kind = "gasoline";
  let retail = {};                     // iso -> USD/litre for the active kind
  let lastCrude = {};                  // { wti:{usd,prev_close}, brent:{...} }
  let retailUpdated = null, retailStale = false, crudeSource = "";
  const series = { wti: [], brent: [] };
  const SRC = "ocm-fuel";
  const NODATA = -1;

  // semantic price ramp (USD/litre) — cheap → expensive, not theme-driven
  const RAMP = [
    [0.0, "#2e7d32"], [0.8, "#7cb342"], [1.6, "#fdd835"],
    [2.4, "#fb8c00"], [3.2, "#e53935"], [4.5, "#8e24aa"],
  ];

  /* ---- choropleth ------------------------------------------------- */
  function bakeWorld() {
    return {
      type: "FeatureCollection",
      features: world.features.map((f) => ({
        type: "Feature",
        geometry: f.geometry,
        properties: { ...f.properties, price: retail[f.properties.iso] ?? NODATA },
      })),
    };
  }

  function addLayers() {
    const t = window.OCM.themeVars();
    map.addSource(SRC, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    const before = map.getStyle().layers.find((l) => l.id.startsWith("ocm-"))?.id;
    map.addLayer({
      id: "ocm-fuel-fill", type: "fill", source: SRC,
      paint: {
        "fill-color": [
          "case", ["<", ["get", "price"], 0], t.surface2,
          ["interpolate", ["linear"], ["get", "price"], ...RAMP.flat()],
        ],
        "fill-opacity": ["case", ["<", ["get", "price"], 0], 0.12, 0.5],
      },
    }, before);
    map.addLayer({
      id: "ocm-fuel-line", type: "line", source: SRC,
      paint: { "line-color": t.border, "line-width": 0.5, "line-opacity": 0.4 },
    }, before);

    map.on("click", "ocm-fuel-fill", (e) => {
      const p = e.features[0].properties;
      if (p.price < 0) return;
      new maplibregl.Popup({ closeButton: false, className: "ocm-pop" })
        .setLngLat(e.lngLat)
        .setHTML(`<b>${p.name}</b><br>${kind} · $${(+p.price).toFixed(2)}/L`)
        .addTo(map);
    });
    map.on("mouseenter", "ocm-fuel-fill", () => (map.getCanvas().style.cursor = "help"));
    map.on("mouseleave", "ocm-fuel-fill", () => (map.getCanvas().style.cursor = ""));
  }

  function redrawChoropleth() {
    const s = map && map.getSource(SRC);
    if (s && world) s.setData(bakeWorld());
  }

  function applyPaint(t) {
    if (!map || !map.getLayer("ocm-fuel-fill")) return;
    map.setPaintProperty("ocm-fuel-fill", "fill-color", [
      "case", ["<", ["get", "price"], 0], t.surface2,
      ["interpolate", ["linear"], ["get", "price"], ...RAMP.flat()],
    ]);
    map.setPaintProperty("ocm-fuel-line", "line-color", t.border);
  }

  function setVisible(v) {
    for (const id of ["ocm-fuel-fill", "ocm-fuel-line"]) {
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", v ? "visible" : "none");
    }
  }

  /* ---- panel --------------------------------------------------- */
  const money = (v) => (v == null ? "—" : "$" + (+v).toFixed(2));

  function tile(sym, label) {
    const d = lastCrude[sym];
    if (!d) return `<div class="ftile"><span class="fsym">${label}</span><span class="fpx">—</span></div>`;
    const chg = d.prev_close ? ((d.usd - d.prev_close) / d.prev_close) * 100 : 0;
    const dir = chg > 0.05 ? "up" : chg < -0.05 ? "dn" : "flat";
    const arrow = dir === "up" ? "▲" : dir === "dn" ? "▼" : "▬";
    return `<div class="ftile">
      <span class="fsym">${label}</span>
      <span class="fpx">${money(d.usd)}</span>
      <span class="fchg ${dir}">${arrow} ${Math.abs(chg).toFixed(2)}%</span></div>`;
  }

  function sparkline() {
    const W = 190, H = 46, pad = 3;
    const all = [...series.wti, ...series.brent].map((p) => p.usd).filter((v) => v != null);
    if (all.length < 2) return `<svg class="fspark" viewBox="0 0 ${W} ${H}"></svg>`;
    const lo = Math.min(...all), hi = Math.max(...all), span = hi - lo || 1;
    const t0 = Math.min(series.wti[0]?.ts ?? Infinity, series.brent[0]?.ts ?? Infinity);
    const t1 = Math.max(series.wti.at(-1)?.ts ?? 0, series.brent.at(-1)?.ts ?? 0);
    const tspan = t1 - t0 || 1;
    const path = (arr, color) => {
      if (arr.length < 2) return "";
      const d = arr.map((p, i) => {
        const x = pad + ((p.ts - t0) / tspan) * (W - 2 * pad);
        const y = H - pad - ((p.usd - lo) / span) * (H - 2 * pad);
        return `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
      }).join(" ");
      return `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.3"/>`;
    };
    return `<svg class="fspark" viewBox="0 0 ${W} ${H}">
      ${path(series.brent, "var(--ocm-orange)")}
      ${path(series.wti, "var(--ocm-warn)")}
      <text x="${pad}" y="9" class="fsl">${money(hi)}</text>
      <text x="${pad}" y="${H - 1}" class="fsl">${money(lo)}</text></svg>`;
  }

  function legend() {
    const stops = RAMP.map(([v, c]) => `${c} ${(v / 4.5) * 100}%`).join(", ");
    return `<div class="flegend" style="background:linear-gradient(90deg,${stops})"></div>
      <div class="flrow"><span>$0</span><span>USD / litre</span><span>$4.5+</span></div>`;
  }

  function render() {
    const el = $("#fuel-panel");
    const upd = retailUpdated
      ? `updated ${ago(retailUpdated)}` : "no retail data yet";
    el.innerHTML =
      `<header>FUEL <span class="fsrc">${crudeSource || ""}</span></header>` +
      tile("wti", "WTI") + tile("brent", "BRENT") +
      sparkline() +
      `<div class="fret">
         <div class="fret-tabs">
           <button data-k="gasoline" class="${kind === "gasoline" ? "on" : ""}">petrol</button>
           <button data-k="diesel" class="${kind === "diesel" ? "on" : ""}">diesel</button>
         </div>
         ${legend()}
         <div class="fret-meta ${retailStale ? "stale" : ""}">${retailStale ? "STALE · " : ""}${upd}</div>
       </div>`;
    el.querySelectorAll(".fret-tabs button").forEach((b) =>
      b.addEventListener("click", () => setKind(b.dataset.k)));
    el.hidden = !enabled;
  }

  function ago(ts) {
    const s = Date.now() / 1000 - ts;
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }

  /* ---- data ------------------------------------------------- */
  async function loadRetail() {
    try {
      const j = await fetch(`api/fuel/retail?kind=${kind}`).then((r) => r.json());
      retail = j.prices || {};
      retailUpdated = j.updated;
      retailStale = !!j.stale;
    } catch { retailStale = true; }
    redrawChoropleth();
    render();
  }

  async function loadCrude() {
    try {
      const boot = await fetch("api/fuel/bootstrap").then((r) => r.json());
      lastCrude = boot.crude || {};
      crudeSource = boot.source || "";
      retailUpdated = boot.retail_updated;
      retailStale = !!boot.retail_stale;
    } catch { /* keep going */ }
    for (const sym of ["wti", "brent"]) {
      try {
        const j = await fetch(`api/fuel/crude?symbol=${sym}&days=120`).then((r) => r.json());
        series[sym] = j.series || [];
      } catch { series[sym] = []; }
    }
    render();
  }

  function setKind(k) {
    if (k === kind) return;
    kind = k;
    loadRetail();
  }

  /* ---- rail --------------------------------------------- */
  function wireRail() {
    const btn = document.querySelector('.layer[data-layer="fuel"]');
    btn.disabled = false;
    btn.classList.toggle("on", enabled);
    btn.title = "Toggle fuel (crude + retail choropleth)";
    btn.addEventListener("click", () => {
      enabled = !enabled;
      btn.classList.toggle("on", enabled);
      setVisible(enabled);
      $("#fuel-panel").hidden = !enabled;
    });
  }

  /* ---- lifecycle ------------------------------------ */
  async function init() {
    map = window.OCM.map;
    try {
      world = await fetch("vendor/world-countries.geo.json").then((r) => r.json());
    } catch { world = { type: "FeatureCollection", features: [] }; }
    addLayers();
    wireRail();
    await loadCrude();
    await loadRetail();
  }

  hub.addEventListener("map-ready", init);
  hub.addEventListener("theme-applied", (e) => applyPaint(e.detail));
  hub.addEventListener("fuel_tick", (e) => {
    lastCrude = e.detail.crude || lastCrude;
    crudeSource = e.detail.source || crudeSource;
    for (const sym of ["wti", "brent"]) {
      const d = lastCrude[sym];
      if (d) series[sym] = [...series[sym], { ts: e.detail.ts, usd: d.usd }].slice(-400);
    }
    if (enabled) render();
  });
  hub.addEventListener("fuel_retail_ready", () => { if (enabled) loadRetail(); });
  hub.addEventListener("fuel_status", (e) => {
    if (e.detail.state === "stale") { retailStale = true; if (enabled) render(); }
  });
})();
