"use strict";

/* Luna Sentia's model monitor: live charts from /api/live, stored history from /api/history. */

const SVG = "http://www.w3.org/2000/svg";
const PHASES = [
  ["wait", "Waiting"],
  ["load", "Loading"],
  ["read", "Reading input"],
  ["write", "Writing output"],
];
const LEANINGS = { vllm: "Points to vLLM", ollama: "Points to Ollama", neutral: "Neutral" };
const TABLE_ROWS = 25;

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const num = (value, digits = 0) =>
  value === null || value === undefined || Number.isNaN(value) ? "–" : Number(value).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
const pct = (value) => (value === null || value === undefined ? "–" : `${Math.round(value * 100)}%`);
const gb = (mb) => (mb === null || mb === undefined ? "–" : `${num(mb / 1024, 1)} GB`);
const ms = (value) => {
  if (value === null || value === undefined) return "–";
  if (value < 1000) return `${Math.round(value)} ms`;
  if (value < 60000) return `${num(value / 1000, 1)} s`;
  return `${Math.floor(value / 60000)} min ${Math.round((value % 60000) / 1000)} s`;
};
const clock = (at, withDate = false) =>
  new Date(at * 1000).toLocaleString(undefined, withDate ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const tokensPerSecond = (tokens, time) => (tokens && time ? tokens / (time / 1000) : null);

function remember(key, value) {
  try {
    localStorage.setItem(`sentia-monitor-${key}`, value);
  } catch {
    /* storage refused; the page works without it */
  }
}
function remembered(key, fallback) {
  try {
    return localStorage.getItem(`sentia-monitor-${key}`) ?? fallback;
  } catch {
    return fallback;
  }
}

function svg(tag, attributes = {}, parent) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  if (parent) parent.appendChild(node);
  return node;
}

/* Tooltip */

