"""Browser lifecycle, proxy handling, and anti-ban throttling.

Design:
- A single shared browser is reused for proxy-less requests (fast: just open
  a fresh tab per request). Chrome's `--proxy-server` is a *launch* flag, so
  requests that specify a proxy get a dedicated, ephemeral browser.
- Authenticated proxies are handled at the CDP layer (Fetch.continueWithAuth),
  so credentials never go into the proxy URL.
- A semaphore caps concurrency and a spacing lock enforces a minimum interval
  (plus jitter) between page loads, which is the main lever against IP bans.
"""
from __future__ import annotations

import asyncio
import contextlib
import random
import time
from typing import AsyncIterator, Optional
from urllib.parse import urlparse

import zendriver as uc
from zendriver import cdp

from .events import bus
from .models import Cookie, Proxy
from .settings import settings


def _normalize_proxy_server(server: str) -> str:
    """Chrome wants 'scheme://host:port' or 'host:port'. Strip any creds."""
    s = server.strip()
    if "@" in s:  # user:pass@host -> host
        s = s.split("@", 1)[1]
    return s


def _registrable_domain(url: Optional[str]) -> Optional[str]:
    """Best-effort registrable domain (".ozon.ru") from a URL. Good enough for
    the second-level .ru/.com domains we target; not a public-suffix parser."""
    if not url:
        return None
    host = urlparse(url).hostname
    if not host:
        return None
    labels = host.lstrip(".").split(".")
    if len(labels) < 2:
        return host
    return "." + ".".join(labels[-2:])


async def apply_cookies(
    browser: uc.Browser,
    cookies: Optional[list[Cookie]],
    default_url: Optional[str] = None,
) -> None:
    """Set cookies before the first navigation. A missing cookie domain falls
    back to the registrable domain of `default_url` (the run's start URL), so
    callers can pin a region without restating the host on every cookie."""
    if not cookies:
        return
    fallback = _registrable_domain(default_url)
    params = [
        cdp.network.CookieParam(
            name=c.name,
            value=c.value,
            domain=c.domain or fallback,
            path=c.path or "/",
        )
        for c in cookies
    ]
    with contextlib.suppress(Exception):
        await browser.cookies.set_all(params)


