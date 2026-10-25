// #history (§7.5): мощность за 24 год / 7 днів / 30 днів, таблица дней, Sankey «Звідки й куди — сьогодні».
import { t, tp, getLang } from "./i18n.js";
import { isNum, number, energy, power, pct, time, date, duration } from "./fmt.js";
import {
  cssVar, alpha, theme, changed, mountChart, destroyChart, baseOpts, xAxis, yAxis,
  nightIntervals, nightHook, tooltipPlugin, renderLegend, withGaps,
} from "./charts.js";

const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";
const RANGES = { "24h": { hours: 24, step: 300 }, "7d": { hours: 168, step: 1800 }, "30d": { hours: 720, step: 3600 } };
const FIELDS = "pv1_power_w,pv2_power_w,load_power_w,grid_power_w";
const num = (v) => (isNum(v) ? Number(v) : null);

let range = "24h";
let reqRange = range;
let cb = { onRange() {} };

export function histUrl() {
  reqRange = range;
  const r = RANGES[range];
  return "/api/samples?hours=" + r.hours + "&step=" + r.step + "&fields=" + FIELDS;
}
// тег диапазона к ответу (запрос, перебитый сменой диапазона, отменяет poll.js)
export const histTag = (rows) => ({ range: reqRange, rows: Array.isArray(rows) ? rows : [] });

let reqSrc = range;
export function srcUrl() {
  reqSrc = range;
  return "/api/load-sources?hours=" + RANGES[range].hours;
}
export const srcTag = (d) => ({ ...(d && typeof d === "object" ? d : {}), range: reqSrc });

export function initHistory(callbacks) {
  cb = { ...cb, ...callbacks };
  $("hist-seg").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-range]");
    if (!b || b.dataset.range === range) return;
    range = b.dataset.range;
    document.querySelectorAll("#hist-seg button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    $("hist-chart").classList.add("loading");
    $("src-chart").classList.add("loading");
    cb.onRange();
  });
}

// ===== График мощности =====
function renderPower(s) {
  const el = $("hist-chart");
  const h = s.hist;
  if (!h && s.errors.hist) { el.classList.remove("loading"); $("hist-empty").hidden = false; return; }
  if (!h || h.range !== range) return;
  el.classList.remove("loading");
  if (!changed("hist", [h, s.tariff, getLang(), theme()])) return;
  const step = RANGES[range].step;
  const rows = withGaps(h.rows.filter((r) => isNum(r.ts)), step);
  if (!h.rows.length) {
    destroyChart(el);
    $("hist-empty").hidden = false;
    el.setAttribute("aria-label", t("history.chart.empty"));
    renderLegend($("hist-legend"), []);
    return;
  }
  $("hist-empty").hidden = true;
  const ts = rows.map((r) => r.ts);
  const pv = rows.map((r) => {
    const a = num(r.pv1_power_w), b = num(r.pv2_power_w);
    return a === null && b === null ? null : ((a || 0) + (b || 0)) / 1000;
  });
  const load = rows.map((r) => (num(r.load_power_w) === null ? null : num(r.load_power_w) / 1000));
  const grid = rows.map((r) => (num(r.grid_power_w) === null ? null : Math.max(0, num(r.grid_power_w)) / 1000));
  const c = { sun: cssVar("--sun"), home: cssVar("--home"), grid: cssVar("--grid") };
  const from = ts[0], to = ts[ts.length - 1];
  const night = range === "30d" ? [] : nightIntervals(s.tariff, from, to);
  const kw = (x) => power(x === null ? null : x * 1000);
  const opts = {
    ...baseOpts(),
    scales: { x: { time: true }, y: { range: (u, lo, hi) => [0, Math.max(1, hi || 0) * 1.08] } },
    series: [{},
      { stroke: c.sun, fill: alpha(c.sun, 0.45), width: 1.5, points: { show: false } },
      { stroke: c.home, width: 2, points: { show: false } },
      { stroke: c.grid, width: 1.5, points: { show: false } }],
    axes: [xAxis(), yAxis((x) => number(x, 1), { label: t("unit.kw"), labelSize: 16 })],
    hooks: { drawClear: [nightHook(night)] },
    plugins: [tooltipPlugin((u, i) => {
      if (pv[i] === null && load[i] === null && grid[i] === null) return null;
      return {
        title: date(ts[i]) + " " + time(ts[i]),
        rows: [
          { color: c.sun, label: t("history.pv"), value: kw(pv[i]) },
          { color: c.home, label: t("history.load"), value: kw(load[i]) },
          { color: c.grid, label: t("history.import"), value: kw(grid[i]) },
        ],
      };
    })],
  };
  mountChart(el, opts, [ts, pv, load, grid]);
  const legend = [
    { type: "box", color: "color-mix(in srgb, var(--sun) 45%, transparent)", label: t("history.pv") },
    { type: "line", color: "var(--home)", label: t("history.load") },
    { type: "line", color: "var(--grid)", label: t("history.import") },
  ];
  if (night.length) legend.push({ type: "box", color: "var(--night)", label: t("chart.night") });
  renderLegend($("hist-legend"), legend);

  // вывод для screen reader
  let pvMaxI = -1;
  pv.forEach((v, i) => { if (v !== null && (pvMaxI < 0 || v > pv[pvMaxI])) pvMaxI = i; });
  const ld = load.filter((v) => v !== null);
  const gr = grid.filter((v) => v !== null);
  el.setAttribute("aria-label", t("history.chart.aria", {
    period: t("history.range." + range),
    pvMax: pvMaxI >= 0 ? kw(pv[pvMaxI]) : "—",
    pvTime: pvMaxI >= 0 ? date(ts[pvMaxI]) + " " + time(ts[pvMaxI]) : "—",
    loadAvg: ld.length ? kw(ld.reduce((a, b) => a + b, 0) / ld.length) : "—",
    gridMax: gr.length ? kw(Math.max(...gr)) : "—",
  }));
}