const tip = document.getElementById("tip");
function showTip(event, title, rows) {
  tip.innerHTML = `<strong>${esc(title)}</strong><dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
  tip.hidden = false;
  const { innerWidth, innerHeight } = window;
  const box = tip.getBoundingClientRect();
  let x = event.clientX + 14;
  let y = event.clientY + 14;
  if (x + box.width > innerWidth - 8) x = event.clientX - box.width - 14;
  if (y + box.height > innerHeight - 8) y = event.clientY - box.height - 14;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}
const hideTip = () => (tip.hidden = true);

/* A resource chart: one or two series over time, a crosshair, and a tooltip. */

class Chart {
  constructor(container, { title, series, format }) {
    this.series = series;
    this.format = format;
    this.card = document.createElement("section");
    this.card.className = "sm-card";
    this.card.setAttribute("aria-label", title);
    this.card.innerHTML = `
      <div class="sm-chart-top"><h2>${esc(title)}</h2><div class="sm-big"></div></div>
      <div class="sm-plot"></div>
      <div class="sm-axis"><span></span><span></span></div>
      <div class="sm-stats"></div>
      ${series.length > 1 ? `<div class="sm-legend">${series.map((s) => `<span><i class="sm-swatch ${s.dashed ? "dash" : "line"}"></i>${esc(s.label)}</span>`).join("")}</div>` : ""}`;
    container.appendChild(this.card);
    this.big = this.card.querySelector(".sm-big");
    this.axis = this.card.querySelectorAll(".sm-axis span");
    this.stats = this.card.querySelector(".sm-stats");
    this.width = 600;
    this.height = 144;
    this.plot = svg("svg", { viewBox: `0 0 ${this.width} ${this.height}`, preserveAspectRatio: "none", role: "img", "aria-label": title }, this.card.querySelector(".sm-plot"));
    for (const at of [0, 0.5, 1]) svg("line", { class: "sm-grid-line", x1: 0, x2: this.width, y1: at * this.height, y2: at * this.height }, this.plot);
    this.ceiling = svg("line", { class: "sm-line ceiling", x1: 0, x2: this.width, visibility: "hidden" }, this.plot);
    this.paths = series.map((s) => ({
      area: s.area ? svg("path", { class: "sm-area" }, this.plot) : null,
      line: svg("path", { class: `sm-line${s.dashed ? " dashed" : ""}` }, this.plot),
    }));
    this.cross = svg("line", { class: "sm-cross", y1: 0, y2: this.height, visibility: "hidden" }, this.plot);
    this.topLabel = document.createElement("span");
    Object.assign(this.topLabel.style, { position: "absolute", top: "-0.1rem", left: "0.2rem", fontSize: "0.66rem", color: "var(--lv-muted)" });
    this.card.querySelector(".sm-plot").appendChild(this.topLabel);
    this.plot.addEventListener("pointermove", (event) => this.hover(event));
    this.plot.addEventListener("pointerleave", () => this.leave());
    this.data = null;
  }

  update({ points, start, end, max, big, stats, gap, dateAxis, ceiling }) {
    this.data = { points, start, end, max: max || 1 };
    const x = (t) => ((t - start) / (end - start)) * this.width;
    const y = (v) => this.height - (Math.min(v, max) / (max || 1)) * this.height;
    this.series.forEach((s, index) => {
      let line = "";
      let area = "";
      let run = [];
      const flush = () => {
        if (!run.length) return;
        const stepped = s.stepped;
        let d = `M${run[0][0]},${run[0][1]}`;
        for (let i = 1; i < run.length; i++) d += stepped ? `H${run[i][0]}V${run[i][1]}` : `L${run[i][0]},${run[i][1]}`;
        line += d;
        area += `${d}V${this.height}H${run[0][0]}Z`;
        run = [];
      };
      let previous = null;
      for (const point of points) {
        const value = point[s.key];
        if (value === null || value === undefined || (previous !== null && point.at - previous > gap)) flush();
        if (value !== null && value !== undefined) run.push([x(point.at).toFixed(1), y(value).toFixed(1)]);
        previous = point.at;
      }
      flush();
      this.paths[index].line.setAttribute("d", line);
      if (this.paths[index].area) this.paths[index].area.setAttribute("d", area);
    });
    this.ceiling.setAttribute("visibility", ceiling ? "visible" : "hidden");
    if (ceiling) {
      this.ceiling.setAttribute("y1", y(ceiling) + 1);
      this.ceiling.setAttribute("y2", y(ceiling) + 1);
    }
    this.topLabel.textContent = this.format(max);
    this.big.innerHTML = big;
    this.stats.innerHTML = stats;
    this.axis[0].textContent = dateAxis ? clock(start, true) : `${Math.round(end - start)} s ago`;
    this.axis[1].textContent = dateAxis ? clock(end, true) : "now";
    if (this.pointer) this.hover(this.pointer);
  }

  hover(event) {
    this.pointer = event;
    if (!this.data || !this.data.points.length) return;
    const box = this.plot.getBoundingClientRect();
    const { start, end, points } = this.data;
    const at = start + ((event.clientX - box.left) / box.width) * (end - start);
    let nearest = points[0];
    for (const point of points) if (Math.abs(point.at - at) < Math.abs(nearest.at - at)) nearest = point;
    const px = ((nearest.at - start) / (end - start)) * this.width;
    this.cross.setAttribute("x1", px);
    this.cross.setAttribute("x2", px);
    this.cross.setAttribute("visibility", "visible");
    showTip(event, clock(nearest.at, end - start > 86400), this.series.map((s) => [s.label, this.format(nearest[s.key])]));
  }

  leave() {
    this.pointer = null;
    this.cross.setAttribute("visibility", "hidden");
    hideTip();
  }
}

function chartSet(container) {
  container.innerHTML = "";
  return {
    gpu: new Chart(container, { title: "GPU utilisation", format: (v) => (v === null || v === undefined ? "–" : `${Math.round(v)}%`),
      series: [{ key: "gpu_util", label: "Utilisation", area: true }] }),
    vram: new Chart(container, { title: "VRAM", format: (v) => gb(v),
      series: [{ key: "vram_used_mb", label: "All of the GPU", area: true }, { key: "ollama_vram_mb", label: "Ollama's models", dashed: true }] }),
    requests: new Chart(container, { title: "Requests", format: (v) => num(v, 0),
      series: [{ key: "in_flight", label: "In flight", area: true, stepped: true }, { key: "waiting", label: "Waiting", dashed: true, stepped: true }] }),
    power: new Chart(container, { title: "Power", format: (v) => (v === null || v === undefined ? "–" : `${Math.round(v)} W`),
      series: [{ key: "power_w", label: "Power", area: true }] }),
  };
}

const niceCeiling = (value) => {
  if (!value || value <= 0) return 1;
  const step = 10 ** Math.floor(Math.log10(value));
  return Math.ceil(value / step) * step;
};

function drawCharts(charts, points, { start, end, gap, dateAxis, gpuName, parallel, powerLimit }) {
  const last = [...points].reverse().find((p) => p.gpu_util !== null && p.gpu_util !== undefined) || {};
  const total = Math.max(...points.map((p) => p.vram_total_mb || 0), 1);
  const peakFlight = Math.max(...points.map((p) => p.in_flight || 0), 0);
  const peakPower = Math.max(...points.map((p) => p.power_w || 0), 0);
  const average = (key) => {
    const values = points.map((p) => p[key]).filter((v) => v !== null && v !== undefined);
    return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
  };
  const show = dateAxis ? (key) => average(key) : (key) => last[key];
  const label = dateAxis ? "average" : "now";
  charts.gpu.update({
    points, start, end, gap, dateAxis, max: 100,
    big: `${num(show("gpu_util"))}<small>% ${label}</small>`,
    stats: `<span>Memory bandwidth <b>${num(show("mem_util"))}%</b></span><span>SM clock <b>${num(show("sm_clock_mhz"))} MHz</b></span>${gpuName ? `<span>${esc(gpuName)}</span>` : ""}`,
  });
  const used = dateAxis ? Math.max(...points.map((p) => p.vram_used_mb || 0)) : last.vram_used_mb;
  charts.vram.update({
    points, start, end, gap, dateAxis, max: total, ceiling: total,
    big: `${gb(used)}<small>of ${gb(total)}${dateAxis ? " at peak" : ""}</small>`,
    stats: `<span>Free <b>${gb(total - (used || 0))}</b></span><span>Ollama's models <b>${gb(dateAxis ? Math.max(...points.map((p) => p.ollama_vram_mb || 0)) : last.ollama_vram_mb)}</b></span>`,
  });
  const lastFlight = points.length ? points[points.length - 1] : {};
  charts.requests.update({
    points, start, end, gap, dateAxis, max: Math.max(2, peakFlight + 1),
    big: dateAxis ? `${num(peakFlight)}<small>at most in flight</small>` : `${num(lastFlight.in_flight || 0)}<small>in flight</small>`,
    stats: dateAxis
      ? `<span>Busy <b>${num(average("busy_pct"))}%</b> of the time</span><span>Most waiting <b>${num(Math.max(...points.map((p) => p.waiting || 0)))}</b></span>`
      : `<span>Running <b>${num(lastFlight.running || 0)}</b></span><span>Waiting <b>${num(lastFlight.waiting || 0)}</b></span><span>Ollama runs <b>${num(parallel)}</b> per model at once</span>`,
  });
  const limit = powerLimit || niceCeiling(peakPower * 1.2);
  charts.power.update({
    points, start, end, gap, dateAxis, max: limit,
    big: `${num(show("power_w"))}<small>W ${label}</small>`,
    stats: `<span>Temperature <b>${num(dateAxis ? Math.max(...points.map((p) => p.temp_c || 0)) : last.temp_c)} °C</b>${dateAxis ? " peak" : ""}</span>${dateAxis ? "" : `<span>Fan <b>${num(last.fan_pct)}%</b></span>`}<span>Limit <b>${powerLimit ? `${num(powerLimit)} W` : "–"}</b></span>`,
  });
}

/* The call timeline: one row per app, each call split into its phases. */

