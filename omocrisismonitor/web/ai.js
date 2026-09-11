/* AI sidebar — a terminal-style drawer that pulls up from the bottom.
   Runs server-side through the `claude` CLI; this module just triggers /
   displays. Attaches via window.OCM + the `hub` EventTarget. */
(function () {
  const { hub } = window.OCM;
  const $ = (s) => document.querySelector(s);

  let map = null;
  let open = false;
  let thinking = false;
  let summary = null;      // { text, ts, model, reason, cost_usd, view }
  let errorMsg = null;     // set on a failed refresh/load; cleared once we hear back
  let watchdog = 0;        // client-side backstop — see requestRefresh()

  /* has the map moved meaningfully away from the region the current summary
     covers? Manual-refresh-only by design (see SKILL/CHANGELOG) — this never
     triggers a claude call on its own, it just surfaces a hint. */
  function viewChanged() {
    if (!map || !summary) return false;
    const b = map.getBounds();
    const cur = [[b.getSouth(), b.getWest()], [b.getNorth(), b.getEast()]];
    if (!summary.view) return true;              // was global, now we have a specific view
    const [[s1, w1], [n1, e1]] = summary.view;
    const [[s2, w2], [n2, e2]] = cur;
    const overlaps = s2 < n1 && n2 > s1 && w2 < e1 && e2 > w1;
    return !overlaps;
  }

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
    if (errorMsg) {
      body = errorMsg;
    } else if (thinking && !summary) {
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
    $("#ai-hint").hidden = thinking || !viewChanged();
  }

  async function load() {
    try {
      const r = await fetch("api/ai/summary");
      if (!r.ok) {
        errorMsg = r.status === 404
          ? "the AI sidebar isn't on this running server — it may predate this build; relaunch omocrisismonitor."
          : `couldn't load the AI summary (${r.status}).`;
        return;
      }
      const j = await r.json();
      summary = j.summary || null;
      thinking = !!j.busy;
    } catch { /* offline for a moment — stay on whatever we had */ } finally {
      render();
    }
  }

  async function requestRefresh() {
    if (thinking) return;
    thinking = true;
    errorMsg = null;
    render();
    // a stuck "thinking…" forever (e.g. a stale server, or the WS drops
    // right as the reply lands) is worse than a wrong-but-recoverable error —
    // this backstop always resolves it, even if the request "succeeds" at
    // the HTTP level but nothing ever calls back.
    clearTimeout(watchdog);
    watchdog = setTimeout(() => {
      thinking = false;
      errorMsg = "no response after 2 minutes — the AI CLI may be stuck, or this server predates this feature. Try again, or restart the app.";
      render();
    }, 130000);
    try {
      const r = await fetch("api/ai/refresh", { method: "POST" });
      if (!r.ok) {
        clearTimeout(watchdog);
        thinking = false;
        let detail = "";
        try { detail = (await r.json()).detail || ""; } catch { /* not JSON */ }
        errorMsg = r.status === 404
          ? "the AI sidebar isn't on this running server — it may predate this build; relaunch omocrisismonitor."
          : `refresh failed (${r.status})${detail ? ": " + detail : ""}.`;
        render();
      }
    } catch {
      clearTimeout(watchdog);
      thinking = false;
      errorMsg = "couldn't reach the server.";
      render();
    }
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
    $("#ai-hint").addEventListener("click", requestRefresh);
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
    map = window.OCM.map;
    wireRail();
    wireBackendPicker();
    await load();          // hydrate a prior summary without forcing a run
  });
  hub.addEventListener("view-changed", () => render());   // recompute the region hint only
  hub.addEventListener("ai_status", (e) => {
    thinking = e.detail.state === "thinking";
    if (e.detail.state === "error") {
      clearTimeout(watchdog);
      errorMsg = e.detail.detail || "the AI backend reported an error.";
    } else if (thinking) {
      errorMsg = null;      // a fresh run superseded whatever we were showing
    }
    render();
  });
  hub.addEventListener("ai_summary", (e) => {
    clearTimeout(watchdog);
    thinking = false;
    errorMsg = null;
    summary = { text: e.detail.text, ts: e.detail.ts, model: e.detail.model,
               reason: e.detail.reason, cost_usd: e.detail.cost_usd, view: e.detail.view };
    render();
  });
  // note: `dismiss` is NOT wired here — ais.js/conflict.js fire it on every
  // marker click to hand off the shared #detail panel, which would otherwise
  // slam this drawer shut mid-read. Esc closes #detail via that event; the AI
  // drawer only closes via its own button or the rail toggle.
})();
