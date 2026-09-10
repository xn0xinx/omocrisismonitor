/* Conflict layer (GeoConfirmed) — clustered pins coloured by faction, a click
   detail panel, and a date scrubber that winds the map back through history.
   Talks to the shell via window.OCM + the `hub` EventTarget. */
(function () {
  const { hub } = window.OCM;
  const $ = (s) => document.querySelector(s);

  let map = null;
  let enabled = true;
  let boot = null;                      // /api/conflict/bootstrap payload
  let span = null;                      // { min_sort, max_sort, today_sort, floor_sort, window_days }
  let untilSort = null;                 // right edge of the visible window (day int)
  let live = true;                      // untilSort pinned to "now"
  let selected = null;
  let fetchSeq = 0, scrubTimer = 0;

  const SRC = "ocm-conflict";
  const DAY_MS = 86400000;

  /* ---- day-int <-> date ------------------------------------------- */
  const sortToDate = (n) => new Date(n * DAY_MS);
  const fmtDay = (n) => sortToDate(n).toISOString().slice(0, 10);

  /* ---- data ------------------------------------------------------ */
  async function loadEvents() {
    if (!map || !enabled) return;
    const seq = ++fetchSeq;
    const u = live ? "" : `?until=${untilSort}`;
    let fc;
    try {
      fc = await fetch(`api/conflict/events${u}`).then((r) => r.json());
    } catch {
      badge("CONFLICT LINK DOWN", "err");
      return;
    }
    if (seq !== fetchSeq) return;        // a newer scrub superseded this one
    const src = map.getSource(SRC);
    if (src) src.setData(fc);
    const w = fc.window || {};
    badge(
      `${fc.features.length} events · ${fmtDay(w.since_sort)} → ${live ? "now" : fmtDay(w.until_sort)}`,
      live ? "" : "warn",
    );
  }

  /* ---- map layers --------------------------------------------- */
  function addLayers() {
    const t = window.OCM.themeVars();
    map.addSource(SRC, {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] },
      cluster: true,
      clusterRadius: 44,
      clusterMaxZoom: 9,
    });

    map.addLayer({
      id: "ocm-conflict-cluster", type: "circle", source: SRC,
      filter: ["has", "point_count"],
      paint: {
        "circle-color": t.surface2,
        "circle-stroke-color": t.accent,
        "circle-stroke-width": 1,
        "circle-opacity": 0.85,
        "circle-radius": ["step", ["get", "point_count"], 12, 25, 16, 100, 22, 500, 30],
      },
    });
    map.addLayer({
      id: "ocm-conflict-count", type: "symbol", source: SRC,
      filter: ["has", "point_count"],
      layout: {
        "text-field": ["get", "point_count_abbreviated"],
        "text-size": 11, "text-font": ["Noto Sans Regular"],
      },
      paint: { "text-color": t.fg, "text-halo-color": t.surface2, "text-halo-width": 1 },
    });
    map.addLayer({
      id: "ocm-conflict-dot", type: "circle", source: SRC,
      filter: ["!", ["has", "point_count"]],
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 3, 9, 5, 13, 7],
        "circle-color": ["coalesce", ["get", "color"], t.err],
        "circle-stroke-color": t.bg,
        "circle-stroke-width": 1,
        "circle-opacity": 0.9,
      },
    });
    map.addLayer({
      id: "ocm-conflict-sel", type: "circle", source: SRC,
      filter: ["==", ["get", "id"], ""],
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 7, 13, 13],
        "circle-color": "rgba(0,0,0,0)",
        "circle-stroke-color": t.accent, "circle-stroke-width": 2,
      },
    });

    map.on("click", "ocm-conflict-cluster", (e) => {
      const id = e.features[0].properties.cluster_id;
      map.getSource(SRC).getClusterExpansionZoom(id, (err, z) => {
        if (!err) map.easeTo({ center: e.features[0].geometry.coordinates, zoom: z });
      });
    });
    map.on("click", "ocm-conflict-dot", (e) => select(e.features[0].properties.id));
    for (const l of ["ocm-conflict-cluster", "ocm-conflict-dot"]) {
      map.on("mouseenter", l, () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", l, () => (map.getCanvas().style.cursor = ""));
    }
  }

  function applyPaint(t) {
    if (!map || !map.getLayer("ocm-conflict-dot")) return;
    map.setPaintProperty("ocm-conflict-cluster", "circle-color", t.surface2);
    map.setPaintProperty("ocm-conflict-cluster", "circle-stroke-color", t.accent);
    map.setPaintProperty("ocm-conflict-count", "text-color", t.fg);
    map.setPaintProperty("ocm-conflict-count", "text-halo-color", t.surface2);
    map.setPaintProperty("ocm-conflict-dot", "circle-color", ["coalesce", ["get", "color"], t.err]);
    map.setPaintProperty("ocm-conflict-dot", "circle-stroke-color", t.bg);
    map.setPaintProperty("ocm-conflict-sel", "circle-stroke-color", t.accent);
  }

  function setVisible(v) {
    for (const id of ["ocm-conflict-cluster", "ocm-conflict-count", "ocm-conflict-dot", "ocm-conflict-sel"]) {
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", v ? "visible" : "none");
    }
  }

  /* ---- detail panel -------------------------------------------- */
  const F = (v) => (v == null || v === "" ? "—" : v);
  const links = (blob) =>
    !blob ? "" : String(blob).split(/[\s,]+/).filter((u) => /^https?:/.test(u))
      .map((u) => `<a href="${u}" target="_blank" rel="noreferrer">${new URL(u).hostname.replace(/^www\./, "")} ↗</a>`)
      .join("<br>");

  async function select(id) {
    hub.dispatchEvent(new Event("dismiss"));   // release #detail from any other layer
    selected = id;
    map.setFilter("ocm-conflict-sel", ["==", ["get", "id"], id]);
    let d;
    try { d = await fetch(`api/conflict/detail/${id}`).then((r) => (r.ok ? r.json() : null)); } catch { d = null; }
    renderDetail(d || { id });
  }

  function renderDetail(d) {
    const el = $("#detail");
    const rows = [
      ["Date", F(d.date && d.date.slice(0, 10))],
      ["Faction", F(d.faction)],
      ["Origin", F(d.origin)],
      ["Location", d.latitude == null ? "—" : `${(+d.latitude).toFixed(4)}, ${(+d.longitude).toFixed(4)}`],
      ["Plus code", F(d.plusCode)],
    ];
    el.innerHTML =
      `<header><span class="nm">${F(d.name) === "—" ? "Event" : d.name}</span>` +
      `<button id="detail-x" title="Esc">&times;</button></header>` +
      (d.description ? `<p class="desc">${d.description}</p>` : "") +
      `<dl>${rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>` +
      (d.originalSource ? `<div class="srcgrp"><dt>Source</dt><dd>${links(d.originalSource)}</dd></div>` : "") +
      (d.geolocation ? `<div class="srcgrp"><dt>Geolocation</dt><dd>${links(d.geolocation)}</dd></div>` : "");
    el.hidden = false;
    $("#detail-x").onclick = deselect;
  }

  function deselect() {
    if (selected == null) return;
    selected = null;
    if (map.getLayer("ocm-conflict-sel")) map.setFilter("ocm-conflict-sel", ["==", ["get", "id"], ""]);
    $("#detail").hidden = true;
  }

  /* ---- badge + scrubber ------------------------------------- */
  function badge(text, cls) {
    const b = $("#conflict-badge");
    if (!text) { b.hidden = true; return; }
    b.textContent = text;
    b.className = "badge badge-2 " + (cls || "");
    b.hidden = false;
  }

  function buildScrubber() {
    const bar = $("#scrubber");
    const lo = span.floor_sort, hi = span.today_sort;
    bar.innerHTML =
      `<button id="scrub-live" title="jump to now">● LIVE</button>` +
      `<input id="scrub-range" type="range" min="${lo}" max="${hi}" step="1" value="${hi}">` +
      `<span id="scrub-date" class="mono"></span>` +
      `<span class="dim">${span.window_days}d window</span>`;
    untilSort = hi;
    const range = $("#scrub-range"), dateEl = $("#scrub-date");
    const paint = () => {
      dateEl.textContent = live ? "now" : fmtDay(untilSort);
      $("#scrub-live").classList.toggle("on", live);
      bar.classList.toggle("history", !live);
    };
    range.addEventListener("input", () => {
      untilSort = +range.value;
      live = untilSort >= hi;
      paint();
      clearTimeout(scrubTimer);
      scrubTimer = setTimeout(loadEvents, 180);
    });
    $("#scrub-live").addEventListener("click", () => {
      live = true; untilSort = hi; range.value = hi; paint(); loadEvents();
    });
    paint();
  }

  function setScrubberVisible(v) { $("#scrubber").hidden = !v; }

  /* ---- rail ------------------------------------------------- */
  function wireRail() {
    const btn = document.querySelector('.layer[data-layer="conflict"]');
    btn.disabled = false;
    btn.classList.toggle("on", enabled);
    btn.title = "Toggle conflict events";
    btn.addEventListener("click", () => {
      enabled = !enabled;
      btn.classList.toggle("on", enabled);
      setVisible(enabled);
      setScrubberVisible(enabled);
      if (!enabled) { badge(null); deselect(); }
      else loadEvents();
    });
  }

  /* ---- lifecycle ------------------------------------------ */
  async function init() {
    map = window.OCM.map;
    addLayers();
    wireRail();
    try {
      boot = await fetch("api/conflict/bootstrap").then((r) => r.json());
    } catch { badge("CONFLICT LINK DOWN", "err"); return; }
    span = boot.span || {};
    if (span.today_sort == null) {           // nothing ingested yet
      span = { floor_sort: 0, today_sort: Math.floor(Date.now() / DAY_MS),
               window_days: boot.window_days || 90 };
    }
    buildScrubber();
    setScrubberVisible(enabled);
    await loadEvents();
  }

  hub.addEventListener("map-ready", init);
  hub.addEventListener("theme-applied", (e) => applyPaint(e.detail));
  hub.addEventListener("dismiss", deselect);
  hub.addEventListener("conflict_refresh", (e) => {
    if (!enabled) return;
    if (live) loadEvents();
    if (e.detail && e.detail.span && e.detail.span.today_sort) {
      const hi = e.detail.span.today_sort;
      const r = document.querySelector("#scrub-range");
      if (r && +r.max !== hi) { r.max = hi; if (live) { r.value = hi; untilSort = hi; } }
    }
  });
  hub.addEventListener("conflict_status", (e) => {
    if (!enabled) return;
    const s = e.detail.state;
    if (s === "refreshing") badge("CONFLICT refreshing…", "");
    else if (s === "error") badge("CONFLICT source error", "warn");
  });
})();
