"""Воркер очереди записи TOU (inverter_write_requests). Вызывается из Collector.run — тот же владелец соединения."""

import asyncio
import logging
import time

log = logging.getLogger("deya.writer")

BACKUP_START, BACKUP_COUNT = 146, 32  # 146–177
START_REGS = range(148, 154)
POWER_REGS = range(154, 160)
SOC_REGS = range(166, 172)
SOURCE_REGS = range(172, 178)
WHITELIST = frozenset([*START_REGS, *POWER_REGS, *SOC_REGS, *SOURCE_REGS])
MAX_POWER_W = 12000
MIN_SOC, MAX_SOC = 5, 100
SOURCES = (0, 1, 2, 3)  # нет / сеть / генератор / оба
WRITE_PAUSE = 0.5
STUCK_RUNNING_MIN = 10
PENDING_TTL_MIN = 15
PLANNER_SOURCE = "planner"
PLANNER_SOC_MIN, PLANNER_SOC_MAX = 0, 100  # заявки планировщика: только SOC-регистры 166–171
AUTO_OFF_ERROR = "автозапись выключена"


def _valid_hhmm(value: int) -> bool:
    h, m = divmod(value, 100)
    return 0 <= h <= 23 and 0 <= m <= 59


def validate(changes: dict[int, int], live: dict[int, int]) -> list[str]:
    """Повтор проверок админки: whitelist, диапазоны изменяемых значений, возрастание времён по итоговому набору."""
    errors = []
    for reg, value in sorted(changes.items()):
        if reg not in WHITELIST:
            errors.append(f"рег {reg}: запись запрещена (вне 148–159, 166–177)")
        elif type(value) is not int:
            errors.append(f"рег {reg}: значение {value!r} не целое")
        elif reg in START_REGS and not _valid_hhmm(value):
            errors.append(f"рег {reg}: время {value} вне 00:00–23:59")
        elif reg in POWER_REGS and not 0 <= value <= MAX_POWER_W:
            errors.append(f"рег {reg}: мощность {value} вне 0–{MAX_POWER_W}")
        elif reg in SOC_REGS and not MIN_SOC <= value <= MAX_SOC:
            errors.append(f"рег {reg}: SOC {value} вне {MIN_SOC}–{MAX_SOC}")
        elif reg in SOURCE_REGS and value not in SOURCES:
            errors.append(f"рег {reg}: источник {value} не 0–3")
    if errors:
        return errors
    times = [changes.get(r, live.get(r)) for r in START_REGS]
    if any(t is None for t in times):
        errors.append("нет живых значений времени слотов 148–153")
    elif any(b <= a for a, b in zip(times, times[1:])):
        errors.append(f"времена слотов не возрастают: {times}")
    return errors


def write_order(changes: dict[int, int], live: dict[int, int]) -> list[int]:
    """Сначала не-временные регистры, затем времена: уменьшающиеся по возрастанию адреса,
    увеличивающиеся по убыванию — набор времён остаётся возрастающим после каждой записи."""
    others = sorted(r for r in changes if r not in START_REGS)
    times = [r for r in changes if r in START_REGS]
    down = sorted(r for r in times if changes[r] < live[r])
    up = sorted((r for r in times if changes[r] >= live[r]), reverse=True)
    return others + down + up


def _mismatches(regs: dict[int, int], changes: dict[int, int], backup: dict[int, int]) -> list[str]:
    """Сверка только регистров из WHITELIST (146/147/160–165 не пишем и не сверяем)."""
    result = []
    for reg in sorted(WHITELIST):
        want = changes.get(reg, backup.get(reg))
        got = regs.get(reg)
        if got != want:
            result.append(f"рег {reg}: ожидалось {want}, прочитано {got}")
    return result


def validate_planner(changes: dict[int, int]) -> list[str]:
    """Доп. ограничения заявок планировщика: только SOC 166–171, значение — целое 0–100."""
    errors = []
    for reg, value in sorted(changes.items()):
        if reg not in SOC_REGS:
            errors.append(f"рег {reg}: планировщику разрешены только SOC 166–171")
        elif type(value) is not int or not PLANNER_SOC_MIN <= value <= PLANNER_SOC_MAX:
            errors.append(f"рег {reg}: SOC {value!r} вне {PLANNER_SOC_MIN}–{PLANNER_SOC_MAX}")
    return errors


