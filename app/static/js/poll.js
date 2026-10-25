// Единый планировщик опроса API.
// - AbortController на каждый запрос (+ таймаут);
// - document.hidden -> пауза (таймеры сняты, запросы прерваны), при возврате — немедленный опрос;
// - ошибка -> экспоненциальный бэкофф до 60 с, данные не стираются (onError получает lastOk).
const MAX_BACKOFF = 60000;
const REQUEST_TIMEOUT = 20000;

const tasks = new Map();
let started = false;

async function fetchJSON(url, signal) {
  const r = await fetch(url, { signal, headers: { Accept: "application/json" } });
  if (!r.ok) {
    let detail = "";
    try { const j = await r.json(); detail = j && j.detail ? String(j.detail) : ""; } catch (e) { /* not json */ }
    const err = new Error(detail || url + " " + r.status);
    err.status = r.status;
    err.detail = detail;
    throw err;
  }
  return r.json();
}

/**
 * register(name, {url: string|() => string, interval: ms, enabled?: () => bool, onData(data), onError(err, lastOkTs)})
 */
export function register(name, spec) {
  tasks.set(name, { ...spec, name, timer: null, ctrl: null, fails: 0, lastOk: null, running: false });
  if (started) run(name);
}

function clear(task) {
  if (task.timer) { clearTimeout(task.timer); task.timer = null; }
}

function schedule(task, delay) {
  clear(task);
  if (document.hidden) return;
  task.timer = setTimeout(() => run(task.name), delay);
}

export async function run(name) {
  const task = tasks.get(name);
  if (!task) return;
  clear(task);
  if (document.hidden) return;
  if (task.enabled && !task.enabled()) { schedule(task, task.interval); return; }
  if (task.ctrl) task.ctrl.abort();
  const ctrl = new AbortController();
  task.ctrl = ctrl;
  const to = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT);
  const url = typeof task.url === "function" ? task.url() : task.url;
  try {
    const data = await fetchJSON(url, ctrl.signal);
    if (task.ctrl !== ctrl) return; // перебит более новым запросом
    task.fails = 0;
    task.lastOk = Date.now();
    task.onData && task.onData(data);
    schedule(task, task.interval);
  } catch (err) {
    if (task.ctrl !== ctrl) return;
    if (err.name === "AbortError" && document.hidden) return; // пауза, не ошибка
    task.fails += 1;
    task.onError && task.onError(err, task.lastOk);
    const base = Math.min(task.interval, 10000);
    schedule(task, Math.min(MAX_BACKOFF, base * 2 ** (task.fails - 1)));
  } finally {
    clearTimeout(to);
    if (task.ctrl === ctrl) task.ctrl = null;
  }
}

export function lastOk(name) {
  const task = tasks.get(name);
  return task ? task.lastOk : null;
}

export function start() {
  if (started) return;
  started = true;
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      tasks.forEach((task) => { clear(task); if (task.ctrl) { task.ctrl.abort(); task.ctrl = null; } });
    } else {
      tasks.forEach((task) => run(task.name));
    }
  });
  tasks.forEach((task) => run(task.name));
}
