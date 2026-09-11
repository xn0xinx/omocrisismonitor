/* OmoCrisisMonitor — shell + map.
   - loads a MapTiler vector style, recolours it from the --ocm-* theme vars
   - reconnecting WebSocket (snapshot / theme / ping + per-layer events)
   - clock, fullscreen, live view readout
   Layer modules (ais.js, …) attach via window.OCM + the `hub` EventTarget. */

const $ = (s) => document.querySelector(s);
const hub = new EventTarget();

/* ---- theme ------------------------------------------------------------- */
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
    ok: v("--ocm-ok", "#9ece6a"),
    warn: v("--ocm-warn", "#e0af68"),
    err: v("--ocm-err", "#f7768e"),
    info: v("--ocm-info", "#7dcfff"),
    magenta: v("--ocm-magenta", "#bb9af7"),
    orange: v("--ocm-orange", "#ff9e64"),
    water: v("--ocm-surface-2", "#20233a"),
  };
}

function mix(a, b, amt) {
  const pa = hex(a), pb = hex(b);
  const c = pa.map((x, i) => Math.round(x * (1 - amt) + pb[i] * amt));
  return `#${c.map((x) => x.toString(16).padStart(2, "0")).join("")}`;
}
function hex(s) {
  const m = String(s).replace("#", "");
  const n = m.length === 3 ? m.split("").map((c) => c + c).join("") : m;
  return [0, 2, 4].map((i) => parseInt(n.slice(i, i + 2), 16) || 0);
}

/* classify a basemap layer for recolouring */
function classify(id) {
  if (/water|ocean|sea|bathymetry/i.test(id)) return "water";
  if (/wood|forest|park|grass|landcover|vegetation/i.test(id)) return "green";
  if (/building/i.test(id)) return "building";
  if (/boundary|admin|border/i.test(id)) return "boundary";
  if (/road|bridge|tunnel|transit|rail|street|highway|motorway/i.test(id)) return "road";
  return "land";
}

/* colours for one basemap layer, given its spec + theme */
function layerPaint(l, t) {
  const k = classify(l.id);
  if (l.type === "background") return { "background-color": t.bg };
  if (l.type === "fill") {
    const color = k === "water" ? t.water
      : k === "green" ? mix(t.surface, t.ok, 0.08)
      : k === "building" ? t.surface2
      : t.surface;
    const p = { "fill-color": color, "fill-opacity": k === "building" ? 0.5 : 0.9 };
    if ("fill-outline-color" in (l.paint || {})) p["fill-outline-color"] = t.border;
    return p;
  }
  if (l.type === "line") {
    const p = { "line-color": k === "boundary" ? t.dim : t.border };
    if (k === "boundary") { p["line-dasharray"] = [2, 2]; p["line-opacity"] = 0.7; }
    return p;
  }
  if (l.type === "symbol") {
    return {
      "text-color": /water|marine|ocean/i.test(l.id) ? t.dim : t.fg,
      "text-halo-color": t.bg,
      "text-halo-width": 1.4,
    };
  }
  if (l.type === "fill-extrusion") {
    return { "fill-extrusion-color": t.surface2, "fill-extrusion-opacity": 0.4 };
  }
  return {};
}

function bakeStyle(style, t) {
  for (const l of style.layers || []) {
    l.paint = { ...(l.paint || {}), ...layerPaint(l, t) };
  }
  return style;
}

/* live re-tint without setStyle (keeps custom layers/sources intact) */
function reskinMap() {
  if (!map || !map.isStyleLoaded()) return;
  const t = themeVars();
  for (const l of map.getStyle().layers) {
    if (l.id.startsWith("ocm-")) continue; // owned by layer modules
    const p = layerPaint(l, t);
    for (const [prop, val] of Object.entries(p)) {
      try { map.setPaintProperty(l.id, prop, val); } catch (e) { /* prop n/a */ }
    }
  }
  hub.dispatchEvent(new CustomEvent("theme-applied", { detail: t }));
}

/* ---- map ------------------------------------------------------------- */
let map, boot;

async function initMap() {
  boot = await fetch("api/bootstrap").then((r) => r.json()).catch(() => null);
  if (!boot) { $("#msg").textContent = "bootstrap failed"; return; }

  const { maptiler_key, style, center, zoom } = boot.map;
  if (!maptiler_key) { $("#msg").textContent = "no MapTiler key in config.toml — map disabled"; return; }

  let styleObj;
  try {
    styleObj = await fetch(
      `https://api.maptiler.com/maps/${style}/style.json?key=${maptiler_key}`
    ).then((r) => r.json());
  } catch (e) { $("#msg").textContent = "MapTiler style fetch failed"; return; }
  bakeStyle(styleObj, themeVars());

  map = new maplibregl.Map({
    container: "map", style: styleObj, center, zoom,
    attributionControl: { compact: true }, hash: false,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
  map.on("load", () => {
    $("#msg").textContent = "map ready";
    updateViewInfo();
    hub.dispatchEvent(new Event("map-ready"));
    sendCurrentView();
  });
  map.on("moveend", () => {
    updateViewInfo();
    hub.dispatchEvent(new Event("view-changed"));
    sendCurrentView();
  });
  map.on("error", (e) => console.warn("map:", e && e.error && e.error.message));
}

function updateViewInfo() {
  if (!map) return;
  const c = map.getCenter();
  $("#viewinfo").textContent = `${c.lat.toFixed(2)}, ${c.lng.toFixed(2)}  z${map.getZoom().toFixed(1)}`;
}

/* ---- "what is the user looking at", independent of any layer's on/off
   state (ais.js's /api/view is separate — that one drives the aisstream
   subscription and stays gated by the AIS toggle so we don't open a socket
   just to answer this). Consumed today by the AI sidebar. */
let viewTimer = 0;
function sendCurrentView() {
  clearTimeout(viewTimer);
  viewTimer = setTimeout(() => {
    if (!map) return;
    const b = map.getBounds();
    fetch("api/view/current", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ bbox: [[b.getSouth(), b.getWest()], [b.getNorth(), b.getEast()]] }),
    }).catch(() => {});
  }, 550);
}

/* ---- websocket ----------------------------------------------------- */
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
    else if (m.type === "ping" || m.type === "snapshot") { /* noop */ }
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
  setTimeout(reskinMap, 140);
  $("#msg").textContent = "theme reloaded";
}

/* ---- chrome ------------------------------------------------------ */
function clock() { $("#clock").textContent = new Date().toTimeString().slice(0, 8); }

function toggleFullscreen() {
  document.body.classList.toggle("fs");
  if (!document.fullscreenElement) document.documentElement.requestFullscreen?.().catch(() => {});
  else document.exitFullscreen?.();
}
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea, select")) return;
  if (e.key === "f" || e.key === "F") toggleFullscreen();
  if (e.key === "Escape") hub.dispatchEvent(new Event("dismiss"));
});
$("#full").addEventListener("click", toggleFullscreen);
document.addEventListener("fullscreenchange", () => {
  if (!document.fullscreenElement) document.body.classList.remove("fs");
});

/* ---- expose for layer modules -------------------------------------- */
window.OCM = {
  get map() { return map; },
  get boot() { return boot; },
  hub, themeVars, mix,
};

/* ---- go ---------------------------------------------------------- */
clock(); setInterval(clock, 1000);
connect();
initMap().then(() => { if (boot?.ui?.start_fullscreen) toggleFullscreen(); });
