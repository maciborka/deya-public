// #tech (§7.6): свёрнутый <details>; /api/mode и /api/forecast/status грузятся при первом раскрытии.
import { t } from "./i18n.js";
import { isNum, number, power, powerWithDir, time, date, DASH } from "./fmt.js";

const $ = (id) => document.getElementById(id);
const PROVIDERS = { open_meteo: "Open-Meteo", forecast_solar: "Forecast.Solar", solcast: "Solcast" };
let opened = false;

export const techOpened = () => opened;

export function initTech({ onOpen }) {
  const d = $("tech-d");
  d.addEventListener("toggle", () => {
    if (d.open && !opened) { opened = true; onOpen(); }
  });
}

const unit = (v, digits, key) => (isNum(v) ? number(v, digits, digits) + " " + t(key) : DASH);
const temp = (v) => (isNum(v) ? number(v, 1, 1) + " °C" : DASH);

function table(tbl, head, rows) {
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  head.forEach((h) => { const th = document.createElement("th"); th.scope = "col"; th.textContent = h; hr.append(th); });
  thead.append(hr);
  const tbody = document.createElement("tbody");
  rows.forEach((r) => {
    const tr = document.createElement("tr");
    r.forEach((c, i) => {
      const cell = document.createElement(i === 0 ? "th" : "td");
      if (i === 0) cell.scope = "row";
      if (c && typeof c === "object") { cell.textContent = c.text; if (c.cls) cell.className = c.cls; } else cell.textContent = c;
      tr.append(cell);
    });
    tbody.append(tr);
  });
  tbl.replaceChildren(thead, tbody);
}

function kv(dl, pairs) {
  dl.replaceChildren(...pairs.flatMap(([k, v]) => {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    return [dt, dd];
  }));
}

function renderLive(d) {
  if (!d) return;
  table($("tech-phases"), [t("tech.phase"), t("tech.power"), t("tech.voltage")], ["l1", "l2", "l3"].map((p, i) => [
    "L" + (i + 1), powerWithDir(d["grid_" + p + "_power_w"], "grid"), unit(d["grid_" + p + "_voltage_v"], 1, "unit.v"),
  ]));
  $("tech-freq").textContent = t("tech.freq", { v: unit(d.grid_freq_hz, 2, "unit.hz") });
  kv($("tech-temps"), [[t("tech.temp.dc"), temp(d.dc_temp_c)], [t("tech.temp.ac"), temp(d.ac_temp_c)], [t("tech.temp.batt"), temp(d.batt_temp_c)]]);
  table($("tech-pv"), [t("tech.string"), t("tech.voltage"), t("tech.current"), t("tech.power")], [1, 2].map((n) => [
    t("tech.pv" + n), unit(d["pv" + n + "_voltage_v"], 1, "unit.v"), unit(d["pv" + n + "_current_a"], 1, "unit.a"), power(d["pv" + n + "_power_w"]),
  ]));
}

const onOff = (v) => (v === true || v === 1 ? t("tech.on") : v === false || v === 0 ? t("tech.off") : DASH);

function renderMode(s) {
  const m = s.mode;
  const err = $("tech-mode-err");
  err.hidden = !!m || !s.errors.mode;
  err.textContent = s.errors.mode ? t("tech.noData") : "";
  if (!m) return;
  const gc = m.grid_charge || {}, ex = m.export || {}, tou = m.tou || {};
  const days = Array.isArray(tou.days) && tou.days.length === 7 ? t("tech.tou.allDays") : (tou.days_label || (tou.days || []).join(", ") || DASH);
  kv($("tech-mode"), [
    [t("tech.mode.pattern"), m.energy_pattern || DASH],
    [t("tech.mode.work"), m.work_mode || DASH],
    [t("tech.mode.gridCharge"), onOff(gc.enabled) + (gc.enabled && isNum(gc.current_a) ? " · " + unit(gc.current_a, 0, "unit.a") : "")],
    [t("tech.mode.export"), onOff(ex.enabled) + (ex.enabled && isNum(ex.max_power_w) ? " · " + t("tech.mode.upTo", { v: power(ex.max_power_w) }) : "")],
    [t("tech.mode.tou"), onOff(tou.enabled) + (tou.enabled ? " · " + days : "")],
  ]);
  $("tech-mode-ts").textContent = isNum(m.snapshot_ts) ? t("tech.mode.snapshot", { time: date(m.snapshot_ts) + " " + time(m.snapshot_ts) }) : "";
  const src = (sl) => (sl.source === 1 ? t("tech.tou.src.grid") : sl.source === 0 ? t("tech.tou.src.none") : (sl.source_label || DASH));
  table($("tech-tou"), [t("tech.tou.n"), t("tech.tou.time"), t("tech.power"), "SOC", t("tech.tou.source")], (tou.slots || []).map((sl) => [
    String(sl.n), (sl.start || DASH) + "–" + (sl.end || DASH), power(sl.power_w), isNum(sl.soc_pct) ? number(sl.soc_pct, 0) + "%" : DASH, src(sl),
  ]));
}

function renderRuns(s) {
  const st = s.fstatus;
  const err = $("tech-runs-err");
  err.hidden = !!st || !s.errors.fstatus;
  err.textContent = s.errors.fstatus ? t("tech.noData") : "";
  if (!st) return;
  const names = Object.fromEntries((st.provider_details || []).map((p) => [p.code, p.name]));
  table($("tech-runs"), [t("tech.runs.time"), t("tech.runs.provider"), t("tech.runs.panel"), t("tech.runs.state")], (st.runs || []).slice(0, 8).map((r) => [
    date(r.ts) + " " + time(r.ts), names[r.provider] || PROVIDERS[r.provider] || r.provider || DASH, r.panel || DASH,
    r.ok ? { text: t("tech.runs.ok", { n: isNum(r.n) ? r.n : 0 }), cls: "ok" } : { text: t("tech.runs.err", { e: r.error || "" }).trim(), cls: "bad" },
  ]));
  $("tech-next").textContent = isNum(st.next_run_ts) ? t("tech.runs.next", { time: time(st.next_run_ts) }) : "";
}

export function renderTech(s) {
  if (!opened) return;
  renderLive(s.latest);
  renderMode(s);
  renderRuns(s);
}
