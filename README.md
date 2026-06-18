# browser-as-a-service

Командно-управляемый сервис веб-скрейпинга поверх [zendriver](https://github.com/cdpdriver/zendriver)
(поддерживаемый форк nodriver — undetected Chrome через CDP, наследник
undetected-chromedriver) + автогенерируемый типобезопасный **zod**-клиент для Node.js.

Цель: пробиваться через антибот-защиту Ozon и доставать данные, которых нет в
официальном API (например, текущие цены на товары), дёргая всё это снаружи по HTTP.

```
┌──────────────┐  HTTP (JSON)   ┌────────────────────────────┐
│ Node.js app  │ ─────────────► │ FastAPI service (Python)   │
│ zod-клиент   │ ◄───────────── │  zendriver → реальный Chrome│
└──────────────┘   результат    └────────────────────────────┘
        ▲                                   │
        │  npm run generate ◄─ openapi.json ┘  (контракт)
```

## Структура

| Путь | Что это |
|------|---------|
| [`service/`](./service) | FastAPI-сервис: движок сценариев, прокси, троттлинг, кэш |
| [`client/`](./client) | сгенерированный из OpenAPI typesafe zod-клиент + обёртка |
| `service/smoke.py` | smoke-тест: проверка, что Chrome пробивает Ozon |

## Быстрый старт

### 1. Сервис (Python)

```bash
python3 -m venv .venv
.venv/bin/pip install -r service/requirements.txt
cd service
../.venv/bin/python -m uvicorn app.main:app --port 8077
```

Проверка:
```bash
curl localhost:8077/health
curl -X POST localhost:8077/ozon/price -H 'content-type: application/json' \
  -d '{"url":"https://www.ozon.ru/product/...."}'
```
Swagger UI: <http://localhost:8077/docs>

**Админ-панель** поднимается вместе с сервисом (отдельный процесс не нужен):
<http://localhost:8077/admin>. В ней «бегут» живые логи — кто (IP клиента) и что
делает (запрос, скрейп, старт браузера), статус сервиса (браузер, Chrome,
concurrency, аптайм, счётчики запросов/ошибок). Логи стримятся через SSE
(`GET /admin/events`), статус — `GET /admin/status`.

### 2. Клиент (Node.js)

```bash
cd client
npm install
npm run generate     # перегенерировать из service (см. ниже)
npm run example:price -- "https://www.ozon.ru/product/...."
```

Использование в коде:
```ts
import { createScrapeClient } from "browser-as-a-service-client";

const client = createScrapeClient("http://127.0.0.1:8077");

// удобный эндпоинт
const { price_value, card_price_value } = await client.ozonPrice({ url });

// универсальный сценарий из «команд» — полностью типизирован
const { data } = await client.run({
  start_url: url,
  steps: [
    { action: "wait_for", selector: "[data-widget=webPrice]" },
    { action: "extract", name: "price", selector: "[data-widget=webPrice]", kind: "text" },
    { action: "extract", name: "title", selector: "h1" },
  ],
});
```

## Как обновлять клиент при изменении API

Контракт — единый источник правды: меняешь pydantic-модели в
[`service/app/models.py`](./service/app/models.py) → перегенерируешь клиент.
Эндпоинты **не переписываются руками**:

```bash
cd service && ../.venv/bin/python export_openapi.py   # -> client/openapi.json
cd ../client && npm run generate                       # -> src/generated.ts (zod)
```

## Команды (actions) движка `/run`

`navigate`, `wait_for`, `wait_for_text`, `sleep`, `click`, `scroll`,
`extract` (text/html/attr, в т.ч. `many`), `find_text`, `eval` (любой JS),
`screenshot`. Каждая — отдельная типизированная модель, объединённая в
discriminated union по полю `action`. Добавить новую команду = добавить модель
и ветку в [`engine.py`](./service/app/engine.py).

## Защита Ozon, прокси и бан по IP

Главные выводы (проверено на живом Ozon):

- **Headless палится.** Ozon отдаёт заглушку «Похоже, нет соединения» / `Antibot
  Captcha`. Нужен **headful** Chrome (обычное окно). На Linux-сервере это значит
  виртуальный дисплей — **Xvfb** (`xvfb-run -a python -m uvicorn ...`).
- **Холодный заход на карточку палится.** Поэтому сервис при старте браузера
  один раз заходит на главную (`WARMUP_URL`), получает антибот-куки сессии, и
  только потом ходит на товары. Это снимает капчу.
- **Бан по IP — самое слабое место, и zendriver его НЕ решает** (это просто
  контроллер браузера). Стратегия:
  1. **Троттлинг + кэш** (заложено: `MIN_INTERVAL_S`, `JITTER_S`,
     `PRICE_CACHE_TTL_S`). Для мониторинга своих цен редких заходов часто хватает
     и со своего IP.
  2. **Резидентные/мобильные прокси с ротацией** при масштабе. Датацентровые IP
     Ozon режет мгновенно — не тратить время.
  3. Прокси с логином/паролем поддержаны через CDP `Fetch.continueWithAuth`
     (креды не нужно зашивать в URL) — просто передай `proxy` в запросе.

```jsonc
// пример запроса с прокси
{ "url": "https://www.ozon.ru/product/...",
  "proxy": { "server": "http://1.2.3.4:8080", "username": "user", "password": "pass" } }
```

### Asocks (резидентные прокси по требованию)

Вместо ручного `proxy` можно попросить сервис сам взять резидентный прокси у
[Asocks](https://asocks.com) (`https://api.asocks.com/v2`). Включается **по
запросу** — решает тот, кто его делает:

```jsonc
{ "url": "https://www.ozon.ru/product/...",
  "use_proxy": true,
  "proxy_country": "RU" }   // ISO-код страны; без него берётся любой доступный порт
```

Как это работает:
- ключ берётся из `ASOCKS_API_KEY` (в корневом `.env`, см. `.env.example`);
- сервис переиспользует уже существующий активный порт нужной страны, а если
  такого нет — создаёт его (`POST proxy/create-port`) и ждёт готовности;
- результат кешируется на `ASOCKS_POOL_TTL_S` секунд, чтобы всплеск запросов не
  плодил порты и не молотил API;
- порты Asocks — это **SOCKS5 с логином/паролем**, а Chrome не умеет
  авторизацию SOCKS5. Поэтому сервис поднимает локальный мост
  ([`socks_bridge.py`](./service/app/socks_bridge.py)): SOCKS5 без авторизации на
  `127.0.0.1`, который форвардит наверх в Asocks с кредами. Chrome ходит в
  `socks5://127.0.0.1:<порт>` — креды нигде не светятся;
- явный `proxy` в запросе имеет приоритет над `use_proxy`.

Диагностика — `GET /admin/asocks`: показывает баланс аккаунта и число портов.
**Важно:** создание порта тратит денежный баланс (`balance`), а не только трафик
(`balance_traffic`). При нулевом балансе API вернёт `Insufficient funds`, и
сервис отдаст `ok:false` с этим сообщением (Chrome при этом не запускается).
В таком случае пополни баланс Asocks или создай порт вручную в дашборде — сервис
подхватит его через `proxy/ports` без создания.

## Регион витрины (цена и наличие)

Цена, наличие и ранжирование на маркетплейсах персонализируются по региону.
Чтобы снять цену «как видит покупатель в нужном городе», регион закрепляют до
первого захода:

- проще всего — query-параметром в `start_url` там, где он работает (Я.Маркет:
  `?lr=213` — Москва);
- универсально — куки через поле `cookies` запроса (`/run` и `/ozon/price`).
  Куки ставятся ДО первой навигации; домен по умолчанию берётся из `start_url`
  (регистрируемый, напр. `.ozon.ru`), так что переписывать host на каждой куке
  не нужно.

```jsonc
{ "start_url": "https://www.ozon.ru/product/...",
  "cookies": [ { "name": "<region-cookie>", "value": "<moscow>" } ],
  "steps": [ /* ... */ ] }
```

## Переменные окружения сервиса

| Переменная | По умолчанию | Назначение |
|-----------|--------------|-----------|
| `CHROME_PATH` | автоопределение | путь к Chrome (избегает битого homebrew chromium) |
| `MAX_CONCURRENCY` | `1` | одновременных операций браузера |
| `MIN_INTERVAL_S` / `JITTER_S` | `2.0` / `1.5` | пауза между заходами (анти-бан) |
| `PRICE_CACHE_TTL_S` | `900` | TTL кэша цен Ozon |
| `WARMUP_URL` | `https://www.ozon.ru/` | прогрев сессии при старте браузера |
| `HEADLESS` | `0` | `1` включит headless (Ozon заблокирует) |
| `RUN_TIMEOUT_S` | `90` | жёсткий таймаут одного запуска |
| `ASOCKS_API_KEY` | — | ключ Asocks; включает `use_proxy` в запросах |
| `ASOCKS_POOL_TTL_S` | `300` | сколько переиспользовать выданный порт |
| `ASOCKS_BASE_URL` | `https://api.asocks.com/v2` | база API Asocks |
| `ASOCKS_TIMEOUT_S` | `30` | таймаут HTTP-вызова к API Asocks |
| `ASOCKS_TYPE_ID` / `ASOCKS_PROXY_TYPE_ID` / `ASOCKS_SERVER_PORT_TYPE_ID` | `1` / `2` / `1` | поля `create-port` (по умолчанию — SOCKS5-порт с авторизацией) |

## Замечания по zendriver

- Используется `zendriver` (PyPI), а не оригинальный `nodriver`: версия
  `nodriver==0.50.3` содержит «голый» байт `±` без объявления кодировки в
  `cdp/network.py` и не импортируется на современном Python. `zendriver` —
  поддерживаемый форк с уже вмёрженными багфиксами и тем же API.
- nodriver/zendriver сами выбирали `/opt/homebrew/bin/chromium`, который на этой
  машине не стартует. Сервис явно указывает на Google Chrome (`CHROME_PATH`).