// ===== Чим живився дім =====
const SRC_KEYS = [["pv_kwh", "sun", "pv"], ["batt_kwh", "batt", "batt"], ["grid_kwh", "grid", "grid"]];

function barSeries(color) {
  return {
    stroke: color, fill: color, width: 0, points: { show: false }, fillTo: () => 0,
    paths: window.uPlot.paths.bars({ size: [0.78, 48], gap: 1 }),
  };
}

function renderSources(s) {
  const el = $("src-chart"), empty = $("src-empty"), tot = $("src-tot");
  const d = s.loadsrc;
  if (!d && s.errors.loadsrc) { el.classList.remove("loading"); empty.hidden = false; return; }
  if (!d || d.range !== range) return;
  el.classList.remove("loading");
  if (!changed("src", [d, s.tariff, getLang(), theme()])) return;
  const pts = (Array.isArray(d.points) ? d.points : []).filter((p) => isNum(p.ts));
  const has = pts.some((p) => num(p.pv_kwh) !== null || num(p.batt_kwh) !== null || num(p.grid_kwh) !== null);
  tot.hidden = !has;
  el.hidden = !has;
  empty.hidden = has;
  if (!has) {
    destroyChart(el);
    renderLegend($("src-legend"), []);
    el.setAttribute("aria-label", t("history.src.empty"));
    return;
  }
  const c = { sun: cssVar("--sun"), batt: cssVar("--batt"), grid: cssVar("--grid") };
  const totals = d.totals || {}, share = d.share || {};
  tot.replaceChildren(...SRC_KEYS.map(([k, col, lbl]) => {
    const box = document.createElement("div");
    box.style.setProperty("--c", c[col]);
    const l = document.createElement("div");
    l.className = "lbl";
    l.textContent = t("history.src." + lbl);
    const b = document.createElement("b");
    b.textContent = energy(totals[k]);
    const p = document.createElement("span");
    p.textContent = pct((num(share[lbl]) || 0) * 100);
    box.append(l, b, p);
    return box;
  }));

  const area = range === "24h";
  const v = (p, k) => {
    const x = num(p[k]);
    if (x === null) return null;
    if (!area) return x;
    const cov = num(p.coverage) || 0, dur = num(p.dur_sec) || 0;
    return cov > 0 && dur > 0 ? x * 3600 / (cov * dur) : null; // средняя мощность, кВт
  };
  const xs = pts.map((p) => p.ts + (num(p.dur_sec) || 0) / 2);
  const pv = pts.map((p) => v(p, "pv_kwh")), bt = pts.map((p) => v(p, "batt_kwh")), gr = pts.map((p) => v(p, "grid_kwh"));
  const ok = (i) => pv[i] !== null && bt[i] !== null && gr[i] !== null;
  const pb = pv.map((x, i) => (ok(i) ? x + bt[i] : null)), all = pb.map((x, i) => (ok(i) ? x + gr[i] : null));
  const from = pts[0].ts, last = pts[pts.length - 1], to = last.ts + (num(last.dur_sec) || 0);
  const night = range === "30d" ? [] : nightIntervals(s.tariff, from, to);
  const unit = t(area ? "unit.kw" : "unit.kwh");
  const fmt = (x) => (x === null ? "—" : number(x, area ? 2 : 1) + " " + unit);
  const line = (color) => ({ stroke: color, fill: alpha(color, 0.75), width: 1, points: { show: false }, fillTo: () => 0 });
  const mk = area ? line : barSeries;
  const opts = {
    ...baseOpts(),
    scales: { x: { time: true, range: () => [from, to] }, y: { range: (u, lo, hi) => [0, Math.max(area ? 1 : 0.5, hi || 0) * 1.08] } },
    series: [{}, mk(c.grid), mk(c.batt), mk(c.sun)],
    axes: [xAxis(), yAxis((x) => number(x, 1), { label: unit, labelSize: 16 })],
    hooks: { drawClear: [nightHook(night)] },
    plugins: [tooltipPlugin((u, i) => {
      if (!ok(i)) return null;
      const p = pts[i];
      return {
        title: area ? date(p.ts) + " " + time(p.ts) : range === "7d" ? date(p.ts) + " " + time(p.ts) + "–" + time(p.ts + p.dur_sec) : date(p.ts),
        rows: [
          { color: c.sun, label: t("history.src.pv"), value: fmt(pv[i]) },
          { color: c.batt, label: t("history.src.batt"), value: fmt(bt[i]) },
          { color: c.grid, label: t("history.src.grid"), value: fmt(gr[i]) },
          { color: "transparent", label: t("history.src.sum"), value: fmt(all[i]) },
        ],
      };
    })],
  };
  mountChart(el, opts, [xs, all, pb, pv]);
  const legend = [
    { type: "box", color: "var(--sun)", label: t("history.src.pv") },
    { type: "box", color: "var(--batt)", label: t("history.src.batt") },
    { type: "box", color: "var(--grid)", label: t("history.src.grid") },
  ];
  if (night.length) legend.push({ type: "box", color: "var(--night)", label: t("chart.night") });
  renderLegend($("src-legend"), legend);
  el.setAttribute("aria-label", t("history.src.aria", {
    period: t("history.range." + range), pv: energy(totals.pv_kwh), batt: energy(totals.batt_kwh), grid: energy(totals.grid_kwh),
  }));
}

