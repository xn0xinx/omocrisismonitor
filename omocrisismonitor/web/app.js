/* OmoCrisisMonitor — Phase 0 shell.
   - loads a MapTiler vector style, recolours it from the --ocm-* theme vars
   - reconnecting WebSocket to the server (snapshot / theme / ping)
   - clock, fullscreen, live view readout
   Data layers (AIS / conflict / fuel / AI / alerts) land in later phases and
   hang off `hub` + the #rail buttons. */

const $ = (s) => document.querySelector(s);
const hub = new EventTarget();

/* ---- theme -------------------------------------------------------------- */
function themeVars() {
  const cs = getComputedStyle(document.documentElement);
  const v = (n, d) => (cs.getPropertyValue(n).trim() || d);
  return {
    bg: v("--ocm-bg", "#16161e"),
    surface: v("--ocm-surface", "#1a1b26"),
    surface2: v("--ocm-surface-2", "#21232f"),
    border: v("--ocm-border", "#3b3f51"),
    fg: v("--ocm-fg", "#c0caf5"),
    dim: v("--ocm-dim", "#565f89"),
    accent: v("--ocm-accent", "#7aa2f7"),
    water: v("--ocm-surface-2", "#20233a"),
  };
}

/* Recolour a MapLibre style object in place from the theme. Heuristic by
   layer id / type — good enough for a dark ops basemap that tracks the OS. */
function paintStyle(style, t) {
  const isWater = (id) => /water|ocean|sea|bathymetry/i.test(id);
  const isLandGreen = (id) => /wood|forest|park|grass|landcover|vegetation/i.test(id);
  const isBoundary = (id) => /boundary|admin|border/i.test(id);
  const isRoad = (id) => /road|bridge|tunnel|transit|rail|street|highway|motorway/i.test(id);
  const isBuilding = (id) => /building/i.test(id);

  for (const l of style.layers || []) {
    l.paint = l.paint || {};
    l.layout = l.layout || {};
    if (l.type === "background") {
      l.paint["background-color"] = t.bg;
    } else if (l.type === "fill") {
      if (isWater(l.id)) l.paint["fill-color"] = t.water;
      else if (isLandGreen(l.id)) l.paint["fill-color"] = mix(t.surface, t.accent, 0.08);
      else if (isBuilding(l.id)) l.paint["fill-color"] = t.surface2;
      else l.paint["fill-color"] = t.surface;
      l.paint["fill-opacity"] = isBuilding(l.id) ? 0.5 : 0.9;
      if (l.paint["fill-outline-color"] !== undefined) l.paint["fill-outline-color"] = t.border;
    } else if (l.type === "line") {
      l.paint["line-color"] = isBoundary(l.id) ? t.dim : isRoad(l.id) ? t.border : t.border;
      if (isBoundary(l.id)) { l.paint["line-dasharray"] = [2, 2]; l.paint["line-opacity"] = 0.7; }
    } else if (l.type === "symbol") {
      l.paint["text-color"] = /water|marine|ocean/i.test(l.id) ? t.dim : t.fg;
      l.paint["text-halo-color"] = t.bg;
      l.paint["text-halo-width"] = 1.4;
      if (l.paint["icon-color"] !== undefined) l.paint["icon-color"] = t.dim;
    } else if (l.type === "fill-extrusion") {
      l.paint["fill-extrusion-color"] = t.surface2;
      l.paint["fill-extrusion-opacity"] = 0.4;
    }
  }
  return style;
}

function mix(a, b, amt) {
  const pa = hex(a), pb = hex(b);
  const c = pa.map((x, i) => Math.round(x * (1 - amt) + pb[i] * amt));
  return `#${c.map((x) => x.toString(16).padStart(2, "0")).join("")}`;
}
function hex(s) {
  const m = s.replace("#", "");
  const n = m.length === 3 ? m.split("").map((c) => c + c).join("") : m;
  return [0, 2, 4].map((i) => parseInt(n.slice(i, i + 2), 16) || 0);
}

/* ---- map -------------------------------------------------------------- */
let map, boot;

async function initMap() {
  boot = await fetch("api/bootstrap").then((r) => r.json()).catch(() => null);
  if (!boot) { $("#msg").textContent = "bootstrap failed"; return; }

  const { maptiler_key, style, center, zoom } = boot.map;
  if (!maptiler_key) {
    $("#msg").textContent = "no MapTiler key in config.toml — map disabled";
    return;
  }
  const styleUrl = `https://api.maptiler.com/maps/${style}/style.json?key=${maptiler_key}`;
  let styleObj;
  try {
    styleObj = await fetch(styleUrl).then((r) => r.json());
  } catch (e) {
    $("#msg").textContent = "MapTiler style fetch failed";
    return;
  }
  paintStyle(styleObj, themeVars());

  map = new maplibregl.Map({
    container: "map",
    style: styleObj,
    center, // [lng, lat]
    zoom,
    attributionControl: { compact: true },
    hash: false,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
  map.on("load", () => { $("#msg").textContent = "map ready"; $("#phase").textContent = "phase 0 · shell"; });
  map.on("moveend", updateViewInfo);
  map.on("error", (e) => console.warn("map:", e && e.error));
}

function reskinMap() {
  if (!map || !map.isStyleLoaded()) return;
  const t = themeVars();
  const s = paintStyle(map.getStyle(), t);
  map.setStyle(s, { diff: false });
}

function updateViewInfo() {
  if (!map) return;
  const c = map.getCenter();
  $("#viewinfo").textContent =
    `${c.lat.toFixed(2)}, ${c.lng.toFixed(2)}  z${map.getZoom().toFixed(1)}`;
}

/* ---- websocket ------------------------------------------------------- */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  const conn = $("#conn");
  const set = (state, text) => { conn.dataset.state = state; conn.querySelector("span").textContent = text; };

  set("wait", "link");
  ws.onopen = () => set("ok", "live");
  ws.onclose = () => { set("down", "down"); setTimeout(connect, 2000); };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === "theme") reloadTheme();
    else if (m.type === "ping") { /* keepalive */ }
    else hub.dispatchEvent(new CustomEvent(m.type, { detail: m }));
  };
}

function reloadTheme() {
  const link = document.querySelector('link[href^="theme.css"], link[href*="/theme.css"]');
  if (link) {
    const u = new URL(link.href, location.href);
    u.searchParams.set("v", Date.now());
    link.href = u.pathname + u.search;
  }
  // give the new sheet a tick to apply, then repaint the map
  setTimeout(reskinMap, 120);
  $("#msg").textContent = "theme reloaded";
}

/* ---- chrome -------------------------------------------------------- */
function clock() {
  const t = new Date();
  $("#clock").textContent = t.toTimeString().slice(0, 8);
}

function toggleFullscreen() {
  document.body.classList.toggle("fs");
  if (!document.fullscreenElement) document.documentElement.requestFullscreen?.().catch(() => {});
  else document.exitFullscreen?.();
}

document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea, select")) return;
  if (e.key === "f" || e.key === "F") toggleFullscreen();
});
$("#full").addEventListener("click", toggleFullscreen);
document.addEventListener("fullscreenchange", () => {
  if (!document.fullscreenElement) document.body.classList.remove("fs");
});

/* ---- go ----------------------------------------------------------- */
clock(); setInterval(clock, 1000);
connect();
initMap().then(() => { if (boot?.ui?.start_fullscreen) toggleFullscreen(); });