function phases(call) {
  const wall = call.wall_ms || 0;
  if (call.outcome !== "ok" || call.load_ms === null || call.load_ms === undefined) return null;
  const parts = { load: call.load_ms || 0, read: call.prompt_ms || 0, write: call.output_ms || 0 };
  parts.wait = Math.max(wall - parts.load - parts.read - parts.write, 0);
  return parts;
}

function callRows(call) {
  const rows = [
    ["App", call.app], ["Model", call.model], ["Endpoint", call.endpoint], ["Started", clock(call.started_at)],
    ["Outcome", call.outcome], ["Wall time", ms(call.wall_ms ?? (Date.now() / 1000 - call.started_at) * 1000)],
  ];
  if (call.outcome !== "running") {
    rows.push(["Waiting", ms(call.wait_ms)], ["Loading", ms(call.load_ms)],
      ["Reading input", `${ms(call.prompt_ms)} · ${num(call.prompt_tokens)} tok · ${num(tokensPerSecond(call.prompt_tokens, call.prompt_ms))} tok/s`],
      ["Writing output", `${ms(call.output_ms)} · ${num(call.output_tokens)} tok · ${num(tokensPerSecond(call.output_tokens, call.output_ms), 1)} tok/s`]);
  }
  rows.push(["Images", num(call.images)], ["Context asked", call.num_ctx ? num(call.num_ctx) : "default"], ["Already in flight", num(call.ahead)]);
  if (call.error) rows.push(["Error", call.error.slice(0, 120)]);
  return rows;
}

function drawTimeline(container, calls, { start, end, now }) {
  const visible = calls.filter((c) => (c.finished_at ?? now) >= start && c.started_at <= end);
  if (!visible.length) {
    container.innerHTML = `<p class="sm-empty">No model calls in this window.</p>`;
    return;
  }
  const width = Math.max(container.clientWidth, 280);
  const labelWidth = 64;
  const plotWidth = width - labelWidth;
  const laneHeight = 16;
  const x = (t) => labelWidth + ((Math.min(Math.max(t, start), end) - start) / (end - start)) * plotWidth;
  // Calls of one app that overlap go in separate lanes of its row.
  const apps = [...new Set(visible.map((c) => c.app))].sort();
  const lanes = new Map(apps.map((app) => [app, []]));
  const placed = visible
    .slice()
    .sort((a, b) => a.started_at - b.started_at)
    .map((call) => {
      const ends = lanes.get(call.app);
      const finish = call.finished_at ?? now;
      let lane = ends.findIndex((e) => e <= call.started_at);
      if (lane < 0) lane = ends.push(finish) - 1;
      else ends[lane] = finish;
      return { call, lane };
    });
  let top = 0;
  const rowTop = new Map();
  for (const app of apps) {
    rowTop.set(app, top);
    top += Math.max(lanes.get(app).length, 1) * laneHeight + 8;
  }
  const height = top + 18;
  container.innerHTML = "";
  container.classList.add("sm-timeline");
  const plot = svg("svg", { viewBox: `0 0 ${width} ${height}`, height, role: "img", "aria-label": `Timeline of ${visible.length} model calls` }, container);
  // Enough room for each time label: two on a phone, four on a wider screen.
  const ticks = plotWidth < 460 ? 2 : 4;
  for (let i = 0; i <= ticks; i++) {
    const t = start + ((end - start) * i) / ticks;
    const px = labelWidth + (plotWidth * i) / ticks;
    svg("line", { class: "sm-grid-line", x1: px, x2: px, y1: 0, y2: top }, plot);
    const text = svg("text", { class: "sm-grid-label", x: px, y: height - 4, "text-anchor": i === 0 ? "start" : i === ticks ? "end" : "middle" }, plot);
    text.textContent = end - start > 86400 ? clock(t, true) : clock(t);
  }
  for (const app of apps) {
    const label = svg("text", { class: "sm-row-label", x: 0, y: rowTop.get(app) + 12 }, plot);
    label.textContent = app;
  }
  const pxPerSecond = plotWidth / (end - start);
  for (const { call, lane } of placed) {
    const y = rowTop.get(call.app) + lane * laneHeight + 2;
    const x0 = x(call.started_at);
    const x1 = Math.max(x(call.finished_at ?? now), x0 + 2);
    const group = svg("g", { class: "sm-call" }, plot);
    const split = phases(call);
    if (call.outcome === "running") {
      svg("rect", { class: "sm-seg-run", x: x0, y, width: x1 - x0, height: 11, rx: 2 }, group);
    } else if (!split) {
      svg("rect", { class: call.outcome === "ok" ? "sm-seg-wait" : "sm-seg-fail", x: x0, y, width: x1 - x0, height: 11, rx: 2 }, group);
    } else {
      // Phases laid out back from the end: waiting, loading, reading, writing.
      let cursor = x(call.started_at) - (call.started_at < start ? (start - call.started_at) * pxPerSecond : 0);
      for (const [key] of PHASES) {
        const w = (split[key] / 1000) * pxPerSecond;
        const from = Math.max(cursor, labelWidth);
        const to = Math.min(cursor + w, labelWidth + plotWidth);
        if (to - from >= 1) {
          const gapped = to - from > 4 ? 1 : 0;
          svg("rect", { class: `sm-seg-${key}`, x: from + gapped, y, width: Math.max(to - from - 2 * gapped, 1), height: 11, rx: 2 }, group);
        }
        cursor += w;
      }
    }
    const hit = svg("rect", { class: "sm-hit", x: x0 - 3, y: y - 3, width: x1 - x0 + 6, height: laneHeight }, group);
    hit.addEventListener("pointermove", (event) => showTip(event, `${call.app} · ${call.model}`, callRows(call)));
    hit.addEventListener("pointerleave", hideTip);
  }
}

/* Tables */

