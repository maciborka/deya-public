// Deya dashboard — точка входа (ES-модуль): шапка, #now, #tonight, #forecast, #history, #tech.
import { initI18n, setLang, getLang, getLangs, t } from "./js/i18n.js";
import { register, start, run } from "./js/poll.js";
import { renderHeader, connState } from "./js/header.js";
import { renderNow } from "./js/now.js";
import { renderTonight } from "./js/tonight.js";
import { initForecast, renderForecast, getWhatIf, noForecast } from "./js/forecast.js";
import { initHistory, renderHistory, histUrl, histTag, srcUrl, srcTag } from "./js/history.js";
import { initWeather, renderWeather } from "./js/weather.js";
import { initTech, renderTech, techOpened } from "./js/tech.js";

const THEME_KEY = "deya.theme";
const SEC = 1000, MIN = 60 * SEC;

// Общее состояние: последние успешные данные не стираются при ошибках.
const store = {
  latest: null, plan: null, preview: null, forecast: null, tariff: null, strategy: null,
  soc12: null, whatif: null, hist: null, daily: null, flows: null, weather: null, mode: null, fstatus: null, dayimport: null, outages: null, loadsrc: null,
  loaded: {}, errors: {}, lastOk: {},
};

// Каждая секция изолирована: ошибка рендера одной не ломает остальные.
function safe(fn) {
  try { fn(store); } catch (e) { console.error(e); }
}
function renderAll() {
  safe(renderHeader);
  safe(renderNow);
  safe(renderWeather);
  safe(renderTonight);
  safe(renderForecast);
  safe(renderHistory);
  safe(renderTech);
}

// ===== Тема =====
function storedTheme() {
  try { const v = localStorage.getItem(THEME_KEY); return v === "dark" || v === "light" ? v : null; } catch (e) { return null; }
}
function systemTheme() {
  return window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const btn = document.getElementById("btn-theme");
  const key = theme === "dark" ? "header.theme.toLight" : "header.theme.toDark";
  btn.dataset.i18nAria = key;
  btn.setAttribute("aria-label", t(key));
  btn.title = t(key);
}
function initTheme() {
  applyTheme(storedTheme() || systemTheme());
  document.getElementById("btn-theme").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* ignore */ }
    applyTheme(next);
    renderAll(); // графики берут цвета из токенов темы
  });
  if (window.matchMedia) {
    const mq = matchMedia("(prefers-color-scheme: light)");
    const onChange = () => { if (!storedTheme()) { applyTheme(systemTheme()); renderAll(); } };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
  }
}

// ===== Язык =====
function renderLangSeg() {
  const seg = document.getElementById("lang-seg");
  seg.replaceChildren(...getLangs().map((l) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = l.short;
    b.lang = l.code;
    b.setAttribute("aria-label", l.name);
    b.setAttribute("aria-pressed", String(l.code === getLang()));
    b.addEventListener("click", async () => {
      if (l.code === getLang()) return;
      await setLang(l.code);
      applyTheme(document.documentElement.dataset.theme);
      renderLangSeg();
      renderAll();
    });
    return b;
  }));
}

// ===== Опрос =====
function task(name, url, interval, extra) {
  register(name, {
    url, interval,
    onData(data) {
      store[name] = extra && extra.map ? extra.map(data) : data;
      store.loaded[name] = true;
      store.errors[name] = null;
      store.lastOk[name] = Date.now();
      renderAll();
      if (extra && extra.after) extra.after();
    },
    onError(err) {
      store.errors[name] = err;
      renderAll();
      if (extra && extra.after) extra.after();
    },
    ...(extra || {}),
  });
}

function hasFinalDecision() {
  const p = store.plan;
  if (!p || !p.decision) return false;
  const a = p.apply || {};
  if (a.has_decision === false) return false;
  return !(a.night_date && p.decision.night_date && a.night_date !== p.decision.night_date);
}

async function main() {
  initTheme();
  await initI18n();
  applyTheme(document.documentElement.dataset.theme);
  renderLangSeg();
  initForecast({
    rerender: renderAll,
    onWhatIf(target) {
      store.whatif = null;
      store.errors.whatif = null;
      renderAll();
      if (target !== null) run("whatif");
    },
  });
  initWeather();
  initHistory({ onRange: () => { run("hist"); run("loadsrc"); } });
  initTech({ onOpen: () => { renderAll(); run("mode"); run("fstatus"); } });
  renderAll();

  task("latest", "/api/latest", 10 * SEC);
  task("plan", "/api/plan", MIN);
  // preview нужен только пока нет финального решения на эту ночь
  task("preview", "/api/plan/preview", MIN, { enabled: () => !hasFinalDecision() || !store.preview });
  // нет прогноза -> сразу узнать время следующей загрузки (next_run_ts) для пустого состояния
  task("forecast", "/api/plan/forecast?hours=24", 5 * MIN, { after: () => { if (noForecast(store) && !store.fstatus) run("fstatus"); } });
  task("tariff", "/api/tariff", 5 * MIN);
  task("strategy", "/api/strategy", 5 * MIN);
  // #forecast: факт SOC за 12 ч; what-if — только GET, по запросу ползунка (debounce в forecast.js)
  task("soc12", "/api/samples?hours=12&step=300&fields=soc_pct", 5 * MIN);
  task("whatif", () => "/api/plan/forecast?hours=24&target=" + getWhatIf(), 5 * MIN, { enabled: () => getWhatIf() !== null });
  // #history
  task("hist", histUrl, 5 * MIN, { map: histTag });
  task("loadsrc", srcUrl, 5 * MIN, { map: srcTag });
  task("daily", "/api/daily?days=30", 5 * MIN);
  task("flows", "/api/flows", 5 * MIN);
  task("weather", "/api/weather", 30 * MIN);
  task("dayimport", "/api/day-import", 5 * MIN);
  task("outages", "/api/outages?days=30", 5 * MIN);
  // #tech — после первого раскрытия; статус прогноза нужен и для пустого состояния #forecast
  task("mode", "/api/mode", 5 * MIN, { enabled: techOpened });
  task("fstatus", "/api/forecast/status", 5 * MIN, { enabled: () => techOpened() || noForecast(store) });
  start();

  // Часы связи в шапке; #now перерисовываем, когда меняется онлайн/офлайн без нового ответа.
  let lastConn = null;
  setInterval(() => {
    if (document.hidden) return;
    renderHeader(store);
    const ok = connState(store).ok;
    if (ok !== lastConn) { lastConn = ok; renderNow(store); }
  }, SEC);
}

main();
