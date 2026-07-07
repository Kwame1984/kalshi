/* Kalshi Ops dashboard — vanilla JS, no dependencies, refreshes every 5 s. */
"use strict";

const REFRESH_MS = 5000;
const $ = (id) => document.getElementById(id);

const state = {
  equityMode: "paper",
  watchStatus: "active",
  serverOffsetMs: 0,       // server_time - client now
  lastHeartbeat: null,     // ISO string from bot_status
  killOn: false,
  equityPoints: [],        // parsed [{t: ms, equity, cash, exposure}]
};

/* ---------------- utils ---------------- */

async function getJSON(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

function parseTs(v) {
  if (v == null) return null;
  if (typeof v === "number") return v > 1e12 ? v : v * 1000; // s vs ms epoch
  let t = Date.parse(v);
  if (!Number.isNaN(t)) return t;
  t = Date.parse(v + "Z"); // naive ISO -> assume UTC
  return Number.isNaN(t) ? null : t;
}

const fmt = {
  usd(v, digits = 2) {
    if (v == null || Number.isNaN(v)) return "—";
    const sign = v < 0 ? "-" : "";
    return `${sign}$${Math.abs(v).toFixed(digits)}`;
  },
  signedUsd(v) {
    if (v == null || Number.isNaN(v)) return "—";
    return `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(2)}`;
  },
  cents(v) { return v == null ? "—" : `${Math.round(v * 100)}¢`; },
  time(ms) {
    if (ms == null) return "—";
    return new Date(ms).toLocaleTimeString([], { hour12: false });
  },
  dateTime(ms) {
    if (ms == null) return "—";
    const d = new Date(ms);
    return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${fmt.time(ms)}`;
  },
  age(msAgo) {
    if (msAgo == null) return "—";
    const s = Math.max(0, Math.round(msAgo / 1000));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  },
  countdown(ms) {
    if (ms == null) return "—";
    const s = Math.round(ms / 1000);
    if (s <= 0) return "closed";
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  },
};

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function setRows(tbodyTable, emptyId, rows, build) {
  const tbody = $(tbodyTable).querySelector("tbody");
  tbody.replaceChildren(...rows.map(build));
  $(emptyId).hidden = rows.length > 0;
  $(tbodyTable).parentElement.style.display = rows.length ? "" : "none";
}

function sideCell(side, contracts) {
  // contracts may be signed (yes-contracts) or unsigned with explicit side
  let s = side, n = contracts;
  if (!s && typeof n === "number") { s = n >= 0 ? "yes" : "no"; n = Math.abs(n); }
  else if (typeof n === "number") { n = Math.abs(n); }
  const td = el("td");
  const tag = el("span", s === "yes" ? "side-yes" : "side-no", (s || "?").toUpperCase());
  td.append(tag);
  if (n != null) td.append(` ×${n}`);
  return td;
}

/* ---------------- status rail ---------------- */

function renderStatus(data) {
  const st = data.status || {};
  const mode = st.mode || data.trading_mode || "paper";

  const badge = $("mode-badge");
  badge.textContent = mode.toUpperCase();
  badge.className = `badge ${mode}`;

  const running = st.engine_running === "true" || st.engine_running === "1" || st.engine_running === "yes";
  const engine = $("engine-state");
  engine.textContent = running ? "RUNNING" : "STOPPED";
  engine.className = `cell-value ${running ? "ok" : "dim"}`;

  state.serverOffsetMs = parseTs(data.server_time) - Date.now();
  state.lastHeartbeat = st.last_heartbeat || null;
  renderHeartbeat();

  const pnl = st.daily_pnl != null ? Number(st.daily_pnl) : null;
  const pnlEl = $("daily-pnl");
  pnlEl.textContent = pnl == null || Number.isNaN(pnl) ? "—" : fmt.signedUsd(pnl);
  pnlEl.className = `cell-value num ${pnl > 0 ? "ok" : pnl < 0 ? "bad" : "dim"}`;

  state.killOn = st.kill_switch === "on";
  const haltCell = $("halt-cell");
  if (state.killOn || (st.halt_reason && st.halt_reason !== "")) {
    haltCell.hidden = false;
    $("halt-reason").textContent = st.halt_reason || "kill switch on";
  } else {
    haltCell.hidden = true;
  }
  renderKillButton();
}

function renderHeartbeat() {
  const hb = parseTs(state.lastHeartbeat);
  const now = Date.now() + state.serverOffsetMs;
  const age = hb != null ? now - hb : null;
  $("heartbeat-age").textContent = age != null ? `${fmt.age(age)} ago` : "never";
  const pulse = $("pulse");
  pulse.className = "pulse " + (age == null ? "" : age < 30000 ? "alive" : age < 180000 ? "stale" : "dead");
}

/* ---------------- kill switch ---------------- */

let armTimer = null;

function renderKillButton() {
  const btn = $("kill-btn");
  if (btn.dataset.state === "arm") return; // don't clobber mid-confirm
  if (state.killOn) {
    btn.dataset.state = "engaged";
    $("kill-text").textContent = "KILLED — RESUME?";
  } else {
    btn.dataset.state = "idle";
    $("kill-text").textContent = "KILL SWITCH";
  }
}

$("kill-btn").addEventListener("click", async () => {
  const btn = $("kill-btn");
  if (btn.dataset.state === "arm") {
    clearTimeout(armTimer);
    const turnOn = btn.dataset.pending === "on";
    btn.dataset.state = "busy";
    $("kill-text").textContent = "…";
    try {
      const res = await fetch("/api/kill_switch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ on: turnOn }),
      });
      if (!res.ok) throw new Error(res.status);
      state.killOn = turnOn;
    } catch (e) {
      console.error("kill switch failed", e);
    }
    btn.dataset.state = state.killOn ? "engaged" : "idle";
    renderKillButton();
    refresh();
    return;
  }
  // first click: arm and require confirmation
  btn.dataset.pending = state.killOn ? "off" : "on";
  btn.dataset.state = "arm";
  $("kill-text").textContent = state.killOn ? "CONFIRM RESUME" : "CONFIRM KILL";
  clearTimeout(armTimer);
  armTimer = setTimeout(() => { btn.dataset.state = "idle"; renderKillButton(); }, 4000);
});

/* ---------------- equity chart ---------------- */

const CHART = { padL: 58, padR: 12, padT: 10, padB: 22 };

function renderEquity(rows) {
  const wrap = $("equity-chart-wrap");
  const svg = $("equity-chart");
  const tip = $("equity-tip");
  tip.hidden = true;

  const pts = rows
    .map((r) => ({ t: parseTs(r.ts), equity: r.equity, cash: r.cash, exposure: r.exposure }))
    .filter((p) => p.t != null && p.equity != null);
  state.equityPoints = pts;

  const empty = $("equity-empty");
  if (pts.length < 2) {
    svg.replaceChildren();
    empty.hidden = false;
    $("equity-latest").textContent = pts.length === 1 ? fmt.usd(pts[0].equity) : "";
    return;
  }
  empty.hidden = true;

  const W = Math.max(wrap.clientWidth - 8, 320);
  const H = wrap.clientHeight - 8;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const t0 = pts[0].t, t1 = pts[pts.length - 1].t;
  let lo = Math.min(...pts.map((p) => p.equity));
  let hi = Math.max(...pts.map((p) => p.equity));
  if (hi === lo) { hi += 1; lo -= 1; }
  const padY = (hi - lo) * 0.08;
  lo -= padY; hi += padY;

  const x = (t) => CHART.padL + ((t - t0) / (t1 - t0 || 1)) * (W - CHART.padL - CHART.padR);
  const y = (v) => CHART.padT + (1 - (v - lo) / (hi - lo)) * (H - CHART.padT - CHART.padB);

  const NS = "http://www.w3.org/2000/svg";
  const mk = (tag, attrs) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };
  const parts = [];

  // recessive horizontal grid + y labels (4 ticks)
  for (let i = 0; i <= 3; i++) {
    const v = lo + ((hi - lo) * i) / 3;
    const yy = y(v);
    parts.push(mk("line", { x1: CHART.padL, x2: W - CHART.padR, y1: yy, y2: yy, stroke: "#24304A", "stroke-width": 1 }));
    const lbl = mk("text", { x: CHART.padL - 8, y: yy + 3.5, "text-anchor": "end", fill: "#566178", "font-size": 10, "font-family": "ui-monospace, monospace" });
    lbl.textContent = `$${v.toFixed(v >= 1000 ? 0 : 2)}`;
    parts.push(lbl);
  }
  // x labels: first / mid / last
  for (const t of [t0, (t0 + t1) / 2, t1]) {
    const lbl = mk("text", { x: x(t), y: H - 6, "text-anchor": "middle", fill: "#566178", "font-size": 10, "font-family": "ui-monospace, monospace" });
    lbl.textContent = fmt.dateTime(t);
    parts.push(lbl);
  }

  const lineD = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.equity).toFixed(1)}`).join("");
  const baseY = y(lo);
  parts.push(mk("path", {
    d: `${lineD}L${x(t1).toFixed(1)},${baseY}L${x(t0).toFixed(1)},${baseY}Z`,
    fill: "rgba(62,140,232,0.10)", stroke: "none",
  }));
  parts.push(mk("path", { d: lineD, fill: "none", stroke: "#3E8CE8", "stroke-width": 2, "stroke-linejoin": "round" }));

  // crosshair elements (hidden until hover)
  const cross = mk("line", { x1: 0, x2: 0, y1: CHART.padT, y2: H - CHART.padB, stroke: "#566178", "stroke-width": 1, "stroke-dasharray": "3,3", visibility: "hidden" });
  const dot = mk("circle", { r: 4, fill: "#3E8CE8", stroke: "#151D2C", "stroke-width": 2, visibility: "hidden" });
  parts.push(cross, dot);
  svg.replaceChildren(...parts);

  $("equity-latest").textContent = fmt.usd(pts[pts.length - 1].equity);

  svg.onmousemove = (ev) => {
    const rect = svg.getBoundingClientRect();
    const mx = ((ev.clientX - rect.left) / rect.width) * W;
    const tAt = t0 + ((mx - CHART.padL) / (W - CHART.padL - CHART.padR)) * (t1 - t0);
    let best = pts[0];
    for (const p of pts) if (Math.abs(p.t - tAt) < Math.abs(best.t - tAt)) best = p;
    const bx = x(best.t), by = y(best.equity);
    cross.setAttribute("x1", bx); cross.setAttribute("x2", bx);
    cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", bx); dot.setAttribute("cy", by);
    dot.setAttribute("visibility", "visible");
    tip.innerHTML = `<span class="t">${fmt.dateTime(best.t)}</span>` +
      `<span class="v">${fmt.usd(best.equity)}</span> ` +
      `<span class="s">cash ${fmt.usd(best.cash)} · exp ${fmt.usd(best.exposure)}</span>`;
    tip.hidden = false;
    const wrapRect = wrap.getBoundingClientRect();
    const px = (bx / W) * rect.width + (rect.left - wrapRect.left);
    tip.style.left = `${Math.min(px + 12, wrapRect.width - tip.offsetWidth - 6)}px`;
    tip.style.top = `${Math.max((by / H) * rect.height - 40, 2)}px`;
  };
  svg.onmouseleave = () => {
    tip.hidden = true;
    cross.setAttribute("visibility", "hidden");
    dot.setAttribute("visibility", "hidden");
  };
}

/* ---------------- tables ---------------- */

function renderPositions(rows) {
  let total = 0, any = false;
  setRows("positions-table", "positions-empty", rows, (p) => {
    const tr = el("tr");
    const tick = el("td", "tick", p.ticker);
    tick.title = p.title || p.ticker;
    tr.append(tick, sideCell(null, p.contracts));
    tr.append(Object.assign(el("td", "r"), { textContent: fmt.cents(p.avg_price) }));
    tr.append(Object.assign(el("td", "r"), { textContent: fmt.cents(p.last_price) }));
    const pnl = p.unrealized_pnl;
    const td = el("td", `r ${pnl > 0 ? "pnl-pos" : pnl < 0 ? "pnl-neg" : "dim"}`, pnl == null ? "—" : fmt.signedUsd(pnl));
    tr.append(td);
    if (pnl != null) { total += pnl; any = true; }
    return tr;
  });
  $("pos-total").textContent = any ? fmt.signedUsd(total) : "";
  $("pos-total").className = `panel-stat num ${total > 0 ? "pnl-pos" : total < 0 ? "pnl-neg" : ""}`;
}

function renderFills(rows) {
  setRows("fills-table", "fills-empty", rows, (f) => {
    const tr = el("tr");
    tr.append(el("td", "dim", fmt.dateTime(parseTs(f.ts))));
    tr.append(el("td", "tick", f.ticker));
    tr.append(sideCell(f.side, null));
    tr.append(el("td", "r", f.contracts != null ? String(Math.abs(f.contracts)) : "—"));
    tr.append(el("td", "r", fmt.cents(f.price)));
    tr.append(el("td", "r dim", f.fee != null ? fmt.usd(f.fee) : "—"));
    tr.append(el("td", "dim", f.strategy || ""));
    return tr;
  });
}

function renderSignals(rows) {
  setRows("signals-table", "signals-empty", rows, (s) => {
    const tr = el("tr");
    tr.append(el("td", "dim", fmt.dateTime(parseTs(s.ts))));
    tr.append(el("td", "tick", s.ticker));
    tr.append(sideCell(s.side, null));
    tr.append(el("td", "r", fmt.cents(s.fair_value)));
    tr.append(el("td", "r", fmt.cents(s.market_price)));
    const edge = s.fair_value != null && s.market_price != null ? s.fair_value - s.market_price : null;
    tr.append(el("td", `r ${edge > 0 ? "pnl-pos" : edge < 0 ? "pnl-neg" : "dim"}`,
      edge == null ? "—" : `${edge >= 0 ? "+" : ""}${Math.round(edge * 100)}¢`));
    const acted = s.acted === 1 || s.acted === true || s.acted === "1";
    tr.append(el("td", acted ? "acted-y" : "acted-n", acted ? "● acted" : "○ skip"));
    const r = el("td", "reason", s.reason || "");
    r.title = s.reason || "";
    tr.append(r);
    return tr;
  });
}

/* ---------------- watchlist ---------------- */

function renderWatchlist(rows) {
  const root = $("watchlist");
  const now = Date.now() + state.serverOffsetMs;
  const bySeries = new Map();
  for (const m of rows) {
    const key = m.series_ticker || "—";
    if (!bySeries.has(key)) bySeries.set(key, []);
    bySeries.get(key).push(m);
  }
  const groups = [];
  for (const [series, mkts] of bySeries) {
    mkts.sort((a, b) => (parseTs(a.close_time) || 0) - (parseTs(b.close_time) || 0) || a.ticker.localeCompare(b.ticker));
    const g = el("div", "series-group");
    const head = el("div", "series-head");
    head.append(el("span", "st", series), el("span", "sc", `${mkts.length} market${mkts.length === 1 ? "" : "s"}`));
    g.append(head);
    for (const m of mkts.slice(0, 12)) {
      const row = el("div", "mkt-row");
      const t = el("span", "mt", m.ticker);
      t.title = (m.title || "").replace(/\*\*/g, "");
      const closeMs = parseTs(m.close_time);
      const left = closeMs != null ? closeMs - now : null;
      const cd = el("span", "mc" + (left != null && left < 6 * 3600 * 1000 && left > 0 ? " soon" : ""),
        left == null ? "—" : fmt.countdown(left));
      row.append(t, el("span", "ms", m.subtitle || ""), el("span", "mp", fmt.cents(m.last_price)), cd);
      g.append(row);
    }
    if (mkts.length > 12) g.append(el("div", "mkt-row dim", `… ${mkts.length - 12} more`));
    groups.push(g);
  }
  root.replaceChildren(...groups);
  $("watchlist-empty").hidden = rows.length > 0;
}

/* ---------------- backtests ---------------- */

function renderBacktests(items) {
  const panel = $("backtests-panel");
  panel.hidden = items.length === 0;
  if (!items.length) return;
  const root = $("backtests");
  root.replaceChildren(...items.map(({ name, result }) => {
    const card = el("div", "bt-card");
    card.append(el("h3", null, name));
    const metrics = result && typeof result === "object" ? (result.metrics || result) : {};
    const dl = el("dl");
    for (const [k, v] of Object.entries(metrics)) {
      if (typeof v === "object") continue;
      dl.append(el("dt", null, k.replace(/_/g, " ")));
      dl.append(el("dd", null, typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(3)) : String(v)));
    }
    card.append(dl);
    return card;
  }));
}

