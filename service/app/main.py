"""FastAPI service exposing the scraping engine over HTTP.

OpenAPI is served at /openapi.json and Swagger UI at /docs. The typesafe
zod TS client is generated from that schema (see client/).
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request

from .admin import router as admin_router
from .asocks import client as asocks_client
from .browser import apply_cookies, manager
from .engine import run_steps
from .events import bus
from .models import (
    HealthResponse,
    NavigateStep,
    OzonPriceRequest,
    OzonPriceResponse,
    Proxy,
    RunRequest,
    RunResponse,
)
from .ozon import detect_antibot, extract_ozon_price
from .settings import settings

# Simple in-memory Ozon price cache: url -> (timestamp, OzonPriceResponse)
_price_cache: dict[str, tuple[float, OzonPriceResponse]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus.emit("info", "system", "system", "service started", "admin panel at /admin")
    yield
    bus.emit("warn", "system", "system", "service stopping", "")
    await manager.shutdown()


app = FastAPI(
    title="browser-as-a-service",
    version="0.1.0",
    description=(
        "Command-driven browser automation over nodriver (undetected Chrome). "
        "Run typed scenarios against bot-protected sites such as Ozon."
    ),
    lifespan=lifespan,
)
app.include_router(admin_router)


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "?"


async def _resolve_proxy(req, who: str, fresh: bool = False) -> Optional[Proxy]:
    """Pick the proxy for a request: an explicit `proxy` always wins; otherwise
    an Asocks residential proxy when the caller opts in with `use_proxy`.
    `fresh` rotates to a new Asocks exit IP (used when retrying past an antibot
    wall). Returns None for a direct connection. May raise AsocksError, surfaced
    to the caller as a failed run."""
    if req.proxy is not None:
        return req.proxy
    if req.use_proxy:
        proxy = await asocks_client.acquire(req.proxy_country, fresh=fresh)
        bus.emit(
            "info", "scrape", who,
            "asocks proxy (rotated)" if fresh else "asocks proxy", proxy.server,
        )
        return proxy
    return None


def _scrape_attempts(req) -> int:
    """How many proxy-rotating attempts to make. Rotation only helps when we
    pull a re-rollable Asocks proxy, so an explicit `proxy` or a direct
    connection gets a single attempt."""
    if req.proxy is None and req.use_proxy:
        return max(1, settings.scrape_max_attempts)
    return 1


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    """Feed every request into the admin log: who (client ip), what (method +
    path), and the outcome (status + duration). The admin panel's own traffic
    is skipped so the live stream doesn't narrate itself."""
    path = request.url.path
    if path.startswith("/admin") or path.startswith("/docs") or path == "/openapi.json":
        return await call_next(request)

    who = _client_host(request)
    bus.total += 1
    bus.active += 1
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as exc:  # noqa: BLE001
        bus.errors += 1
        bus.emit("error", "http", who, f"{request.method} {path}", f"unhandled: {exc}")
        raise
    finally:
        bus.active -= 1

    elapsed = int((time.monotonic() - started) * 1000)
    failed = response.status_code >= 400
    if failed:
        bus.errors += 1
    bus.emit(
        "error" if failed else "success",
        "http",
        who,
        f"{request.method} {path}",
        f"{response.status_code} · {elapsed}ms",
    )
    return response


@app.get("/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        browser_ready=await manager.health(),
        chrome_path=settings.chrome_path,
    )


@app.post("/run", response_model=RunResponse, tags=["scrape"])
async def run(req: RunRequest, request: Request) -> RunResponse:
    """Execute a scenario (list of steps) in a real Chrome and return the
    extracted data. Use `start_url` for the initial navigation."""
    if settings.chrome_path is None:
        raise HTTPException(500, "No Chrome executable found; set CHROME_PATH.")

    who = _client_host(request)
    bus.emit(
        "info", "scrape", who, "run scenario",
        f"{len(req.steps)} steps · {req.start_url or 'no start_url'}"
        + (" · via proxy" if (req.proxy or req.use_proxy) else ""),
    )

    steps = list(req.steps)
    if req.start_url:
        steps.insert(0, NavigateStep(url=req.start_url))

    # URL of the first navigation — used as the default cookie domain.
    first_url = req.start_url or next(
        (s.url for s in steps if isinstance(s, NavigateStep)), None
    )

    started = time.monotonic()
    try:
        proxy = await _resolve_proxy(req, who)
        async with manager.acquire(proxy, req.headless) as (browser, tab):
            await apply_cookies(browser, req.cookies, first_url)
            data, step_results = await asyncio.wait_for(
                run_steps(tab, steps, browser=browser),
                timeout=settings.run_timeout_s,
            )
            final_url = None
            try:
                final_url = await tab.evaluate("location.href", return_by_value=True)
            except Exception:
                pass
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - started) * 1000)
        bus.emit("error", "scrape", who, "run failed", f"timeout · {elapsed}ms")
        return RunResponse(
            ok=False,
            elapsed_ms=elapsed,
            error=f"run exceeded {settings.run_timeout_s}s timeout",
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.monotonic() - started) * 1000)
        bus.emit("error", "scrape", who, "run failed", f"{exc} · {elapsed}ms")
        return RunResponse(
            ok=False,
            elapsed_ms=elapsed,
            error=str(exc),
        )

    ok = all(s.ok for s in step_results)
    elapsed = int((time.monotonic() - started) * 1000)
    bus.emit(
        "success" if ok else "warn", "scrape", who,
        "run done" if ok else "run partial",
        f"{final_url or '?'} · {elapsed}ms",
    )
    return RunResponse(
        ok=ok,
        final_url=final_url,
        elapsed_ms=elapsed,
        data=data,
        steps=step_results,
    )


