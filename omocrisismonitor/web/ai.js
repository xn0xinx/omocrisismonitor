/* AI sidebar — a terminal-style drawer that pulls up from the bottom.
   Runs server-side through the `claude` CLI; this module just triggers /
   displays. Attaches via window.OCM + the `hub` EventTarget. */
(function () {
  const { hub } = window.OCM;
  const $ = (s) => document.querySelector(s);

  let open = false;
  let thinking = false;
  let summary = null;      // { text, ts, model, reason, cost_usd }

  function ago(ts) {
    const s = Date.now() / 1000 - ts;
    if (s < 60) return "just now";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }

  function render() {
    const el = $("#ai-drawer");
    $("#ai-cursor").hidden = !thinking;
    $("#ai-refresh").disabled = thinking;
    let body;
    if (thinking && !summary) {
      body = "gathering conflict / shipping / fuel state and asking claude…";
    } else if (!summary) {
      body = "no synthesis yet — hit refresh, or wait for something notable to change.";
    } else {
      body = summary.text;
    }
    $("#ai-body").textContent = body;
    $("#ai-meta").textContent = summary
      ? `${summary.model} · ${summary.reason} · ${ago(summary.ts)}` +
        (summary.cost_usd ? ` · $${summary.cost_usd.toFixed(3)}` : "")
      : "";
    el.classList.toggle("thinking", thinking);
  }

  async function load() {
    try {
      const j = await fetch("api/ai/summary").then((r) => r.json());
      summary = j.summary || null;
      thinking = !!j.busy;
    } catch { /* stay on whatever we had */ }
    render();
  }

  async function requestRefresh() {
    if (thinking) return;
    thinking = true;
    render();
    try {
      await fetch("api/ai/refresh", { method: "POST" });
    } catch { thinking = false; render(); }
  }

  function setOpen(v) {
    open = v;
    $("#ai-drawer").hidden = !open;
    if (open && !summary && !thinking) requestRefresh();
  }

  function wireRail() {
    const btn = document.querySelector('.layer[data-layer="ai"]');
    if (!btn) return;
    btn.disabled = false;
    btn.title = "Toggle the AI synthesis drawer";
    btn.addEventListener("click", () => {
      setOpen(!open);
      btn.classList.toggle("on", open);
    });
    $("#ai-refresh").addEventListener("click", requestRefresh);
    $("#ai-close").addEventListener("click", () => { setOpen(false); btn.classList.remove("on"); });
  }

  function wireBackendPicker() {
    const sel = $("#ai-backend");
    const info = (window.OCM.boot && window.OCM.boot.ai) || {};
    const backends = info.backends || [{ key: "claude", label: "Claude", available: true }];
    sel.innerHTML = backends.map((b) =>
      `<option value="${b.key}" ${b.available ? "" : "disabled"}>${b.label}${b.available ? "" : " (unavailable)"}</option>`
    ).join("");
    sel.value = info.backend || "claude";
    sel.addEventListener("change", async () => {
      try {
        await fetch("api/ai/backend", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ key: sel.value }),
        });
        requestRefresh();          // show the switch actually took effect
      } catch { /* leave the dropdown as-is; next refresh will retry */ }
    });
  }

  hub.addEventListener("map-ready", async () => {
    wireRail();
    wireBackendPicker();
    await load();          // hydrate a prior summary without forcing a run
  });
  hub.addEventListener("ai_status", (e) => {
    thinking = e.detail.state === "thinking";
    render();
  });
  hub.addEventListener("ai_summary", (e) => {
    thinking = false;
    summary = { text: e.detail.text, ts: e.detail.ts, model: e.detail.model,
               reason: e.detail.reason, cost_usd: e.detail.cost_usd };
    render();
  });
  // note: `dismiss` is NOT wired here — ais.js/conflict.js fire it on every
  // marker click to hand off the shared #detail panel, which would otherwise
  // slam this drawer shut mid-read. Esc closes #detail via that event; the AI
  // drawer only closes via its own button or the rail toggle.
})();
