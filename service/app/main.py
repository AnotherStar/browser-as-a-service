"""FastAPI service exposing the scraping engine over HTTP.

OpenAPI is served at /openapi.json and Swagger UI at /docs. The typesafe
zod TS client is generated from that schema (see client/).
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
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
    Proxy,
    RunRequest,
    RunResponse,
)
from .settings import settings


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
        "Generic browser automation over zendriver (undetected Chrome) with "
        "fingerprint masking and Asocks residential proxies. Run typed scenarios "
        "against bot-protected sites and get the DOM back; site-specific parsing "
        "and retry logic live in the caller."
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
        proxy = await _resolve_proxy(req, who, fresh=req.rotate_proxy)
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
