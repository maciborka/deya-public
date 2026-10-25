// #tonight (§7.3): цель ночного заряда, статус записи (7 состояний), деньги за 24 ч, детали, предупреждения.
import { t, tp } from "./i18n.js";
import { isNum, pct, money, energy, time, kyivHHMM } from "./fmt.js";
import { controlMode } from "./header.js";

const $ = (id) => document.getElementById(id);
const DEFAULT_DECISION_TIME = "22:45";
const MORNING = "07:00";

const ICONS = {
  pending: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  applied: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.7 2.7L16 9.8"/>',
  queued: '<path d="M7 3h10M7 21h10M8 3c0 5 8 5 8 9s-8 4-8 9M16 3c0 5-8 5-8 9s8 4 8 9"/>',
  failed: '<path d="M12 3l9.5 17h-19z"/><path d="M12 10v4M12 17h.01"/>',
  advice: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5h.01"/>',
  off: '<path d="M12 3v8"/><path d="M6.3 6.8a8 8 0 1 0 11.4 0"/>',
  none: '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
};

function decisionTime(s) {
  return (s.strategy && s.strategy.decision_time) || DEFAULT_DECISION_TIME;
}

// Финальное решение на текущую ночь (а не вчерашнее).
function finalDecision(s) {
  const plan = s.plan;
  if (!plan || !plan.decision) return null;
  const a = plan.apply || {};
  if (a.has_decision === false) return null;
  if (a.night_date && plan.decision.night_date && a.night_date !== plan.decision.night_date) return null;
  return plan.decision;
}

function applyBlock(s) {
  return (s.plan && s.plan.apply) || (s.preview && s.preview.apply) || {};
}

// Сырой статус: бэкенд отдаёт apply.status = pending|none|advice|queued|applied|failed|skipped.
// Fallback, если поля нет: по наличию решения и времени (Europe/Kyiv).
function rawStatus(s, fin) {
  const a = applyBlock(s);
  if (a.status) return a.status;
  if (fin && fin.status) return fin.status;
  if (fin) return "advice";
  const now = kyivHHMM();
  const dt = decisionTime(s);
  return now >= dt || now < MORNING ? "none" : "pending";
}

// Итоговое состояние чипа (одно из 7)
export function tonightState(s) {
  const fin = finalDecision(s);
  const raw = rawStatus(s, fin);
  const mode = controlMode(s);
  let state = raw;
  if (raw === "skipped") state = "off";
  else if (mode === "off" && !["applied", "queued", "failed"].includes(raw)) state = "off";
  else if (raw === "advice" && mode === "auto") state = "queued"; // авто: решение принято, ждёт записи
  if (!ICONS[state]) state = fin ? "advice" : "pending";
  return { state, raw, fin, mode };
}

function invTargets(a) {
  const v = a.inverter_night_targets_pct;
  if (v === null || v === undefined) return t("tonight.inverterUnknown");
  if (Array.isArray(v)) return v.length ? v.map((x) => pct(x)).join(" / ") : t("tonight.inverterNone");
  return pct(v);
}

function uniq(list) {
  const out = [];
  list.forEach((w) => { const x = String(w || "").trim(); if (x && !out.includes(x)) out.push(x); });
  return out;
}

function statusText(st, s, dec) {
  const a = applyBlock(s);
  const fin = st.fin;
  const created = fin && fin.created_at;
  switch (st.state) {
    case "pending": return { text: t("tonight.status.pending", { time: decisionTime(s) }) };
    case "applied": return { text: a.applied_at ? t("tonight.status.applied", { time: time(a.applied_at) }) : t("tonight.status.appliedNoTime"), note: a.apply_note };
    case "queued": return { text: created ? t("tonight.status.queued", { time: time(created) }) : t("tonight.status.queuedNoTime"), note: a.apply_note };
    case "failed": {
      const at = a.applied_at || created;
      const head = at ? t("tonight.status.failed", { time: time(at) }) : t("tonight.status.failedNoTime");
      const note = [a.apply_note, t("tonight.status.failedStay", { v: invTargets(a) })].filter(Boolean).join(" · ");
      return { text: head, note };
    }
    case "advice": return { text: t("tonight.status.advice"), note: a.apply_note };
    case "off": return { text: t("tonight.status.off"), note: st.raw === "skipped" ? a.apply_note : null };
    case "none": {
      const reason = (dec && (dec.warnings || [])[0]) || (s.forecast && (s.forecast.warnings || [])[0]) ||
        (s.errors.preview && s.errors.preview.detail) || null;
      const manual = a.manual_no_decision ? t("tonight.status.manualNoDecision") : null;
      const note = [manual, reason, t("tonight.status.noneStay", { v: invTargets(a) })].filter(Boolean).join(" · ");
      return { text: t("tonight.status.none"), note };
    }
    default: return { text: "—" };
  }
}