// ===== Таблица дней =====
const dayLabel = (d) => (typeof d === "string" && /^\d{4}-\d{2}-\d{2}/.test(d) ? d.slice(8, 10) + "." + d.slice(5, 7) : String(d || "—"));

function renderDaily(s) {
  const rows = Array.isArray(s.daily) ? [...s.daily].sort((a, b) => String(b.date).localeCompare(String(a.date))) : null;
  if (!rows) return;
  if (!changed("daily", [s.daily, getLang()])) return;
  $("daily-body").replaceChildren(...rows.map((r) => {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.scope = "row";
    th.textContent = dayLabel(r.date);
    tr.append(th);
    ["pv_today_kwh", "load_today_kwh", "grid_import_today_kwh", "grid_export_today_kwh"].forEach((k) => {
      const td = document.createElement("td");
      td.textContent = energy(r[k]);
      tr.append(td);
    });
    return tr;
  }));
  const note = $("daily-note");
  note.hidden = rows.length >= 2;
  note.textContent = rows.length >= 2 ? "" : tp("history.accum", rows.length);
}

// ===== Sankey =====
const SRC = [["pv", "sun", "history.flows.pv"], ["grid", "grid", "history.flows.grid"], ["batt", "batt", "history.flows.batt"]];
const DST = [["load", "home", "history.flows.load"], ["batt", "batt", "history.flows.battIn"], ["grid", "grid", "history.flows.gridOut"]];
const MIN_KWH = 0.01;

function flowList(fl) {
  const g = (k) => Math.max(0, num(fl[k]) || 0);
  return [
    ["pv", "load", g("pv_to_load")], ["pv", "batt", g("pv_to_batt")], ["pv", "grid", g("pv_to_grid")],
    ["grid", "load", g("grid_to_load")], ["grid", "batt", g("grid_to_batt")], ["batt", "load", g("batt_to_load")],
  ].filter((f) => f[2] >= MIN_KWH);
}

function sumBy(list, side, key) { return list.filter((f) => f[side] === key).reduce((a, f) => a + f[2], 0); }

function el(tag, attrs, text) {
  const e = document.createElementNS(SVG, tag);
  Object.keys(attrs || {}).forEach((k) => e.setAttribute(k, attrs[k]));
  if (text !== undefined) e.textContent = text;
  return e;
}

