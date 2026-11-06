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

function drawModels(container, models, totalVram, now) {
  if (!models.length) {
    container.innerHTML = `<p class="sm-empty">No model is loaded. Ollama loads one on the next call, which adds loading time to it.</p>`;
    return;
  }
  container.innerHTML = `<div class="sm-models">${models.map((m) => {
    const left = m.expires_at ? Math.max(m.expires_at - now, 0) : null;
    const share = totalVram ? Math.min((m.vram_mb / totalVram) * 100, 100) : 0;
    return `<div class="sm-model"><strong>${esc(m.name)}</strong>
      <span>${esc([m.parameters, m.quantization].filter(Boolean).join(" · "))}${m.context ? ` · ${num(m.context)} context` : ""}</span>
      <div class="sm-meter" role="img" aria-label="${esc(gb(m.vram_mb))} of VRAM"><i style="width:${share.toFixed(1)}%"></i></div>
      <span>${gb(m.vram_mb)} of VRAM${left === null ? "" : left > 1 ? ` · unloads in ${ms(left * 1000)} if idle` : " · unloading"}</span></div>`;
  }).join("")}</div>`;
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
  drawTimeline(document.getElementById("live-timeline"), [...running, ...live.calls], { start, end: now, now });
  drawModels(document.getElementById("models"), live.models, last.vram_total_mb, now);
  callsTable(document.getElementById("live-calls"), [...running, ...live.calls]);
  document.getElementById("live-calls-note").textContent = `The last ${Math.min(live.calls.length, TABLE_ROWS)} of ${live.calls.length} kept in memory`;
  drawVerdict(document.getElementById("live-verdict"), live.signals, "Calls in the last few minutes, from memory");
  const problems = [live.gpu.problem, live.engineProblem].filter(Boolean);
  document.getElementById("live-note").textContent = problems.length ? problems.join(" · ") : "Every model call the Luna apps make passes through here.";
}

function connect() {
  live.source?.close();
  const source = new EventSource("/api/live");
  live.source = source;
  source.addEventListener("snapshot", (event) => {
    setStatus("live", "Live");
    applyLive(JSON.parse(event.data), true);
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
    const response = await fetch(`/api/history?seconds=${past.range}`);
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
  drawVerdict(document.getElementById("history-verdict"), data.signals, "This range");
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
