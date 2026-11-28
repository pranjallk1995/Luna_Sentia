"use strict";

/* Luna Sentia's model monitor: the live GPU and calls from /api/live, the stored history from /api/history. */

const SVG = "http://www.w3.org/2000/svg";
const PHASES = [
  ["wait", "Waiting"],
  ["load", "Loading"],
  ["read", "Reading input"],
  ["write", "Writing output"],
];
const LEANINGS = { vllm: "vLLM", ollama: "Ollama", neutral: "Neutral" };
const CALL_ROWS = 25;

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
const hourMinute = (at) => new Date(at * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
const tokensPerSecond = (tokens, time) => (tokens && time ? tokens / (time / 1000) : null);
const plural = (n, word) => `${num(n)} ${word}${n === 1 ? "" : "s"}`;
/* "4 min", "2 h 5 min", "2 d 11 h": how long ago, or how long until. */
const span = (seconds) => {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ${Math.round((seconds % 3600) / 60)} min`;
  return `${Math.floor(seconds / 86400)} d ${Math.round((seconds % 86400) / 3600)} h`;
};

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

/* Replaces an element's markup only when it changed, so a button keeps its focus between the live updates. */
const drawn = new WeakMap();
function put(element, html) {
  if (drawn.get(element) === html) return;
  drawn.set(element, html);
  element.innerHTML = html;
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

/* The apps, their names and their colours: one colour per app wherever it appears. */

const APP_NAMES = {
  visia: "Luna Visia", lexia: "Luna Lexia", vibia: "Luna Vibia", socia: "Luna Socia", fiora: "Luna Fiora", vita: "Luna Vita",
  lumia: "Luna Lumia", unia: "Luna Unia", sentia: "the monitor, reloading it", "model-check": "model checks",
};
const appName = (app) => APP_NAMES[app] || app;
const SHORT_NAMES = { sentia: "Sentia", "model-check": "Checks" };
const shortName = (app) => SHORT_NAMES[app] || (APP_NAMES[app] ? APP_NAMES[app].replace("Luna ", "") : app);
const APP_COLORS = {
  socia: "#f472b6", lexia: "#38bdf8", visia: "#a78bfa", vibia: "#fbbf24", vita: "#4ade80",
  lumia: "#fb923c", fiora: "#2dd4bf", unia: "#e879f9",
};
const appColor = (app) => APP_COLORS[app] || "#a3a3a3";
// The Luna apps, in the order the page lists them; the monitor and model checks are not apps.
const APP_KEYS = ["socia", "visia", "lexia", "vibia", "vita", "fiora", "lumia", "unia"];
const appRank = (app) => (APP_KEYS.includes(app) ? APP_KEYS.indexOf(app) : APP_KEYS.length);
const byApp = (a, b) => appRank(a) - appRank(b) || a.localeCompare(b);
const RESIDENT_AFTER = 30 * 86400; // Ollama's "keep loaded for good" expires centuries from now.
const IMAGES_FOR = "Socia's pictures of Luna";
const IMAGE_STAGE = { reading: "reading the prompt", drawing: "drawing" };
// Blocks on the GPU that are not an app's: Ollama's models, the image model, and the rest.
const OLLAMA_COLORS = ["#c4b5fd", "#b394f0", "#9f7aea", "#8b5cf6"];
const IMAGE_COLOR = "#fdba74";
const OTHER_COLOR = "#4b4b4b";

/* The Ollama models each app is set up to use, as in the README's Models table
   (tests/test_monitor.py keeps the two the same). */
const APP_OLLAMA_MODELS = {
  socia: ["huihui_ai/qwen3-abliterated:8b", "huihui_ai/qwen3-vl-abliterated:8b-instruct", "qwen3-embedding:0.6b"],
  visia: ["huihui_ai/qwen3-vl-abliterated:8b-instruct"],
  lexia: ["huihui_ai/qwen3-vl-abliterated:8b-instruct", "qwen3-embedding:0.6b"],
  vibia: ["huihui_ai/qwen3-vl-abliterated:8b-instruct"],
  fiora: ["huihui_ai/qwen3-vl-abliterated:8b-instruct"],
};

/* The filter: the apps whose calls to show. None picked shows everything. The GPU stays whole: the card is shared. */
const filter = (() => {
  let saved = {};
  try {
    saved = JSON.parse(remembered("filter", "{}")) || {};
  } catch {
    /* an unreadable saved filter shows everything */
  }
  return { apps: new Set(saved.apps || []) };
})();
const filtering = () => filter.apps.size > 0;
const showsCall = (call) => !filter.apps.size || filter.apps.has(call.app);
const filterQuery = () => new URLSearchParams({ apps: [...filter.apps].join(",") }).toString();

/* The apps to pick from: those set up with Ollama models, those calling now or kept in memory, and any picked. */
function appChoices(counted) {
  const apps = new Set([...filter.apps, ...Object.keys(APP_OLLAMA_MODELS), ...counted.keys()]);
  return [...apps].sort(byApp);
}

function drawApps(counted, total) {
  const chip = (app, label, count, on, color) => `<button type="button" data-app="${esc(app)}" aria-pressed="${on}" style="--app:${color}">
    ${app ? '<i aria-hidden="true"></i>' : ""}${esc(label)}${count !== null ? ` <span>${num(count)}</span>` : ""}</button>`;
  const html = chip("", "All", total, !filtering(), "var(--lv-strong)") +
    appChoices(counted).map((app) => chip(app, shortName(app), counted.get(app) || 0, filter.apps.has(app), appColor(app))).join("");
  document.querySelectorAll("[data-apps]").forEach((element) => put(element, html));
}

function changeFilter() {
  remember("filter", JSON.stringify({ apps: [...filter.apps] }));
  connect();
  if (current === "live") drawLive();
  if (current === "history") loadHistory();
}

/* The four numbers of the Now row, each with a sparkline of the window. */

function sparkline(points, key, max, start, end) {
  const x = (t) => (((t - start) / (end - start || 1)) * 200).toFixed(1);
  const y = (v) => (34 - (Math.min(v, max) / (max || 1)) * 30).toFixed(1);
  const kept = points.filter((p) => p[key] !== null && p[key] !== undefined);
  if (kept.length < 2) return { line: "", area: "" };
  const line = `M${kept.map((p) => `${x(p.at)} ${y(p[key])}`).join(" L")}`;
  return { line, area: `${line} L${x(kept[kept.length - 1].at)} 36 L${x(kept[0].at)} 36 Z` };
}

function drawNow(container, points, { start, end, windowLabel }) {
  const last = [...points].reverse().find((p) => p.gpu_util !== null && p.gpu_util !== undefined) || points[points.length - 1] || {};
  const latest = points[points.length - 1] || {};
  const total = last.vram_total_mb || Math.max(...points.map((p) => p.vram_total_mb || 0), 0) || null;
  const peakFlight = Math.max(0, ...points.map((p) => p.in_flight || 0));
  const peakPower = Math.max(0, ...points.map((p) => p.power_w || 0));
  const powerMax = last.power_limit_w || Math.max(peakPower * 1.2, 1);
  const tile = (label, side, value, note, key, max) => {
    const { line, area } = sparkline(points, key, max, start, end);
    return `<div class="sm-tile"><span class="sm-tile-top"><span>${esc(label)}</span><span>${esc(side)}</span></span>
      <strong>${value}</strong>
      <svg viewBox="0 0 200 36" preserveAspectRatio="none" role="img" aria-label="${esc(label)} over the last ${esc(windowLabel)}"><path class="sm-area" d="${line ? area : ""}"></path><path class="sm-line" d="${line}"></path></svg>
      <span class="sm-tile-note">${note}</span></div>`;
  };
  put(container, [
    tile("GPU", live.gpu.name || "utilisation", `${num(last.gpu_util)}%`,
      `Memory bandwidth ${num(last.mem_util)}% · SM clock ${num(last.sm_clock_mhz)} MHz`, "gpu_util", 100),
    tile("VRAM", total ? `of ${gb(total)}` : "in use", gb(last.vram_used_mb),
      `Ollama's models ${gb(last.ollama_vram_mb)} of it`, "vram_used_mb", total || 1),
    tile("Requests", "in flight · waiting", `${num(latest.in_flight || 0)} · ${num(latest.waiting || 0)}`,
      `Most at once: ${num(peakFlight)} · Ollama runs ${num(live.parallel)} per model`, "in_flight", Math.max(2, peakFlight + 1)),
    tile("Power", last.temp_c !== undefined && last.temp_c !== null ? `${num(last.temp_c)} °C` : "", `${num(last.power_w)} W`,
      `Fan ${num(last.fan_pct)}% · limit ${last.power_limit_w ? `${num(last.power_limit_w)} W` : "–"}`, "power_w", powerMax),
  ].join(""));
}

