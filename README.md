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

## Замечания по zendriver

- Используется `zendriver` (PyPI), а не оригинальный `nodriver`: версия
  `nodriver==0.50.3` содержит «голый» байт `±` без объявления кодировки в
  `cdp/network.py` и не импортируется на современном Python. `zendriver` —
  поддерживаемый форк с уже вмёрженными багфиксами и тем же API.
- nodriver/zendriver сами выбирали `/opt/homebrew/bin/chromium`, который на этой
  машине не стартует. Сервис явно указывает на Google Chrome (`CHROME_PATH`).
