# Syrve ↔ Ontopo connector

Сервис-коннектор между кассовой системой **Syrve** (Syrve Cloud API) и платформой бронирования **Ontopo**.

| Направление | Что передаётся | Как |
|---|---|---|
| Syrve → Ontopo | Столы (зал, номер, вместимость) и **свободные столы по слотам времени** на N дней вперёд | Периодически (по умолчанию раз в 2 мин) и сразу после каждого изменения брони; отправляется только при изменениях |
| Ontopo → Syrve | **Бронирования**: создание, изменение, отмена | Вебхук `POST /webhooks/ontopo` (+ опционально опрос API Ontopo); бронь создаётся в Syrve как резерв на конкретный стол |

```
             ┌──────────────────── connector ────────────────────┐
 Syrve Cloud │  AvailabilitySync: столы + резервы → свободные    │  PUT availability
   API  ◄────┤  слоты (по умолчанию шаг 15 мин, длительность 2 ч)├──────────────► Ontopo
             │                                                   │
 reserve/    │  ReservationSync: inbox (SQLite) → подбор стола → │  webhook / poll
 create ◄────┤  reserve/create | reserve/cancel → статус в Ontopo│◄────────────── Ontopo
             └───────────────────────────────────────────────────┘
```

## Как это работает

### Свободные столы (Syrve → Ontopo)
1. Из Syrve берутся залы и столы терминальной группы (`reserve/available_restaurant_sections`).
2. Берутся все резервы/банкеты на горизонт `horizon_days` (`reserve/restaurant_sections_workload`).
   Опционально (`consider_open_orders: true`) столы с открытыми заказами считаются занятыми прямо сейчас (гости без брони).
3. Для каждого слота в часы работы считается, какие столы свободны на `default_duration_minutes`
   с учётом буфера на уборку `buffer_minutes`.
4. Результат (`tables` + `slots[].freeTableIds/freeSeats/maxPartySize`) отправляется в Ontopo, если он изменился.

### Бронирования (Ontopo → Syrve)
1. Вебхук проверяется (HMAC-SHA256) и **сначала сохраняется в SQLite** — ничего не теряется при падении Syrve или сети.
2. Обработчик приводит Syrve к состоянию брони в Ontopo (идемпотентно, дубли безопасны):
   * **новая** — подбирается стол: стол, указанный Ontopo (через `table_map`, номер или имя стола), иначе
     самый маленький свободный стол, вмещающий гостей (опционально — объединение столов одного зала);
     в Syrve создаётся резерв (`reserve/create`), подтверждение ждём через `reserve/status_by_id`;
   * **изменённая** (время, гости, стол, контакты) — сначала создаётся новый резерв, потом отменяется старый,
     по возможности на том же столе;
   * **отменённая / no-show** — `reserve/cancel` с причиной `ClientRefused` / `ClientNotAppeared`.
3. Результат сообщается в Ontopo (`confirmed` с назначенными столами или `rejected` с причиной).
4. Сетевые ошибки и 5xx повторяются с экспоненциальной задержкой (`max_attempts`), затем событие попадает в
   `/admin/events/failed`, откуда его можно перезапустить.
5. После каждого изменения сразу пересчитываются свободные столы.

## Быстрый старт

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp config.example.yaml config.yaml
cp .env.example .env            # заполнить, затем: set -a; . ./.env; set +a

python -m connector discover              # показать организации, терминальные группы, залы и столы Syrve
python -m connector preview-availability  # посмотреть, что уйдёт в Ontopo (ничего не отправляет)
python -m connector serve                 # запустить сервис на :8080
```

Docker: `docker compose up -d --build` (берёт `config.yaml` и `.env` из текущей папки, база в `./data`).

Тесты: `pytest`.

### Настройка Syrve
1. В Syrve Office / веб-кабинете создайте **API-логин** для Syrve Cloud API и привяжите к нему организацию → `SYRVE_API_LOGIN`.
2. Убедитесь, что на терминальной группе включены резервы/банкеты, а у столов задана вместимость.
3. `python -m connector discover` покажет `organization_id`, `terminal_group_id` и id залов.
4. Регион API: `https://api-eu.syrve.live` (EU). Для другого региона поменяйте `syrve.base_url`.

### Настройка Ontopo
У Ontopo нет публичной документации партнёрского API — доступ выдаётся по запросу партнёрам/интеграторам POS.
Коннектор реализует контракт ниже; адреса, заголовок авторизации и путь подписи настраиваются в `config.yaml`,
а все имена полей собраны в одном файле [`connector/ontopo/mapping.py`](connector/ontopo/mapping.py) —
когда Ontopo пришлёт свою спецификацию, правки нужны только там.

## Контракт с Ontopo

**Авторизация исходящих запросов:** заголовок `Authorization: Bearer <ONTOPO_API_KEY>` (настраивается).