/* What is on the GPU: every block of VRAM, whose it is, and what is not loaded. */

/* Who called each Ollama model: the calls kept in the database, and those in memory since. */
function callersOf(model, usage, calls) {
  const callers = new Map();
  for (const u of usage || []) if (u.model === model) callers.set(u.app, u.calls);
  for (const c of calls) if (c.model === model && !callers.has(c.app)) callers.set(c.app, 1);
  return [...callers.entries()].sort((x, y) => y[1] - x[1]).map(([app]) => app);
}

function gpuBlocks(now, calls) {
  const inventory = live.inventory || {};
  const blocks = [];
  const idle = [];
  live.models.forEach((m, index) => {
    const left = m.expires_at ? m.expires_at - now : null;
    const state = left === null ? "loaded" : left > RESIDENT_AFTER ? "stays loaded" : left > 1 ? `loaded · unloads in ${span(left)} if idle` : "unloading";
    const callers = callersOf(m.name, inventory.usage, calls);
    const what = [callers.length ? callers.map(shortName).join(", ") : "", m.context ? `${num(m.context)} context` : ""].filter(Boolean).join(" · ");
    blocks.push({ key: `ollama ${m.name}`, name: m.name, where: `Ollama${what ? ` · ${what}` : ""}`, mb: m.vram_mb || 0,
      color: OLLAMA_COLORS[index % OLLAMA_COLORS.length], state, label: m.name.split("/").pop() });
  });
  const loadedNames = new Set(live.models.map((m) => m.name));
  for (const m of inventory.ollama || []) if (!loadedNames.has(m.name)) idle.push({ group: "Ollama", color: OLLAMA_COLORS[1], name: m.name.split("/").pop(), full: m.name, size: gb(m.size_mb) });
  // The image model is loaded only while a picture is made: its text encoder, then its diffusion model.
  const images = inventory.images;
  if (images && !images.problem && images.drawing) {
    const part = images.stage === "reading" ? "text encoder" : "diffusion model";
    blocks.push({ key: "images", name: "Qwen-Image", where: `Sentia imagegen · ${IMAGES_FOR} · its ${part}`, mb: images.vram_mb || 0,
      color: IMAGE_COLOR, state: `${IMAGE_STAGE[images.stage] || "working"} now`, busy: true, label: "Qwen-Image" });
  } else if (images && images.problem) {
    idle.push({ group: "Image model", color: IMAGE_COLOR, name: "Qwen-Image", size: "", note: images.problem });
  } else if (images) {
    const size = (images.files || []).reduce((n, f) => n + (f.size_mb || 0), 0);
    idle.push({ group: "Image model", color: IMAGE_COLOR, name: "Qwen-Image", size: size ? gb(size) : "", note: "loads for each picture" });
  }
  for (const app of live.appModels || []) {
    if (app.problem) {
      idle.push({ group: shortName(app.app), color: appColor(app.app), name: "its models", size: "", note: app.problem });
      continue;
    }
    const held = app.gpu ? app.gpu.reserved_mb : null;
    const measured = app.gpu && app.gpu.measured;
    const loaded = app.models.filter((m) => m.loaded);
    for (const m of app.models) {
      if (!m.loaded) idle.push({ group: shortName(app.app), color: appColor(app.app), name: m.name, size: m.vram_mb ? gb(m.vram_mb) : "", note: m.vram_mb ? "" : "size shown once loaded" });
    }
    if (!loaded.length) continue;
    const stateOf = (models) => (models.some((m) => m.active) ? "working now" : "loaded");
    if (measured) {
      // Measured per model (Socia): one block each, and the CUDA runtime they share.
      for (const m of loaded) {
        blocks.push({ key: `${app.app} ${m.name}`, name: m.name, where: `${shortName(app.app)} · ${m.purpose}`, mb: m.vram_mb && !m.vram_last_seen ? m.vram_mb : 0,
          color: appColor(app.app), state: stateOf([m]), busy: m.active, label: m.name, device: m.device });
      }
      if (app.gpu.context_mb > 0) {
        blocks.push({ key: `${app.app} cuda`, name: `CUDA runtime`, where: `${shortName(app.app)} · shared by its models`, mb: app.gpu.context_mb,
          color: appColor(app.app), state: "loaded", label: "CUDA", faded: true });
      }
    } else {
      // Otherwise the app's whole process is one block, holding all its loaded models.
      const onGpu = loaded.filter((m) => !/CPU/.test(m.device || ""));
      const onCpu = loaded.filter((m) => /CPU/.test(m.device || ""));
      if (onGpu.length) {
        blocks.push({ key: `${app.app} process`, name: onGpu.map((m) => m.name).join(", "), where: `${shortName(app.app)} · ${onGpu.map((m) => m.purpose).filter(Boolean).join("; ")}`,
          mb: held || 0, color: appColor(app.app), state: stateOf(onGpu), busy: onGpu.some((m) => m.active), label: shortName(app.app) });
      }
      for (const m of onCpu) {
        blocks.push({ key: `${app.app} ${m.name}`, name: m.name, where: `${shortName(app.app)} · ${m.purpose} · on the CPU`, mb: 0,
          color: appColor(app.app), state: stateOf([m]), busy: m.active, label: m.name });
      }
    }
  }
  const last = live.samples[live.samples.length - 1] || {};
  const total = last.vram_total_mb || null;
  const used = last.vram_used_mb ?? null;
  const counted = blocks.reduce((n, b) => n + b.mb, 0);
  if (used !== null && used - counted > 64) {
    blocks.push({ key: "other", name: "Everything else", where: "CUDA contexts, Windows and other processes", mb: used - counted,
      color: OTHER_COLOR, state: "always", label: "other" });
  }
  return { blocks, idle, total, used };
}

