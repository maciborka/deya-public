// #pvfc: прогноз PV (консенсус, как у планировщика) против факта по часам выбранного дня (вчера / сегодня / завтра).
import { t, getLang } from "./i18n.js";
import { isNum, number, energy, pct, time, TZ } from "./fmt.js";
import { cssVar, alpha, theme, changed, forget, mountChart, destroyChart, baseOpts, xAxis, yAxis, nowHook, tooltipPlugin, renderLegend } from "./charts.js";

const $ = (id) => document.getElementById(id);
const PROVIDERS = { open_meteo: "Open-Meteo", forecast_solar: "Forecast.Solar", solcast: "Solcast" };
const DAY_KEYS = { "-1": "pvfc.day.yesterday", 0: "pvfc.day.today", 1: "pvfc.day.tomorrow" };
const FIXED = 5; // индекс первого ряда провайдера в данных uPlot (0 x, 1 факт, 2 p50, 3 p10, 4 p90)
const num = (v) => (isNum(v) ? Number(v) : null);

let offset = 0;
let cb = { onDay() {} };
const hiddenProviders = new Set();

const ymd = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });

// Локальная дата Киева со сдвигом в днях → YYYY-MM-DD
function dayStr(shift) {
  const [y, m, d] = ymd.format(new Date()).split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + shift)).toISOString().slice(0, 10);
}

export const pvfcUrl = () => "/api/pv/hourly?date=" + dayStr(offset);

export function initPvfc(callbacks) {
  cb = { ...cb, ...callbacks };
  $("pvfc-seg").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-day]");
    if (!b || Number(b.dataset.day) === offset) return;
    offset = Number(b.dataset.day);
    document.querySelectorAll("#pvfc-seg button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    cb.onDay();
  });
}

function summary(d) {
  const tot = d.totals || {};
  const hasFc = isNum(tot.p50_kwh), hasAct = offset <= 0 && isNum(tot.actual_kwh);
  const range = { lo: energy(tot.p10_kwh), hi: energy(tot.p90_kwh) };
  if (hasFc && hasAct) {
    const p = isNum(tot.actual_vs_p50_pct) ? t("pvfc.sum.pct", { pct: pct(tot.actual_vs_p50_pct) }) : "";
    return t("pvfc.sum.full", { actual: energy(tot.actual_kwh), fc: energy(tot.p50_kwh), pct: p, ...range });
  }
  if (hasFc) return t("pvfc.sum.forecast", { fc: energy(tot.p50_kwh), ...range });
  if (hasAct) return t("pvfc.sum.fact", { actual: energy(tot.actual_kwh) });
  return t("pvfc.sum.none");
}