/* ---------------- controls ---------------- */

$("equity-modes").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-mode]");
  if (!btn) return;
  state.equityMode = btn.dataset.mode;
  for (const b of $("equity-modes").querySelectorAll("button")) b.classList.toggle("on", b === btn);
  refresh();
});

$("watch-status").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-status]");
  if (!btn) return;
  state.watchStatus = btn.dataset.status;
  for (const b of $("watch-status").querySelectorAll("button")) b.classList.toggle("on", b === btn);
  refresh();
});

/* ---------------- refresh loop ---------------- */

let firstLoad = true;

async function refresh() {
  const conn = $("conn-state");
  try {
    const [status, equity, positions, fills, signals, markets, backtests] = await Promise.all([
      getJSON("/api/status"),
      getJSON(`/api/equity?mode=${encodeURIComponent(state.equityMode)}`),
      getJSON("/api/positions"),
      getJSON("/api/fills?limit=40"),
      getJSON("/api/signals?limit=40"),
      getJSON(`/api/markets${state.watchStatus ? `?status=${encodeURIComponent(state.watchStatus)}` : ""}`),
      getJSON("/api/backtests"),
    ]);
    renderStatus(status);
    if (firstLoad) {
      // default the equity tab to the engine's actual mode
      const mode = (status.status && status.status.mode) || status.trading_mode;
      const btn = document.querySelector(`#equity-modes button[data-mode="${mode}"]`);
      if (btn && state.equityMode !== mode) { btn.click(); firstLoad = false; return; }
      firstLoad = false;
    }
    renderEquity(equity.equity || []);
    renderPositions(positions.positions || []);
    renderFills(fills.fills || []);
    renderSignals(signals.signals || []);
    renderWatchlist(markets.markets || []);
    renderBacktests(backtests.backtests || []);
    conn.textContent = `live · updated ${new Date().toLocaleTimeString([], { hour12: false })}`;
    conn.className = "conn ok";
    $("refresh-note").textContent = `refreshes every ${REFRESH_MS / 1000}s`;
  } catch (err) {
    conn.textContent = `disconnected — ${err.message}`;
    conn.className = "conn bad";
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
setInterval(renderHeartbeat, 1000); // heartbeat age ticks between fetches
window.addEventListener("resize", () => renderEquity(
  state.equityPoints.map((p) => ({ ts: p.t, equity: p.equity, cash: p.cash, exposure: p.exposure }))
));
