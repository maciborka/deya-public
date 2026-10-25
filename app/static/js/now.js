// #now (§7.2): схема потоков SVG, плитка SOC, итоги дня.
import { t } from "./i18n.js";
import { isNum, power, energy, money, pct, time, gridDir, battDir, gridDirKey, battDirKey } from "./fmt.js";
import { connState } from "./header.js";

const $ = (id) => document.getElementById(id);
const ACTIVE_W = 50;
const num = (v) => (isNum(v) ? Number(v) : null);

// Длительность цикла пунктира: больше мощность — быстрее (2.5 с … 0.8 с)
function duration(w) {
  const k = Math.min(Math.abs(w), 6000) / 6000;
  return (2.5 - 1.7 * k).toFixed(2) + "s";
}

function setLine(id, watts, color) {
  const el = $("ln-" + id);
  const on = watts !== null && watts > ACTIVE_W;
  el.setAttribute("class", "ln" + (on ? " on c-" + color : ""));
  if (on) el.style.setProperty("--dur", duration(watts));
  else el.style.removeProperty("--dur");
}

// Активность линий по правилу «солнце сначала в дім, затем в АКБ, остаток — в мережу».
export function flows(pv, load, grid, batt) {
  const p = Math.max(pv || 0, 0);
  const l = Math.max(load || 0, 0);
  const g = grid || 0;
  const b = batt || 0;
  const pvToHome = p > ACTIVE_W && l > 0 ? Math.min(p, l) : 0;
  const pvSurplus = Math.max(p - l, 0);
  const charge = b < -ACTIVE_W ? -b : 0;
  const discharge = b > ACTIVE_W ? b : 0;
  const pvToBatt = p > ACTIVE_W && charge ? Math.min(pvSurplus, charge) : 0;
  const homeFromGrid = Math.max(0, l - p - discharge);
  return {
    "sun-home": pvToHome,
    "sun-batt": pvToBatt,
    "sun-grid": g < -ACTIVE_W ? -g : 0,
    "grid-home": g > ACTIVE_W && homeFromGrid > ACTIVE_W ? homeFromGrid : 0,
    "grid-batt": g > ACTIVE_W && charge - pvToBatt > ACTIVE_W ? charge - pvToBatt : 0,
    "batt-home": discharge,
  };
}

const LINE_COLOR = { "sun-home": "sun", "sun-batt": "sun", "sun-grid": "sun", "grid-home": "grid", "grid-batt": "grid", "batt-home": "batt" };

function setText(id, v) { $(id).textContent = v; }

function renderStale(s) {
  const el = $("now-stale");
  if (s.errors.latest) {
    el.hidden = false;
    if (!s.latest) el.textContent = t("now.noData");
    else el.textContent = s.lastOk.latest ? t("stale", { time: time(s.lastOk.latest) }) : t("stale.never");
  } else el.hidden = true;
}

export function renderNow(s) {
  const sec = $("now");
  const d = s.latest;
  renderStale(s);
  if (!d) {
    if (s.errors.latest) sec.removeAttribute("aria-busy");
    return;
  }
  sec.removeAttribute("aria-busy");
  const conn = connState(s);
  sec.classList.toggle("offline", !conn.ok);

  const pv1 = num(d.pv1_power_w), pv2 = num(d.pv2_power_w);
  const pv = pv1 === null && pv2 === null ? null : (pv1 || 0) + (pv2 || 0);
  const load = num(d.load_power_w), grid = num(d.grid_power_w), batt = num(d.batt_power_w), soc = num(d.soc_pct);

  // значения на схеме
  setText("fv-pv", power(pv));
  setText("fv-east", t("now.east", { v: power(pv1) }));
  setText("fv-west", t("now.west", { v: power(pv2) }));
  setText("fv-load", power(load));
  setText("fv-grid", gridDirKey(grid) === "dir.idle" ? "0 " + t("unit.w") : power(grid));
  const out = d.grid_state === "down" || d.grid_state === "partial";
  setText("fv-grid-dir", out ? t("outage.node." + d.grid_state) : gridDir(grid));
  $("fv-grid").closest(".node").classList.toggle("out", out);
  setText("fv-soc", pct(soc));
  setText("fv-batt", battDirKey(batt) === "dir.idle" ? "0 " + t("unit.w") : power(batt));
  setText("fv-batt-dir", battDir(batt));
  $("fv-ring").setAttribute("stroke-dasharray", (soc === null ? 0 : Math.min(Math.max(soc, 0), 100)) + " 100");

  const fl = flows(pv, load, grid, batt);
  Object.keys(fl).forEach((k) => setLine(k, fl[k], LINE_COLOR[k]));

  const gridTxt = gridDirKey(grid) === "dir.idle" ? gridDir(grid) : power(grid) + " " + gridDir(grid);
  const battTxt = battDirKey(batt) === "dir.idle" ? battDir(batt) : battDir(batt) + " " + power(batt);
  $("flow").setAttribute("aria-label", t("now.flow.aria", {
    pv: power(pv), east: power(pv1), west: power(pv2), load: power(load), grid: gridTxt, soc: pct(soc), batt: battTxt,
  }));

  // плитка SOC
  setText("soc-v", pct(soc));
  const bms = num(d.bms_soc_pct);
  $("soc-v").title = bms === null ? "" : t("now.soc.bms", { pct: pct(bms) });
  const cap = s.strategy ? num(s.strategy.battery_capacity_kwh) : null;
  setText("soc-kwh", cap !== null && soc !== null ? t("now.soc.of", { kwh: energy(soc / 100 * cap), cap: energy(cap) }) : "");
  $("socbar-fill").style.width = (soc === null ? 0 : Math.min(Math.max(soc, 0), 100)) + "%";
  const reserve = s.strategy ? num(s.strategy.reserve_soc_pct) : null;
  const mark = $("socbar-res");
  mark.hidden = reserve === null;
  if (reserve !== null) mark.style.left = Math.min(Math.max(reserve, 0), 100) + "%";
  setText("soc-res", reserve === null ? "" : t("now.soc.reserve", { pct: pct(reserve) }));
  $("socbar").setAttribute("aria-label", t("now.soc.barAria", { soc: pct(soc), reserve: pct(reserve) }));

  // итоги дня
  setText("td-pv", energy(num(d.pv_today_kwh)));
  setText("td-load", energy(num(d.load_today_kwh)));
  setText("td-import", energy(num(d.grid_import_today_kwh)));
  setText("td-export", energy(num(d.grid_export_today_kwh)));
  const di = s.dayimport;
  const tile = $("tile-dayimp");
  if (di && isNum(di.import_kwh)) {
    const good = Number(di.import_kwh) < 0.5;
    setText("td-dayimp", energy(di.import_kwh) + " · " + money(di.cost_uah));
    tile.classList.toggle("good", good);
    tile.classList.toggle("bad", !good);
    tile.title = t(good ? "now.dayImport.good" : "now.dayImport.bad");
  } else {
    setText("td-dayimp", "—");
    tile.classList.remove("good", "bad");
    tile.removeAttribute("title");
  }
}
