/* TQQQ Strategy Dashboard
 * Reads docs/data/signals.json (produced by scripts/generate_dashboard_data.py,
 * which itself calls the untouched strategy files) and renders it. This file
 * contains NO strategy logic of its own — only display.
 */

const EXPLAIN = {
  v10: `
    <p><b>Goal:</b> hold <b>TQQQ</b> (3x leveraged QQQ) while the market trend is confirmed up,
    step down to plain <b>QQQ</b> or <b>cash (SGOV)</b> when risk rises, and use a "bodyguard"
    overlay to avoid staying leveraged into extreme, over-extended rallies.</p>
    <ul>
      <li><b>Entry (into TQQQ):</b> SPY <i>and</i> QQQ are both trading more than 3% above
        their own 180-day simple moving average, and the bodyguard is not active.</li>
      <li><b>Exit (out of TQQQ, to cash):</b> once in TQQQ, exit if SPY falls more than 6%
        below its 50-day EMA.</li>
      <li><b>Bodyguard warn</b> — either SPY or QQQ trades &gt;10% above its 230-day EMA:
        if currently in TQQQ, step down one level to QQQ.</li>
      <li><b>Bodyguard danger</b> — either SPY or QQQ trades &gt;30% above its 230-day EMA:
        force out to cash regardless of trend.</li>
      <li><b>Bodyguard clears</b> once both SPY and QQQ are back to ≤5% above their 230-day EMA;
        normal entry/exit rules resume.</li>
    </ul>
    <p>Signal is read from today's <b>closing</b> price; the trade is placed at
    <b>tomorrow's market open</b>.</p>`,
  v2: `
    <p><b>Goal:</b> the same three-state idea as v10 (TQQQ / QQQ / cash), with different
    moving-average lengths and thresholds, and a bodyguard driven by QQQ only instead of
    the worse of SPY/QQQ.</p>
    <ul>
      <li><b>Entry (into TQQQ):</b> SPY trades more than 4% above its own 160-day simple
        moving average, and the bodyguard is not active.</li>
      <li><b>Exit (out of TQQQ, to cash):</b> once in TQQQ, exit if SPY falls more than 2%
        below its 145-day simple moving average.</li>
      <li><b>Bodyguard warn</b> — QQQ trades &gt;15% above its 210-day EMA: if currently in
        TQQQ, step down one level to QQQ.</li>
      <li><b>Bodyguard danger</b> — QQQ trades &gt;35% above its 210-day EMA: force out to cash
        regardless of trend.</li>
      <li><b>Bodyguard clears</b> once QQQ is back to ≤6% above its 210-day EMA; normal
        entry/exit rules resume.</li>
    </ul>
    <p>Signal is read from today's <b>closing</b> price; the trade is placed at
    <b>tomorrow's market open</b>.</p>`,
};

let DATA = null;
let CURRENT = "v10";

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function stateClass(state) {
  return "s" + state;
}

