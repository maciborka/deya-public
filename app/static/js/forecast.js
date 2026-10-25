// #forecast (§7.4): вкладки (SOC / заряд і розряд / чим живиться дім), разброс PV, итоги 24 ч, what-if, пустое состояние.
import { t, getLang } from "./i18n.js";
import { isNum, number, energy, money, pct, time, date } from "./fmt.js";
import {
  cssVar, alpha, theme, changed, forget, mountChart, destroyChart, baseOpts, xAxis, yAxis,
  nightIntervals, nightHook, nowHook, zeroHook, tooltipPlugin, renderLegend,
} from "./charts.js";

const $ = (id) => document.getElementById(id);
const TAB_KEY = "deya.tab";
const TABS = ["soc", "flow", "load"];
const H12 = 12 * 3600;
const PROVIDERS = { open_meteo: "Open-Meteo", forecast_solar: "Forecast.Solar", solcast: "Solcast" };
const num = (v) => (isNum(v) ? Number(v) : null);

let tab = "soc";
let whatIf = null; // what-if target, % или null
let wiTimer = null;
let cb = { onWhatIf() {}, rerender() {} };

export const getWhatIf = () => whatIf;

function setTab(next, persist = true) {
  tab = TABS.includes(next) ? next : "soc";
  if (persist) { try { localStorage.setItem(TAB_KEY, tab); } catch (e) { /* ignore */ } }
  document.querySelectorAll("#fc-tabs button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tab === tab)));
}

export function initForecast(callbacks) {
  cb = { ...cb, ...callbacks };
  let stored = null;
  try { stored = localStorage.getItem(TAB_KEY); } catch (e) { /* ignore */ }
  setTab(stored, false);
  $("fc-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (!b || b.dataset.tab === tab) return;
    setTab(b.dataset.tab);
    cb.rerender();
  });
  const range = $("wi-range");
  range.addEventListener("input", () => {
    whatIf = Number(range.value);
    $("wi-v").textContent = pct(whatIf);
    $("wi-reset").disabled = false;
    $("wi-result").textContent = t("forecast.whatif.loading");
    clearTimeout(wiTimer);
    wiTimer = setTimeout(() => cb.onWhatIf(whatIf), 400);
  });
  $("wi-reset").addEventListener("click", () => {
    clearTimeout(wiTimer);
    whatIf = null;
    cb.onWhatIf(null);
  });
}

// ===== Состояние =====
function providerVals(f) {
  const pr = (f && f.providers_kwh) || {};
  return Object.keys(pr).filter((k) => isNum(pr[k])).map((k) => ({ code: k, kwh: Number(pr[k]) }));
}

export function noForecast(s) {
  const f = s.forecast;
  if (!f) return !!s.errors.forecast;
  if (!providerVals(f).length) return true;
  const runs = (s.fstatus && s.fstatus.runs) || [];
  return runs.length > 0 && runs.every((r) => !r.ok);
}

function whatIfData(s) {
  const w = s.whatif;
  if (whatIf === null || !w || !isNum(w.target_plan_pct) || Math.round(w.target_plan_pct) !== Math.round(whatIf)) return null;
  return w;
}

