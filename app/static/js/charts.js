// Общие помощники uPlot (§7.4–7.5): цвета из CSS-токенов, оси в Europe/Kyiv, ночной фон из tariff.zones,
// вертикаль «зараз», нулевая линия, свой tooltip, HTML-легенда, монтирование с ResizeObserver.
import { t } from "./i18n.js";
import { TZ, time, date, isNum } from "./fmt.js";

/* global uPlot */
const U = () => window.uPlot;

export const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
export const theme = () => document.documentElement.dataset.theme || "dark";

// "#RRGGBB" + alpha -> rgba()
export function alpha(color, a) {
  const m = /^#?([0-9a-f]{6})$/i.exec(color || "");
  if (!m) return color;
  const n = parseInt(m[1], 16);
  return "rgba(" + (n >> 16 & 255) + "," + (n >> 8 & 255) + "," + (n & 255) + "," + a + ")";
}

const FONT = '600 11px Manrope, system-ui, sans-serif';

export function chartHeight(el) {
  const base = Number(el && el.dataset.h) || 280;
  return window.innerWidth < 600 ? Math.round(base * 0.8) : base;
}

// Мемо по идентичности зависимостей: перерисовка только при смене данных/темы/языка/вкладки.
const memo = new Map();
export function changed(key, deps) {
  const prev = memo.get(key);
  memo.set(key, deps);
  return !prev || prev.length !== deps.length || prev.some((v, i) => v !== deps[i]);
}
export function forget(key) { memo.delete(key); }

// ===== Монтирование =====
const charts = new Map(); // el.id -> {u, ro}

export function mountChart(el, opts, data) {
  destroyChart(el);
  const width = Math.max(el.clientWidth, 200);
  const u = new (U())({ ...opts, width, height: chartHeight(el) }, data, el);
  let raf = 0;
  const ro = new ResizeObserver(() => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      const w = Math.max(el.clientWidth, 200), h = chartHeight(el);
      if (w !== u.width || h !== u.height) u.setSize({ width: w, height: h });
    });
  });
  ro.observe(el);
  charts.set(el.id, { u, ro });
  return u;
}

export function destroyChart(el) {
  const c = charts.get(el.id);
  if (!c) return;
  c.ro.disconnect();
  c.u.destroy();
  charts.delete(el.id);
}

// ===== Базовые опции =====
export function baseOpts() {
  return {
    tzDate: (ts) => U().tzDate(new Date(ts * 1e3), TZ),
    legend: { show: false },
    cursor: { drag: { x: false, y: false, setScale: false }, points: { size: 7 } },
    padding: [10, 6, 0, 0],
  };
}

// Подписи времени: HH:MM, на смене суток — ДД.ММ; для периодов > 2 суток — только ДД.ММ.
export function timeValues(u, vals) {
  const span = (u.scales.x.max || 0) - (u.scales.x.min || 0);
  if (span > 2 * 86400) return vals.map((v) => (v === null ? "" : date(v)));
  let prev = null;
  return vals.map((v) => {
    if (v === null) return "";
    const d = date(v);
    const out = prev !== null && d !== prev ? d : time(v);
    prev = d;
    return out;
  });
}

export function xAxis(extra) {
  const mut = cssVar("--muted"), line = cssVar("--line");
  return { stroke: mut, font: FONT, grid: { stroke: line, width: 1 }, ticks: { stroke: line, width: 1, size: 4 }, values: timeValues, space: 56, ...(extra || {}) };
}

export function yAxis(fmt, extra) {
  const mut = cssVar("--muted"), line = cssVar("--line");
  return {
    stroke: mut, font: FONT, labelFont: FONT, grid: { stroke: line, width: 1 }, ticks: { show: false }, size: 46, gap: 6,
    values: (u, vals) => vals.map((v) => (v === null ? "" : fmt(v))), ...(extra || {}),
  };
}

// ===== Ночной тариф =====
function sod(str) {
  const m = /^(\d{1,2}):(\d{2})(?::(\d{2}))?/.exec(str || "");
  return m ? Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3] || 0) : null;
}
const kyivParts = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
function kyivSod(ts) {
  const p = kyivParts.formatToParts(new Date(ts * 1000));
  const g = (k) => Number((p.find((x) => x.type === k) || {}).value || 0) % (k === "hour" ? 24 : 60);
  return g("hour") * 3600 + g("minute") * 60 + g("second");
}
const kyivMidnight = (ts) => ts - kyivSod(ts);
// ts в Киеве для «полночь + s секунд» с поправкой на смену летнего времени
function atSod(mid, s) {
  let ts = mid + s;
  const diff = kyivSod(ts) - (s % 86400);
  if (Math.abs(diff) <= 3600) ts -= diff;
  return ts;
}