class BrowserManager:
    def __init__(self) -> None:
        self._shared: Optional[uc.Browser] = None
        self._shared_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(settings.max_concurrency)
        self._space_lock = asyncio.Lock()
        self._last_load = 0.0

    # -- shared browser ---------------------------------------------------- #

    async def _get_shared(self, headless: bool) -> uc.Browser:
        async with self._shared_lock:
            if self._shared is None:
                bus.emit(
                    "info", "system", "system", "launching browser",
                    f"headless={headless}",
                )
                self._shared = await self._launch(headless=headless)
                await self._warmup(self._shared)
                bus.emit("success", "system", "system", "browser ready", "")
            return self._shared

    @staticmethod
    async def _warmup(browser: uc.Browser) -> None:
        """Visit the warmup URL once so the browser holds valid anti-bot
        cookies before any direct (cold) page hit."""
        if not settings.warmup_url:
            return
        with contextlib.suppress(Exception):
            tab = await browser.get(settings.warmup_url)
            await tab.sleep(settings.warmup_settle_s)

    async def _launch(
        self, headless: bool, proxy_server: Optional[str] = None
    ) -> uc.Browser:
        args = [
            f"--lang={settings.lang}",
            f"--accept-lang={settings.accept_lang}",
            # Without a GPU (headless server / Xvfb) Chrome exposes NO WebGL at
            # all — a strong bot signal. Force ANGLE/SwiftShader so a WebGL
            # context (with a renderer string) exists.
            "--enable-unsafe-swiftshader",
            "--use-gl=angle",
            "--use-angle=swiftshader",
            "--ignore-gpu-blocklist",
            "--window-size=1920,1080",
        ]
        if proxy_server:
            args.append(f"--proxy-server={proxy_server}")
        return await uc.start(
            headless=headless,
            browser_executable_path=settings.chrome_path,
            browser_args=args,
        )

    @staticmethod
    async def _apply_stealth(tab) -> None:
        """Nudge the fingerprint toward a real Russian user before navigation:
        Moscow timezone and ru-RU locale. The headless Linux default leaks UTC +
        en-US, which on a Russian site + Russian proxy IP reads as a bot."""
        with contextlib.suppress(Exception):
            await tab.send(
                cdp.emulation.set_timezone_override(
                    timezone_id=settings.browser_timezone
                )
            )
        with contextlib.suppress(Exception):
            await tab.send(cdp.emulation.set_locale_override(locale=settings.lang))

    # -- proxy auth -------------------------------------------------------- #

    @staticmethod
    async def _install_proxy_auth(tab, proxy: Proxy) -> None:
        """Enable CDP Fetch and answer proxy auth challenges with creds."""

        async def on_paused(event: cdp.fetch.RequestPaused, conn):
            with contextlib.suppress(Exception):
                await conn.send(cdp.fetch.continue_request(event.request_id))

        async def on_auth(event: cdp.fetch.AuthRequired, conn):
            resp = cdp.fetch.AuthChallengeResponse(
                response="ProvideCredentials",
                username=proxy.username or "",
                password=proxy.password or "",
            )
            with contextlib.suppress(Exception):
                await conn.send(
                    cdp.fetch.continue_with_auth(event.request_id, resp)
                )

        tab.add_handler(cdp.fetch.RequestPaused, on_paused)
        tab.add_handler(cdp.fetch.AuthRequired, on_auth)
        await tab.send(cdp.fetch.enable(handle_auth_requests=True))

    # -- throttle ---------------------------------------------------------- #

    async def _throttle(self) -> None:
        async with self._space_lock:
            wait = settings.min_interval_s + random.uniform(0, settings.jitter_s)
            delta = time.monotonic() - self._last_load
            if delta < wait:
                await asyncio.sleep(wait - delta)
            self._last_load = time.monotonic()

    # -- public acquire ---------------------------------------------------- #

    @contextlib.asynccontextmanager
    async def acquire(
        self, proxy: Optional[Proxy], headless: bool
    ) -> AsyncIterator[tuple[uc.Browser, object]]:
        """Yield (browser, blank_tab) ready for a scenario. Handles cleanup
        and concurrency/throttle gating."""
        async with self._sem:
            await self._throttle()
            ephemeral: Optional[uc.Browser] = None
            tab = None
            try:
                if proxy is not None:
                    ephemeral = await self._launch(
                        headless=headless,
                        proxy_server=_normalize_proxy_server(proxy.server),
                    )
                    browser = ephemeral
                    tab = await browser.get("about:blank")
                    await self._apply_stealth(tab)
                    # Proxy auth (HTTP 407) is answered per CDP session, so it
                    # must be installed on every tab we navigate: the warmup tab
                    # here, and the work tab below.
                    if proxy.username or proxy.password:
                        await self._install_proxy_auth(tab, proxy)
                    await self._warmup(browser)
                    tab = await browser.get("about:blank", new_tab=True)
                    await self._apply_stealth(tab)
                    if proxy.username or proxy.password:
                        await self._install_proxy_auth(tab, proxy)
                else:
                    browser = await self._get_shared(headless)
                    tab = await browser.get("about:blank", new_tab=True)
                    await self._apply_stealth(tab)
                yield browser, tab
            finally:
                if ephemeral is not None:
                    with contextlib.suppress(Exception):
                        await ephemeral.stop()
                elif tab is not None:
                    with contextlib.suppress(Exception):
                        await tab.close()

    async def health(self) -> bool:
        try:
            b = await self._get_shared(settings.default_headless)
            return bool(b.info)
        except Exception:
            return False

    def is_alive(self) -> bool:
        """Whether the shared browser is currently up. Unlike `health`, this
        never launches one — safe for the admin status snapshot."""
        return self._shared is not None

    async def shutdown(self) -> None:
        if self._shared is not None:
            with contextlib.suppress(Exception):
                await self._shared.stop()
            self._shared = None
            bus.emit("info", "system", "system", "browser stopped", "")


manager = BrowserManager()
