#!/usr/bin/env python3
"""Read-only зонд Deye через WiFi-логгер Solarman LSW3 (протокол V5, TCP 8899).
НИЧЕГО НЕ ПИШЕТ в инвертор. Запуск на машине в той же LAN:
    python3 -m pip install -r tools/requirements.txt
    python3 tools/probe.py            # IP/серийник из DEYA_LOGGER_IP/DEYA_LOGGER_SERIAL или --ip/--serial
Сохраняет сырые регистры в data/probe_<время>.json и печатает расшифровку.
"""
import argparse, json, os, sys, time
from datetime import datetime
from pathlib import Path

try:
    from pysolarmanv5 import PySolarmanV5
except ImportError:
    sys.exit("Нет pysolarmanv5: python3 -m pip install pysolarmanv5")

BLOCKS = [(0, 22), (60, 3), (98, 40), (138, 40), (178, 60), (500, 60), (560, 60), (620, 64)]

def s16(v): return v - 0x10000 if v >= 0x8000 else v
def hhmm(v): return f"{v // 100:02d}:{v % 100:02d}"
def u32le(lo, hi): return (hi or 0) << 16 | (lo or 0)  # Deye: младшее слово первым (rule 3/4 в ha-solarman)
def temp(v): return round(((v or 1000) - 1000) * 0.1, 1)
MODELS = {0x0005: "LV 3-Phase Hybrid", 0x0500: "LV 3-Phase Hybrid", 0x0006: "HV 3-Phase Hybrid",
          0x0007: "HV 3-Phase 6-15kW", 0x0600: "HV 3-Phase 6-15kW", 0x0003: "1-Phase Hybrid", 0x0300: "1-Phase Hybrid"}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default=os.environ.get("DEYA_LOGGER_IP"), required="DEYA_LOGGER_IP" not in os.environ)
    ap.add_argument("--serial", type=int, default=os.environ.get("DEYA_LOGGER_SERIAL"),
                    required="DEYA_LOGGER_SERIAL" not in os.environ)
    ap.add_argument("--port", type=int, default=8899)
    a = ap.parse_args()

    m = PySolarmanV5(a.ip, a.serial, port=a.port, mb_slave_id=1, socket_timeout=10, verbose=False)
    regs, errors = {}, []
    for start, n in BLOCKS:
        for attempt in range(3):
            try:
                vals = m.read_holding_registers(register_addr=start, quantity=n)
                regs.update({start + i: v for i, v in enumerate(vals)})
                break
            except Exception as e:
                if attempt == 2: errors.append(f"{start}+{n}: {e!r}")
                time.sleep(1.5)
        time.sleep(0.5)
    m.disconnect()

    r = lambda k: regs.get(k)
    out = Path(__file__).resolve().parent.parent / "data" / f"probe_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"ts": datetime.now().isoformat(), "regs": regs, "errors": errors}, indent=1))

    print(f"Прочитано регистров: {len(regs)}, ошибок блоков: {len(errors)}")
    for e in errors: print("  ERR", e)
    if not regs: sys.exit(1)

    sn = "".join(chr(x >> 8) + chr(x & 0xFF) for x in (r(i) or 0 for i in range(3, 8)))
    print(f"\nТип устройства (рег0): {hex(r(0) or 0)} = {MODELS.get(r(0), '?')}   SN: {sn!r}   Ном. мощность: {u32le(r(20), r(21)) * 0.1:.0f} Вт")
    print(f"Ёмкость АКБ (рег102): {r(102)} А·ч   Заряд/разряд АКБ всего: {u32le(r(516), r(517))*0.1:.1f}/{u32le(r(518), r(519))*0.1:.1f} кВт·ч")
    print("\n— Сейчас —")
    print(f"Состояние: {r(500)}  SOC: {r(588)} %  U АКБ: {(r(587) or 0)*0.01:.2f} В  P АКБ: {s16(r(590) or 0)} Вт (+разряд/−заряд)  I АКБ: {s16(r(591) or 0)*0.01:.1f} А")
    print(f"Темп. DC/AC/АКБ: {temp(r(540))}/{temp(r(541))}/{temp(r(586))} °C")
    print(f"PV1..4: {[r(i) for i in range(672, 676)]} Вт   Сеть: {s16(r(625) or 0)} Вт   Нагрузка: {r(653)} Вт")
    print(f"Сеть L1-L3: {[round((r(i) or 0)*0.1, 1) for i in (598,599,600)]} В, {(r(609) or 0)*0.01:.2f} Гц")
    print("\n— Сегодня, кВт·ч —")
    print(f"ФЭС: {(r(529) or 0)*0.1:.1f}  Заряд: {(r(514) or 0)*0.1:.1f}  Разряд: {(r(515) or 0)*0.1:.1f}  "
          f"Импорт: {(r(520) or 0)*0.1:.1f}  Экспорт: {(r(521) or 0)*0.1:.1f}  Нагрузка: {(r(526) or 0)*0.1:.1f}")
    print("\n— Настройки —")
    print(f"Energy pattern(141): {r(141)}  Work mode(142): {r(142)}  Заряд от сети(130): {r(130)}  Ток от сети(128): {r(128)} А")
    print(f"Макс ток заряда/разряда(108/109): {r(108)}/{r(109)} А   SOC shutdown/restart/low(115-117): {r(115)}/{r(116)}/{r(117)}")
    print(f"TOU(146): {r(146)}  (бит0 вкл={bool((r(146) or 0)&1)})")
    for i in range(6):
        print(f"  слот {i+1}: с {hhmm(r(148+i) or 0)}  P={r(154+i)} Вт  SOC={r(166+i)} %  заряд={ {0:'нет',1:'сеть',2:'ген',3:'оба'}.get(r(172+i), r(172+i)) }")
    print(f"\nСырые данные: {out}")

if __name__ == "__main__":
    main()
