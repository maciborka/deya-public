// #weather: прогноз погоды (Open-Meteo): сегодня + завтра, раскрываемая неделя.
import { t, locale } from "./i18n.js";
import { isNum, number, time, DASH } from "./fmt.js";

const $ = (id) => document.getElementById(id);
const OPEN_KEY = "deya.weatherOpen";
const ICONS = "/static/vendor/meteocons/";

// WMO weather_code -> [день, ночь]
const ICON_BY_CODE = {
  0: ["clear-day", "clear-night"], 1: ["clear-day", "clear-night"],
  2: ["partly-cloudy-day", "partly-cloudy-night"], 3: ["overcast", "overcast"],
  45: ["fog", "fog"], 48: ["fog", "fog"],
  51: ["drizzle", "drizzle"], 53: ["drizzle", "drizzle"], 55: ["drizzle", "drizzle"],
  56: ["sleet", "sleet"], 57: ["sleet", "sleet"], 66: ["sleet", "sleet"], 67: ["sleet", "sleet"],
  61: ["rain", "rain"], 63: ["rain", "rain"], 65: ["raindrops", "raindrops"],
  80: ["rain", "rain"], 81: ["rain", "rain"], 82: ["raindrops", "raindrops"],
  71: ["snow", "snow"], 73: ["snow", "snow"], 75: ["snowflake", "snowflake"], 77: ["snow", "snow"],
  85: ["snow", "snow"], 86: ["snowflake", "snowflake"],
  95: ["thunderstorms-rain", "thunderstorms-rain"], 96: ["hail", "hail"], 99: ["hail", "hail"],
};

export function iconFor(code, isDay = true) {
  const pair = ICON_BY_CODE[code];
  if (!pair) return "cloudy";
  return pair[isDay === false ? 1 : 0];
}

function codeLabel(code) {
  const key = "weather.code." + code;
  const s = t(key);
  return s === key ? t("weather.code.unknown") : s;
}

function icon(name, size, alt) {
  const img = document.createElement("img");
  img.src = ICONS + name + ".svg";
  img.width = size;
  img.height = size;
  img.alt = alt || "";
  if (!alt) img.setAttribute("aria-hidden", "true");
  img.className = "wx-ico";
  return img;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

const deg = (v) => (isNum(v) ? number(Math.round(Number(v)), 0) + "°" : DASH);
const pctv = (v) => (isNum(v) ? number(Math.round(Number(v)), 0) + "%" : DASH);

function stat(iconName, text, label) {
  const row = el("div", "wx-stat");
  row.title = label;
  row.append(icon(iconName, 22), el("span", "", text));
  return row;
}

function dayCard(title, d, cur) {
  const card = el("div", "tile wx-card");
  const head = el("div", "wx-head");
  head.append(el("div", "lbl strong", title));
  const main = el("div", "wx-main");
  const isDay = cur ? cur.is_day !== false : true;
  // Иконка — по дневному коду, чтобы совпадала с подписью; день/ночь — по текущему часу.
  main.append(icon(iconFor(d.code, isDay), 96, codeLabel(d.code)));
  const temps = el("div", "wx-temps");
  if (cur && isNum(cur.temp)) {
    temps.append(el("div", "wx-now", deg(cur.temp)), el("div", "sub", t("weather.now")));
  }
  temps.append(el("div", cur ? "sub" : "wx-now", t("weather.maxmin", { max: deg(d.t_max), min: deg(d.t_min) })));
  main.append(temps);
  card.append(head, main, el("div", "sub wx-desc", codeLabel(d.code)));
  const stats = el("div", "wx-stats");
  const mm = isNum(d.precip_mm) ? number(d.precip_mm, 1, 1) + " " + t("weather.mm") : DASH;
  stats.append(
    stat("raindrop", mm + " · " + pctv(d.precip_prob), t("weather.precip")),
    stat("overcast", pctv(d.cloud), t("weather.cloud")),
    stat("wind", isNum(d.wind_max) ? number(d.wind_max, 1, 1) + " " + t("weather.ms") : DASH, t("weather.wind")),
  );
  if (cur) {
    stats.append(
      stat("sunrise", hhmm(d.sunrise), t("weather.sunrise")),
      stat("sunset", hhmm(d.sunset), t("weather.sunset")),
    );
  }
  card.append(stats);
  return card;
}

// sunrise/sunset приходят как локальное время Киева "YYYY-MM-DDTHH:MM"
function hhmm(s) {
  return typeof s === "string" && s.length >= 16 ? s.slice(11, 16) : DASH;
}

function weekday(dateStr, short = true) {
  const [y, m, d] = dateStr.split("-").map(Number);
  return new Intl.DateTimeFormat(locale(), { weekday: short ? "short" : "long", timeZone: "UTC" })
    .format(new Date(Date.UTC(y, m - 1, d)));
}

function weekRow(d) {
  const c = el("div", "wx-day");
  c.append(
    el("div", "wx-dn", weekday(d.date)),
    icon(iconFor(d.code, true), 60, codeLabel(d.code)),
    el("div", "wx-dt", deg(d.t_max) + " / " + deg(d.t_min)),
    el("div", "sub", isNum(d.precip_mm) ? number(d.precip_mm, 1, 1) + " " + t("weather.mm") : DASH),
  );
  return c;
}

function isOpen() {
  try { return localStorage.getItem(OPEN_KEY) === "1"; } catch (e) { return false; }
}
function setOpen(v) {
  try { localStorage.setItem(OPEN_KEY, v ? "1" : "0"); } catch (e) { /* ignore */ }
}

function syncToggle() {
  const open = isOpen();
  const btn = $("wx-toggle");
  btn.setAttribute("aria-expanded", String(open));
  btn.textContent = t(open ? "weather.week.hide" : "weather.week.show");
  $("wx-week").hidden = !open;
}

export function initWeather() {
  $("wx-toggle").addEventListener("click", () => { setOpen(!isOpen()); syncToggle(); });
}

export function renderWeather(s) {
  const sec = $("weather");
  if (!sec) return;
  const d = s.weather;
  const stale = $("wx-stale");
  const msg = $("wx-msg");
  syncToggle();
  if (!d || !Array.isArray(d.days) || !d.days.length) {
    if (s.errors.weather || s.loaded.weather) sec.removeAttribute("aria-busy");
    $("wx-days").replaceChildren();
    $("wx-week").replaceChildren();
    $("wx-toggle").hidden = true;
    msg.hidden = !(s.errors.weather || s.loaded.weather);
    msg.textContent = t("weather.empty");
    stale.hidden = true;
    return;
  }
  sec.removeAttribute("aria-busy");
  msg.hidden = true;
  $("wx-toggle").hidden = false;
  if (d.stale || s.errors.weather) {
    stale.hidden = false;
    stale.textContent = t("stale", { time: time(d.fetched_at) });
  } else stale.hidden = true;

  const cards = [dayCard(t("weather.today"), d.days[0], d.current)];
  if (d.days[1]) cards.push(dayCard(t("weather.tomorrow"), d.days[1], null));
  $("wx-days").replaceChildren(...cards);
  $("wx-week").replaceChildren(...d.days.map(weekRow));
}