function drawGpu(now, calls) {
  const { blocks, idle, total, used } = gpuBlocks(now, calls);
  const picked = blocks.some((b) => b.key === live.block) ? live.block : null;
  document.getElementById("gpu-summary").innerHTML = total
    ? `${esc(live.gpu.name || "GPU")} · <b>${gb(used)}</b> of ${gb(total)} · ${gb(Math.max(total - used, 0))} free`
    : esc(live.gpu.problem || "Waiting for the GPU's first sample.");
  const bar = document.getElementById("gpu-bar");
  bar.setAttribute("aria-label", `VRAM: ${blocks.filter((b) => b.mb).map((b) => `${b.name} ${gb(b.mb)}`).join(", ") || "nothing reported yet"}`);
  const dim = (b) => (picked && picked !== b.key ? " sm-dimmed" : "");
  put(bar, total ? blocks.filter((b) => b.mb > 0).map((b) => `<button type="button" class="sm-vram-block${dim(b)}${b.faded ? " sm-faded" : ""}" data-block="${esc(b.key)}"
      style="width:${Math.min((b.mb / total) * 100, 100).toFixed(2)}%;--fill:${b.color}" title="${esc(`${b.name} · ${gb(b.mb)}`)}" aria-label="${esc(`${b.name}, ${gb(b.mb)}`)}">
      <span>${esc(b.label)}</span></button>`).join("") : "");
  put(document.getElementById("gpu-blocks"), blocks.length ? blocks.map((b) => `<button type="button" class="sm-block${dim(b)}" data-block="${esc(b.key)}" aria-pressed="${picked === b.key}">
      <i style="--fill:${b.color}" class="${b.faded ? "sm-faded" : ""}" aria-hidden="true"></i>
      <span class="sm-block-name"><strong>${esc(b.name)}</strong><span>${esc(b.where)}</span></span>
      <span class="sm-block-size"><strong>${b.mb ? gb(b.mb) : b.device && /CPU/.test(b.device) ? "CPU" : "–"}</strong><span class="${b.busy ? "sm-busy" : ""}">${esc(b.state)}</span></span>
    </button>`).join("") : `<p class="sm-empty">No model is loaded. Ollama loads one on the next call, which adds loading time to it.</p>`);
  // Not loaded now: one row per place a model runs, its models as chips with their size.
  const groups = new Map();
  for (const item of idle) groups.set(item.group, [...(groups.get(item.group) || []), item]);
  put(document.getElementById("gpu-idle"), groups.size ? `<h3 class="sm-idle-title">Not loaded now <span class="sm-dim">${plural(idle.length, "model")}</span></h3>
    <dl class="sm-idle">${[...groups.entries()].map(([group, items]) => `<div class="sm-idle-row">
      <dt><i style="background:${items[0].color}" aria-hidden="true"></i>${esc(group)}</dt>
      <dd><ul>${items.map((m) => `<li title="${esc([m.full || m.name, m.note].filter(Boolean).join(" · "))}"><span>${esc(m.name)}</span>${m.size ? `<b>${esc(m.size)}</b>` : ""}${m.note && !m.size ? `<span class="sm-dim">${esc(m.note)}</span>` : ""}</li>`).join("")}</ul></dd>
    </div>`).join("")}</dl>` : "");
}