function callsTable(container, calls) {
  if (!calls.length) {
    container.innerHTML = `<p class="sm-empty">No model calls yet. They appear here as the apps make them.</p>`;
    return;
  }
  const rows = calls.slice(0, TABLE_ROWS).map((c) => `<tr>
      <td>${esc(clock(c.started_at))}</td><td>${esc(c.app)}</td><td>${esc(c.model)}</td><td>${esc(c.endpoint.replace("/api/", ""))}</td>
      <td><span class="sm-outcome ${esc(c.outcome)}">${esc(c.outcome)}</span></td>
      <td class="num">${ms(c.wall_ms)}</td><td class="num">${ms(c.wait_ms)}</td><td class="num">${ms(c.load_ms)}</td>
      <td class="num">${num(c.prompt_tokens)}</td><td class="num">${num(tokensPerSecond(c.prompt_tokens, c.prompt_ms))}</td>
      <td class="num">${num(c.output_tokens)}</td><td class="num">${num(tokensPerSecond(c.output_tokens, c.output_ms), 1)}</td>
      <td class="num">${num(c.images)}</td><td class="num">${c.num_ctx ? num(c.num_ctx) : "–"}</td><td class="num">${num(c.ahead)}</td></tr>`);
  container.innerHTML = `<table class="sm-table"><thead><tr>
      <th>Started</th><th>App</th><th>Model</th><th>Call</th><th>Outcome</th><th class="num">Wall</th><th class="num">Waiting</th><th class="num">Loading</th>
      <th class="num">Input tok</th><th class="num">Input tok/s</th><th class="num">Output tok</th><th class="num">Output tok/s</th>
      <th class="num">Images</th><th class="num">Context</th><th class="num">Ahead</th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
}

function groupsTable(container, groups) {
  if (!groups.length) {
    container.innerHTML = `<p class="sm-empty">No calls in this range.</p>`;
    return;
  }
  container.innerHTML = `<table class="sm-table"><thead><tr>
      <th>App</th><th>Model</th><th class="num">Calls</th><th class="num">Problems</th><th class="num">Typical wall</th><th class="num">Slowest 5%</th>
      <th class="num">Waiting share</th><th class="num">Loading</th><th class="num">Input tok/s</th><th class="num">Output tok/s</th><th class="num">Images per call</th><th class="num">Largest context</th>
      </tr></thead><tbody>${groups.map((g) => `<tr><td>${esc(g.app)}</td><td>${esc(g.model)}</td><td class="num">${num(g.calls)}</td><td class="num">${num(g.problems)}</td>
      <td class="num">${ms(g.wall_p50)}</td><td class="num">${ms(g.wall_p95)}</td><td class="num">${pct(g.wall_sum ? g.wait_sum / g.wall_sum : null)}</td>
      <td class="num">${ms(g.load_sum)}</td><td class="num">${num(tokensPerSecond(g.prompt_tokens, g.prompt_ms))}</td>
      <td class="num">${num(tokensPerSecond(g.output_tokens, g.output_ms), 1)}</td><td class="num">${num(g.images_avg, 1)}</td><td class="num">${g.num_ctx_max ? num(g.num_ctx_max) : "–"}</td></tr>`).join("")}</tbody></table>`;
}

function eventsTable(container, events) {
  if (!events.length) {
    container.innerHTML = `<p class="sm-empty">Ollama did not load or unload a model in this range.</p>`;
    return;
  }
  container.innerHTML = `<table class="sm-table"><thead><tr><th>When</th><th>Model</th><th>Event</th><th class="num">VRAM</th><th class="num">Context</th></tr></thead>
    <tbody>${events.slice().reverse().map((e) => `<tr><td>${esc(clock(e.at, true))}</td><td>${esc(e.model)}</td><td>${esc(e.event)}</td>
    <td class="num">${gb(e.vram_mb)}</td><td class="num">${e.context ? num(e.context) : "–"}</td></tr>`).join("")}</tbody></table>`;
}

const APP_NAMES = {
  visia: "Luna Visia", lexia: "Luna Lexia", vibia: "Luna Vibia", socia: "Luna Socia", fiora: "Luna Fiora", vita: "Luna Vita",
  lumia: "Luna Lumia", unia: "Luna Unia", sentia: "the monitor, reloading it", "model-check": "model checks",
};
const appName = (app) => APP_NAMES[app] || app;
// The Luna apps, in the order "Models by app" lists them; the monitor and model checks are not apps.
const APP_KEYS = new Set(["socia", "visia", "lexia", "vibia", "vita", "fiora", "lumia", "unia"]);
/* "4 min", "2 h 5 min", "2 d 11 h": how long ago, or how long until. */
const span = (seconds) => {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ${Math.round((seconds % 3600) / 60)} min`;
  return `${Math.floor(seconds / 86400)} d ${Math.round((seconds % 86400) / 3600)} h`;
};
const RESIDENT_AFTER = 30 * 86400; // Ollama's "keep loaded for good" expires centuries from now.

function vramCell(mb, totalVram, note = "") {
  if (!mb) return `<span class="sm-dim">${note || "–"}</span>`;
  const share = totalVram ? Math.min((mb / totalVram) * 100, 100) : 0;
  return `<div class="sm-cell-meter"><div class="sm-meter" role="img" aria-label="${esc(gb(mb))} of VRAM"><i style="width:${share.toFixed(1)}%"></i></div>${gb(mb)}${note ? ` <span class="sm-dim">${note}</span>` : ""}</div>`;
}

const usage = (m, now) => [
  m.calls ? `${num(m.calls)} call${m.calls === 1 ? "" : "s"}, ${ms(m.busy_seconds * 1000)} busy` : "",
  m.failures ? `${num(m.failures)} failed` : "",
  m.last_used ? `last used ${span(now - m.last_used)} ago` : "not used since the app started",
].filter(Boolean).join(" · ");

/* Who called each Ollama model: the calls kept in the database, and those in memory since. */
function callersOf(model, usage, calls) {
  const byApp = new Map();
  for (const u of usage || []) if (u.model === model) byApp.set(u.app, { calls: u.calls, last: u.last_call });
  for (const c of calls) if (c.model === model && !byApp.has(c.app)) byApp.set(c.app, { calls: 1, last: c.started_at });
  return [...byApp.entries()].sort((x, y) => y[1].calls - x[1].calls);
}
const callersText = (callers, now) => callers.length
  ? callers.map(([app, u]) => `${appName(app)} (${num(u.calls)} call${u.calls === 1 ? "" : "s"}, last ${span(now - u.last)} ago)`).join(", ")
  : "";
