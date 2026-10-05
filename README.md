# Syrve ↔ Ontopo connector

Сервис-коннектор между кассовой системой **Syrve** (Syrve Cloud API) и платформой бронирования **Ontopo**
(по спецификации «Ontopo POS API Integration Specification»).

| Направление | Что передаётся | Как |
|---|---|---|
| Ontopo → Syrve | **Бронирования**: создание, перенос, отмена, no-show | Вебхук Ontopo → `POST /webhooks/ontopo` (+ страховочный опрос `/pos/reservationsForToday`) → резерв на стол в Syrve |
| Syrve → Ontopo | **Занятость столов**: гости сели / оплатили, гости без брони (walk-in), назначенный стол | Заказы на столах Syrve → `POST /pos/setReservation` |

В API Ontopo нет отдельной публикации «свободных столов»: Ontopo считает свободные столы сам, по своим броням и
по заказам, которые передаёт POS. Поэтому коннектор отдаёт в Ontopo всё, что делает стол занятым или свободным:
- открыт заказ на столе с бронью → бронь получает статус `seated`;
- заказ оплачен → статус `done`, Ontopo освобождает стол;
- заказ на столе без брони (гости с улицы) → в Ontopo создаётся запись от POS, стол виден занятым;
- Syrve назначил стол под бронь → номер стола уходит в Ontopo.

```
                 ┌─────────────────────────── connector ───────────────────────────┐
  Ontopo webhook │ inbox (SQLite) → ReservationSync: подбор стола,                 │ reserve/create
  SyncData  ────►│ reserve/create | reserve/cancel, перенос = новый резерв + отмена├──────────────► Syrve
                 │                                                                 │
  /pos/set-      │ OrderSync (каждые 30 c): order/by_table → seated / done /       │ order/by_table
  Reservation ◄──┤ walk-in + данные заказа (стадия, сумма, позиции)                │◄────────────── Syrve
                 └─────────────────────────────────────────────────────────────────┘
```

## Как это работает

### Ontopo → Syrve
1. Ontopo присылает `SyncData` на `POST /webhooks/ontopo`. Коннектор сохраняет запись в SQLite и сразу отвечает
   `200` (так требует спецификация). Дальше запись обрабатывает фоновый обработчик.
2. Ключ связи — `syncId`. Обработка идемпотентна: повторы и «эхо» собственных обновлений ничего не меняют.

| `reservation.status` | Действие в Syrve |
|---|---|
| `approved`, `seated` | создать резерв; при изменении даты, времени, длительности, числа гостей, стола, гостя или пожеланий — новый резерв, затем отмена старого |
| `canceled` | отменить резерв (`ClientRefused`) |
| `noShow` | отменить резерв (`ClientNotAppeared`) |
| `standby` (лист ожидания) | снять резерв со стола (`Other`) |
| `done` | ничего: резерв остаётся в истории |

3. Стол выбирается так: сначала стол из `reservation.table` (через `table_map`, иначе по номеру, затем по названию
   стола Syrve). Если такого стола нет или он занят — самый маленький свободный стол, на котором поместятся гости.
   Объединение нескольких столов включается отдельно.
4. Если стол выбрал Syrve, коннектор сообщает его в Ontopo (`setReservation` с тем же `syncId`).
5. Сетевые ошибки и ошибки 5xx повторяются с растущей паузой (`max_attempts`). После этого событие видно
   в `/admin/events/failed` и перезапускается через `/admin/events/retry`.
6. Каждые 5 минут коннектор перечитывает `/pos/reservationsForToday` — на случай потерянного вебхука.

### Syrve → Ontopo
Раз в `orders.sync_interval_seconds` коннектор читает заказы на столах (`/api/1/order/by_table`) за последние 24 часа:
- Заказ открыт на столе брони Ontopo в окне от «время брони − `early_arrival_minutes`» до её конца →
  `setReservation` с `syncId` этой брони, `status: seated` и данными заказа.
