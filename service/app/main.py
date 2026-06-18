"""FastAPI service exposing the scraping engine over HTTP.

OpenAPI is served at /openapi.json and Swagger UI at /docs. The typesafe
zod TS client is generated from that schema (see client/).
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException

from .browser import apply_cookies, manager
from .engine import run_steps
from .models import (
    HealthResponse,
    NavigateStep,
    OzonPriceRequest,
    OzonPriceResponse,
    RunRequest,
    RunResponse,
)
from .ozon import extract_ozon_price
from .settings import settings

# Simple in-memory Ozon price cache: url -> (timestamp, OzonPriceResponse)
_price_cache: dict[str, tuple[float, OzonPriceResponse]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
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


@app.get("/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        browser_ready=await manager.health(),
        chrome_path=settings.chrome_path,
    )


@app.post("/run", response_model=RunResponse, tags=["scrape"])
async def run(req: RunRequest) -> RunResponse:
    """Execute a scenario (list of steps) in a real Chrome and return the
    extracted data. Use `start_url` for the initial navigation."""
    if settings.chrome_path is None:
        raise HTTPException(500, "No Chrome executable found; set CHROME_PATH.")

    steps = list(req.steps)
    if req.start_url:
        steps.insert(0, NavigateStep(url=req.start_url))

    # URL of the first navigation — used as the default cookie domain.
    first_url = req.start_url or next(
        (s.url for s in steps if isinstance(s, NavigateStep)), None
    )

    started = time.monotonic()
    try:
        async with manager.acquire(req.proxy, req.headless) as (browser, tab):
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
        return RunResponse(
            ok=False,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=f"run exceeded {settings.run_timeout_s}s timeout",
        )
    except Exception as exc:  # noqa: BLE001
        return RunResponse(
            ok=False,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=str(exc),
        )

    ok = all(s.ok for s in step_results)
    return RunResponse(
        ok=ok,
        final_url=final_url,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        data=data,
        steps=step_results,
    )


@app.post("/ozon/price", response_model=OzonPriceResponse, tags=["ozon"])
async def ozon_price(req: OzonPriceRequest) -> OzonPriceResponse:
    """Convenience endpoint: open an Ozon product page and return its price."""
    if settings.chrome_path is None:
        raise HTTPException(500, "No Chrome executable found; set CHROME_PATH.")

    now = time.monotonic()
    if req.use_cache:
        hit = _price_cache.get(req.url)
        if hit and (now - hit[0]) < settings.price_cache_ttl_s:
            cached = hit[1].model_copy(update={"cached": True})
            return cached

    started = time.monotonic()
    try:
        async with manager.acquire(req.proxy, req.headless) as (browser, tab):
            await apply_cookies(browser, req.cookies, req.url)
            await asyncio.wait_for(
                _open_and_settle(tab, req.url), timeout=settings.run_timeout_s
            )
            parsed = await extract_ozon_price(tab)
    except asyncio.TimeoutError:
        return OzonPriceResponse(
            ok=False, url=req.url,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=f"exceeded {settings.run_timeout_s}s timeout",
        )
    except Exception as exc:  # noqa: BLE001
        return OzonPriceResponse(
            ok=False, url=req.url,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=str(exc),
        )

    ok = parsed.get("price_value") is not None or parsed.get("price_text") is not None
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
    )
    if ok:
        _price_cache[req.url] = (now, resp)
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