const IMAGES_FOR = "Socia's pictures of Luna";
const IMAGE_STAGE = { reading: "reading the prompt", drawing: "drawing" };

/* Every model loaded now, Ollama's, the image model's and the apps' own, in one table; every other model below,
   one line each, by where it runs. */
function drawModels(container, list, byApp, models, apps, inventory, calls, totalVram, now) {
  inventory = inventory || {};
  const rows = models.filter((m) => showsModel(m.name)).map((m) => {
    const left = m.expires_at ? m.expires_at - now : null;
    const state = left === null ? "loaded" : left > RESIDENT_AFTER ? "stays loaded (resident)" : left > 1 ? `unloads in ${span(left)} if idle` : "unloading";
    const callers = callersText(callersOf(m.name, inventory.usage, calls), now);
    const what = [[m.parameters, m.quantization].filter(Boolean).join(" · "), m.context ? `${num(m.context)} context` : ""].filter(Boolean).join(" · ");
    return `<tr><td><strong>${esc(m.name)}</strong><br><span class="sm-dim">${esc(what)}</span></td><td>Ollama</td>
      <td class="wrap">${callers ? `Called by ${esc(callers)}` : "<span class=\"sm-dim\">No call kept</span>"}</td>
      <td>${vramCell(m.vram_mb, totalVram)}</td><td class="wrap">${esc(state)}</td></tr>`;
  });
  // The image model is loaded only while a picture is made: its text encoder, then its diffusion model.
  const images = inventory.images;
  const imagesLoaded = images && !images.problem && images.drawing;
  const showsImages = showsApp("socia") && !filter.models.size;
  if (imagesLoaded && showsImages) {
    const part = images.stage === "reading" ? "text encoder" : "diffusion model";
    const fileLines = (images.files || []).filter((f) => images.stage !== "reading" ? f.kind !== "text encoder" : f.kind === "text encoder")
      .map((f) => `${esc(f.kind)}: ${esc(f.name)}`).join("<br>");
    rows.push(`<tr><td><strong>Qwen-Image</strong><br><span class="sm-dim">${fileLines}</span></td><td>Sentia imagegen</td>
      <td class="wrap">${IMAGES_FOR}</td><td>${vramCell(images.vram_mb, totalVram, images.vram_mb ? `its ${part}` : `its ${part}, loading`)}</td>
      <td class="wrap"><b>${esc(IMAGE_STAGE[images.stage] || "working")} now</b> · gone when the picture is done</td></tr>`);
  }
  const loadedNames = new Set(models.map((m) => m.name));
  // Not loaded: a name and its size each; who uses them is in "Models by app".
  const chip = (name, note) => `<li>${esc(name)}${note ? ` <span class="sm-dim">${esc(note)}</span>` : ""}</li>`;
  const idleOllama = (inventory.ollama || []).filter((m) => !loadedNames.has(m.name) && showsModel(m.name)).map((m) => chip(m.name, gb(m.size_mb)));
  const idleImages = images && !imagesLoaded && showsImages
    ? images.problem ? [`<li class="sm-dim">${esc(images.problem)}</li>`]
      : (images.files || []).map((f) => chip(f.name, [f.kind, f.size_mb ? gb(f.size_mb) : ""].filter(Boolean).join(" · ")))
    : [];
  const idleApps = [];
  for (const app of apps || []) {
    // The models the apps run themselves make no calls here: shown for a chosen app, unless models are chosen.
    if (!showsApp(app.app) || filter.models.size) continue;
    if (app.problem) { idleApps.push(`<li><b>${esc(appName(app.app))}</b> <span class="sm-dim">${esc(app.problem)}</span></li>`); continue; }
    const held = app.gpu ? app.gpu.reserved_mb : null;
    const measured = app.gpu && app.gpu.measured;
    let first = true;
    for (const m of app.models) {
      if (!m.loaded) {
        const size = m.vram_mb ? gb(m.vram_mb) : "size shown once loaded";
        idleApps.push(chip(m.name, `${appName(app.app)} · ${size}`));
        continue;
      }
      // Measured per model where the app can (Socia); otherwise its whole process's share, on its first model.
      const memory = m.vram_mb && !m.vram_last_seen ? vramCell(m.vram_mb, totalVram, "measured at load")
        : held && !measured ? vramCell(first ? held : 0, totalVram, first ? "the app's process" : "in the same process")
        : vramCell(0, totalVram, /CPU/.test(m.device) ? "CPU, no VRAM" : "not reported");
      first = false;
      rows.push(`<tr><td><strong>${esc(m.name)}</strong><br><span class="sm-dim">${esc(m.device)}</span></td><td>${esc(appName(app.app))}</td>
        <td class="wrap">${esc(m.purpose)}</td><td>${memory}</td>
        <td class="wrap">${m.active ? "<b>working now</b> · " : ""}${esc(usage(m, now))}</td></tr>`);
    }
    if (measured && app.gpu.context_mb > 0) {
      rows.push(`<tr><td><strong>CUDA runtime</strong><br><span class="sm-dim">shared by its models</span></td><td>${esc(appName(app.app))}</td>
        <td class="wrap"><span class="sm-dim">The GPU context every model in the app uses</span></td><td>${vramCell(app.gpu.context_mb, totalVram)}</td><td>loaded</td></tr>`);
    }
  }
  container.innerHTML = rows.length
    ? `<table class="sm-table sm-models-table"><colgroup><col class="c-model"><col class="c-where"><col class="c-for"><col class="c-vram"><col class="c-state"></colgroup>
       <thead><tr><th>Model</th><th>Runs in</th><th>For</th><th>VRAM</th><th>State</th></tr></thead><tbody>${rows.join("")}</tbody></table>`
    : filtering() ? `<p class="sm-empty">None of the chosen apps' or models' models is loaded now.</p>`
    : `<p class="sm-empty">No model is loaded. Ollama loads one on the next call, which adds loading time to it.</p>`;
  const group = (title, kind, items, empty) => `<h4 class="sm-sub">${title}</h4><ul class="sm-chips sm-chips-${kind}">${items.length ? items.join("") : `<li class="sm-dim">${empty}</li>`}</ul>`;
  list.innerHTML = [
    group("Ollama", "ollama", idleOllama, inventory.ollama ? "Every installed model is loaded." : "Waiting for Ollama's list."),
    images ? group("Image model", "images", idleImages, "It is making a picture, above.") : "",
    group("Inside the apps", "apps", idleApps, apps ? "Every model the apps run themselves is loaded." : "Waiting for the apps' first report."),
  ].join("");
  drawModelsByApp(byApp, apps, inventory, calls, loadedNames, imagesLoaded, now);
}