/* The call lanes: one row per app, each call split into its phases. */

function phases(call) {
  const wall = call.wall_ms || 0;
  if (call.outcome !== "ok" || call.load_ms === null || call.load_ms === undefined) return null;
  const parts = { load: call.load_ms || 0, read: call.prompt_ms || 0, write: call.output_ms || 0 };
  parts.wait = Math.max(wall - parts.load - parts.read - parts.write, 0);
  return parts;
}

function callRows(call) {
  const rows = [
    ["App", appName(call.app)], ["Model", call.model], ["Endpoint", call.endpoint], ["Started", clock(call.started_at)],
    ["Outcome", call.outcome], ["Wall time", ms(call.wall_ms ?? (Date.now() / 1000 + live.offset - call.started_at) * 1000)],
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
  const labelWidth = 72;
  const plotWidth = width - labelWidth;
  const laneHeight = 22;
  const x = (t) => labelWidth + ((Math.min(Math.max(t, start), end) - start) / (end - start)) * plotWidth;
  // Calls of one app that overlap go in separate lanes of its row.
  const apps = [...new Set(visible.map((c) => c.app))].sort(byApp);
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
  const gap = 6;
  for (const app of apps) {
    rowTop.set(app, top);
    top += Math.max(lanes.get(app).length, 1) * laneHeight + 8 + gap;
  }
  const height = top + 14;
  container.innerHTML = "";
  container.classList.add("sm-timeline");
  const plot = svg("svg", { viewBox: `0 0 ${width} ${height}`, height, role: "img", "aria-label": `Lanes of ${visible.length} model calls by app` }, container);
  for (const app of apps) {
    const rowHeight = Math.max(lanes.get(app).length, 1) * laneHeight + 8;
    svg("rect", { class: "sm-lane", x: labelWidth, y: rowTop.get(app), width: plotWidth, height: rowHeight, rx: 6 }, plot);
    const label = svg("text", { class: "sm-row-label", x: 0, y: rowTop.get(app) + rowHeight / 2 + 4, fill: appColor(app) }, plot);
    label.textContent = shortName(app);
  }
  const ticks = plotWidth < 460 ? 2 : 4;
  for (let i = 0; i <= ticks; i++) {
    const px = labelWidth + (plotWidth * i) / ticks;
    const text = svg("text", { class: "sm-grid-label", x: px, y: height - 2, "text-anchor": i === 0 ? "start" : i === ticks ? "end" : "middle" }, plot);
    const ago = ((end - start) * (ticks - i)) / ticks;
    text.textContent = i === ticks ? "now" : `−${ago >= 60 ? `${num(ago / 60, ago % 60 ? 1 : 0)} min` : `${Math.round(ago)} s`}`;
  }
  const pxPerSecond = plotWidth / (end - start);
  for (const { call, lane } of placed) {
    const y = rowTop.get(call.app) + 4 + lane * laneHeight + 2;
    const h = laneHeight - 6;
    const x0 = x(call.started_at);
    const x1 = Math.max(x(call.finished_at ?? now), x0 + 3);
    const group = svg("g", { class: "sm-call" }, plot);
    const split = phases(call);
    if (call.outcome === "running") {
      svg("rect", { class: "sm-seg-run", x: x0, y, width: x1 - x0, height: h, rx: 3 }, group);
    } else if (!split) {
      svg("rect", { class: call.outcome === "ok" ? "sm-seg-wait" : "sm-seg-fail", x: x0, y, width: x1 - x0, height: h, rx: 3 }, group);
    } else {
      // Phases in order from the start: waiting, loading, reading, writing.
      let cursor = x(call.started_at) - (call.started_at < start ? (start - call.started_at) * pxPerSecond : 0);
      for (const [key] of PHASES) {
        const w = (split[key] / 1000) * pxPerSecond;
        const from = Math.max(cursor, labelWidth);
        const to = Math.min(cursor + w, labelWidth + plotWidth);
        if (to - from >= 1) svg("rect", { class: `sm-seg-${key}`, x: from, y, width: Math.max(to - from, 1), height: h }, group);
        cursor += w;
      }
      svg("rect", { class: "sm-seg-ring", x: x0, y, width: x1 - x0, height: h, rx: 3 }, group);
    }
    const hit = svg("rect", { class: "sm-hit", x: x0 - 3, y: y - 2, width: x1 - x0 + 6, height: h + 4 }, group);
    hit.addEventListener("pointermove", (event) => showTip(event, `${appName(call.app)} · ${call.model}`, callRows(call)));
    hit.addEventListener("pointerleave", hideTip);
    hit.addEventListener("click", () => {
      live.openCall = callKey(call);
      drawLive();
      document.querySelector(`[data-call="${CSS.escape(live.openCall)}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  }
}

/* Recent calls: one line each with its phases; click one for its numbers. */

const callKey = (call) => `${call.app} ${call.started_at}`;
function phaseBar(call) {
  const split = phases(call);
  if (!split) {
    const kind = call.outcome === "running" ? "run" : call.outcome === "ok" ? "wait" : "fail";
    return `<span class="sm-phasebar" aria-hidden="true"><i class="sm-seg-${kind}-bar" style="width:100%"></i></span>`;
  }
  const whole = Object.values(split).reduce((a, b) => a + b, 0) || 1;
  return `<span class="sm-phasebar" aria-hidden="true">${PHASES.map(([key]) => `<i class="sm-${key}" style="width:${((split[key] / whole) * 100).toFixed(1)}%"></i>`).join("")}</span>`;
}
const OUTCOME_CLASS = { ok: "good", running: "run", cancelled: "warn" };

function drawCalls(container, calls) {
  if (!calls.length) {
    put(container, `<p class="sm-empty">${filtering() ? "No calls from the chosen apps yet." : "No model calls yet. They appear here as the apps make them."}</p>`);
    return;
  }
  put(container, calls.slice(0, CALL_ROWS).map((c) => {
    const key = callKey(c);
    const open = live.openCall === key;
    const what = [c.endpoint.replace("/api/", ""), c.model, c.images ? plural(c.images, "image") : ""].filter(Boolean).join(" · ");
    return `<div class="sm-call-row${open ? " sm-open" : ""}">
      <button type="button" data-call="${esc(key)}" aria-expanded="${open}">
        <span class="sm-call-at">${esc(clock(c.started_at))}</span>
        <span class="sm-call-app" style="color:${appColor(c.app)}">${esc(shortName(c.app))}</span>
        <span class="sm-call-what">${esc(what)}</span>
        ${phaseBar(c)}
        <span class="sm-call-wall">${c.outcome === "running" ? "running" : ms(c.wall_ms)}</span>
        <span class="sm-outcome ${OUTCOME_CLASS[c.outcome] || "bad"}">${esc(c.outcome)}</span>
      </button>
      ${open ? `<dl class="sm-call-facts">${callRows(c).map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>` : ""}
    </div>`;
  }).join(""));
}

/* The verdict: a meter leaning to Ollama or vLLM, and the findings behind it. */

function drawVerdict(container, signals, scope) {
  if (!signals) {
    put(container, `<p class="sm-empty">Measuring…</p>`);
    return;
  }
  const value = (f) => (f.key === "headroom" ? gb(f.value) : pct(f.value));
  const toVllm = signals.findings.filter((f) => f.leaning === "vllm").length;
  const toOllama = signals.findings.filter((f) => f.leaning === "ollama").length;
  const lean = Math.min(Math.max(50 + (50 * (toVllm - toOllama)) / Math.max(signals.findings.length, 1), 4), 96);
  const cut = signals.verdict.search(/[:.] /);
  const headline = cut > 0 ? signals.verdict.slice(0, cut) : signals.verdict;
  const reason = cut > 0 ? signals.verdict.slice(cut + 2) : "";
  put(container, `
    <div class="sm-verdict-head"><span class="sm-note">Should you switch to vLLM?</span><strong>${esc(headline)}</strong>${reason ? `<span class="sm-note">${esc(reason)}</span>` : ""}</div>
    ${signals.findings.length ? `<div class="sm-meter-lean" role="img" aria-label="${esc(`${plural(toVllm, "finding")} point to vLLM, ${toOllama} to Ollama`)}">
      <div class="sm-meter-track"><i style="left:${lean.toFixed(1)}%"></i></div><div class="sm-meter-ends"><span>Ollama</span><span>vLLM</span></div></div>` : ""}
    ${signals.findings.map((f) => `<div class="sm-finding">
      <div class="sm-finding-top"><span>${esc(f.label)}</span><span class="sm-lean ${esc(f.leaning)}">${esc(LEANINGS[f.leaning] || f.leaning)}</span></div>
      <b>${value(f)}</b><p>${esc(f.note)}</p></div>`).join("")}
    <p class="sm-note">${esc(scope)} · <button type="button" class="sm-link" data-section="guide">how these rules of thumb work</button></p>`);
}

function drawTotals(container, signals, title) {
  if (!signals) return put(container, "");
  const facts = [
    ["Calls", num(signals.calls)], ["Problems", num(signals.problems)], ["Busy", ms(signals.busy_seconds * 1000)],
    ["Overlapping", ms(signals.overlap_seconds * 1000)], ["Output", `${num(signals.output_rate, 1)} tok/s`],
    ["Input", `${num(signals.prompt_rate)} tok/s`], ["GPU while busy", signals.gpu_util_busy === null ? "–" : `${num(signals.gpu_util_busy)}%`],
    ["Most at once", num(signals.peak_in_flight)],
  ];
  put(container, `<div class="sm-card-head"><h2>${esc(title)}</h2></div>
    <dl class="sm-facts-grid">${facts.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>`);
}

function phaseLegends() {
  const html = PHASES.map(([key, label]) => `<span><i class="sm-swatch sm-${key}"></i>${label}</span>`).join("") +
    `<span><i class="sm-swatch sm-running"></i>Running now</span>`;
  document.querySelectorAll('[data-legend="phases"]').forEach((el) => (el.innerHTML = html));
}

/* Live */

const live = {
  samples: [], calls: [], inFlight: [], models: [], signals: null, parallel: 1, gpu: {}, offset: 0,
  window: Number(remembered("window", "300")), source: null, engineProblem: null, block: null, openCall: null,
};
const WINDOW_LABELS = { 60: "minute", 300: "5 minutes", 600: "10 minutes" };
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
  const windowLabel = WINDOW_LABELS[live.window] || `${live.window} s`;
  drawNow(document.getElementById("live-now"), live.samples.filter((s) => s.at >= start - 2), { start, end: now, windowLabel });
  const running = live.inFlight.map((c) => ({ ...c, outcome: "running" }));
  const all = [...running, ...live.calls];
  drawGpu(now, all);
  const counted = new Map();
  for (const c of all) counted.set(c.app, (counted.get(c.app) || 0) + 1);
  drawApps(counted, all.length);
  const shown = all.filter(showsCall);
  drawTimeline(document.getElementById("live-timeline"), shown, { start, end: now, now });
  drawCalls(document.getElementById("live-calls"), shown);
  document.getElementById("live-calls-note").textContent = `The last ${Math.min(shown.length, CALL_ROWS)} of ${num(shown.length)}${filtering() ? " chosen" : ""} kept in memory · click one for its numbers`;
  const scope = filtering() ? "The chosen apps' calls in the last few minutes, from memory" : "Calls in the last few minutes, from memory";
  drawVerdict(document.getElementById("live-verdict"), live.signals, scope);
  drawTotals(document.getElementById("live-totals"), live.signals, "In the last few minutes");
  const problems = [live.gpu.problem, live.engineProblem].filter(Boolean);
  const alert = document.getElementById("live-problem");
  alert.textContent = problems.join(" · ");
  alert.hidden = !problems.length;
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

const past = { range: Number(remembered("range", "86400")), data: null, disk: null, diskAt: 0 };
const RANGE_LABELS = { 3600: "hour", 21600: "6 hours", 86400: "24 hours", 604800: "7 days", 2592000: "30 days" };

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
  if (Date.now() - past.diskAt > 5 * 60000) loadDisk();
}

async function loadDisk() {
  past.diskAt = Date.now();
  try {
    const response = await fetch("/api/storage");
    const data = await response.json();
    past.disk = response.ok ? data : { problem: data.error || "Disk use could not be read." };
  } catch {
    past.disk = { problem: "Disk use could not be read." };
  }
  drawDisk();
}

function drawHistory() {
  const data = past.data;
  if (!data) return;
  const counted = new Map();
  for (const g of data.groups || []) counted.set(g.app, (counted.get(g.app) || 0) + g.calls);
  drawApps(counted, [...counted.values()].reduce((a, b) => a + b, 0));
  const range = data.end - data.start;
  const shown = data.calls.length < data.calls_total ? ` · verdict from the latest ${num(data.calls.length)} of ${num(data.calls_total)} calls` : "";
  document.getElementById("history-note").textContent = `From the monitor's database · the last ${RANGE_LABELS[past.range] || span(range)}${shown}`;
  drawHistoryTotals(data, range);
  drawBusy(data);
  drawAppCalls(document.getElementById("history-apps"), data.groups || []);
  drawVerdict(document.getElementById("history-verdict"), data.signals, filtering() ? "The chosen apps' calls in this range" : "This range");
  drawEvents(document.getElementById("history-events"), data.events || []);
}

function drawHistoryTotals(data, range) {
  const signals = data.signals || {};
  const summary = data.summary || {};
  const groups = data.groups || [];
  const calls = data.calls_total ?? groups.reduce((n, g) => n + g.calls, 0);
  const problems = summary.problems ?? groups.reduce((n, g) => n + g.problems, 0);
  const peak = Math.max(0, ...data.samples.map((s) => s.vram_used_mb || 0));
  const total = Math.max(0, ...data.samples.map((s) => s.vram_total_mb || 0));
  const tiles = [
    ["Calls", num(calls), `${plural(new Set(groups.map((g) => g.app)).size, "app")}, ${plural(new Set(groups.map((g) => g.model)).size, "model")}`, ""],
    ["Problems", num(problems), calls ? `${num((problems / calls) * 100, 1)}% of calls` : "no calls", problems ? "warn" : ""],
    ["GPU busy", ms((signals.busy_seconds || 0) * 1000), `${num(((signals.busy_seconds || 0) / range) * 100, 1)}% of the time`, ""],
    ["Typical call", ms(summary.wall_p50), `slowest 5%: ${ms(summary.wall_p95)}`, ""],
    ["Output", `${num(signals.output_rate, 1)} tok/s`, `input ${num(signals.prompt_rate)} tok/s`, ""],
    ["Peak VRAM", peak ? gb(peak) : "–", total ? `${gb(total - peak)} from full` : "", total && total - peak < 1024 ? "warn" : ""],
  ];
  put(document.getElementById("history-totals"), tiles.map(([k, v, note, tone]) => `<div class="sm-total"><span>${esc(k)}</span><strong>${esc(v)}</strong>
    <span class="${tone ? `sm-${tone}` : ""}">${esc(note)}</span></div>`).join(""));
}

/* When the GPU was busy: a bar per stretch of the range, split by app, and VRAM over them. */
function drawBusy(data) {
  const container = document.getElementById("history-busy");
  const bar = data.busy_bar_seconds;
  if (!bar || !data.busy) {
    put(container, `<p class="sm-empty">No busy time to show.</p>`);
    return;
  }
  const bars = Math.round((data.end - data.start) / bar);
  const columns = Array.from({ length: bars }, (_, i) => ({ at: data.start + i * bar, parts: new Map() }));
  for (const row of data.busy) {
    const column = columns[Math.min(Math.max(Math.floor((row.at - data.start) / bar + 0.001), 0), bars - 1)];
    const part = column.parts.get(row.app) || { busy: 0, calls: 0 };
    part.busy += row.busy_seconds || 0;
    part.calls += row.calls || 0;
    column.parts.set(row.app, part);
  }
  const apps = [...new Set(data.busy.map((row) => row.app))].sort(byApp);
  // The bars' scale: the busiest bar, rounded up to a step, so a quiet range still shows its calls.
  const busiest = Math.max(...columns.map((c) => [...c.parts.values()].reduce((n, p) => n + p.busy, 0) / bar), 0);
  const top = [0.05, 0.1, 0.25, 0.5, 1].find((step) => busiest <= step) || 1;
  const long = data.end - data.start > 86400;
  const when = (at) => (long ? clock(at, true) : hourMinute(at));
  const total = Math.max(0, ...data.samples.map((s) => s.vram_total_mb || 0)) || 1;
  const points = data.samples.filter((s) => s.vram_used_mb !== null && s.vram_used_mb !== undefined)
    .map((s) => `${(((s.at - data.start) / (data.end - data.start)) * 1000).toFixed(1)} ${(100 - (s.vram_used_mb / total) * 100).toFixed(1)}`);
  const html = `<div class="sm-busy">
      <span class="sm-busy-y" aria-hidden="true"><span>${Math.round(top * 100)}%</span><span>${num(top * 50, top < 0.1 ? 1 : 0)}%</span></span>
      <div class="sm-busy-plot">
        ${columns.map((column) => {
          const parts = [...column.parts.entries()].sort((a, b) => byApp(a[0], b[0]));
          const sum = parts.reduce((n, [, p]) => n + p.busy, 0);
          const label = `${when(column.at)} to ${when(column.at + bar)}: ${sum ? parts.map(([app, p]) => `${shortName(app)} ${ms(p.busy * 1000)} in ${plural(p.calls, "call")}`).join(", ") : "idle"}`;
          return `<span class="sm-busy-col" title="${esc(label)}" aria-label="${esc(label)}" role="img">${parts.map(([app, p]) =>
            `<i style="height:${Math.min((p.busy / bar / top) * 100, 100).toFixed(2)}%;background:${appColor(app)}"></i>`).join("")}</span>`;
        }).join("")}
        ${points.length > 1 ? `<svg viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true"><path d="M${points.join(" L")}"></path></svg>` : ""}
      </div>
    </div>
    <div class="sm-busy-x" aria-hidden="true">${[0, 0.25, 0.5, 0.75].map((f) => `<span>${esc(when(data.start + f * (data.end - data.start)))}</span>`).join("")}<span>now</span></div>
    <div class="sm-legend">${apps.map((app) => `<span><i class="sm-swatch" style="background:${appColor(app)}"></i>${esc(shortName(app))}</span>`).join("")}
      <span><i class="sm-swatch dash"></i>VRAM in use (top: full)</span></div>`;
  put(container, apps.length ? html : `<p class="sm-empty">No calls in this range.</p>`);
  document.getElementById("busy-note").textContent = `Each bar: the share of ${span(bar)} spent on calls, by app · top of the scale: ${Math.round(top * 100)}%`;
}

function drawAppCalls(container, groups) {
  if (!groups.length) {
    put(container, `<p class="sm-empty">No calls in this range.</p>`);
    return;
  }
  const max = Math.max(...groups.map((g) => g.wall_p95 || 0), 1);
  const sorted = groups.slice().sort((a, b) => byApp(a.app, b.app) || b.calls - a.calls);
  put(container, sorted.map((g) => {
    const out = tokensPerSecond(g.output_tokens, g.output_ms);
    const wait = g.wall_sum ? g.wait_sum / g.wall_sum : null;
    return `<div class="sm-app-row">
      <span class="sm-app-name" style="color:${appColor(g.app)}">${esc(shortName(g.app))}</span>
      <span class="sm-app-bars">
        <span class="sm-range" aria-hidden="true"><i class="sm-p95" style="width:${(((g.wall_p95 || 0) / max) * 100).toFixed(1)}%;--app:${appColor(g.app)}"></i><i class="sm-p50" style="width:${(((g.wall_p50 || 0) / max) * 100).toFixed(1)}%;--app:${appColor(g.app)}"></i></span>
        <span class="sm-note">${esc(g.model)} · typical ${ms(g.wall_p50)} · slowest ${ms(g.wall_p95)}${out ? ` · ${num(out, 1)} tok/s out` : ""}${wait !== null ? ` · ${pct(wait)} waiting` : ""}</span>
      </span>
      <span class="sm-app-count"><strong>${num(g.calls)}</strong><span>calls</span></span>
      <span class="sm-app-count"><strong class="${g.problems ? "sm-warn" : ""}">${num(g.problems)}</strong><span>problems</span></span>
    </div>`;
  }).join(""));
}

function drawEvents(container, events) {
  if (!events.length) {
    put(container, `<p class="sm-empty">Ollama did not load or unload a model in this range.</p>`);
    return;
  }
  const long = past.range > 86400;
  const loads = events.filter((e) => e.event === "loaded").length;
  put(container, `<ul class="sm-events">${events.slice().reverse().slice(0, 12).map((e) => {
    const loaded = e.event === "loaded";
    return `<li><span class="sm-dim">${esc(long ? clock(e.at, true) : hourMinute(e.at))}</span>
      <span class="sm-sign ${loaded ? "sm-good" : "sm-bad"}" aria-label="${esc(e.event)}">${loaded ? "+" : "−"}</span>
      <span class="sm-event-model" title="${esc(`${e.model}${e.context ? ` · ${num(e.context)} context` : ""}`)}">${esc(e.model.split("/").pop())}</span>
      <span class="sm-dim">${gb(e.vram_mb)}</span></li>`;
  }).join("")}</ul>
  <p class="sm-note">${plural(loads, "load")} and ${plural(events.length - loads, "unload")} in this range${events.length > 12 ? ", the latest 12 shown" : ""}</p>`);
}

function drawDisk() {
  const container = document.getElementById("history-disk");
  const disk = past.disk;
  if (!disk) return put(container, `<p class="sm-empty">Measuring…</p>`);
  if (disk.problem) return put(container, `<p class="sm-empty">${esc(disk.problem)}</p>`);
  const apps = (disk.apps || []).filter((a) => a.total_bytes > 0);
  const max = Math.max(...apps.map((a) => a.total_bytes), 1);
  document.getElementById("disk-total").textContent = disk.volumes_size ? `${disk.volumes_size} in all` : "";
  put(container, `${apps.length ? `<ul class="sm-disk">${apps.map((a) => `<li title="${esc([...a.databases, ...a.volumes].map((v) => `${v.name}: ${v.size || "–"}`).join("\n"))}">
      <span class="sm-disk-top"><span>${esc(APP_NAMES[a.app] ? shortName(a.app) : a.app)}</span><span class="sm-dim">${esc(a.total)}</span></span>
      <span class="sm-meter" aria-hidden="true"><i style="width:${((a.total_bytes / max) * 100).toFixed(1)}%"></i></span></li>`).join("")}</ul>` : `<p class="sm-empty">Nothing measured yet.</p>`}
    ${(disk.notes || []).map((note) => `<p class="sm-note">${esc(note)}</p>`).join("")}`);
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

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-section], [data-app], [data-block], [data-call], [data-window], [data-range]");
  if (!target) return;
  const { section, app, block, call, window: win, range } = target.dataset;
  if (section) {
    show(section);
    if (section === "guide") window.scrollTo({ top: 0 });
  } else if (app !== undefined) {
    if (!app) filter.apps.clear();
    else if (filter.apps.has(app)) filter.apps.delete(app);
    else filter.apps.add(app);
    changeFilter();
  } else if (block) {
    live.block = live.block === block ? null : block;
    drawLive();
  } else if (call) {
    live.openCall = live.openCall === call ? null : call;
    drawLive();
  } else if (win) {
    live.window = Number(win);
    remember("window", live.window);
    pressed("[data-window]", "window", live.window);
    drawLive();
  } else if (range) {
    past.range = Number(range);
    remember("range", past.range);
    pressed("[data-range]", "range", past.range);
    loadHistory();
  }
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