// Раскладка узлов одной стороны: высота ∝ кВт·ч, но слот не меньше MIN_SLOT (место под подпись).
function layoutSide(nodes, total, H, gap, k) {
  let y = 0;
  return nodes.map((n) => {
    const h = Math.max(n.v * k, 2);
    const slot = Math.max(h, 34);
    const node = { ...n, h, y: y + (slot - h) / 2, slot, top: y };
    y += slot + gap;
    return node;
  });
}

function scaleFor(nodes, H, gap) {
  const total = nodes.reduce((a, n) => a + n.v, 0);
  if (!total) return 0;
  let k = (H - gap * (nodes.length - 1)) / total;
  for (let it = 0; it < 3; it++) {
    const small = nodes.filter((n) => n.v * k < 34);
    const rest = total - small.reduce((a, n) => a + n.v, 0);
    if (!rest) break;
    k = (H - gap * (nodes.length - 1) - 34 * small.length) / rest;
  }
  return Math.max(k, 0);
}

function sankeySvg(svg, list) {
  const W = 560, H = 260, TOP = 16, GAP = 14, X0 = 150, X1 = 400, NW = 10;
  const src = SRC.map(([key, color, label]) => ({ key, color, label, v: sumBy(list, 0, key) })).filter((n) => n.v > 0);
  const dst = DST.map(([key, color, label]) => ({ key, color, label, v: sumBy(list, 1, key) })).filter((n) => n.v > 0);
  const k = Math.min(scaleFor(src, H, GAP), scaleFor(dst, H, GAP));
  const L = layoutSide(src, 0, H, GAP, k), R = layoutSide(dst, 0, H, GAP, k);
  const hL = L.length ? L[L.length - 1].top + L[L.length - 1].slot : 0;
  const hR = R.length ? R[R.length - 1].top + R[R.length - 1].slot : 0;
  const offL = TOP + (H - hL) / 2, offR = TOP + (H - hR) / 2;
  svg.setAttribute("viewBox", "0 0 " + W + " " + (H + TOP * 2));
  const gRib = el("g", { class: "sk-ribbons" }), gNode = el("g", { class: "sk-nodes" });
  const useL = Object.fromEntries(L.map((n) => [n.key, 0])), useR = Object.fromEntries(R.map((n) => [n.key, 0]));
  // ленты: порядок по источникам, внутри — по приёмникам (DST)
  L.forEach((a) => {
    R.forEach((b) => {
      const f = list.find((x) => x[0] === a.key && x[1] === b.key);
      if (!f) return;
      const w = Math.max(f[2] * k, 1);
      const y0 = offL + a.y + useL[a.key], y1 = offR + b.y + useR[b.key];
      useL[a.key] += f[2] * k;
      useR[b.key] += f[2] * k;
      const xa = X0 + NW, xb = X1, xm = (xa + xb) / 2;
      const d = "M" + xa + " " + y0 + " C" + xm + " " + y0 + " " + xm + " " + y1 + " " + xb + " " + y1 +
        " L" + xb + " " + (y1 + w) + " C" + xm + " " + (y1 + w) + " " + xm + " " + (y0 + w) + " " + xa + " " + (y0 + w) + " Z";
      const p = el("path", { d, class: "sk-rib k-" + a.color });
      p.append(el("title", {}, t(SRC.find((x) => x[0] === a.key)[2]) + " → " + t(DST.find((x) => x[0] === b.key)[2]) + ": " + energy(f[2])));
      gRib.append(p);
    });
  });
  const node = (n, x, off, anchor, tx) => {
    gNode.append(el("rect", { x, y: off + n.y, width: NW, height: n.h, rx: 3, class: "sk-node k-" + n.color }));
    const cy = off + n.y + n.h / 2;
    gNode.append(el("text", { x: tx, y: cy - 4, "text-anchor": anchor, class: "sk-name" }, t(n.label)));
    gNode.append(el("text", { x: tx, y: cy + 15, "text-anchor": anchor, class: "sk-val" }, energy(n.v)));
  };
  L.forEach((n) => node(n, X0, offL, "end", X0 - 10));
  R.forEach((n) => node(n, X1, offR, "start", X1 + NW + 10));
  svg.replaceChildren(gRib, gNode);
}