### 1. Публикация свободных столов — `PUT {base_url}/venues/{venue_id}/availability`
```json
{
  "venueId": "venue-1",
  "source": "syrve",
  "timezone": "Asia/Jerusalem",
  "generatedAt": "2026-10-05T12:00:00+03:00",
  "slotMinutes": 15,
  "defaultDurationMinutes": 120,
  "tables": [{"id": "…", "number": 5, "name": "5", "capacity": 4, "areaId": "…", "areaName": "Терраса"}],
  "slots": [
    {"start": "2026-10-05T19:00:00+03:00", "freeTableIds": ["…"], "freeSeats": 8, "maxPartySize": 6}
  ]
}
```

### 2. Вебхук бронирования — `POST {connector}/webhooks/ontopo`
Подпись: `X-Ontopo-Signature: sha256=<hex HMAC-SHA256(тело запроса, ONTOPO_WEBHOOK_SECRET)>`.
```json
{
  "event": "reservation.created",          // .updated | .cancelled
  "reservation": {
    "id": "R-123",
    "status": "confirmed",                 // cancelled | no_show | … → отмена в Syrve
    "start": "2026-10-05T19:00:00+03:00",
    "durationMinutes": 120,                // необязательно (или "end")
    "partySize": 4,
    "guest": {"firstName": "Dana", "lastName": "Levi", "phone": "050-123-4567", "email": "…"},
    "notes": "у окна",
    "tableIds": ["A5"]                     // необязательно
  }
}
```
Принимаются и snake_case-варианты (`start_time`, `party_size`, `reservation_id`, `guests: {count}` …).
Ответ: `202 {"accepted": true, "eventId": 1}`; `401` — неверная подпись; `400` — не JSON / нет id брони.

### 3. Статус на стороне POS — `POST {base_url}/venues/{venue_id}/reservations/{reservation_id}/pos-status`
```json
{"reservationId": "R-123", "status": "confirmed", "posReservationId": "<syrve id>",
 "tables": [{"id": "…", "name": "5", "number": 5}], "message": null}
```
Отключается пустым `reservation_status_path`.

### 4. (Опционально) опрос броней — `GET {base_url}/venues/{venue_id}/reservations?updatedSince=<ISO>`
Включается `poll_reservations: true` — страховка на случай потерянных вебхуков.
Ответ — массив броней или `{"reservations": [...]}`.

## HTTP API коннектора

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/health` | проверка живости |
| POST | `/webhooks/ontopo` | приём броней из Ontopo |
| GET | `/status` | время последней публикации, ошибки, счётчики очереди |
| GET | `/admin/availability/preview` | текущий расчёт свободных столов |
| POST | `/admin/sync/availability` | принудительно отправить свободные столы |
| GET | `/admin/reservations` | соответствие броней Ontopo ↔ резервов Syrve |
| GET | `/admin/events/failed` | события, которые не удалось обработать |
| POST | `/admin/events/retry` | перезапустить их |

`/status` и `/admin/*` требуют заголовок `X-Admin-Token`, если задан `server.admin_token`.

## Настройки (`config.yaml`)

| Ключ | По умолчанию | Описание |
|---|---|---|
| `timezone` | `Asia/Jerusalem` | часовой пояс ресторана (Syrve хранит локальное время) |
| `syrve.restaurant_section_ids` | `[]` | какие залы публиковать (пусто — все) |
| `syrve.consider_open_orders` | `false` | считать занятыми столы с открытыми заказами |
| `availability.horizon_days` | `14` | на сколько дней вперёд публиковать |
| `availability.slot_minutes` | `15` | шаг слотов |
| `availability.default_duration_minutes` | `120` | на сколько занимается стол при посадке |
| `availability.buffer_minutes` | `15` | буфер между бронями |
| `availability.last_seating_offset_minutes` | `60` | последняя посадка за N минут до закрытия |
| `availability.opening_hours` | 12:00–23:00 | часы по дням (`default`, `mon`…`sun`, `null` — выходной; время в кавычках) |
| `reservations.table_map` | `{}` | id стола Ontopo → id стола Syrve |
| `reservations.auto_assign_tables` | `true` | подбирать стол, если Ontopo его не указал или он занят |
| `reservations.allow_table_combining` | `false` | объединять столы одного зала для больших компаний |

## Ограничения и допущения
* Изменение брони реализовано как «создать новый резерв → отменить старый» — используются только
  стабильные методы Syrve Cloud API (`reserve/create`, `reserve/cancel`, `reserve/status_by_id`).
* Если терминал Syrve офлайн, резерв остаётся в статусе `InProgress` и будет доставлен Syrve позже;
  коннектор считает его созданным.
* Телефоны приводятся к международному формату; номера, начинающиеся с `0`, считаются израильскими (`+972`).
* Брони, созданные в самом Syrve (по телефону, в зале), автоматически попадают в публикуемую занятость.
