// Форматирование чисел/времени — только через Intl, единицы из словаря.
import { t, locale } from "./i18n.js";

export const TZ = "Europe/Kyiv";
export const DASH = "—";
const IDLE_W = 50;

export const isNum = (v) => v !== null && v !== undefined && v !== "" && Number.isFinite(Number(v));

const cache = new Map();
function nf(min, max) {
  const key = locale() + "|" + min + "|" + max;
  if (!cache.has(key)) cache.set(key, new Intl.NumberFormat(locale(), { minimumFractionDigits: min, maximumFractionDigits: max }));
  return cache.get(key);
}
function dtf(opts) {
  const key = locale() + "|" + JSON.stringify(opts);
  if (!cache.has(key)) cache.set(key, new Intl.DateTimeFormat(locale(), { timeZone: TZ, hour12: false, ...opts }));
  return cache.get(key);
}

export function number(v, digits = 0, minDigits = digits) {
  return isNum(v) ? nf(minDigits, digits).format(Number(v)) : DASH;
}

// Мощность по модулю: < 1000 Вт -> "612 Вт", иначе "1,84 кВт". Направление — отдельно словами (dir*).
export function power(w) {
  if (!isNum(w)) return DASH;
  const a = Math.abs(Number(w));
  if (Math.round(a) < 1000) return number(Math.round(a), 0) + " " + t("unit.w");
  return number(a / 1000, 2, 2) + " " + t("unit.kw");
}

export function energy(kwh) {
  return isNum(kwh) ? number(kwh, 1, 1) + " " + t("unit.kwh") : DASH;
}

export function pct(v) {
  return isNum(v) ? number(Math.round(Number(v)), 0) + "%" : DASH;
}

export function money(v) {
  return isNum(v) ? number(v, 2, 2) + " " + t("unit.uah") : DASH;
}

function toDate(v) {
  if (v instanceof Date) return v;
  if (typeof v === "number") return new Date(v < 1e12 ? v * 1000 : v);
  if (typeof v === "string" && v) return new Date(v);
  return null;
}

// HH:MM (или HH:MM:SS), 24 ч, Europe/Kyiv. Принимает unix-сек, мс, ISO-строку или Date.
export function time(v, seconds = false) {
  const d = toDate(v);
  if (!d || isNaN(d)) return DASH;
  const opts = { hour: "2-digit", minute: "2-digit" };
  if (seconds) opts.second = "2-digit";
  return dtf(opts).format(d);
}

// ДД.ММ
export function date(v) {
  const d = toDate(v);
  if (!d || isNaN(d)) return DASH;
  const parts = dtf({ day: "2-digit", month: "2-digit" }).formatToParts(d);
  const get = (type) => (parts.find((p) => p.type === type) || {}).value || "";
  return get("day") + "." + get("month");
}

// Текущее время в Киеве как "HH:MM" (для сравнения с decision_time).
export function kyivHHMM(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false }).formatToParts(now);
  const get = (type) => (parts.find((p) => p.type === type) || {}).value || "00";
  return get("hour").replace("24", "00") + ":" + get("minute");
}

// Направления словами. grid: + импорт / − экспорт; batt: + разряд / − заряд.
export function gridDirKey(w) {
  if (!isNum(w) || Math.abs(w) < IDLE_W) return "dir.idle";
  return w > 0 ? "dir.fromGrid" : "dir.toGrid";
}
export function battDirKey(w) {
  if (!isNum(w) || Math.abs(w) < IDLE_W) return "dir.idle";
  return w > 0 ? "dir.discharging" : "dir.charging";
}
export const gridDir = (w) => t(gridDirKey(w));
export const battDir = (w) => t(battDirKey(w));

// "1,84 кВт · з мережі" / "простій"
export function powerWithDir(w, kind) {
  if (!isNum(w)) return DASH;
  const key = kind === "grid" ? gridDirKey(w) : battDirKey(w);
  if (key === "dir.idle") return t(key);
  return power(w) + " · " + t(key);
}

// Длительность в секундах → «N год MM хв» / «N хв» / «менше хвилини»
export function duration(sec) {
  const m = Math.floor(Math.max(0, Number(sec) || 0) / 60);
  if (m >= 60) return t("outage.dur.hm", { h: Math.floor(m / 60), m: String(m % 60).padStart(2, "0") });
  return m >= 1 ? t("outage.dur.m", { m }) : t("outage.dur.s");
}
