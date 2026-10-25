from datetime import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

from .inverter_modes import decode_mode, decode_state, slot_at
from .models import Sample, SettingsSnapshot, StrategySettings, TariffPlan

KYIV = ZoneInfo("Europe/Kyiv")


def _local(ts):
    return datetime.fromtimestamp(ts, KYIV).strftime("%Y-%m-%d %H:%M:%S") if ts else None


def current_tariff_zone(now=None):
    now = (now or timezone.now()).astimezone(KYIV)
    plan = (
        TariffPlan.objects.filter(is_active=True, valid_from__lte=now.date())
        .exclude(valid_to__lt=now.date())
        .order_by("-valid_from")
        .first()
    )
    if plan is None:
        return None
    t = now.time()
    for z in plan.zones.filter(direction="import"):
        if z.start_time < z.end_time:
            hit = z.start_time <= t < z.end_time
        else:
            hit = t >= z.start_time or t < z.end_time
        if hit:
            return {"plan": plan.name, "zone": str(z), "price": z.price_per_kwh, "currency": plan.currency}
    return None


def build_overview(now=None):
    snapshot = SettingsSnapshot.objects.order_by("-id").first()
    mode = decode_mode(snapshot.regs) if snapshot else None
    current_slot = None
    if mode:
        local = (now or timezone.now()).astimezone(KYIV)
        current_slot = slot_at(mode["tou"]["slots"], local.hour * 100 + local.minute)
    sample = Sample.objects.order_by("-ts").first()
    strategy = StrategySettings.load()
    return {
        "mode": mode,
        "current_slot": current_slot,
        "snapshot_time": _local(snapshot.ts) if snapshot else None,
        "sample": sample,
        "sample_time": _local(sample.ts) if sample else None,
        "state_label": decode_state(sample.state) if sample and sample.state is not None else None,
        "strategy": strategy,
        "tariff": current_tariff_zone(now),
    }


def dashboard_callback(request, context):
    context["overview"] = build_overview()
    return context