- Другие заказы (walk-in) создаются в Ontopo как записи от POS: новый `syncId` = `syrve-<id заказа>`, заказ и пустой
  `reservation: {}`, как требует спецификация. Отключается параметром `create_walk_ins: false`.
- Изменения заказа (стадия, сумма, позиции, столы) отправляются только при реальных изменениях.
- Заказ закрыт → `stage: paid`, `status: done`. Удалённый walk-in-заказ → `status: canceled`.

| Статус заказа Syrve | `order.stage` |
|---|---|
| `New` (есть позиции) | `ordered` |
| `Bill` | `check` |
| `Closed` | `paid` |

Поле `order.tables` заполняется номерами столов Syrve. `netTotal` — это `sum` заказа, `discountAmount` — сумма скидок,
`dinnersCount` — число гостей.

## Быстрый старт

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp config.example.yaml config.yaml
cp .env.example .env            # заполнить, затем: set -a; . ./.env; set +a

python -m connector discover       # организации, терминальные группы, залы и столы Syrve
python -m connector ontopo-today   # брони Ontopo на сегодня (проверка API-ключа)
python -m connector serve          # сервис на :8080
```

Docker: `docker compose up -d --build`. Контейнер берёт `config.yaml` и `.env` из текущей папки, база хранится в `./data`.

Тесты: `pytest`.

### Что настроить
**Syrve**
1. Создайте API-логин Syrve Cloud API с доступом к организации → `SYRVE_API_LOGIN`.
2. На терминальной группе должны быть включены резервы, у столов должна быть задана вместимость.
3. Команда `python -m connector discover` покажет `organization_id` и `terminal_group_id`.

**Ontopo**
1. Получите у Ontopo API-ключ заведения → `ONTOPO_API_KEY` (передаётся в заголовке `x-api-key`).
2. Сейчас используется staging: `https://top-openapi.staging-01.ontopo.cz`. Для продакшена укажите `ONTOPO_BASE_URL`
   (в спецификации адрес пока не указан).
3. Передайте Ontopo адрес вебхука: `https://<ваш-хост>/webhooks/ontopo?token=<ONTOPO_WEBHOOK_TOKEN>`.
   Спецификация не описывает подпись вебхуков, поэтому защита сделана секретным токеном в URL
   (также принимается заголовок `x-api-key`).
4. Номера столов в Ontopo должны совпадать с номерами столов Syrve. Если не совпадают, задайте
   `reservations.table_map`.

## HTTP API коннектора

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/health` | проверка живости |
| POST | `/webhooks/ontopo` | приём `SyncData` от Ontopo |
| GET | `/status` | последняя синхронизация заказов и опроса, ошибки, очередь |
| GET | `/admin/links` | связи `syncId` ↔ резерв/заказ Syrve |
| GET | `/admin/events/failed` | события, которые не удалось обработать |
| POST | `/admin/events/retry` | перезапустить их |
| POST | `/admin/sync/orders` | отправить заказы в Ontopo сейчас |
| POST | `/admin/sync/reservations` | перечитать брони Ontopo сейчас |

Если задан `server.admin_token`, для `/status` и `/admin/*` нужен заголовок `X-Admin-Token`.

## Ограничения и открытые вопросы к Ontopo
- **Брони, созданные в самом Syrve** (по телефону), в Ontopo не передаются: по спецификации POS может создать запись
  только вместе с заказом. Стол станет занятым в Ontopo, когда гости сядут и откроется заказ. Если Ontopo
  разрешит создавать брони без заказа, это добавляется в `connector/orders.py`.
- В спецификации не описан объект `patron`. Коннектор понимает `firstName`/`lastName`/`name`, `phone`, `email`.
- Перенос брони в Syrve сделан как «новый резерв → отмена старого», потому что используются только
  `reserve/create`, `reserve/cancel` и `reserve/status_by_id`.
- Если терминал Syrve офлайн, резерв остаётся в статусе `InProgress`. Syrve доставит его позже.
- Телефоны приводятся к международному формату; номера, начинающиеся с `0`, считаются израильскими (`+972`).