// ===== Графики =====
function socChart(el, s, f, w) {
  const now = num(f.now_ts) || Math.floor(Date.now() / 1000);
  const pts = f.points || [];
  const fact = (s.soc12 || []).filter((r) => r.ts >= now - H12 - 600 && r.ts <= now + 60);
  const wpts = w ? w.points || [] : [];
  const ts = [...new Set([...fact.map((r) => r.ts), ...pts.map((p) => p.ts), ...wpts.map((p) => p.ts)])].sort((a, b) => a - b);
  const fm = new Map(fact.map((r) => [r.ts, num(r.soc_pct)]));
  const pm = new Map(pts.map((p) => [p.ts, p]));
  const wm = new Map(wpts.map((p) => [p.ts, p]));
  const col = (m, k) => ts.map((x) => { const p = m.get(x); return p ? num(p[k]) : null; });
  const data = [ts, ts.map((x) => (fm.has(x) ? fm.get(x) : null)), col(pm, "soc_as_is"), col(pm, "soc_lo"), col(pm, "soc_hi"),
    col(pm, "soc_plan"), col(pm, "floor_pct")];
  if (w) data.push(col(wm, "soc_plan"));

  const c = { text: cssVar("--text"), batt: cssVar("--batt"), muted: cssVar("--muted"), grid: cssVar("--grid") };
  const from = Math.min(now - H12, ts[0] || now), to = Math.max(ts[ts.length - 1] || now, now + 3600);
  const slot = num(f.slot_sec) || 900;
  const night = nightIntervals(s.tariff, from, to, pts, slot);
  const line = (stroke, width, extra) => ({ stroke, width, spanGaps: true, points: { show: false }, ...extra });
  const series = [{},
    line(c.text, 3, { spanGaps: false }),
    line(c.muted, 2, { dash: [6, 5] }),
    line("transparent", 1),
    line("transparent", 1),
    line(c.batt, 3),
    line(c.grid, 2, { dash: [2, 4], paths: window.uPlot.paths.stepped({ align: 1 }) })];
  if (w) series.push(line(c.text, 2.5, { dash: [8, 6] }));

  const opts = {
    ...baseOpts(),
    scales: { x: { time: true, range: () => [from, to] }, y: { range: [0, 100] } },
    series,
    bands: [{ series: [4, 3], fill: alpha(c.batt, 0.16) }],
    axes: [xAxis(), yAxis((v) => number(v, 0) + "%", { splits: () => [0, 25, 50, 75, 100] })],
    hooks: { drawClear: [nightHook(night)], draw: [nowHook(now)] },
    plugins: [tooltipPlugin((u, i) => {
      const d = u.data;
      const rows = [];
      const add = (si, color, key) => { if (d[si] && d[si][i] !== null && d[si][i] !== undefined) rows.push({ color, label: t(key), value: pct(d[si][i]) }); };
      add(1, c.text, "forecast.soc.fact");
      add(5, c.batt, "forecast.soc.plan");
      if (w) add(7, c.text, "forecast.soc.whatifShort");
      add(2, c.muted, "forecast.soc.asIs");
      if (d[3][i] !== null && d[4][i] !== null) rows.push({ color: alpha(c.batt, 0.5), label: t("forecast.soc.band"), value: pct(d[3][i]) + "–" + pct(d[4][i]) });
      add(6, c.grid, "forecast.soc.floor");
      return { title: date(d[0][i]) + " " + time(d[0][i]), rows };
    })],
  };
  mountChart(el, opts, data);

  const legend = [
    { type: "line", color: "var(--text)", label: t("forecast.soc.fact") },
    { type: "line", color: "var(--batt)", label: t("forecast.soc.plan") },
    { type: "box", color: "color-mix(in srgb, var(--batt) 16%, transparent)", label: t("forecast.soc.band") },
    { type: "dash", color: "var(--muted)", label: t("forecast.soc.asIs") },
    { type: "dot", color: "var(--grid)", label: t("forecast.soc.floor") },
  ];
  if (w) legend.splice(2, 0, { type: "dash", color: "var(--text)", label: t("forecast.soc.whatif", { pct: pct(whatIf) }) });
  if (night.length) legend.push({ type: "box", color: "var(--night)", label: t("chart.night") });

  // вывод для screen reader
  const plan = pts.map((p) => num(p.soc_plan));
  let minI = -1;
  plan.forEach((v, i) => { if (v !== null && (minI < 0 || v < plan[minI])) minI = i; });
  const lastFact = fact.length ? num(fact[fact.length - 1].soc_pct) : null;
  const cur = s.latest && isNum(s.latest.soc_pct) ? Number(s.latest.soc_pct) : lastFact !== null ? lastFact : plan[0];
  const end = f.soc_end ? f.soc_end.plan : plan[plan.length - 1];
  const aria = t("forecast.soc.aria", {
    now: pct(cur), min: minI >= 0 ? pct(plan[minI]) : "—", minTime: minI >= 0 ? time(pts[minI].ts) : "—", end: pct(end),
  });
  return { legend, aria };
}

function barSeries(color) {
  return {
    stroke: color, fill: color, width: 0, points: { show: false }, fillTo: () => 0,
    paths: window.uPlot.paths.bars({ size: [0.78, 48], gap: 1 }),
  };
}