@app.post("/ozon/price", response_model=OzonPriceResponse, tags=["ozon"])
async def ozon_price(req: OzonPriceRequest, request: Request) -> OzonPriceResponse:
    """Convenience endpoint: open an Ozon product page and return its price."""
    if settings.chrome_path is None:
        raise HTTPException(500, "No Chrome executable found; set CHROME_PATH.")

    who = _client_host(request)
    now = time.monotonic()
    if req.use_cache:
        hit = _price_cache.get(req.url)
        if hit and (now - hit[0]) < settings.price_cache_ttl_s:
            cached = hit[1].model_copy(update={"cached": True})
            bus.emit("success", "scrape", who, "ozon price (cache)", req.url)
            return cached

    bus.emit("info", "scrape", who, "ozon price", req.url)
    started = time.monotonic()
    attempts = _scrape_attempts(req)
    parsed: dict = {}
    block: Optional[str] = None
    try:
        for attempt in range(attempts):
            proxy = await _resolve_proxy(req, who, fresh=attempt > 0)
            async with manager.acquire(proxy, req.headless) as (browser, tab):
                await apply_cookies(browser, req.cookies, req.url)
                await asyncio.wait_for(
                    _open_and_settle(tab, req.url), timeout=settings.run_timeout_s
                )
                block = await detect_antibot(tab)
                parsed = await extract_ozon_price(tab)
            got_price = (
                parsed.get("price_value") is not None
                or parsed.get("price_text") is not None
            )
            if block is None and got_price:
                break
            if attempt < attempts - 1:
                bus.emit(
                    "warn", "scrape", who, "ozon antibot, rotating proxy",
                    f"{block or 'no price'} · attempt {attempt + 1}/{attempts}",
                )
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - started) * 1000)
        bus.emit("error", "scrape", who, "ozon price failed", f"timeout · {elapsed}ms")
        return OzonPriceResponse(
            ok=False, url=req.url,
            elapsed_ms=elapsed,
            error=f"exceeded {settings.run_timeout_s}s timeout",
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.monotonic() - started) * 1000)
        bus.emit("error", "scrape", who, "ozon price failed", f"{exc} · {elapsed}ms")
        return OzonPriceResponse(
            ok=False, url=req.url,
            elapsed_ms=elapsed,
            error=str(exc),
        )

    ok = parsed.get("price_value") is not None or parsed.get("price_text") is not None
    error = None
    if not ok:
        if block == "ip_block":
            error = "ozon antibot: proxy exit IP rejected as VPN/proxy"
        elif block == "captcha":
            error = "ozon antibot: captcha challenge shown"
    resp = OzonPriceResponse(
        ok=bool(ok),
        url=req.url,
        title=parsed.get("title"),
        price_text=parsed.get("price_text"),
        price_value=parsed.get("price_value"),
        card_price_value=parsed.get("card_price_value"),
        cached=False,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        error=error,
    )
    if ok:
        _price_cache[req.url] = (now, resp)
    bus.emit(
        "success" if ok else "warn", "scrape", who,
        "ozon price done" if ok else "ozon price blocked",
        (f"{resp.price_value if resp.price_value is not None else resp.price_text or '?'}"
         if ok else (error or "no price"))
        + f" · {resp.elapsed_ms}ms",
    )
    return resp


async def _open_and_settle(tab, url: str) -> None:
    # Warm up the session: a cold, direct hit to a product page triggers
    # Ozon's antibot. Visiting the homepage first establishes the antibot
    # cookies, after which the product page loads cleanly.
    title = ""
    await tab.get(url)
    try:
        await tab.select("[data-widget=webPrice]", timeout=8)
        return
    except Exception:
        title = str(await tab.evaluate("document.title", return_by_value=True) or "")

    if "antibot" in title.lower() or "captcha" in title.lower():
        await tab.get("https://www.ozon.ru/")
        await tab.sleep(4)
        await tab.get(url)
    try:
        await tab.select("[data-widget=webPrice]", timeout=12)
    except Exception:
        await tab.sleep(4)
