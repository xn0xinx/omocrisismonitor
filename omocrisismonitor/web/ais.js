/* AIS layer — subscribes the server to the map viewport, renders vessels,
   shows a detail panel + trail on click. Talks to the shell via window.OCM. */
(function () {
  const { hub } = window.OCM;
  const $ = (s) => document.querySelector(s);

  let map = null;
  let enabled = true;                 // layer toggle (rail button)
  const vessels = new Map();          // mmsi -> props
  const trail = { mmsi: null, pts: [] };
  let selected = null;
  let lastUpsert = 0;
  let rebuildTimer = 0, viewTimer = 0, staleTimer = 0;

  const SRC = "ocm-ais", TRAIL = "ocm-ais-trail";
  const CAT_KEYS = {
    cargo: "accent", tanker: "orange", passenger: "magenta",
    fishing: "ok", special: "info", pleasure: "dim", other: "dim",
  };

  /* ---- geojson ------------------------------------------------------ */
  const fc = () => ({
    type: "FeatureCollection",
    features: [...vessels.values()].map((v) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [v.lon, v.lat] },
      properties: {
        mmsi: v.mmsi, name: v.name, cat: v.cat,
        cog: v.cog == null ? 0 : v.cog,
        moving: (v.sog || 0) > 0.5 ? 1 : 0,
      },
    })),
  });

  function rebuild() {
    clearTimeout(rebuildTimer);
    rebuildTimer = setTimeout(() => {
      const s = map && map.getSource(SRC);
      if (s) s.setData(fc());
    }, 120);
  }

  function trailFC() {
    return {
      type: "FeatureCollection",
      features: trail.pts.length > 1
        ? [{ type: "Feature", geometry: { type: "LineString", coordinates: trail.pts } }]
        : [],
    };
  }

  /* ---- style ------------------------------------------------------- */
  function catColor(t) {
    return ["match", ["get", "cat"],
      ...Object.entries(CAT_KEYS).flatMap(([k, tk]) => [k, t[tk]]),
      t.dim];
  }

  function addLayers() {
    const t = window.OCM.themeVars();
    map.addSource(SRC, { type: "geojson", data: fc() });
    map.addSource(TRAIL, { type: "geojson", data: trailFC() });

    map.addLayer({
      id: "ocm-ais-trail", type: "line", source: TRAIL,
      paint: { "line-color": t.accent, "line-width": 1.5, "line-opacity": 0.7, "line-dasharray": [1, 1] },
    });
    map.addLayer({
      id: "ocm-ais-dot", type: "circle", source: SRC,
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 2, 2, 6, 3.2, 11, 5.5],
        "circle-color": catColor(t),
        "circle-opacity": ["case", ["==", ["get", "moving"], 1], 0.95, 0.6],
        "circle-stroke-color": t.bg, "circle-stroke-width": 0.6,
      },
    });
    map.addLayer({
      id: "ocm-ais-arrow", type: "symbol", source: SRC, minzoom: 6,
      filter: ["==", ["get", "moving"], 1],
      layout: {
        "text-field": "▲", "text-size": 13, "text-allow-overlap": true,
        "text-rotate": ["get", "cog"], "text-rotation-alignment": "map",
        "text-ignore-placement": true,
      },
      paint: { "text-color": catColor(t), "text-halo-color": t.bg, "text-halo-width": 1 },
    });
    map.addLayer({
      id: "ocm-ais-label", type: "symbol", source: SRC, minzoom: 10,
      layout: {
        "text-field": ["get", "name"], "text-size": 10, "text-offset": [0, 1.1],
        "text-anchor": "top", "text-optional": true,
      },
      paint: { "text-color": t.dim, "text-halo-color": t.bg, "text-halo-width": 1.2 },
    });
    map.addLayer({
      id: "ocm-ais-sel", type: "circle", source: SRC,
      filter: ["==", ["get", "mmsi"], -1],
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 2, 6, 11, 12],
        "circle-color": "rgba(0,0,0,0)",
        "circle-stroke-color": t.accent, "circle-stroke-width": 2,
      },
    });

    map.on("click", "ocm-ais-dot", (e) => select(e.features[0].properties.mmsi));
    map.on("mouseenter", "ocm-ais-dot", () => (map.getCanvas().style.cursor = "crosshair"));
    map.on("mouseleave", "ocm-ais-dot", () => (map.getCanvas().style.cursor = ""));
  }

  function applyPaint(t) {
    if (!map || !map.getLayer("ocm-ais-dot")) return;
    map.setPaintProperty("ocm-ais-dot", "circle-color", catColor(t));
    map.setPaintProperty("ocm-ais-dot", "circle-stroke-color", t.bg);
    map.setPaintProperty("ocm-ais-arrow", "text-color", catColor(t));
    map.setPaintProperty("ocm-ais-arrow", "text-halo-color", t.bg);
    map.setPaintProperty("ocm-ais-label", "text-color", t.dim);
    map.setPaintProperty("ocm-ais-label", "text-halo-color", t.bg);
    map.setPaintProperty("ocm-ais-sel", "circle-stroke-color", t.accent);
    map.setPaintProperty("ocm-ais-trail", "line-color", t.accent);
  }

  function setVisible(v) {
    for (const id of ["ocm-ais-dot", "ocm-ais-arrow", "ocm-ais-label", "ocm-ais-sel", "ocm-ais-trail"]) {
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", v ? "visible" : "none");
    }
  }

  /* ---- viewport → server ---------------------------------------- */
  function sendView() {
    clearTimeout(viewTimer);
    viewTimer = setTimeout(() => {
      if (!map) return;
      let body = { bbox: null };
      if (enabled) {
        const b = map.getBounds();
        body = { bbox: [[b.getSouth(), b.getWest()], [b.getNorth(), b.getEast()]] };
      }
      fetch("api/view", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      }).catch(() => {});
    }, 550);
  }

  /* ---- detail panel ------------------------------------------- */
  const F = (v, unit) => (v == null || v === "" ? "—" : unit ? `${v}${unit}` : v);
  function flagEmoji(cc) {
    if (!cc || cc.length !== 2) return "";
    return String.fromCodePoint(...[...cc.toUpperCase()].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
  }

  async function select(mmsi) {
    hub.dispatchEvent(new Event("dismiss"));   // release #detail from any other layer
    mmsi = Number(mmsi);
    selected = mmsi;
    map.setFilter("ocm-ais-sel", ["==", ["get", "mmsi"], mmsi]);
    if (trail.mmsi !== mmsi) { trail.mmsi = mmsi; trail.pts = []; }
    const v = vessels.get(mmsi);
    if (v) pushTrail(v);
    let full = v;
    try { full = await fetch(`api/vessel/${mmsi}`).then((r) => (r.ok ? r.json() : v)); } catch {}
    renderDetail(full || { mmsi });
  }

  function renderDetail(v) {
    const el = $("#detail");
    const rows = [
      ["MMSI", v.mmsi], ["IMO", F(v.imo)], ["Call sign", F(v.callsign)],
      ["Flag", v.flag ? `${flagEmoji(v.flag)} ${v.flag}` : "—"],
      ["Type", `${F(v.type_label)}${v.type != null ? ` (${v.type})` : ""}`],
      ["Nav status", F(v.navstat_label)],
      ["Speed", v.sog == null ? "—" : `${v.sog.toFixed(1)} kn`],
      ["Course", v.cog == null ? "—" : `${v.cog.toFixed(0)}°`],
      ["Heading", v.heading == null ? "—" : `${v.heading}°`],
      ["Destination", F(v.destination)], ["ETA", F(v.eta)],
      ["Draught", v.draught ? `${v.draught} m` : "—"],
      ["Size", v.length ? `${v.length} × ${F(v.beam)} m` : "—"],
      ["Position", v.lat == null ? "—" : `${v.lat.toFixed(4)}, ${v.lon.toFixed(4)}`],
      ["Last report", v.last ? new Date(v.last * 1000).toLocaleTimeString() : "—"],
    ];
    el.innerHTML =
      `<header><span class="nm">${v.name || "MMSI " + v.mmsi}</span>` +
      `<button id="detail-x" title="Esc">&times;</button></header>` +
      `<dl>${rows.map(([k, val]) => `<div><dt>${k}</dt><dd>${val}</dd></div>`).join("")}</dl>` +
      `<a class="ext" href="https://www.vesselfinder.com/?mmsi=${v.mmsi}" target="_blank" rel="noreferrer">vesselfinder ↗</a>`;
    el.hidden = false;
    $("#detail-x").onclick = deselect;
  }

  function deselect() {
    selected = null;
    if (map.getLayer("ocm-ais-sel")) map.setFilter("ocm-ais-sel", ["==", ["get", "mmsi"], -1]);
    trail.mmsi = null; trail.pts = [];
    const s = map.getSource(TRAIL); if (s) s.setData(trailFC());
    $("#detail").hidden = true;
  }

  function pushTrail(v) {
    if (v.mmsi !== trail.mmsi || v.lon == null) return;
    const last = trail.pts[trail.pts.length - 1];
    if (!last || last[0] !== v.lon || last[1] !== v.lat) {
      trail.pts.push([v.lon, v.lat]);
      if (trail.pts.length > 60) trail.pts.shift();
      const s = map.getSource(TRAIL); if (s) s.setData(trailFC());
    }
  }

  /* ---- badge ------------------------------------------------- */
  function badge(text, cls) {
    const b = $("#ais-badge");
    if (!text) { b.hidden = true; return; }
    b.textContent = text; b.className = "badge " + (cls || ""); b.hidden = false;
  }
  function armStale() {
    clearTimeout(staleTimer);
    staleTimer = setTimeout(() => { if (enabled) badge("AIS STALE", "warn"); }, 45000);
  }

  /* ---- events --------------------------------------------- */
  hub.addEventListener("map-ready", () => {
    map = window.OCM.map;
    addLayers();
    wireRail();
    sendView();
  });
  hub.addEventListener("view-changed", () => { if (map) sendView(); });
  hub.addEventListener("theme-applied", (e) => applyPaint(e.detail));
  hub.addEventListener("dismiss", deselect);

  hub.addEventListener("ais_upsert", (e) => {
    for (const v of e.detail.vessels) {
      vessels.set(v.mmsi, v);
      if (v.mmsi === trail.mmsi) pushTrail(v);
      if (v.mmsi === selected) renderDetail(v);
    }
    lastUpsert = Date.now();
    badge(null); armStale();
    $("#msg").textContent = `${vessels.size} vessels`;
    rebuild();
  });
  hub.addEventListener("ais_drop", (e) => {
    for (const m of e.detail.mmsi) vessels.delete(m);
    if (e.detail.mmsi.includes(selected)) deselect();
    rebuild();
  });
  hub.addEventListener("ais_status", (e) => {
    if (!enabled) return;
    if (e.detail.state === "zoomed_out") { badge("ZOOM IN TO LOAD SHIPS", ""); }
    else if (e.detail.state === "down") badge("AIS LINK DOWN", "err");
    else if (e.detail.state === "connecting") badge("AIS connecting…", "");
    else badge(null);
  });

  function wireRail() {
    const btn = document.querySelector('.layer[data-layer="ais"]');
    btn.disabled = false;
    btn.classList.toggle("on", enabled);
    btn.title = "Toggle AIS (viewport)";
    btn.addEventListener("click", () => {
      enabled = !enabled;
      btn.classList.toggle("on", enabled);
      setVisible(enabled);
      if (!enabled) { badge(null); deselect(); }
      sendView();
    });
  }
})();