function hourlyChart(el, s, f, kind) {
  const hh = (f.hourly || []).filter((h) => isNum(h.ts));
  const now = num(f.now_ts) || Math.floor(Date.now() / 1000);
  const HALF = 1800;
  const xs = hh.map((h) => h.ts + HALF);
  const v = (h, k) => num(h[k]) || 0;
  const c = { sun: cssVar("--sun"), grid: cssVar("--grid"), batt: cssVar("--batt") };
  const from = hh.length ? hh[0].ts : now, to = hh.length ? hh[hh.length - 1].ts + 3600 : now + 86400;
  const night = nightIntervals(s.tariff, from, to, f.points, num(f.slot_sec) || 900);
  let data, series, range, rowsOf, legend;
  const kwh = (x) => number(x, 2) + " " + t("unit.kwh");

  if (kind === "flow") {
    const pv = hh.map((h) => v(h, "batt_charge_from_pv_kwh")), gr = hh.map((h) => v(h, "batt_charge_from_grid_kwh"));
    const dis = hh.map((h) => -v(h, "batt_discharge_kwh"));
    const top = pv.map((x, i) => x + gr[i]);
    data = [xs, top, pv, dis];
    series = [{}, barSeries(c.grid), barSeries(c.sun), barSeries(c.batt)];
    const hi = Math.max(0.5, ...top), lo = Math.min(-0.5, ...dis);
    range = [lo * 1.12, hi * 1.12];
    rowsOf = (i) => [
      { color: c.sun, label: t("forecast.flow.fromPv"), value: kwh(pv[i]) },
      { color: c.grid, label: t("forecast.flow.fromGrid"), value: kwh(gr[i]) },
      { color: c.batt, label: t("forecast.flow.discharge"), value: kwh(-dis[i]) },
    ];
    legend = [
      { type: "box", color: "var(--sun)", label: t("forecast.flow.fromPv") },
      { type: "box", color: "var(--grid)", label: t("forecast.flow.fromGrid") },
      { type: "box", color: "var(--batt)", label: t("forecast.flow.dischargeDown") },
    ];
  } else {
    const pv = hh.map((h) => v(h, "load_from_pv_kwh")), bt = hh.map((h) => v(h, "load_from_batt_kwh")), gr = hh.map((h) => v(h, "load_from_grid_kwh"));
    const pb = pv.map((x, i) => x + bt[i]), all = pb.map((x, i) => x + gr[i]);
    data = [xs, all, pb, pv];
    series = [{}, barSeries(c.grid), barSeries(c.batt), barSeries(c.sun)];
    range = [0, Math.max(0.5, ...all) * 1.1];
    rowsOf = (i) => [
      { color: c.sun, label: t("forecast.load.pv"), value: kwh(pv[i]) },
      { color: c.batt, label: t("forecast.load.batt"), value: kwh(bt[i]) },
      { color: c.grid, label: t("forecast.load.grid"), value: kwh(gr[i]) },
    ];
    legend = [
      { type: "box", color: "var(--sun)", label: t("forecast.load.pv") },
      { type: "box", color: "var(--batt)", label: t("forecast.load.batt") },
      { type: "box", color: "var(--grid)", label: t("forecast.load.grid") },
    ];
  }
  if (night.length) legend.push({ type: "box", color: "var(--night)", label: t("chart.night") });

  const opts = {
    ...baseOpts(),
    scales: { x: { time: true, range: () => [from, to] }, y: { range: () => range } },
    series,
    axes: [xAxis(), yAxis((x) => number(x, 1), { label: t("unit.kwh"), labelSize: 16 })],
    hooks: { drawClear: [nightHook(night)], draw: [zeroHook("y"), nowHook(now)] },
    plugins: [tooltipPlugin((u, i) => ({ title: time(hh[i].ts) + "–" + time(hh[i].ts + 3600), rows: rowsOf(i) }))],
  };
  mountChart(el, opts, data);

  const e = f.energy_24h || {};
  const aria = kind === "flow"
    ? t("forecast.flow.aria", { pv: energy(e.batt_charge_from_pv_kwh), grid: energy(e.batt_charge_from_grid_kwh), dis: energy(e.batt_discharge_kwh) })
    : t("forecast.load.aria", { load: energy(e.load_kwh), pv: energy(e.load_from_pv_kwh), batt: energy(e.load_from_batt_kwh), grid: energy(e.load_from_grid_kwh) });
  return { legend, aria };
}