/* The Ollama models each app is set up to use, as in the README's Models table
   (tests/test_monitor.py keeps the two the same). */
const APP_OLLAMA_MODELS = {
  socia: ["huihui_ai/qwen3-abliterated:8b", "huihui_ai/qwen3-vl-abliterated:8b-instruct", "qwen3-embedding:0.6b"],
  visia: ["huihui_ai/qwen3-vl-abliterated:8b-instruct"],
  lexia: ["huihui_ai/qwen3-vl-abliterated:8b-instruct", "qwen3-embedding:0.6b"],
  vibia: ["huihui_ai/qwen3-vl-abliterated:8b-instruct"],
  fiora: ["huihui_ai/qwen3-vl-abliterated:8b-instruct"],
};

/* The filter: the apps and models whose calls to show. None picked shows everything; picking apps and
   models shows the calls that match both. The GPU's charts stay whole: the card is shared. */
const filter = (() => {
  let saved = {};
  try {
    saved = JSON.parse(remembered("filter", "{}")) || {};
  } catch {
    /* an unreadable saved filter shows everything */
  }
  return { apps: new Set(saved.apps || []), models: new Set(saved.models || []) };
})();
const filtering = () => filter.apps.size > 0 || filter.models.size > 0;
const showsCall = (call) => (!filter.apps.size || filter.apps.has(call.app)) && (!filter.models.size || filter.models.has(call.model));
const showsApp = (app) => !filter.apps.size || filter.apps.has(app);
function modelsOfChosenApps() {
  const names = new Set();
  for (const app of filter.apps) for (const name of APP_OLLAMA_MODELS[app] || []) names.add(name);
  for (const u of (live.inventory || {}).usage || []) if (filter.apps.has(u.app)) names.add(u.model);
  for (const c of live.calls) if (filter.apps.has(c.app)) names.add(c.model);
  return names;
}
function showsModel(name) {
  if (filter.models.size && !filter.models.has(name)) return false;
  return !filter.apps.size || modelsOfChosenApps().has(name);
}
const filterQuery = () => new URLSearchParams({ apps: [...filter.apps].join(","), models: [...filter.models].join(",") }).toString();

/* What there is to pick: the models Ollama has now (the ones in use) and the Luna apps that use models, plus
   anything calling now and anything already picked, so it can be unpicked. Old callers and models that only the
   history remembers are left out. */
function filterChoices() {
  const apps = new Set(filter.apps), models = new Set(filter.models);
  const installed = new Set(((live.inventory || {}).ollama || []).map((m) => m.name));
  for (const app of Object.keys(APP_OLLAMA_MODELS)) apps.add(app);
  for (const c of live.inFlight) { if (c.app) apps.add(c.app); if (c.model) models.add(c.model); }
  for (const name of installed) models.add(name);
  if (!installed.size) {
    // Ollama's list not in yet: the models the apps were set up with.
    for (const names of Object.values(APP_OLLAMA_MODELS)) names.forEach((name) => models.add(name));
  }
  const order = [...APP_KEYS];
  const rank = (app) => (order.includes(app) ? order.indexOf(app) : order.length);
  return { apps: [...apps].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b)), models: [...models].sort() };
}

let drawnChoices = "";
function drawFilter(force = false) {
  const choices = filterChoices();
  const key = JSON.stringify([choices, [...filter.apps], [...filter.models]]);
  if (!force && key === drawnChoices) return;
  drawnChoices = key;
  const chips = (names, chosen, kind, label) => names.length
    ? names.map((name) => `<button type="button" data-filter="${kind}" data-name="${esc(name)}" aria-pressed="${chosen.has(name)}">${esc(label(name))}</button>`).join("")
    : `<span class="sm-dim">None called yet</span>`;
  document.getElementById("filter-apps").innerHTML = chips(choices.apps, filter.apps, "apps", appName);
  document.getElementById("filter-models").innerHTML = chips(choices.models, filter.models, "models", (name) => name);
  const parts = [filter.apps.size ? [...filter.apps].map(appName).join(", ") : "", filter.models.size ? [...filter.models].join(", ") : ""].filter(Boolean);
  document.getElementById("filter-note").textContent = filtering()
    ? `Showing calls of ${parts.join(" to ")}. The GPU charts show the whole card, which they share.`
    : "Showing every app and model. Pick some to see only their calls.";
  document.getElementById("filter-clear").hidden = !filtering();
}

function changeFilter() {
  remember("filter", JSON.stringify({ apps: [...filter.apps], models: [...filter.models] }));
  drawFilter(true);
  connect();
  if (current === "live") drawLive();
  if (current === "history") loadHistory();
}

/* Which models each app uses: the Ollama models it is set up for, with its calls to them, its
   pictures' models, and the models it runs itself. A dot marks a model loaded now. Models an app
   called that it is not set up for (old ones, model checks) fold into one line. */
