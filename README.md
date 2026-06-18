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
curl -X POST localhost:8077/run -H 'content-type: application/json' -d '{
  "start_url":"https://www.ozon.ru/product/....",
  "use_proxy":true, "proxy_country":"RU",
  "steps":[{"action":"extract","name":"price","selector":"[data-widget=webPrice]"}]
}'
```
Swagger UI: <http://localhost:8077/docs>

> **baas — generic.** Сервис даёт один рабочий примитив — `/run` (выполнить
> сценарий шагов в пробитом через антибот/прокси/фингерпринт Chrome и вернуть
> DOM). Парсинг конкретного маркетплейса, что считать «блоком» и ретраи —
> **на стороне вызывающего** (напр. ai-seller). Раньше был `/ozon/price` —
> убран как протечка Ozon-логики в инфраструктуру.

**Админ-панель** поднимается вместе с сервисом (отдельный процесс не нужен):
<http://localhost:8077/admin>. В ней «бегут» живые логи — кто (IP клиента) и что
делает (запрос, скрейп, старт браузера), статус сервиса (браузер, Chrome,
concurrency, аптайм, счётчики запросов/ошибок), а также **баланс и остаток
трафика** аккаунта Asocks (если задан `ASOCKS_API_KEY`). Логи стримятся через SSE
(`GET /admin/events`), статус — `GET /admin/status`, баланс/трафик —
`GET /admin/asocks` (кэшируется на `ASOCKS_BALANCE_TTL_S`, по умолчанию 60 c,
чтобы панель не долбила API).

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

// локально, без авторизации
const client = createScrapeClient("http://127.0.0.1:8077");

// публичный эндпоинт за HTTP basic auth (https://baas.mse.plus)
const remote = createScrapeClient("https://baas.mse.plus", {
  username: "baas",
  password: process.env.BAAS_PASSWORD!,
});

// универсальный сценарий из «команд» — полностью типизирован.
// use_proxy + proxy_country — пробить через резидентный прокси;
// rotate_proxy: true на ретрае — взять свежий exit-IP.
const { data } = await remote.run({
  start_url: url,
  use_proxy: true,
  proxy_country: "RU",
  steps: [
    { action: "wait_for", selector: "[data-widget=webPrice]" },
    { action: "extract", name: "price", selector: "[data-widget=webPrice]", kind: "text" },
    { action: "extract", name: "title", selector: "h1" },
  ],
});
// парсинг/классификацию блока/ретраи делает вызывающий код
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
- **Fingerprint headless-Linux Chrome палится — это была главная причина капчи.**
  Дефолтный отпечаток на сервере кричит «бот»: нет WebGL, таймзона UTC,
  `languages=en-US`, `platform=Linux`, SwiftShader-GPU. Ozon даёт слайдер-капчу
  даже с чистого (residential) IP. Решение — `BrowserManager._apply_stealth`
  ([browser.py](./service/app/browser.py)): маскировка под реального
  RU-Windows-юзера через CDP (UA + Client Hints = Windows, `Europe/Moscow`,
  `ru-RU`) + pre-load скрипт (WebGL→NVIDIA, `hardwareConcurrency`/`deviceMemory`,
  чистый `languages`). Важно: скрипт-инъекция работает только после
  `Page.enable()`. С этим капча уходит, карточка отдаёт цену.
- **Холодный заход на карточку палится.** Поэтому сервис при старте браузера
  один раз заходит на главную (`WARMUP_URL`), получает антибот-куки сессии, и
  только потом ходит на товары. Это снимает капчу.
- **Бан по IP — самое слабое место, и zendriver его НЕ решает** (это просто
  контроллер браузера). Стратегия:
  1. **Троттлинг** (заложено: `MIN_INTERVAL_S`, `JITTER_S`) + кэш результатов
     на стороне вызывающего. Для мониторинга своих цен редких заходов часто
     хватает и со своего IP.
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
- порт Asocks используется как **HTTP-прокси с логином/паролем** (тот же
  шлюз `host:port` отвечает и по HTTP, и по SOCKS5). Chrome авторизует
  HTTP-прокси нативно — CDP отвечает на запрос `407` (`Fetch.continueWithAuth`),
  поэтому отдельный мост не нужен. SOCKS5 не используем: Chrome не умеет его
  авторизацию, а по HTTP Ozon к тому же реже даёт жёсткий бан;
- явный `proxy` в запросе имеет приоритет над `use_proxy`.

**Ротация при бане по IP.** Ozon отбраковывает IP по репутации: часть прокси он
жёстко блокирует («Похоже, нет соединения / Выключите VPN»), часть пускает, но
показывает капчу. baas сам не решает, что считать блоком (это знание о
конкретном маркетплейсе) — но даёт примитив: на повторном `/run` передай
**`rotate_proxy: true`**, и запрос пойдёт через **свежий exit-IP** (иначе
попадёшь в тот же закэшированный). Логику «увидел блок → повтори с
`rotate_proxy`» строит вызывающий код. Чистого результата добиваются прокси,
которым Ozon доверяет (резидентные/мобильные с хорошей репутацией) — ротация
лишь перебирает выданный пул.

**Порты создавать с городом (Moscow), а не «без города».** Эмпирически: IP без
привязки к городу Ozon метит капчей даже с правильным фингерпринтом, а
Moscow-city mobile/residential проходят. Поэтому авто-создание порта пинит
`ASOCKS_STATE`/`ASOCKS_CITY` (по умолчанию `Moscow`). Рабочий пул — несколько
mobile + residential, все Moscow.

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
- универсально — куки через поле `cookies` запроса `/run`.
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
| `WARMUP_URL` | `https://www.ozon.ru/` | прогрев сессии при старте браузера |
| `HEADLESS` | `0` | `1` включит headless (Ozon заблокирует) |
| `RUN_TIMEOUT_S` | `90` | жёсткий таймаут одного запуска |
| `ASOCKS_API_KEY` | — | ключ Asocks; включает `use_proxy` в запросах |
| `ASOCKS_POOL_TTL_S` | `300` | сколько переиспользовать выданный порт |
| `ASOCKS_BASE_URL` | `https://api.asocks.com/v2` | база API Asocks |
| `ASOCKS_TIMEOUT_S` | `30` | таймаут HTTP-вызова к API Asocks |
| `ASOCKS_TYPE_ID` / `ASOCKS_PROXY_TYPE_ID` / `ASOCKS_SERVER_PORT_TYPE_ID` | `1` / `2` / `1` | поля `create-port` (по умолчанию — SOCKS5-порт с авторизацией) |