def planner_allowed(db) -> bool:
    """Автозапись разрешена сейчас: control_mode == 'auto' и writes_enabled."""
    st = db.strategy_settings()
    return bool(st) and st["control_mode"] == "auto" and bool(st["writes_enabled"])


def execute(client, db, request: dict, sleep=time.sleep) -> str:
    """Выполнить один захваченный запрос (синхронно, в потоке). Возвращает итоговый статус."""
    rid = request["id"]
    plan: list[int] | None = None
    written: dict[int, int] = {}
    backup: dict[int, int] = {}
    try:
        changes = {int(k): v for k, v in request["changes"].items()}
        expected = {int(k): v for k, v in request["expected_old"].items()}
        if request.get("source") == PLANNER_SOURCE:
            # Проверки до любого обращения к инвертору.
            if not planner_allowed(db):
                log.warning("запрос %d (планировщик): отменён — %s", rid, AUTO_OFF_ERROR)
                db.finish_write_request(rid, "cancelled", None, AUTO_OFF_ERROR)
                return "cancelled"
            errors = validate_planner(changes)
            if errors:
                log.warning("запрос %d (планировщик): отказ проверки: %s", rid, "; ".join(errors))
                db.finish_write_request(rid, "failed", None, "; ".join(errors))
                return "failed"
        backup = client.read_block_sync(BACKUP_START, BACKUP_COUNT)
        db.save_write_backup(rid, backup)
        log.info("запрос %d: копия 146–177 сохранена", rid)

        errors = validate(changes, backup)
        if not changes:
            errors.append("нет изменений")
        if errors:
            log.warning("запрос %d: отказ проверки: %s", rid, "; ".join(errors))
            db.finish_write_request(rid, "failed", None, "; ".join(errors))
            return "failed"

        stale = [
            f"рег {reg}: ожидалось {expected.get(reg)}, сейчас {backup.get(reg)}"
            for reg in sorted(changes) if backup.get(reg) != expected.get(reg)
        ]
        if stale:
            log.warning("запрос %d: устарел: %s", rid, "; ".join(stale))
            db.finish_write_request(rid, "stale", None, "; ".join(stale))
            return "stale"

        plan = write_order(changes, backup)
        for reg in plan:
            value = changes[reg]
            log.info("запрос %d: рег %d: %s → %s", rid, reg, backup.get(reg), value)
            client.write_register_sync(reg, value)
            written[reg] = value
            sleep(WRITE_PAUSE)

        readback = client.read_block_sync(BACKUP_START, BACKUP_COUNT)
        bad = _mismatches(readback, changes, backup)
        if bad:
            log.error("запрос %d: чтение обратно не совпало: %s", rid, "; ".join(bad))
            db.finish_write_request(rid, "failed", readback, "чтение обратно не совпало: " + "; ".join(bad))
            return "failed"
        log.info("запрос %d: записано и проверено (%d рег.)", rid, len(changes))
        db.finish_write_request(rid, "done", readback, None)
        return "done"
    except Exception as e:
        log.error("запрос %d: ошибка: %r", rid, e)
        readback = None
        try:
            readback = client.read_block_sync(BACKUP_START, BACKUP_COUNT)
        except Exception as e2:
            log.error("запрос %d: чтение обратно не удалось: %r", rid, e2)
        error = repr(e)
        if plan is not None:
            done_regs = ", ".join(str(r) for r in plan if r in written) or "-"
            left_regs = ", ".join(str(r) for r in plan if r not in written) or "-"
            error += f"; записаны: {done_regs}; не записаны: {left_regs}"
            if readback is not None:
                bad = _mismatches(readback, written, backup)
                if bad:
                    error += "; несовпадения readback: " + "; ".join(bad)
        db.finish_write_request(rid, "failed", readback, error)
        return "failed"


async def process_queue(client, db) -> bool:
    """Снять просроченные запросы, взять из очереди один pending и выполнить; True, если запрос был."""
    stuck = await asyncio.to_thread(db.fail_stuck_writes, STUCK_RUNNING_MIN)
    expired = await asyncio.to_thread(db.expire_pending_writes, PENDING_TTL_MIN)
    if stuck or expired:
        log.warning("зависших запросов: %d, устаревших pending: %d", stuck, expired)
    request = await asyncio.to_thread(db.claim_write_request)
    if request is None:
        return False
    log.info("запрос %d захвачен: %s", request["id"], request["changes"])
    await asyncio.to_thread(execute, client, db, request)
    return True
