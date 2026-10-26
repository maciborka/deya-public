# Changelog

[English](#english) · [Українська](#українська)

## English

### 2026-10-26

**Fixes**
- Planner auto-write could wait forever with "settings snapshot older than the completed write request". Snapshot timestamps are whole seconds and snapshots are stored only on change, so a readback taken in the same second as the request's `finished_at` was treated as stale indefinitely and nightly targets were silently skipped. A snapshot now counts as fresh if it was taken after the request finished or already contains the written register values. The same fix applies to floor-sync.

**Features**
- Cloudy-day rule: new strategy setting `cloudy_full_charge_kwh` (admin, migration 0018, default 12 kWh, empty/0 = off). If tomorrow's expected PV (07:00–23:00, normalised for forecast coverage) is below the threshold, the night grid-charge target is raised to the charge ceiling. Not applied when night charging has no margin or SOC at 23:00 is already at the ceiling; a manual night target still wins. Decisions store `pv_p50_day_kwh`, `cloudy_full`, `cloudy_pre_rule_target_pct` for tuning. Rationale: on cloudy forecasts actual PV came in at 31–64 % of the expected value, the battery hit the floor and day-tariff energy was bought.
- `GET /api/pv/hourly?date=`: hourly actual PV vs the forecast the planner used (as of decision time the evening before): expected value, low/high range, per provider, totals and actual/expected %.
- Dashboard card "Sun: forecast vs actual" with a yesterday/today/tomorrow toggle and plain-language labels (expected / could be from … to …).

## Українська

### 2026-10-26

**Виправлення**
- Автозапис планувальника міг чекати вічно з повідомленням «снімок налаштувань старший за виконану заявку». Час снімка зберігається в цілих секундах, а новий снімок пишеться лише коли регістри змінюються. Тому зчитування, зроблене в ту ж секунду, що й завершення заявки, назавжди вважалося застарілим, і нічна ціль заряду мовчки не записувалася. Тепер снімок вважається свіжим, якщо його знято після завершення заявки або він уже містить записані значення. Те саме виправлено для синхронізації порогів розряду (floor-sync).

**Нове**
- Правило похмурого дня: нове налаштування `cloudy_full_charge_kwh` (адмінка, міграція 0018, за замовчуванням 12 кВт·год, порожнє/0 — вимкнено). Якщо очікуване сонце на завтра (07:00–23:00, з поправкою на неповний прогноз) менше порогу, вночі АКБ заряджається від мережі до стелі заряду. Не застосовується, якщо нічний заряд невигідний або на 23:00 АКБ уже на стелі; ручна нічна ціль має пріоритет. У рішеннях зберігаються `pv_p50_day_kwh`, `cloudy_full`, `cloudy_pre_rule_target_pct` для підбору порогу. Чому: у похмурі дні сонця приходило лише 31–64 % від очікуваного, АКБ доходила до мінімуму й доводилося купувати електрику за денним тарифом.
- `GET /api/pv/hourly?date=`: сонце по годинах — факт проти прогнозу, за яким планувальник приймав рішення (станом на час рішення напередодні ввечері): очікуване значення, діапазон від гіршого до кращого, окремо за кожним сервісом прогнозу, підсумки й % від очікуваного.
- Картка дашборда «Сонце: прогноз і факт» з перемикачем учора/сьогодні/завтра і зрозумілими підписами (очікували / могло бути від … до …).