## Деплой (сервер)

Сервис крутится под **systemd** с виртуальным дисплеем (`xvfb-run`, нужен для
headful Chrome) за nginx. Юнит `/etc/systemd/system/browser-as-a-service.service`:

```ini
[Unit]
Description=browser-as-a-service (FastAPI + zendriver/Chrome over Xvfb)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/var/www/browser-as-a-service/service
EnvironmentFile=-/var/www/browser-as-a-service/.env
ExecStart=/usr/bin/xvfb-run -a /var/www/browser-as-a-service/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8077
Restart=always
RestartSec=3
KillMode=control-group
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

Первичная установка (один раз):
```bash
systemctl daemon-reload && systemctl enable --now browser-as-a-service
```

Обновление кода — скриптом [`deploy.sh`](./deploy.sh) (git pull → pip → рестарт → health-check):
```bash
ssh ai-seller 'cd /var/www/browser-as-a-service && ./deploy.sh'
```

- Секреты (`ASOCKS_API_KEY`) — в `/var/www/browser-as-a-service/.env` (вне git).
- Логи: `journalctl -u browser-as-a-service -f`.
- Статус/рестарт: `systemctl status|restart browser-as-a-service`.

## Замечания по zendriver

- Используется `zendriver` (PyPI), а не оригинальный `nodriver`: версия
  `nodriver==0.50.3` содержит «голый» байт `±` без объявления кодировки в
  `cdp/network.py` и не импортируется на современном Python. `zendriver` —
  поддерживаемый форк с уже вмёрженными багфиксами и тем же API.
- nodriver/zendriver сами выбирали `/opt/homebrew/bin/chromium`, который на этой
  машине не стартует. Сервис явно указывает на Google Chrome (`CHROME_PATH`).
