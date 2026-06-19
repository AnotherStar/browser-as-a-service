"""Admin panel: a live log stream and service status, served by the same app.

Because this router is mounted on the FastAPI app, it comes up automatically
when the service starts (uvicorn) — there is no separate process. Open it at
`/admin`.

- `GET /admin`         -> the HTML panel (self-contained, no build step)
- `GET /admin/status`  -> JSON snapshot (browser/chrome/config + counters)
- `GET /admin/events`  -> Server-Sent-Events stream of log lines (the "running"
                          logs: who is doing what), replaying recent backlog
                          first, then live, with periodic heartbeats.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .asocks import client as asocks_client
from .browser import manager
from .events import bus
from .settings import settings

router = APIRouter(prefix="/admin", tags=["admin"])

# Seconds between SSE heartbeats; also bounds how often we notice a disconnect.
_HEARTBEAT_S = 15.0


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def admin_index() -> HTMLResponse:
    return HTMLResponse(_ADMIN_HTML)


@router.get("/status", include_in_schema=False)
async def admin_status() -> JSONResponse:
    data = bus.stats()
    data.update(
        {
            "service": "up",
            "chrome_path": settings.chrome_path,
            "chrome_detected": settings.chrome_path is not None,
            "browser_alive": manager.is_alive(),
            "sessions": manager.session_count(),
            "max_concurrency": settings.max_concurrency,
            "headless": settings.default_headless,
            "min_interval_s": settings.min_interval_s,
            "asocks_configured": asocks_client.configured,
        }
    )
    return JSONResponse(data)


@router.get("/asocks", include_in_schema=False)
async def admin_asocks() -> JSONResponse:
    """Asocks balance/traffic for the panel: whether a key is set, the money
    `balance` (+ `currency`), remaining `traffic_bytes`, and how many proxy
    ports exist. The balance is cached (`asocks_balance_ttl_s`), so the panel
    can poll this on a timer without hammering the API. Handy for spotting an
    empty balance (port creation needs funds) without leaving the panel."""
    if not asocks_client.configured:
        return JSONResponse({"configured": False})
    out: dict = {"configured": True}
    try:
        out.update(await asocks_client.balance_info())
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    try:
        out["ports"] = len(await asocks_client.list_ports())
    except Exception:  # noqa: BLE001 - ports are secondary; never hide balance
        pass
    return JSONResponse(out)


@router.get("/events", include_in_schema=False)
async def admin_events(request: Request) -> StreamingResponse:
    async def gen():
        q = bus.register()
        try:
            # Replay backlog so a freshly opened panel isn't empty.
            for ev in bus.recent():
                yield ev.sse()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # keep the connection warm
                    continue
                yield ev.sse()
        finally:
            bus.unregister(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering (nginx)
        },
    )


_ADMIN_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>browser-as-a-service · admin</title>
<style>
  :root {
    --bg: #0e1116; --panel: #161b22; --border: #30363d; --fg: #e6edf3;
    --muted: #8b949e; --accent: #58a6ff; --green: #3fb950; --red: #f85149;
    --yellow: #d29922; --purple: #bc8cff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 13px/1.5 ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, monospace;
    height: 100vh; display: flex; flex-direction: column;
  }
  header {
    padding: 12px 16px; border-bottom: 1px solid var(--border);
    background: var(--panel); display: flex; align-items: center; gap: 14px;
    flex-wrap: wrap;
  }
  h1 { font-size: 14px; margin: 0; font-weight: 600; letter-spacing: .2px; }
  h1 .dot {
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: var(--red); margin-right: 8px; vertical-align: middle;
  }
  h1 .dot.live { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .badges { display: flex; gap: 8px; flex-wrap: wrap; margin-left: auto; }
  .badge {
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 4px 9px; color: var(--muted); white-space: nowrap;
  }
  .badge b { color: var(--fg); font-weight: 600; }
  .badge.warn b { color: var(--yellow); }
  .badge.bad b { color: var(--red); }
  .badge.ok b { color: var(--green); }
  main { flex: 1; overflow-y: auto; padding: 8px 0; }
  .row {
    display: flex; gap: 10px; padding: 2px 16px; align-items: baseline;
    border-left: 3px solid transparent; white-space: pre-wrap; word-break: break-word;
  }
  .row:hover { background: #ffffff08; }
  .row .ts { color: var(--muted); flex: none; }
  .row .src {
    flex: none; width: 62px; text-transform: uppercase; font-size: 11px;
    letter-spacing: .5px; opacity: .9;
  }
  .src.http { color: var(--accent); }
  .src.scrape { color: var(--purple); }
  .src.system { color: var(--muted); }
  .row .who { flex: none; color: var(--muted); min-width: 92px; }
  .row .act { color: var(--fg); }
  .row .detail { color: var(--muted); }
  .row.success { border-left-color: var(--green); }
  .row.error { border-left-color: var(--red); }
  .row.error .act { color: var(--red); }
  .row.warn { border-left-color: var(--yellow); }
  footer {
    border-top: 1px solid var(--border); background: var(--panel);
    padding: 6px 16px; color: var(--muted); display: flex; gap: 16px;
    align-items: center;
  }
  footer label { cursor: pointer; user-select: none; }
  footer .spacer { margin-left: auto; }
  button {
    background: var(--bg); color: var(--muted); border: 1px solid var(--border);
    border-radius: 6px; padding: 4px 10px; cursor: pointer; font: inherit;
  }
  button:hover { color: var(--fg); border-color: var(--accent); }
</style>
</head>
<body>
  <header>
    <h1><span class="dot" id="live"></span>browser-as-a-service · admin</h1>
    <div class="badges" id="badges"></div>
  </header>
  <main id="log"></main>
  <footer>
    <label><input type="checkbox" id="follow" checked> автопрокрутка</label>
    <span id="count">0 событий</span>
    <span class="spacer"></span>
    <button id="clear">очистить</button>
  </footer>

<script>
(function () {
  var log = document.getElementById('log');
  var badges = document.getElementById('badges');
  var live = document.getElementById('live');
  var follow = document.getElementById('follow');
  var countEl = document.getElementById('count');
  var MAX_ROWS = 1000;
  var count = 0;

  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  function hhmmss(ts) {
    var d = new Date(ts * 1000);
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function append(ev) {
    var atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
    var row = document.createElement('div');
    row.className = 'row ' + (ev.level || 'info');
    row.innerHTML =
      '<span class="ts">' + hhmmss(ev.ts) + '</span>' +
      '<span class="src ' + esc(ev.source) + '">' + esc(ev.source) + '</span>' +
      '<span class="who">' + esc(ev.who) + '</span>' +
      '<span class="act">' + esc(ev.action) + '</span>' +
      (ev.detail ? '<span class="detail">— ' + esc(ev.detail) + '</span>' : '');
    log.appendChild(row);
    while (log.childElementCount > MAX_ROWS) log.removeChild(log.firstChild);
    count++;
    countEl.textContent = count + ' событий';
    if (follow.checked && atBottom) log.scrollTop = log.scrollHeight;
  }

  // -- live log stream (SSE) --
  function connect() {
    var es = new EventSource('admin/events');
    es.onopen = function () { live.classList.add('live'); };
    es.onmessage = function (e) {
      try { append(JSON.parse(e.data)); } catch (_) {}
    };
    es.onerror = function () {
      live.classList.remove('live');
      // EventSource auto-reconnects; nothing else to do.
    };
  }
  connect();

  // -- status badges (poll) --
  var lastStatus = null;   // latest /admin/status payload
  var asocksInfo = null;   // latest /admin/asocks payload

  function fmtUptime(s) {
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return (h ? h + 'ч ' : '') + (m ? m + 'м ' : '') + sec + 'с';
  }
  function humanBytes(n) {
    if (n == null) return '—';
    var u = ['B', 'KB', 'MB', 'GB', 'TB'], i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 || v >= 100 ? Math.round(v) : v.toFixed(1)) + ' ' + u[i];
  }
  function fmtMoney(v, cur) {
    if (v == null) return '—';
    var n = (Math.round(v * 100) / 100).toFixed(2);
    return cur === 'USD' || !cur ? '$' + n : n + ' ' + cur;
  }
  function badge(cls, label, val) {
    return '<span class="badge ' + cls + '">' + label + ' <b>' + esc(val) + '</b></span>';
  }

  function renderBadges() {
    if (!lastStatus) return;
    var s = lastStatus, html = '';
    html += badge('ok', 'сервис', 'up');
    html += badge(s.browser_alive ? 'ok' : '', 'браузер',
                  s.browser_alive ? 'активен' : 'ожидает');
    html += badge(s.chrome_detected ? '' : 'bad', 'chrome',
                  s.chrome_detected ? 'найден' : 'нет');
    html += badge(s.asocks_configured ? 'ok' : '', 'asocks',
                  s.asocks_configured ? 'подключён' : 'нет ключа');
    // Asocks balance + remaining traffic (slower, separate poll).
    if (s.asocks_configured && asocksInfo) {
      if (asocksInfo.error) {
        html += badge('bad', 'asocks', 'ошибка');
      } else {
        html += badge(asocksInfo.balance > 0 ? 'ok' : 'bad', 'баланс',
                      fmtMoney(asocksInfo.balance, asocksInfo.currency));
        html += badge(asocksInfo.traffic_bytes > 0 ? '' : 'bad', 'трафик',
                      humanBytes(asocksInfo.traffic_bytes));
      }
    }
    html += badge(s.sessions ? 'ok' : '', 'сессии', s.sessions || 0);
    html += badge('', 'concurrency', s.max_concurrency);
    html += badge(s.active_requests ? 'warn' : '', 'в работе', s.active_requests);
    html += badge('', 'запросов', s.total_requests);
    html += badge(s.errors ? 'bad' : '', 'ошибок', s.errors);
    html += badge('', 'аптайм', fmtUptime(s.uptime_s));
    badges.innerHTML = html;
  }

  function refreshStatus() {
    fetch('admin/status').then(function (r) { return r.json(); }).then(function (s) {
      lastStatus = s;
      renderBadges();
    }).catch(function () {
      live.classList.remove('live');
      badges.innerHTML = badge('bad', 'сервис', 'недоступен');
    });
  }
  function refreshAsocks() {
    fetch('admin/asocks').then(function (r) { return r.json(); }).then(function (a) {
      asocksInfo = a.configured ? a : null;
      renderBadges();
    }).catch(function () { /* keep last known balance */ });
  }
  refreshStatus();
  refreshAsocks();
  setInterval(refreshStatus, 3000);
  setInterval(refreshAsocks, 30000);  // balance is cached server-side (~60s)

  document.getElementById('clear').onclick = function () {
    log.innerHTML = ''; count = 0; countEl.textContent = '0 событий';
  };
})();
</script>
</body>
</html>"""
