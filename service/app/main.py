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
from .asocks import AsocksError, client as asocks_client, resolve_proxy_type
from .browser import SessionNotFound, apply_cookies, manager
from .engine import run_steps
from .events import bus
from .models import (
    HealthResponse,
    NavigateStep,
    Proxy,
    RunRequest,
    RunResponse,
    SessionCreateRequest,
    SessionInfo,
    SessionListResponse,
)
from .settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    bus.emit("info", "system", "system", "service started", "admin panel at /admin")
    manager.start_reaper()
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


async def _resolve_one_off_proxy(req, who: str) -> Optional[Proxy]:
    """Pick the proxy for a one-off (sessionless) run: an explicit `proxy` wins;
    otherwise an auto-resolved Asocks residential IP when Asocks is configured.
    Returns None for a direct connection (local dev without Asocks). May raise
    AsocksError, surfaced to the caller as a failed run."""
    if req.proxy is not None:
        return req.proxy
    if asocks_client.configured:
        proxy = await asocks_client.acquire(settings.default_proxy_country)
        bus.emit("info", "scrape", who, "asocks proxy", proxy.server)
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
        + (f" · session {req.session_id}" if req.session_id else " · one-off"),
    )

    steps = list(req.steps)
    if req.start_url:
        steps.insert(0, NavigateStep(url=req.start_url))

    # URL of the first navigation — used as the default cookie domain.
    first_url = req.start_url or next(
        (s.url for s in steps if isinstance(s, NavigateStep)), None
    )

    started = time.monotonic()
    timings: dict[str, int] = {}
    # Ceiling on the WHOLE request (acquire + proxy-auth + steps), not just steps.
    # A run that hangs while launching/warming a browser or authing a slow proxy
    # is outside the steps timeout and would hold its concurrency slot forever —
    # that's what wedges the service. Must exceed the steps timeout so a steps
    # timeout still surfaces as one rather than being masked by this ceiling.
    overall_timeout = max(settings.request_timeout_s, settings.run_timeout_s + 10)

    async def _acquire_and_run():
        # A warm session reuses an already-launched, proxied, warmed browser
        # (acquire_ms drops to ~throttle); a one-off spins up a fresh proxied
        # browser just for this run.
        if req.session_id:
            acquire_cm = manager.acquire_session(req.session_id)
        else:
            t_proxy = time.monotonic()
            proxy = await _resolve_one_off_proxy(req, who)
            timings["proxy_ms"] = int((time.monotonic() - t_proxy) * 1000)
            acquire_cm = manager.acquire(proxy, req.headless)
        t_acquire = time.monotonic()
        async with acquire_cm as (browser, tab):
            # Time to enter the context = throttle wait + browser/tab/stealth setup.
            timings["acquire_ms"] = int((time.monotonic() - t_acquire) * 1000)
            await apply_cookies(browser, req.cookies, first_url)
            t_steps = time.monotonic()
            data, step_results = await asyncio.wait_for(
                run_steps(tab, steps, browser=browser),
                timeout=settings.run_timeout_s,
            )
            timings["steps_ms"] = int((time.monotonic() - t_steps) * 1000)
            final_url = None
            try:
                final_url = await tab.evaluate("location.href", return_by_value=True)
            except Exception:
                pass
            return data, step_results, final_url

    try:
        # Cancelling on timeout unwinds the context managers (semaphore + session
        # lock + tab), so a hung run frees its slot instead of leaking it.
        data, step_results, final_url = await asyncio.wait_for(
            _acquire_and_run(), timeout=overall_timeout
        )
    except SessionNotFound:
        bus.emit(
            "warn", "scrape", who, "run rejected",
            f"session {req.session_id} not found/expired",
        )
        raise HTTPException(
            status_code=404,
            detail=f"session not found or expired: {req.session_id}",
        )
    except asyncio.TimeoutError:
        # Either the steps timeout or the whole-request ceiling fired. Both unwind
        # the context managers above, so the concurrency slot is already released.
        elapsed = int((time.monotonic() - started) * 1000)
        bus.emit("error", "scrape", who, "run failed", f"timeout · {elapsed}ms")
        return RunResponse(
            ok=False,
            elapsed_ms=elapsed,
            error=f"run exceeded timeout ({int(settings.run_timeout_s)}s steps / {int(overall_timeout)}s total)",
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
    phases = " · ".join(f"{k}={v}ms" for k, v in timings.items())
    steps_detail = " · ".join(
        f"{s.action}={s.duration_ms}ms" for s in step_results
    )
    bus.emit(
        "success" if ok else "warn", "scrape", who,
        "run done" if ok else "run partial",
        f"{final_url or '?'} · {elapsed}ms · {phases} · {steps_detail}",
    )
    return RunResponse(
        ok=ok,
        final_url=final_url,
        elapsed_ms=elapsed,
        data=data,
        steps=step_results,
        timings=timings,
    )


@app.post("/sessions", response_model=SessionInfo, tags=["sessions"])
async def create_session(req: SessionCreateRequest, request: Request) -> SessionInfo:
    """Launch a warm, proxied browser and keep it alive for reuse via
    `POST /run` with its `session_id`. Hold several (e.g. a small pool per
    marketplace) to scrape batches in parallel without re-warming each card."""
    if settings.chrome_path is None:
        raise HTTPException(500, "No Chrome executable found; set CHROME_PATH.")
    who = _client_host(request)
    started = time.monotonic()
    try:
        proxy_type_id = resolve_proxy_type(req.proxy_type)
    except AsocksError as exc:
        raise HTTPException(400, str(exc))
    try:
        session = await manager.create_session(
            req.label,
            req.proxy_country,
            req.warmup,
            proxy_type_id,
            req.warmup_url,
            req.warmup_settle_s,
        )
    except Exception as exc:  # noqa: BLE001
        bus.emit("error", "scrape", who, "session create failed", str(exc))
        raise HTTPException(502, f"session create failed: {exc}")
    info = next((i for i in manager.session_infos() if i.id == session.id), None)
    if info is None:  # pragma: no cover - just created it
        raise HTTPException(500, "session vanished after create")
    bus.emit(
        "success", "scrape", who, "session created",
        f"{session.id} · {int((time.monotonic() - started) * 1000)}ms",
    )
    return info


@app.get("/sessions", response_model=SessionListResponse, tags=["sessions"])
async def list_sessions() -> SessionListResponse:
    """List live warm sessions with age/idle/run counters."""
    return SessionListResponse(sessions=manager.session_infos())


@app.delete("/sessions/{session_id}", tags=["sessions"])
async def delete_session(session_id: str) -> dict:
    """Tear down a warm session (stops its browser, frees the exit IP)."""
    ok = await manager.close_session(session_id)
    if not ok:
        raise HTTPException(404, f"session not found: {session_id}")
    return {"ok": True}