function renderBars(dec) {
  const box = $("tn-bars");
  const rows = [
    ["tonight.model.noCharge", dec && dec.cost_no_charge_uah, false, null],
    ["tonight.model.chosen", dec && dec.expected_cost_uah, true, dec && dec.target_pct],
    ["tonight.model.ceiling", dec && dec.cost_to_ceiling_uah, false, null],
    ["tonight.model.full", dec && dec.cost_to_100_uah, false, null],
  ].filter((r) => isNum(r[1]));
  const max = Math.max(0, ...rows.map((r) => Math.abs(Number(r[1]))));
  box.replaceChildren(...rows.map(([key, v, chosen, target]) => {
    const row = document.createElement("div");
    row.className = "bar-row" + (chosen ? " chosen" : "");
    const l = document.createElement("span");
    l.className = "bar-l";
    l.textContent = t(key, { pct: pct(target) });
    const track = document.createElement("span");
    track.className = "bar-t";
    const fill = document.createElement("span");
    fill.className = "bar-f";
    fill.style.display = "block";
    fill.style.width = (max > 0 ? Math.abs(Number(v)) / max * 100 : 0) + "%";
    track.append(fill);
    const val = document.createElement("span");
    val.className = "bar-v";
    val.textContent = money(v);
    row.append(l, track, val);
    return row;
  }));
  box.hidden = rows.length === 0;
}

function renderStale(s) {
  const el = $("tonight-stale");
  const failing = ["plan", "preview", "forecast"].filter((k) => s.errors[k] && s[k]);
  if (!failing.length) { el.hidden = true; return; }
  const since = Math.min(...failing.map((k) => s.lastOk[k] || Infinity));
  el.hidden = false;
  el.textContent = Number.isFinite(since) ? t("stale", { time: time(since) }) : t("stale.never");
}

const FS_ICONS = {
  ok: '<path d="M8 12.5l2.7 2.7L16 9.8"/><circle cx="12" cy="12" r="9"/>',
  queued: ICONS.queued,
  failed: ICONS.failed,
  pending: ICONS.pending,
};

const FS_KEYS = { ok: "tonight.floor.ok", queued: "tonight.floor.queued", failed: "tonight.floor.failed", pending: "tonight.floor.pending" };

// Вероятность покупки днём, ожидаемый объём, предупреждение и статус записи порогов в инвертор.
function renderDay(s, dec) {
  const box = $("tn-day");
  const fs = (s.plan && s.plan.floor_sync) || (s.preview && s.preview.floor_sync) || null;
  const inp = dec && dec.inputs;
  const hasProb = inp && isNum(inp.day_import_prob);
  box.hidden = !hasProb && !fs;
  $("tn-day-prob").hidden = !hasProb;
  $("tn-day-kwh").hidden = !hasProb;
  const warn = $("tn-daywarn");
  if (hasProb) {
    $("tn-day-prob").textContent = t("tonight.day.prob", { p: pct(inp.day_import_prob * 100), tol: pct((inp.day_import_tol || 0) * 100) });
    $("tn-day-kwh").textContent = t("tonight.day.kwh", { kwh: energy(inp.expected_day_import_kwh) });
    const bad = inp.day_import_constraint_met === false;
    warn.hidden = !bad;
    if (bad) {
      const w = ((dec.warnings || inp.warnings || []).find((x) => /днём|вдень|day/i.test(String(x)))) || t("tonight.day.warn");
      $("tn-daywarn-t").textContent = w;
    }
  } else warn.hidden = true;
  const fl = $("tn-floor");
  fl.hidden = !fs;
  if (fs) {
    const kind = FS_ICONS[fs.status] ? fs.status : "pending";
    fl.className = "fl-sync " + kind;
    $("tn-floor-ico").innerHTML = '<svg class="ico" viewBox="0 0 24 24">' + FS_ICONS[kind] + "</svg>";
    const v = isNum(fs.inverter_floor_pct) ? fs.inverter_floor_pct : fs.target_floor_pct;
    $("tn-floor-t").textContent = t(FS_KEYS[kind], { pct: pct(v), target: pct(fs.target_floor_pct) });
  }
}