function sankeyBars(box, list) {
  const side = (defs, idx, titleKey) => {
    const nodes = defs.map(([key, color, label]) => ({ key, color, label, v: sumBy(list, idx, key) })).filter((n) => n.v > 0);
    const total = nodes.reduce((a, n) => a + n.v, 0);
    const wrap = document.createElement("div");
    wrap.className = "skb";
    const h = document.createElement("div");
    h.className = "lbl";
    h.textContent = t(titleKey);
    const bar = document.createElement("div");
    bar.className = "skb-bar";
    bar.setAttribute("aria-hidden", "true");
    const keys = document.createElement("ul");
    keys.className = "skb-keys";
    nodes.forEach((n) => {
      const seg = document.createElement("i");
      seg.className = "k-" + n.color;
      seg.style.width = (total ? n.v / total * 100 : 0) + "%";
      bar.append(seg);
      const li = document.createElement("li");
      const sw = document.createElement("i");
      sw.className = "k-" + n.color;
      const name = document.createElement("span");
      name.textContent = t(n.label);
      const val = document.createElement("b");
      val.textContent = energy(n.v) + " · " + pct(total ? n.v / total * 100 : 0);
      li.append(sw, name, val);
      keys.append(li);
    });
    wrap.append(h, bar, keys);
    return wrap;
  };
  box.replaceChildren(side(SRC, 0, "history.flows.sources"), side(DST, 1, "history.flows.sinks"));
}

function renderFlows(s) {
  const fl = s.flows;
  if (!fl) return;
  if (!changed("flows", [fl, getLang()])) return;
  const list = flowList(fl.flows_kwh || {});
  const svg = $("sankey"), bars = $("sankey-bars"), empty = $("sankey-empty");
  const has = list.length > 0;
  svg.toggleAttribute("hidden", !has); // у SVGElement нет свойства hidden
  bars.hidden = !has;
  empty.hidden = has;
  if (has) {
    sankeySvg(svg, list);
    sankeyBars(bars, list);
    const v = (a, b) => energy(list.filter((f) => f[0] === a && (!b || f[1] === b)).reduce((x, f) => x + f[2], 0));
    const aria = t("history.flows.aria", {
      pv: v("pv"), pvLoad: v("pv", "load"), pvBatt: v("pv", "batt"), pvGrid: v("pv", "grid"),
      grid: v("grid"), battLoad: v("batt", "load"),
    });
    svg.setAttribute("aria-label", aria);
    bars.setAttribute("aria-label", aria);
  }
  const note = $("sankey-note");
  const cov = num(fl.coverage);
  note.hidden = !(cov !== null && cov < 0.95 && has);
  note.textContent = cov !== null ? t("history.flows.coverage", { pct: pct(cov * 100) }) : "";
}

// ===== Отключения света =====
function renderOutages(s) {
  const o = s.outages;
  if (!o || !Array.isArray(o.items)) return;
  if (!changed("outages", [o, getLang()])) return;
  const items = o.items;
  $("out-empty").hidden = items.length > 0;
  const sum = $("out-summary");
  sum.hidden = !items.length;
  sum.textContent = items.length ? t("history.outages.summary", { count: o.count, dur: duration(o.total_sec) }) : "";
  $("out-list").replaceChildren(...items.map((i) => {
    const li = document.createElement("li");
    const h = document.createElement("div");
    h.className = "out-h";
    const when = document.createElement("span");
    when.textContent = date(i.start_ts) + " " + time(i.start_ts) + " – " +
      (i.end_ts === null ? t("history.outages.ongoing") : time(i.end_ts));
    const dur = document.createElement("span");
    dur.textContent = duration(i.duration_sec);
    const phases = (i.phases_down || []).map((p) => "L" + p).join(", ");
    const kind = document.createElement("span");
    kind.className = "out-kind";
    kind.textContent = t("history.outages.kind." + (i.kind === "down" ? "down" : "partial"), { phases });
    h.append(when, dur, kind);
    const d = document.createElement("div");
    d.className = "out-d";
    d.textContent = t("history.outages.batt", { kwh: energy(i.batt_kwh) }) + " · " +
      t("history.outages.soc", { a: pct(i.soc_start), b: pct(i.soc_end) });
    li.append(h, d);
    return li;
  }));
}

function renderStale(s) {
  const el = $("hist-stale");
  const failing = ["hist", "daily", "flows"].filter((k) => s.errors[k] && s[k]);
  el.hidden = !failing.length;
  if (failing.length) {
    const since = Math.min(...failing.map((k) => s.lastOk[k] || Infinity));
    el.textContent = Number.isFinite(since) ? t("stale", { time: time(since) }) : t("stale.never");
  }
}

export function renderHistory(s) {
  const sec = $("history");
  const any = s.loaded.hist || s.loaded.daily || s.loaded.flows;
  if (!any && !(s.errors.hist && s.errors.daily && s.errors.flows)) return;
  sec.removeAttribute("aria-busy");
  renderStale(s);
  renderPower(s);
  renderSources(s);
  renderDaily(s);
  renderFlows(s);
  renderOutages(s);
}