function chart(el, d) {
  const hh = d.hours || [];
  const HALF = 1800;
  const xs = hh.map((h) => h.ts + HALF);
  const names = [...new Set(hh.flatMap((h) => Object.keys(h.providers || {})))].sort();
  const c = { sun: cssVar("--sun"), batt: cssVar("--batt"), text: cssVar("--text"), muted: cssVar("--muted") };
  const col = (k) => hh.map((h) => num(h[k]));
  const data = [xs, col("actual_kwh"), col("p50_kwh"), col("p10_kwh"), col("p90_kwh"),
    ...names.map((p) => hh.map((h) => num((h.providers || {})[p])))];
  const hi = Math.max(0.5, ...data.slice(1).flat().filter((v) => v !== null));
  const from = hh.length ? hh[0].ts : 0, to = hh.length ? hh[hh.length - 1].ts + 3600 : 1;
  const nowTs = Math.floor(Date.now() / 1000);
  const line = (stroke, width, extra) => ({ stroke, width, spanGaps: false, points: { show: false }, ...extra });
  const provColors = [c.batt, c.muted, c.text];
  const series = [{},
    { stroke: c.sun, fill: c.sun, width: 0, points: { show: false }, fillTo: () => 0, paths: window.uPlot.paths.bars({ size: [0.78, 48], gap: 1 }) },
    line(c.text, 2.5),
    line("transparent", 1),
    line("transparent", 1),
    ...names.map((p, i) => line(provColors[i % provColors.length], 1.25, { dash: [4, 4], show: !hiddenProviders.has(p) }))];

  const kwh = (x) => (x === null || x === undefined ? "—" : number(x, 2) + " " + t("unit.kwh"));
  const opts = {
    ...baseOpts(),
    scales: { x: { time: true, range: () => [from, to] }, y: { range: () => [0, hi * 1.12] } },
    series,
    bands: [{ series: [4, 3], fill: alpha(c.batt, 0.16) }],
    axes: [xAxis({ space: 40 }), yAxis((x) => number(x, 1), { label: t("unit.kwh"), labelSize: 16 })],
    hooks: { draw: offset === 0 ? [nowHook(nowTs)] : [] },
    plugins: [tooltipPlugin((u, i) => {
      const rows = [];
      if (data[1][i] !== null) rows.push({ color: c.sun, label: t("pvfc.series.actual"), value: kwh(data[1][i]) });
      if (data[2][i] !== null) rows.push({ color: c.text, label: t("pvfc.series.p50"), value: kwh(data[2][i]) });
      if (data[3][i] !== null && data[4][i] !== null) {
        rows.push({ color: alpha(c.batt, 0.5), label: t("pvfc.series.band"), value: number(data[3][i], 2) + "–" + kwh(data[4][i]) });
      }
      names.forEach((p, k) => {
        const v = data[FIXED + k][i];
        if (v !== null && !hiddenProviders.has(p)) rows.push({ color: provColors[k % provColors.length], label: PROVIDERS[p] || p, value: kwh(v) });
      });
      return { title: hh[i].hour + "–" + time(hh[i].ts + 3600), rows };
    })],
  };
  const u = mountChart(el, opts, data);

  const legend = [
    { type: "box", color: "var(--sun)", label: t("pvfc.series.actual") },
    { type: "line", color: "var(--text)", label: t("pvfc.series.p50") },
    { type: "box", color: "color-mix(in srgb, var(--batt) 16%, transparent)", label: t("pvfc.series.band") },
    ...names.map((p, i) => ({ type: "dash", color: ["var(--batt)", "var(--muted)", "var(--text)"][i % 3], label: PROVIDERS[p] || p })),
  ];
  const lg = $("pvfc-legend");
  renderLegend(lg, legend);
  names.forEach((p, k) => {
    const item = lg.children[3 + k];
    item.style.cursor = "pointer";
    item.setAttribute("role", "button");
    item.tabIndex = 0;
    item.setAttribute("aria-pressed", String(!hiddenProviders.has(p)));
    item.style.opacity = hiddenProviders.has(p) ? "0.45" : "";
    const toggle = () => {
      const show = hiddenProviders.has(p);
      if (show) hiddenProviders.delete(p); else hiddenProviders.add(p);
      u.setSeries(FIXED + k, { show });
      item.setAttribute("aria-pressed", String(show));
      item.style.opacity = show ? "" : "0.45";
    };
    item.addEventListener("click", toggle);
    item.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
  });
}

export function renderPvfc(s) {
  const sec = $("pvfc");
  const d = s.pvfc;
  if (!d && !s.errors.pvfc) return; // первая загрузка / смена дня — скелетон
  sec.removeAttribute("aria-busy");
  const stale = $("pvfc-stale");
  const failing = s.errors.pvfc && d;
  stale.hidden = !failing;
  if (failing) stale.textContent = s.lastOk.pvfc ? t("stale", { time: time(s.lastOk.pvfc) }) : t("stale.never");
  const el = $("pvfc-chart");
  if (!d) {
    destroyChart(el);
    forget("pvfc");
    $("pvfc-sum").textContent = t("pvfc.empty");
    $("pvfc-warn").hidden = true;
    $("pvfc-legend").replaceChildren();
    return;
  }
  const sum = summary(d);
  $("pvfc-sum").textContent = sum;
  const warn = $("pvfc-warn");
  warn.hidden = !(d.warnings && d.warnings.length);
  warn.textContent = warn.hidden ? "" : t("pvfc.warn.noForecast");
  if (changed("pvfc", [d, offset, getLang(), theme()])) {
    chart(el, d);
    el.setAttribute("aria-label", t("pvfc.aria", { day: t(DAY_KEYS[offset]), summary: sum }));
  }
}
