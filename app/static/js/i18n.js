// i18n: плоские словари /static/i18n/<lang>.json, список языков — /static/i18n/index.json.
// Новый язык (например ru) = новый файл + строка в index.json, без правок кода.
const BASE = "/static/i18n/";
const FALLBACK = "uk";
const KEY = "deya.lang";

let langs = [FALLBACK];
let lang = FALLBACK;
let dict = {};
let fallbackDict = {};
const meta = {}; // lang -> {short, name}

async function loadJSON(url) {
  const r = await fetch(url, { cache: "no-cache" });
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}

export function getLang() { return lang; }
export function getLangs() { return langs.map((code) => ({ code, ...(meta[code] || { short: code.toUpperCase(), name: code }) })); }
export function locale() { return dict["meta.locale"] || fallbackDict["meta.locale"] || "uk-UA"; }

export function t(key, params) {
  let s = dict[key];
  if (s === undefined) s = fallbackDict[key];
  if (s === undefined) return key;
  if (params) s = s.replace(/\{(\w+)\}/g, (m, k) => (params[k] === undefined || params[k] === null ? m : String(params[k])));
  return s;
}

// Плюрализация через Intl.PluralRules: ключи <base>.one/few/many/other
export function tp(base, n, params) {
  const cat = new Intl.PluralRules(locale()).select(n);
  const key = dict[base + "." + cat] !== undefined || fallbackDict[base + "." + cat] !== undefined ? base + "." + cat : base + ".other";
  return t(key, { n, ...(params || {}) });
}

export async function initI18n() {
  try {
    const list = await loadJSON(BASE + "index.json");
    if (Array.isArray(list) && list.length) langs = list;
  } catch (e) { /* остаёмся на uk */ }
  try { fallbackDict = await loadJSON(BASE + FALLBACK + ".json"); } catch (e) { fallbackDict = {}; }
  meta[FALLBACK] = { short: fallbackDict["meta.short"] || "UA", name: fallbackDict["meta.name"] || FALLBACK };
  let stored = null;
  try { stored = localStorage.getItem(KEY); } catch (e) { /* private mode */ }
  await setLang(stored && langs.includes(stored) ? stored : FALLBACK, false);
  // метаданные остальных языков для переключателя
  await Promise.all(langs.filter((c) => !meta[c]).map(async (c) => {
    try { const d = await loadJSON(BASE + c + ".json"); meta[c] = { short: d["meta.short"] || c.toUpperCase(), name: d["meta.name"] || c }; } catch (e) { /* skip */ }
  }));
}

export async function setLang(code, persist = true) {
  if (!langs.includes(code)) code = FALLBACK;
  if (code === FALLBACK) dict = fallbackDict;
  else {
    try { dict = await loadJSON(BASE + code + ".json"); } catch (e) { dict = fallbackDict; code = FALLBACK; }
  }
  lang = code;
  meta[code] = { short: dict["meta.short"] || code.toUpperCase(), name: dict["meta.name"] || code };
  document.documentElement.lang = code;
  if (persist) { try { localStorage.setItem(KEY, code); } catch (e) { /* ignore */ } }
  applyStatic(document);
  document.title = t("app.title");
}

// Статический текст: data-i18n -> textContent, data-i18n-aria -> aria-label, data-i18n-title -> title
export function applyStatic(root) {
  root.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  root.querySelectorAll("[data-i18n-aria]").forEach((el) => { el.setAttribute("aria-label", t(el.dataset.i18nAria)); });
  root.querySelectorAll("[data-i18n-title]").forEach((el) => { el.setAttribute("title", t(el.dataset.i18nTitle)); });
}