export function renderTonight(s) {
  const sec = $("tonight");
  const anyLoaded = s.loaded.plan || s.loaded.preview || s.loaded.forecast;
  const allTried = (s.loaded.plan || s.errors.plan) && (s.loaded.preview || s.errors.preview || finalDecision(s));
  if (!anyLoaded && !allTried) return;
  sec.removeAttribute("aria-busy");
  renderStale(s);

  const st = tonightState(s);
  const prevDec = s.preview && s.preview.decision;
  const dec = st.state === "pending" || st.state === "none" ? (prevDec || st.fin) : (st.fin || prevDec);
  const a = applyBlock(s);

  const tgt = $("tn-target");
  tgt.textContent = dec ? pct(dec.target_pct) : "—";
  const manual = !!(dec && dec.manual);
  if (manual) {
    const badge = document.createElement("span");
    badge.className = "tn-badge";
    badge.textContent = t("tonight.manual");
    tgt.append(badge);
  }
  const auto = $("tn-auto");
  auto.hidden = !(manual && isNum(dec.auto_target_pct));
  auto.textContent = auto.hidden ? "" : t("tonight.auto", { pct: pct(dec.auto_target_pct) });
  $("tn-inv").textContent = t("tonight.inverterNow", { v: invTargets(a) });

  // статус
  const chip = $("tn-status");
  chip.className = "st-chip st-" + st.state;
  $("tn-status-ico").innerHTML = '<svg class="ico" viewBox="0 0 24 24">' + ICONS[st.state] + "</svg>";
  const stx = (s.plan || s.preview) ? statusText(st, s, dec) : { text: t("tonight.noPlan") };
  $("tn-status-t").textContent = stx.text;
  const note = $("tn-status-note");
  note.hidden = !stx.note;
  note.textContent = stx.note || "";

  renderDay(s, dec);

  // деньги — energy_24h из /api/plan/forecast
  const e = s.forecast && s.forecast.energy_24h;
  $("tn-cost").textContent = e ? money(e.cost_uah) : "—";
  $("tn-take").textContent = e && isNum(e.grid_import_kwh) ? t("tonight.money.take", { kwh: energy(e.grid_import_kwh) }) : "";

  $("tn-soc07").textContent = dec ? pct(dec.soc_at_07) : "—";
  $("tn-min").textContent = dec ? pct(dec.min_soc_tomorrow_worst) : "—";

  // детали
  const det = $("tn-details");
  det.hidden = !dec;
  $("tn-expl").textContent = (dec && dec.explanation) || "";
  $("tn-expl").hidden = !(dec && dec.explanation);
  renderBars(dec);
  const sav = $("tn-saving");
  sav.hidden = !(dec && isNum(dec.expected_saving_uah));
  sav.textContent = dec && isNum(dec.expected_saving_uah) ? t("tonight.saving", { v: money(dec.expected_saving_uah) }) : "";

  // предупреждения: plan/preview + forecast, без дублей
  const warns = uniq([...((dec && dec.warnings) || []), ...((s.forecast && s.forecast.warnings) || [])]);
  const wbox = $("tn-warns");
  wbox.hidden = warns.length === 0;
  $("tn-warns-s").textContent = tp("tonight.warn", warns.length);
  $("tn-warns-list").replaceChildren(...warns.map((w) => { const li = document.createElement("li"); li.textContent = w; return li; }));
}