function fmtPct(v) {
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}%`;
}

/* ── theme ─────────────────────────────────────────────────────────── */

function initTheme() {
  const saved = localStorage.getItem("tqqq-theme");
  const theme = saved || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  applyTheme(theme);
  document.getElementById("themeBtn").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme") || "light";
    applyTheme(cur === "dark" ? "light" : "dark");
  });
}

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("tqqq-theme", theme);
  document.getElementById("themeBtn").textContent = theme === "dark" ? "☀️" : "🌙";
  if (DATA) renderChart(DATA.strategies[CURRENT]); // redraw with new computed colors
}

/* ── load ──────────────────────────────────────────────────────────── */

async function load() {
  try {
    const res = await fetch("./data/signals.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    DATA = await res.json();
  } catch (err) {
    document.getElementById("lastUpdated").textContent =
      "Could not load data/signals.json — has the update workflow run yet?";
    console.error(err);
    return;
  }
  renderHeader();
  wireTabs();
  renderStrategy(CURRENT);
}

function renderHeader() {
  const gen = new Date(DATA.generated_at);
  document.getElementById("lastUpdated").textContent =
    `Data as of ${DATA.as_of_date} · updated ${gen.toLocaleString()}`;

  const ageMs = Date.now() - gen.getTime();
  const staleDays = ageMs / (1000 * 60 * 60 * 24);
  document.getElementById("staleWarning").classList.toggle("show", staleDays > 4);
}

function wireTabs() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      CURRENT = btn.dataset.strategy;
      renderStrategy(CURRENT);
    });
  });
}

/* ── main render ───────────────────────────────────────────────────── */

function renderStrategy(id) {
  const s = DATA.strategies[id];
  renderStateCard(s);
  renderEntryExit(s);
  renderBodyguard(s);
  renderThresholds(s);
  renderChart(s);
  renderTrades(s);
  document.getElementById("explainPanel").innerHTML =
    `<h2>How This Strategy Works</h2>${EXPLAIN[id]}`;
}

function renderStateCard(s) {
  const pill = document.getElementById("statePill");
  pill.textContent = s.state_ticker;
  pill.className = "state-pill " + stateClass(s.state);

  document.getElementById("stateAsOf").innerHTML =
    `Holding: <b>${s.state_name}</b> &middot; as of ${s.as_of_date}`;
  document.getElementById("stateDays").textContent =
    `${s.days_in_state} trading day${s.days_in_state === 1 ? "" : "s"} in this state`;

  const banner = document.getElementById("actionBanner");
  if (s.changed) {
    banner.classList.add("show", "changed");
    document.getElementById("actionText").textContent =
      `⚡ Signal changed: was ${s.prev_state_name} → now ${s.state_name}`;
    document.getElementById("actionSub").textContent =
      `Action for next market open: sell ${STATE_TICKERS[s.prev_state]}, buy ${s.state_ticker}.`;
  } else {
    banner.classList.remove("show", "changed");
  }

  const p = s.prices;
  document.getElementById("prices").innerHTML = `
    <div><span class="p-label">SPY</span><span class="p-val">$${p.spy.toFixed(2)}</span></div>
    <div><span class="p-label">QQQ</span><span class="p-val">$${p.qqq.toFixed(2)}</span></div>
    <div><span class="p-label">TQQQ</span><span class="p-val">$${p.tqqq.toFixed(2)}</span></div>
  `;
}

const STATE_TICKERS = { 2: "TQQQ", 1: "QQQ", 0: "SGOV" };

function gaugeRow(name, valuePct, readyWhen, note) {
  // readyWhen: true => met/ready (green), false => not yet (neutral)
  const clamped = Math.max(0, Math.min(100, 50 - valuePct * 4)); // heuristic fill, clamps at edges
  const color = readyWhen ? "var(--good)" : "var(--accent)";
  return `
    <div class="bar-row">
      <div class="bar-label"><span class="name">${name}</span>
        <span class="val" style="color:${readyWhen ? "var(--good)" : "var(--text)"}">${fmtPct(valuePct)}${readyWhen ? " ✓" : ""}</span>
      </div>
      <div class="bar-track"><div class="bar-fill" style="width:${clamped}%;background:${color}"></div></div>
      ${note ? `<div class="bar-note">${note}</div>` : ""}
    </div>`;
}

function renderEntryExit(s) {
  let html = "";
  html += `<div class="bar-note" style="margin-bottom:10px;">${s.entry.description}</div>`;
  if (s.id === "v10") {
    html += gaugeRow("SPY vs entry", s.entry.spy_gap_pct, s.entry.spy_gap_pct <= 0,
      s.entry.spy_gap_pct <= 0 ? "Met" : `SPY needs to rise ${Math.abs(s.entry.spy_gap_pct).toFixed(2)}%`);
    html += gaugeRow("QQQ vs entry", s.entry.qqq_gap_pct, s.entry.qqq_gap_pct <= 0,
      s.entry.qqq_gap_pct <= 0 ? "Met" : `QQQ needs to rise ${Math.abs(s.entry.qqq_gap_pct).toFixed(2)}%`);
  } else {
    html += gaugeRow("SPY vs entry", s.entry.spy_gap_pct, s.entry.spy_gap_pct <= 0,
      `Trigger: $${s.entry.trigger_price.toFixed(2)}`);
  }

  if (s.exit) {
    html += `<div class="bar-note" style="margin:14px 0 10px;">${s.exit.description}</div>`;
    const safe = s.exit.spy_gap_pct >= 0;
    html += gaugeRow("SPY vs exit", s.exit.spy_gap_pct, safe,
      `Trigger: $${s.exit.trigger_price.toFixed(2)} · ${safe ? "SPY must fall " + s.exit.spy_gap_pct.toFixed(2) + "% to trigger exit" : "exit condition met"}`);
  } else {
    html += `<div class="bar-note" style="margin-top:14px;">Exit rule only applies while holding TQQQ.</div>`;
  }
  document.getElementById("entryExitPanel").innerHTML = html;
}

function zoneTagHtml(zone) {
  const map = { CLEAR: "zone-clear", ELEVATED: "zone-elevated", WARN: "zone-warn", DANGER: "zone-danger" };
  return `<span class="zone-tag ${map[zone] || "zone-clear"}">${zone}</span>`;
}

function renderBodyguard(s) {
  const bg = s.bodyguard;
  let html = "";
  html += `<div class="bar-row">
    <div class="bar-label"><span class="name">Active</span>
      <span class="val" style="color:${bg.active ? "var(--danger)" : "var(--good)"}">${bg.active ? "YES" : "NO"}</span>
    </div>
    ${bg.active ? `<div class="bar-note">Since ${bg.since}</div>` : ""}
  </div>`;

  if (s.id === "v10") {
    const zoneFor = (v) => (v > bg.danger_pct ? "DANGER" : v > bg.warn_pct ? "WARN" : "CLEAR");
    html += `<div class="bar-row"><div class="bar-label"><span class="name">SPY ext vs EMA230</span>
      <span class="val">${fmtPct(bg.spy_ext_pct)} ${zoneTagHtml(zoneFor(bg.spy_ext_pct))}</span></div>
      <div class="bar-note">warn &gt;${bg.warn_pct}% · danger &gt;${bg.danger_pct}%</div></div>`;
    html += `<div class="bar-row"><div class="bar-label"><span class="name">QQQ ext vs EMA230</span>
      <span class="val">${fmtPct(bg.qqq_ext_pct)} ${zoneTagHtml(zoneFor(bg.qqq_ext_pct))}</span></div>
      <div class="bar-note">warn &gt;${bg.warn_pct}% · danger &gt;${bg.danger_pct}%</div></div>`;
    if (bg.active && bg.clear) {
      html += `<div class="bar-note">To clear: ${bg.clear.closer} is closer — needs ${bg.clear.closer === "SPY" ? bg.clear.spy_needed_pct.toFixed(2) : bg.clear.qqq_needed_pct.toFixed(2)}% move (SPY ${bg.clear.spy_needed_pct.toFixed(2)}% / QQQ ${bg.clear.qqq_needed_pct.toFixed(2)}%)</div>`;
    }
  } else {
    html += `<div class="bar-row"><div class="bar-label"><span class="name">QQQ ext vs EMA210</span>
      <span class="val">${fmtPct(bg.ext_pct)} ${zoneTagHtml(bg.zone)}</span></div>
      <div class="bar-note">warn &gt;${bg.warn_pct}% · danger &gt;${bg.danger_pct}% · clears ≤${bg.return_pct}%</div></div>`;
    if (bg.active && bg.clear) {
      html += `<div class="bar-note">To clear: QQQ needs ${bg.clear.qqq_needed_pct.toFixed(2)}% (to $${bg.clear.qqq_clear_price.toFixed(2)})</div>`;
    }
  }
  document.getElementById("bodyguardPanel").innerHTML = html;
}

function renderThresholds(s) {
  const wrap = document.getElementById("thresholdPanelWrap");
  const table = document.getElementById("thresholdTable");
  let rows;
  if (s.id === "v2" && s.thresholds) {
    rows = s.thresholds.map((t) => ({
      label: t.label,
      level: `$${t.level.toFixed(2)}`,
      dist: fmtPct(t.distance_pct),
      active: t.active,
    }));
  } else {
    const e = s.entry, bg = s.bodyguard, x = s.exit;
    rows = [
      { label: "Entry — SPY vs SMA180+3%", level: "—", dist: fmtPct(e.spy_gap_pct), active: e.spy_gap_pct <= 0 },
      { label: "Entry — QQQ vs SMA180+3%", level: "—", dist: fmtPct(e.qqq_gap_pct), active: e.qqq_gap_pct <= 0 },
      { label: `Bodyguard warn (>${bg.warn_pct}%) — SPY ext`, level: "—", dist: fmtPct(bg.spy_ext_pct), active: bg.spy_ext_pct > bg.warn_pct },
      { label: `Bodyguard warn (>${bg.warn_pct}%) — QQQ ext`, level: "—", dist: fmtPct(bg.qqq_ext_pct), active: bg.qqq_ext_pct > bg.warn_pct },
      { label: `Bodyguard danger (>${bg.danger_pct}%)`, level: "—", dist: `${fmtPct(bg.spy_ext_pct)} / ${fmtPct(bg.qqq_ext_pct)}`, active: bg.spy_ext_pct > bg.danger_pct || bg.qqq_ext_pct > bg.danger_pct },
    ];
    if (x) rows.push({ label: "Exit — SPY vs EMA50×0.94", level: `$${x.trigger_price.toFixed(2)}`, dist: fmtPct(x.spy_gap_pct), active: x.spy_gap_pct < 0 });
  }

  wrap.style.display = "block";
  table.innerHTML = `
    <tr><th>Threshold</th><th>Level</th><th>Distance</th></tr>
    ${rows.map((r) => `<tr><td>${r.label}</td><td>${r.level}</td>
      <td class="${r.active ? "active-row" : ""}">${r.dist}${r.active ? " ←" : ""}</td></tr>`).join("")}
  `;
}

function renderTrades(s) {
  const table = document.getElementById("tradesTable");
  const rows = s.transitions.slice().reverse();
  if (!rows.length) {
    table.innerHTML = `<tr><td>No signal changes in the loaded history window.</td></tr>`;
    return;
  }
  table.innerHTML = `
    <tr><th>Date</th><th>From</th><th>To</th><th>SPY</th></tr>
    ${rows.map((r) => `
      <tr>
        <td>${r.date}</td>
        <td><span class="chip ${stateClass(r.from)}">${STATE_TICKERS[r.from]}</span></td>
        <td><span class="chip ${stateClass(r.to)}">${STATE_TICKERS[r.to]}</span></td>
        <td>$${r.spy.toFixed(2)}</td>
      </tr>`).join("")}
  `;
}

/* ── chart ─────────────────────────────────────────────────────────── */

function renderChart(s) {
  const h = s.history;
  const dates = h.dates;
  const n = dates.length;
  if (!n) return;

  const norm = (arr) => {
    const base = arr[0];
    return arr.map((v) => (v / base) * 100);
  };
  const spyN = norm(h.spy), qqqN = norm(h.qqq), tqqqN = norm(h.tqqq);
  const all = spyN.concat(qqqN, tqqqN);
  const yMinRaw = Math.min(...all), yMaxRaw = Math.max(...all);
  const pad = (yMaxRaw - yMinRaw) * 0.06 || 1;
  const y0 = yMinRaw - pad, y1 = yMaxRaw + pad;

  const W = 900, H = 320, L = 46, R = 12, T = 12, B = 26;
  const plotW = W - L - R, plotH = H - T - B;
  const xScale = (i) => L + (i / (n - 1)) * plotW;
  const yScale = (v) => T + (1 - (v - y0) / (y1 - y0)) * plotH;

  const linePath = (arr, color, width) => {
    const d = arr.map((v, i) => `${i === 0 ? "M" : "L"}${xScale(i).toFixed(1)},${yScale(v).toFixed(1)}`).join(" ");
    return `<path d="${d}" fill="none" stroke="${color}" stroke-width="${width}" stroke-linejoin="round" stroke-linecap="round"/>`;
  };

  // contiguous state runs for background shading
  const runs = [];
  let runStart = 0;
  for (let i = 1; i <= n; i++) {
    if (i === n || h.state[i] !== h.state[runStart]) {
      runs.push({ start: runStart, end: i - 1, state: h.state[runStart] });
      runStart = i;
    }
  }
  const stateColor = { 2: cssVar("--tqqq"), 1: cssVar("--qqq"), 0: cssVar("--cash") };

  let svg = "";
  runs.forEach((r) => {
    const x1 = xScale(r.start);
    const x2 = r.end === n - 1 ? xScale(r.end) : xScale(r.end + 1);
    svg += `<rect x="${x1.toFixed(1)}" y="${T}" width="${Math.max(x2 - x1, 0.5).toFixed(1)}" height="${plotH}" fill="${stateColor[r.state]}" opacity="0.10"/>`;
  });

  const gridColor = cssVar("--grid-line");
  const faint = cssVar("--text-faint");
  svg += `<line x1="${L}" y1="${yScale(100).toFixed(1)}" x2="${W - R}" y2="${yScale(100).toFixed(1)}" stroke="${gridColor}" stroke-width="1" stroke-dasharray="4 4"/>`;

  svg += linePath(spyN, "#6366f1", 1.6);
  svg += linePath(qqqN, "#f59e0b", 1.6);
  svg += linePath(tqqqN, "#ec4899", 1.4);

  svg += `<text x="${L}" y="${(T + 9).toFixed(1)}" font-size="10" fill="${faint}">${Math.round(y1)}</text>`;
  svg += `<text x="${L}" y="${(T + plotH - 2).toFixed(1)}" font-size="10" fill="${faint}">${Math.round(y0)}</text>`;
  svg += `<text x="${L}" y="${H - 6}" font-size="10" fill="${faint}">${dates[0]}</text>`;
  svg += `<text x="${W - R}" y="${H - 6}" font-size="10" fill="${faint}" text-anchor="end">${dates[n - 1]}</text>`;

  const chart = document.getElementById("chart");
  chart.setAttribute("viewBox", `0 0 ${W} ${H}`);
  chart.innerHTML = svg;

  document.getElementById("chartLegend").innerHTML = `
    <span class="lg"><span class="dot" style="background:#6366f1"></span>SPY</span>
    <span class="lg"><span class="dot" style="background:#f59e0b"></span>QQQ</span>
    <span class="lg"><span class="dot" style="background:#ec4899"></span>TQQQ</span>
    <span class="lg"><span class="dot" style="background:${stateColor[2]}"></span>held TQQQ</span>
    <span class="lg"><span class="dot" style="background:${stateColor[1]}"></span>held QQQ</span>
    <span class="lg"><span class="dot" style="background:${stateColor[0]}"></span>held cash</span>
    <span style="color:var(--text-faint);">— lines indexed to 100 at chart start</span>
  `;
}

initTheme();
load();
