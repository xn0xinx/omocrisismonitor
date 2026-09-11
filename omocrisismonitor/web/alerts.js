/* Alerts panel — watch-rule CRUD + recent hits + the correlation readout.
   Docks right, next to (not on top of) #detail. Attaches via window.OCM +
   the `hub` EventTarget. */
(function () {
  const { hub } = window.OCM;
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  let map = null;
  let enabled = true;
  let rules = [];
  let formKind = "conflict_region";
  let ruleBbox = null;      // captured via "use current view" for ship_box
  let corrBbox = null;
  let corr = null;

  const CATS = ["any", "cargo", "tanker", "passenger", "fishing", "special", "pleasure", "other"];

  function ago(ts) {
    const s = Date.now() / 1000 - ts;
    if (s < 60) return "just now";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }
  const bboxStr = (b) => b ? `[${b[0][0].toFixed(1)},${b[0][1].toFixed(1)}] – [${b[1][0].toFixed(1)},${b[1][1].toFixed(1)}]` : "no box selected";
  const curBox = () => {
    const b = map.getBounds();
    return [[b.getSouth(), b.getWest()], [b.getNorth(), b.getEast()]];
  };

  /* ---- rules -------------------------------------------------- */
  function describeRule(r) {
    const p = r.params;
    if (r.kind === "conflict_region") return `${p.conflict || bboxStr(p.bbox)} · +${p.min_new} new`;
    if (r.kind === "ship_box") return `${p.category} in box · ≥${p.min_count}`;
    if (r.kind === "fuel_threshold") return `${p.symbol.toUpperCase()} ${p.op} $${p.value}`;
    return r.kind;
  }

  async function loadRules() {
    try { rules = await fetch("api/alerts/rules").then((r) => r.json()); } catch { rules = []; }
    renderRules();
  }

  function renderRules() {
    const el = $("#alerts-rules");
    el.innerHTML = rules.length
      ? rules.map((r) => `
        <div class="arule ${r.enabled ? "" : "off"}" data-id="${r.id}">
          <span class="atoggle" title="toggle">${r.enabled ? "●" : "○"}</span>
          <span class="alabel">${esc(r.label || r.kind)}</span>
          <button class="adel" title="delete">&times;</button>
          <span class="adesc dim">${esc(describeRule(r))}</span>
        </div>`).join("")
      : `<p class="dim small">no watch rules yet</p>`;
    el.querySelectorAll(".arule").forEach((row) => {
      const id = row.dataset.id;
      row.querySelector(".atoggle").addEventListener("click", () => toggleRule(id, row));
      row.querySelector(".adel").addEventListener("click", () => deleteRule(id));
    });
  }

  async function toggleRule(id, row) {
    const turnOn = row.classList.contains("off");   // currently off -> turning on
    await fetch(`api/alerts/rules/${id}`, {
      method: "PATCH", headers: { "content-type": "application/json" },
      body: JSON.stringify({ enabled: turnOn }),
    }).catch(() => {});
    loadRules();
  }

  async function deleteRule(id) {
    await fetch(`api/alerts/rules/${id}`, { method: "DELETE" }).catch(() => {});
    loadRules();
  }

  function fieldsFor(kind) {
    if (kind === "conflict_region") {
      return `<input id="af-conflict" placeholder="theatre slug (ukraine, israel…)">
        <label class="dim small">new events ≥ <input id="af-minnew" type="number" min="1" value="1"></label>`;
    }
    if (kind === "ship_box") {
      return `<button id="af-usebox" type="button">use current view</button>
        <span id="af-bbox" class="dim small">${bboxStr(ruleBbox)}</span>
        <select id="af-cat">${CATS.map((c) => `<option value="${c}">${c}</option>`).join("")}</select>
        <label class="dim small">count ≥ <input id="af-mincount" type="number" min="1" value="1"></label>`;
    }
    return `<select id="af-symbol"><option value="wti">WTI</option><option value="brent">Brent</option></select>
      <select id="af-op"><option value=">">&gt;</option><option value="<">&lt;</option></select>
      <input id="af-value" type="number" step="0.5" placeholder="USD">`;
  }

  function renderForm() {
    $("#af-fields").innerHTML = fieldsFor(formKind);
    if (formKind === "ship_box") {
      $("#af-usebox").addEventListener("click", () => {
        ruleBbox = curBox();
        $("#af-bbox").textContent = bboxStr(ruleBbox);
      });
    }
  }

  async function saveRule() {
    const kind = formKind;
    let params = {};
    if (kind === "conflict_region") {
      params = { conflict: $("#af-conflict").value.trim() || null,
                min_new: +$("#af-minnew").value || 1 };
    } else if (kind === "ship_box") {
      if (!ruleBbox) { alert("click “use current view” first"); return; }
      params = { bbox: ruleBbox, category: $("#af-cat").value,
                min_count: +$("#af-mincount").value || 1 };
    } else {
      const value = parseFloat($("#af-value").value);
      if (Number.isNaN(value)) { alert("enter a USD value"); return; }
      params = { symbol: $("#af-symbol").value, op: $("#af-op").value, value };
    }
    const r = await fetch("api/alerts/rules", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ kind, params, label: labelFor(kind, params) }),
    });
    if (!r.ok) { alert("couldn’t save that rule (" + (await r.text()) + ")"); return; }
    ruleBbox = null;
    loadRules();
  }

  function labelFor(kind, p) {
    if (kind === "conflict_region") return p.conflict ? `${p.conflict} watch` : "region watch";
    if (kind === "ship_box") return `${p.category} box watch`;
    return `${p.symbol.toUpperCase()} ${p.op} $${p.value}`;
  }

  /* ---- hits ----------------------------------------------------- */
  async function loadHits() {
    let hits = [];
    try { hits = await fetch("api/alerts/hits?limit=20").then((r) => r.json()); } catch { /* keep empty */ }
    $("#alerts-hits").innerHTML = hits.length
      ? hits.map((h) => `<div class="ahit"><span class="alabel">${esc(h.label || h.kind)}</span>
          <span class="dim small">${ago(h.ts)}</span><br><span class="small">${esc(h.summary)}</span></div>`).join("")
      : `<p class="dim small">no hits yet</p>`;
  }

  /* ---- correlate -------------------------------------------- */
  function sparkOverlay(series) {
    const W = 280, H = 60, pad = 3;
    const bars = series.conflict, line = series.wti;
    const maxBar = Math.max(1, ...bars);
    const nums = line.filter((v) => v != null);
    const lo = nums.length ? Math.min(...nums) : 0, hi = nums.length ? Math.max(...nums) : 1;
    const span = hi - lo || 1;
    const n = bars.length;
    const bw = (W - 2 * pad) / n;
    const barsSvg = bars.map((v, i) => {
      const h = (v / maxBar) * (H - 2 * pad);
      return `<rect x="${(pad + i * bw).toFixed(1)}" y="${(H - pad - h).toFixed(1)}" width="${(bw * 0.7).toFixed(1)}" height="${h.toFixed(1)}" fill="var(--ocm-dim)" opacity="0.5"/>`;
    }).join("");
    const pts = line.map((v, i) => {
      if (v == null) return null;
      const x = pad + i * bw + bw / 2;
      const y = H - pad - ((v - lo) / span) * (H - 2 * pad);
      return [x, y];
    });
    let path = "";
    pts.forEach((p, i) => { if (p) path += `${path ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)} `; });
    return `<svg class="acspark" viewBox="0 0 ${W} ${H}">${barsSvg}<path d="${path}" fill="none" stroke="var(--ocm-accent)" stroke-width="1.4"/></svg>`;
  }

  function fmtR(r) { return r == null ? "—" : r.toFixed(2); }

  function renderCorr() {
    const el = $("#ac-result");
    if (!corr) { el.innerHTML = ""; return; }
    const c = corr.correlations;
    el.innerHTML =
      sparkOverlay(corr.series) +
      `<div class="acnums">
        <span>r(conflict, WTI) <b>${fmtR(c.conflict_vs_wti)}</b></span>
        <span>r(conflict, Brent) <b>${fmtR(c.conflict_vs_brent)}</b></span>
        <span>r(conflict, ships) <b>${fmtR(c.conflict_vs_ships)}</b></span>
      </div>` +
      corr.notes.map((n) => `<p class="dim small">${esc(n)}</p>`).join("");
  }

  async function runCorrelate() {
    if (!corrBbox) { alert("click “use current view” first"); return; }
    const [[s, w], [n, e]] = corrBbox;
    const days = $("#ac-days").value;
    try {
      corr = await fetch(`api/correlate?bbox=${s},${w},${n},${e}&days=${days}`).then((r) => r.json());
    } catch { corr = null; }
    renderCorr();
  }

  /* ---- shell ------------------------------------------------ */
  function build() {
    const el = $("#alerts-panel");
    el.innerHTML = `
      <header>ALERTS <span class="grow"></span><button id="alerts-close" title="Esc">&times;</button></header>
      <section>
        <div class="asec-title">WATCH RULES</div>
        <div id="alerts-rules"></div>
        <div id="alerts-form">
          <select id="af-kind">
            <option value="conflict_region">conflict in theatre/box</option>
            <option value="ship_box">ships in box</option>
            <option value="fuel_threshold">fuel threshold</option>
          </select>
          <div id="af-fields"></div>
          <button id="af-save">add rule</button>
        </div>
      </section>
      <section>
        <div class="asec-title">RECENT HITS</div>
        <div id="alerts-hits"></div>
      </section>
      <section>
        <div class="asec-title">CORRELATE</div>
        <div class="acorr-row">
          <button id="ac-usebox" type="button">use current view</button>
          <span id="ac-bbox" class="dim small">no box selected</span>
        </div>
        <div class="acorr-row">
          <select id="ac-days"><option value="30">30d</option><option value="90" selected>90d</option><option value="180">180d</option></select>
          <button id="ac-run">run</button>
        </div>
        <div id="ac-result"></div>
      </section>`;
    $("#alerts-close").addEventListener("click", () => setOpen(false));
    $("#af-kind").addEventListener("change", (e) => { formKind = e.target.value; renderForm(); });
    $("#af-save").addEventListener("click", saveRule);
    $("#ac-usebox").addEventListener("click", () => {
      corrBbox = curBox();
      $("#ac-bbox").textContent = bboxStr(corrBbox);
    });
    $("#ac-run").addEventListener("click", runCorrelate);
    renderForm();
  }

  function setOpen(v) {
    enabled = v;
    $("#alerts-panel").hidden = !v;
    const btn = document.querySelector('.layer[data-layer="alerts"]');
    if (btn) btn.classList.toggle("on", v);
    if (v) { loadRules(); loadHits(); }
  }

  function wireRail() {
    const btn = document.querySelector('.layer[data-layer="alerts"]');
    if (!btn) return;
    btn.disabled = false;
    btn.title = "Watch rules, hits & correlation";
    btn.addEventListener("click", () => setOpen($("#alerts-panel").hidden));
  }

  hub.addEventListener("map-ready", () => {
    map = window.OCM.map;
    build();
    wireRail();
  });
  hub.addEventListener("alert_hit", () => { if (enabled) loadHits(); });
})();