// Ночные зоны = импортные зоны дешевле максимальной цены. Без тарифа — по price в points прогноза.
export function nightIntervals(tariff, from, to, points, slot) {
  const zones = ((tariff && tariff.zones) || []).filter((z) => !z.direction || z.direction === "import");
  const prices = zones.map((z) => Number(z.price_per_kwh)).filter(Number.isFinite);
  const out = [];
  if (prices.length > 1 && Math.min(...prices) < Math.max(...prices)) {
    const max = Math.max(...prices);
    const night = zones.filter((z) => Number(z.price_per_kwh) < max)
      .map((z) => [sod(z.start_time), sod(z.end_time)]).filter(([a, b]) => a !== null && b !== null);
    let mid = kyivMidnight(from - 86400);
    for (let guard = 0; mid < to && guard < 400; guard++) {
      night.forEach(([a, b]) => {
        const s = atSod(mid, a), e = b > a ? atSod(mid, b) : atSod(mid, 86400 + b);
        if (e > from && s < to) out.push([s, e]);
      });
      mid = kyivMidnight(mid + 26 * 3600);
    }
    return out;
  }
  const pr = (points || []).map((p) => p.price).filter(isNum).map(Number);
  if (pr.length && Math.min(...pr) < Math.max(...pr)) {
    const max = Math.max(...pr);
    (points || []).forEach((p) => { if (isNum(p.price) && Number(p.price) < max) out.push([p.ts, p.ts + (slot || 900)]); });
  }
  return out;
}

export function nightHook(intervals) {
  return (u) => {
    if (!intervals.length) return;
    const c = u.ctx, { left, top, width, height } = u.bbox;
    c.save();
    c.fillStyle = cssVar("--night");
    intervals.forEach(([a, b]) => {
      const x0 = Math.max(u.valToPos(a, "x", true), left), x1 = Math.min(u.valToPos(b, "x", true), left + width);
      if (x1 > x0) c.fillRect(x0, top, x1 - x0, height);
    });
    c.restore();
  };
}

export function nowHook(nowTs) {
  return (u) => {
    const x = u.valToPos(nowTs, "x", true);
    const { left, top, width, height } = u.bbox;
    if (!(x >= left && x <= left + width)) return;
    const c = u.ctx, dpr = window.devicePixelRatio || 1;
    c.save();
    c.strokeStyle = cssVar("--text");
    c.lineWidth = 1.5 * dpr;
    c.setLineDash([4 * dpr, 4 * dpr]);
    c.beginPath();
    c.moveTo(x, top);
    c.lineTo(x, top + height);
    c.stroke();
    c.setLineDash([]);
    c.fillStyle = cssVar("--muted");
    c.font = (11 * dpr) + "px Manrope, system-ui, sans-serif";
    c.textBaseline = "top";
    const label = t("chart.now");
    const w = c.measureText(label).width;
    c.fillText(label, x + 4 * dpr + w > left + width ? x - w - 4 * dpr : x + 4 * dpr, top + 2 * dpr);
    c.restore();
  };
}

export function zeroHook(scale) {
  return (u) => {
    const y = u.valToPos(0, scale, true);
    const { left, top, width, height } = u.bbox;
    if (!(y >= top && y <= top + height)) return;
    const c = u.ctx;
    c.save();
    c.strokeStyle = cssVar("--muted");
    c.lineWidth = window.devicePixelRatio || 1;
    c.beginPath();
    c.moveTo(left, y);
    c.lineTo(left + width, y);
    c.stroke();
    c.restore();
  };
}

// ===== Tooltip =====
// rowsFn(u, idx) -> {title, rows:[{color, label, value}]} | null
export function tooltipPlugin(rowsFn) {
  let tip = null;
  const hide = () => { if (tip) tip.hidden = true; };
  return {
    hooks: {
      init: (u) => {
        tip = document.createElement("div");
        tip.className = "u-tip";
        tip.hidden = true;
        u.over.appendChild(tip);
        u.over.addEventListener("mouseleave", hide);
      },
      setCursor: (u) => {
        const i = u.cursor.idx;
        if (i === null || i === undefined || u.cursor.left < 0) { hide(); return; }
        const r = rowsFn(u, i);
        if (!r || !r.rows.length) { hide(); return; }
        const head = document.createElement("div");
        head.className = "u-tip-h";
        head.textContent = r.title;
        const rows = r.rows.map((x) => {
          const row = document.createElement("div");
          row.className = "u-tip-r";
          const sw = document.createElement("i");
          sw.style.background = x.color;
          const l = document.createElement("span");
          l.textContent = x.label;
          const v = document.createElement("b");
          v.textContent = x.value;
          row.append(sw, l, v);
          return row;
        });
        tip.replaceChildren(head, ...rows);
        tip.hidden = false;
        const ow = u.over.clientWidth, oh = u.over.clientHeight;
        const tw = tip.offsetWidth, th = tip.offsetHeight;
        let x = u.cursor.left + 14;
        if (x + tw > ow) x = Math.max(0, u.cursor.left - tw - 14);
        const y = Math.max(0, Math.min(u.cursor.top - th / 2, oh - th));
        tip.style.transform = "translate(" + Math.round(x) + "px," + Math.round(y) + "px)";
      },
    },
  };
}

// ===== HTML-легенда =====
// items: [{type: line|dash|dot|box, color: CSS-цвет (можно var(--x)), label}]
export function renderLegend(el, items) {
  el.replaceChildren(...items.map((it) => {
    const s = document.createElement("span");
    s.className = "lg-i";
    const sw = document.createElement("i");
    sw.className = "sw sw-" + it.type;
    sw.style.setProperty("--c", it.color);
    sw.setAttribute("aria-hidden", "true");
    const l = document.createElement("span");
    l.textContent = it.label;
    s.append(sw, l);
    return s;
  }));
}

// Подставить null на месте пропусков (> 2.5 шагов), чтобы линии не соединяли дыры в данных.
export function withGaps(rows, step) {
  const out = [];
  rows.forEach((r, i) => {
    if (i && r.ts - rows[i - 1].ts > step * 2.5) out.push({ ts: rows[i - 1].ts + step });
    out.push(r);
  });
  return out;
}