function drawModelsByApp(container, apps, inventory, calls, loadedNames, imagesLoaded, now) {
  const called = new Map(); // "app model" -> {calls, last}
  for (const u of inventory.usage || []) called.set(`${u.app} ${u.model}`, { calls: u.calls, last: u.last_call });
  for (const c of calls) if (!called.has(`${c.app} ${c.model}`)) called.set(`${c.app} ${c.model}`, { calls: 1, last: c.started_at });
  const own = new Map();
  if (inventory.images && !inventory.images.problem) own.set("socia", [{ name: "Qwen-Image 2.1 Turbo (Sentia imagegen)", loaded: imagesLoaded }]);
  for (const app of apps || []) for (const m of app.models || []) own.set(app.app, [...(own.get(app.app) || []), { name: m.name, loaded: m.loaded }]);
  const item = (m) => `<li${m.loaded ? ' class="sm-on" title="Loaded now"' : ""}>${esc(m.name)}${m.calls
    ? ` <span class="sm-dim">${num(m.calls)} call${m.calls === 1 ? "" : "s"}, last ${span(now - m.last)} ago</span>` : ""}</li>`;
  const list = (items, extra = "") => items.length || extra ? `<ul class="sm-app-models">${items.map(item).join("")}${extra}</ul>` : `<span class="sm-dim">–</span>`;
  const rows = [...APP_KEYS].filter(showsApp).map((app) => {
    const setUp = APP_OLLAMA_MODELS[app] || [];
    const ollama = setUp.map((name) => ({ name, loaded: loadedNames.has(name), ...(called.get(`${app} ${name}`) || {}) }));
    const others = [...called.keys()].filter((key) => key.startsWith(`${app} `) && !setUp.includes(key.slice(app.length + 1)));
    const tried = others.length
      ? `<li class="sm-older" title="${esc(others.map((key) => key.slice(app.length + 1)).join(", "))}">+${others.length} other${others.length === 1 ? "" : "s"} called before</li>` : "";
    if (!ollama.length && !tried && !own.has(app)) return "";
    return `<tr><td><strong>${esc(appName(app))}</strong></td><td>${list(ollama, tried)}</td><td>${list(own.get(app) || [])}</td></tr>`;
  }).filter(Boolean);
  container.innerHTML = `<table class="sm-table sm-by-app"><thead><tr><th>App</th><th>Ollama</th><th>Runs itself</th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
}

/* The verdict: the rules of thumb and the numbers behind them. */

function drawVerdict(container, signals, scope) {
  if (!signals) {
    container.innerHTML = `<p class="sm-empty">Measuring…</p>`;
    return;
  }
  const value = (f) => (f.key === "headroom" ? gb(f.value) : pct(f.value));
  container.innerHTML = `
    <div class="sm-verdict-head"><strong>${esc(signals.verdict)}</strong><span class="sm-note">${esc(scope)} · rules of thumb, see the Guide</span></div>
    ${signals.findings.length ? `<div class="sm-findings">${signals.findings.map((f) => `<div class="sm-finding">
      <div class="sm-finding-top"><span>${esc(f.label)}</span><span class="sm-lean ${esc(f.leaning)}">${esc(LEANINGS[f.leaning] || f.leaning)}</span></div>
      <b>${value(f)}</b><p>${esc(f.note)}</p></div>`).join("")}</div>` : ""}
    <p class="sm-facts"><span>Calls <b>${num(signals.calls)}</b></span><span>Problems <b>${num(signals.problems)}</b></span>
      <span>Busy <b>${ms(signals.busy_seconds * 1000)}</b></span><span>Overlapping <b>${ms(signals.overlap_seconds * 1000)}</b></span>
      <span>Most at once <b>${num(signals.peak_in_flight)}</b></span><span>Output <b>${num(signals.output_rate, 1)} tok/s</b></span>
      <span>Input <b>${num(signals.prompt_rate)} tok/s</b></span><span>GPU while busy <b>${signals.gpu_util_busy === null ? "–" : `${num(signals.gpu_util_busy)}%`}</b></span></p>`;
}

function phaseLegends() {
  const html = PHASES.map(([key, label]) => `<span><i class="sm-swatch sm-${key}"></i>${label}</span>`).join("") +
    `<span><i class="sm-swatch sm-running"></i>Running now</span>`;
  document.querySelectorAll('[data-legend="phases"]').forEach((el) => (el.innerHTML = html));
}

/* Live */

const live = {
  samples: [], calls: [], inFlight: [], models: [], signals: null, parallel: 1, gpu: {}, offset: 0,
  window: Number(remembered("window", "60")), charts: null, source: null, engineProblem: null,
};
const status = document.getElementById("status");
function setStatus(kind, text) {
  status.className = `sm-status ${kind}`;
  status.querySelector("span").textContent = text;
}

function applyLive(data, full) {
  live.offset = data.now - Date.now() / 1000;
  if (full) {
    live.samples = data.samples;
    live.calls = data.calls.slice().reverse();
  } else {
    live.samples.push(...data.samples);
    live.calls.unshift(...data.calls.slice().reverse());
  }
  live.samples = live.samples.slice(-600);
  live.calls = live.calls.slice(0, 300);
  live.inFlight = data.in_flight;
  live.models = data.models;
  live.appModels = data.app_models;
  live.inventory = data.inventory;
  live.signals = data.signals;
  live.parallel = data.parallel;
  live.gpu = data.gpu || {};
  live.engineProblem = data.engine_problem;
  if (current === "live") drawLive();
}

function drawLive() {
  const now = Date.now() / 1000 + live.offset;
  const start = now - live.window;
  live.charts ??= chartSet(document.getElementById("live-charts"));
  const points = live.samples.filter((s) => s.at >= start - 2);
  const last = live.samples[live.samples.length - 1] || {};
  drawCharts(live.charts, points, { start, end: now, gap: 5, gpuName: live.gpu.name, parallel: live.parallel, powerLimit: last.power_limit_w });
  const running = live.inFlight.map((c) => ({ ...c, outcome: "running" }));
  const shown = [...running, ...live.calls].filter(showsCall);
  drawFilter();
  drawTimeline(document.getElementById("live-timeline"), shown, { start, end: now, now });
  drawModels(document.getElementById("models"), document.getElementById("app-models"), document.getElementById("models-by-app"), live.models, live.appModels,
    live.inventory, [...running, ...live.calls], last.vram_total_mb, now);
  callsTable(document.getElementById("live-calls"), shown);
  const kept = live.calls.filter(showsCall).length;
  document.getElementById("live-calls-note").textContent = filtering()
    ? `The last ${Math.min(kept, TABLE_ROWS)} of ${kept} chosen calls, of ${live.calls.length} kept in memory`
    : `The last ${Math.min(live.calls.length, TABLE_ROWS)} of ${live.calls.length} kept in memory`;
  drawVerdict(document.getElementById("live-verdict"), live.signals, filtering() ? "The chosen calls in the last few minutes, from memory" : "Calls in the last few minutes, from memory");
  const problems = [live.gpu.problem, live.engineProblem].filter(Boolean);
  document.getElementById("live-note").textContent = problems.length ? problems.join(" · ") : "Every model call the Luna apps make passes through here.";
}

/* The version this page's script came with, from its own URL (app.js?v=...). */
const PAGE_ASSETS = new URL(document.querySelector('script[src*="app.js"]').src).searchParams.get("v");

function connect() {
  live.source?.close();
  const source = new EventSource(`/api/live?${filterQuery()}`);
  live.source = source;
  source.addEventListener("snapshot", (event) => {
    const data = JSON.parse(event.data);
    /* The server was upgraded while this page stayed open: its old script would misread the new data. */
    if (data.assets && data.assets !== PAGE_ASSETS) return location.reload();
    setStatus("live", "Live");
    applyLive(data, true);
  });
  source.addEventListener("update", (event) => applyLive(JSON.parse(event.data), false));
  source.onerror = async () => {
    setStatus("down", "Reconnecting");
    const check = await fetch("/api/history?seconds=60").catch(() => null);
    if (check && (check.status === 401 || check.status === 403)) location.reload();
    if (source.readyState === EventSource.CLOSED) setTimeout(connect, 3000);
  };
}

/* History */

const past = { range: Number(remembered("range", "3600")), charts: null, data: null };

async function loadHistory() {
  const error = document.getElementById("history-error");
  try {
    const response = await fetch(`/api/history?seconds=${past.range}&${filterQuery()}`);
    if (response.status === 401 || response.status === 403) return location.reload();
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "The history could not be loaded.");
    past.data = data;
    error.hidden = true;
    drawHistory();
  } catch (problem) {
    error.textContent = problem.message;
    error.hidden = false;
  }
}

function drawHistory() {
  const data = past.data;
  if (!data) return;
  drawFilter();
  past.charts ??= chartSet(document.getElementById("history-charts"));
  const bucket = data.bucket_seconds;
  drawCharts(past.charts, data.samples, { start: data.start, end: data.end, gap: bucket * 1.5, dateAxis: true, gpuName: live.gpu.name, parallel: live.parallel });
  const timelineCard = document.getElementById("history-timeline-card");
  timelineCard.hidden = past.range > 86400;
  if (!timelineCard.hidden) drawTimeline(document.getElementById("history-timeline"), data.calls, { start: data.start, end: data.end, now: data.end });
  groupsTable(document.getElementById("groups"), data.groups);
  eventsTable(document.getElementById("events"), data.events);
  const shown = data.calls.length < data.calls_total ? ` · verdict from the latest ${num(data.calls.length)} of ${num(data.calls_total)} calls` : "";
  document.getElementById("history-note").textContent = `Stored in the lunamonitor database, in ${ms(bucket * 1000)} steps${shown}.`;
  drawVerdict(document.getElementById("history-verdict"), data.signals, filtering() ? "The chosen calls in this range" : "This range");
}

/* Sections and controls */

let current = remembered("section", "live");
let historyTimer = null;
function show(section) {
  current = ["live", "history", "guide"].includes(section) ? section : "live";
  remember("section", current);
  document.querySelectorAll(".lv-nav-item").forEach((item) => {
    const on = item.dataset.section === current;
    item.classList.toggle("lv-nav-active", on);
    if (on) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  for (const id of ["live", "history", "guide"]) document.getElementById(id).hidden = id !== current;
  document.getElementById("filter").hidden = current === "guide";
  hideTip();
  clearInterval(historyTimer);
  if (current === "live") drawLive();
  if (current === "history") {
    loadHistory();
    historyTimer = setInterval(loadHistory, 60000);
  }
}

function pressed(selector, attribute, value) {
  document.querySelectorAll(selector).forEach((button) => button.setAttribute("aria-pressed", String(Number(button.dataset[attribute]) === value)));
}

document.querySelectorAll(".lv-nav-item").forEach((item) => item.addEventListener("click", () => show(item.dataset.section)));
document.querySelectorAll("[data-window]").forEach((button) =>
  button.addEventListener("click", () => {
    live.window = Number(button.dataset.window);
    remember("window", live.window);
    pressed("[data-window]", "window", live.window);
    drawLive();
  }));
document.querySelectorAll("[data-range]").forEach((button) =>
  button.addEventListener("click", () => {
    past.range = Number(button.dataset.range);
    remember("range", past.range);
    pressed("[data-range]", "range", past.range);
    loadHistory();
  }));
document.getElementById("filter").addEventListener("click", (event) => {
  const chip = event.target.closest("[data-filter]");
  if (!chip) return;
  const chosen = filter[chip.dataset.filter];
  if (chosen.has(chip.dataset.name)) chosen.delete(chip.dataset.name);
  else chosen.add(chip.dataset.name);
  changeFilter();
});
document.getElementById("filter-clear").addEventListener("click", () => {
  filter.apps.clear();
  filter.models.clear();
  changeFilter();
});
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => (current === "live" ? drawLive() : current === "history" && drawHistory()), 150);
});

pressed("[data-window]", "window", live.window);
pressed("[data-range]", "range", past.range);
phaseLegends();
show(current);
connect();
