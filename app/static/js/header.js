// Шапка (§7.1): тариф, связь, режим.
import { t } from "./i18n.js";
import { isNum, money, pct, time, duration } from "./fmt.js";

const $ = (id) => document.getElementById(id);
export const CONN_MAX_AGE = 60; // с

export function connState(s, nowSec = Date.now() / 1000) {
  const d = s.latest;
  if (!d || !isNum(d.ts)) return { ok: false, ts: null };
  const age = Math.max(0, nowSec - Number(d.ts));
  return { ok: !s.errors.latest && age <= CONN_MAX_AGE, ts: Number(d.ts), age };
}

export function controlMode(s) {
  return (s.plan && s.plan.control_mode) || (s.preview && s.preview.control_mode) || (s.strategy && s.strategy.control_mode) || null;
}

function renderTariff(s) {
  const chip = $("chip-tariff");
  const cur = s.tariff && s.tariff.current;
  if (!cur || !isNum(cur.price_per_kwh)) { chip.hidden = true; return; }
  const prices = ((s.tariff.zones || []).filter((z) => !z.direction || z.direction === "import"))
    .map((z) => Number(z.price_per_kwh)).filter((v) => Number.isFinite(v));
  let zone = cur.name || "";
  let isDay = true;
  if (prices.length > 1 && Math.min(...prices) < Math.max(...prices)) {
    isDay = Number(cur.price_per_kwh) >= Math.max(...prices);
    zone = t(isDay ? "tariff.day" : "tariff.night");
  }
  const price = money(cur.price_per_kwh) + "/" + t("unit.kwh");
  chip.hidden = false;
  chip.classList.toggle("is-day", isDay);
  chip.querySelector(".chip-t").textContent = zone + " · " + price;
  chip.setAttribute("aria-label", t("tariff.aria", { zone, price }));
}

function renderConn(s) {
  const chip = $("chip-conn");
  const c = connState(s);
  const txt = chip.querySelector(".chip-t");
  chip.classList.toggle("ok", c.ok);
  if (!s.loaded.latest && !s.errors.latest) {
    chip.classList.remove("bad");
    txt.textContent = t("conn.loading");
    return;
  }
  chip.classList.toggle("bad", !c.ok);
  if (c.ok) txt.textContent = t("conn.online", { time: time(c.ts, true) });
  else if (c.ts === null) txt.textContent = t("conn.offlineNoData");
  else txt.textContent = t("conn.offline", { time: time(c.ts), min: Math.floor(c.age / 60) });
}

function renderMode(s) {
  const chip = $("chip-mode");
  const m = controlMode(s);
  if (!m) { chip.hidden = true; return; }
  const name = ["off", "advise", "auto"].includes(m) ? t("mode." + m) : m;
  chip.hidden = false;
  chip.querySelector(".chip-t").textContent = name;
  chip.setAttribute("aria-label", t("mode.aria", { mode: name }));
}

function renderFloor(s) {
  const chip = $("chip-floor");
  const st = s.strategy;
  if (!st || !isNum(st.normal_floor_pct)) { chip.hidden = true; return; }
  const emerg = st.emergency_mode && st.emergency_mode !== "off";
  const v = emerg ? st.emergency_floor_pct : st.normal_floor_pct;
  const txt = t(emerg ? "floor.chip.emergency" : "floor.chip.normal", { pct: pct(v) });
  chip.hidden = false;
  chip.classList.toggle("emerg", !!emerg);
  chip.querySelector(".chip-t").textContent = txt;
  chip.setAttribute("aria-label", txt);
}

function renderOutage(s) {
  const chip = $("chip-outage");
  const d = s.latest;
  const st = d && d.grid_state;
  if (st !== "down" && st !== "partial") { chip.hidden = true; return; }
  let txt;
  if (st === "down") {
    const since = isNum(d.outage_since_ts) ? Number(d.outage_since_ts) : null;
    txt = since === null ? t("outage.chip.downNoTime")
      : t("outage.chip.down", { time: time(since), dur: duration(Date.now() / 1000 - since) });
  } else {
    const phases = [1, 2, 3].filter((i) => isNum(d["grid_l" + i + "_voltage_v"]) && Number(d["grid_l" + i + "_voltage_v"]) < 180)
      .map((i) => "L" + i).join(", ");
    txt = t("outage.chip.partial", { phases });
  }
  chip.hidden = false;
  chip.querySelector(".chip-t").textContent = txt;
  chip.setAttribute("aria-label", txt);
}

export function renderHeader(s) {
  renderOutage(s);
  renderFloor(s);
  renderTariff(s);
  renderConn(s);
  renderMode(s);
}