// ===== Блоки под графиком =====
function renderSpread(f) {
  const vals = providerVals(f);
  const v = $("fc-spread-v");
  if (!vals.length) v.textContent = "—";
  else if (vals.length === 1) v.textContent = energy(vals[0].kwh);
  else {
    const ks = vals.map((x) => x.kwh);
    v.textContent = t("forecast.spread.range", { min: number(Math.min(...ks), 1, 1), max: number(Math.max(...ks), 1, 1), unit: t("unit.kwh") });
  }
  $("fc-spread-l").textContent = vals.length > 1 ? t("forecast.spread.diverge") : t("forecast.spread.single");
  $("fc-prov").replaceChildren(...vals.map((x) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    const n = document.createElement("span");
    n.className = "chip-n";
    n.textContent = PROVIDERS[x.code] || x.code;
    const b = document.createElement("b");
    b.textContent = energy(x.kwh);
    chip.append(n, b);
    return chip;
  }));
}

function renderTotals(f) {
  const e = f.energy_24h || {};
  $("fc-t-load").textContent = energy(e.load_kwh);
  $("fc-t-import").textContent = energy(e.grid_import_kwh);
  $("fc-t-cost").textContent = money(e.cost_uah);
}

function renderWhatIf(s, f, w) {
  const range = $("wi-range");
  if (whatIf === null) {
    if (document.activeElement !== range && isNum(f.target_plan_pct)) range.value = String(Math.round(f.target_plan_pct));
    $("wi-v").textContent = pct(range.value);
    $("wi-result").textContent = isNum(f.target_plan_pct) ? t("forecast.whatif.hint", { pct: pct(f.target_plan_pct) }) : "";
  } else {
    $("wi-v").textContent = pct(whatIf);
    if (w) {
      const e = w.energy_24h || {}, pe = f.energy_24h || {};
      $("wi-result").textContent = t("forecast.whatif.result", {
        cost: money(e.cost_uah), plan: money(pe.cost_uah), kwh: energy(e.grid_import_kwh), soc: pct(w.soc_end && w.soc_end.plan),
      });
    } else if (s.errors.whatif) {
      $("wi-result").textContent = t("forecast.whatif.error", { detail: s.errors.whatif.detail || "" }).trim();
    } else $("wi-result").textContent = t("forecast.whatif.loading");
  }
  $("wi-reset").disabled = whatIf === null;
}

function renderEmpty(s) {
  const st = s.fstatus;
  const next = st && isNum(st.next_run_ts) ? time(st.next_run_ts) : null;
  $("fc-empty-t").textContent = t("forecast.empty.title");
  const parts = [next ? t("forecast.empty.next", { time: next }) : t("forecast.empty.noNext")];
  if (!s.forecast && s.errors.forecast && s.errors.forecast.detail) parts.push(s.errors.forecast.detail);
  $("fc-empty-d").textContent = parts.join(" ");
}

function renderStale(s) {
  const el = $("fc-stale");
  const failing = s.errors.forecast && s.forecast;
  el.hidden = !failing;
  if (failing) el.textContent = s.lastOk.forecast ? t("stale", { time: time(s.lastOk.forecast) }) : t("stale.never");
}

export function renderForecast(s) {
  const sec = $("forecast");
  const f = s.forecast;
  if (!f && !s.errors.forecast) return; // первая загрузка — скелетон
  sec.removeAttribute("aria-busy");
  renderStale(s);
  const empty = noForecast(s);
  $("fc-empty").hidden = !empty;
  $("fc-content").hidden = empty;
  const el = $("fc-chart");
  if (empty) {
    destroyChart(el);
    forget("forecast");
    renderEmpty(s);
    return;
  }
  const w = whatIfData(s);
  if (changed("forecast", [f, s.soc12, s.tariff, w, tab, getLang(), theme()])) {
    const r = tab === "soc" ? socChart(el, s, f, w) : hourlyChart(el, s, f, tab);
    renderLegend($("fc-legend"), r.legend);
    el.setAttribute("aria-label", r.aria);
  }
  renderSpread(f);
  renderTotals(f);
  renderWhatIf(s, f, w);
}
