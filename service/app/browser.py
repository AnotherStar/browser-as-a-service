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
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional
from urllib.parse import urlparse

import zendriver as uc
from zendriver import cdp

from .asocks import client as asocks_client
from .events import bus
from .models import Cookie, Proxy, SessionInfo
from .settings import settings


class SessionNotFound(KeyError):
    """A run referenced a session id that isn't (or is no longer) alive."""


@dataclass
class _Session:
    """A warm, proxied browser kept alive for reuse across runs. Each holds its
    own exit IP, so several run in parallel; anti-ban throttling is per-session
    (per-IP) via its own spacing clock."""

    id: str
    browser: uc.Browser
    proxy: Optional[Proxy]
    label: Optional[str]
    created_at: float  # time.monotonic() — for age/TTL math
    created_ts: float  # time.time() epoch — for the info payload
    last_used: float  # time.monotonic() of the last run
    runs: int = 0
    # Serialises runs onto this session's single browser (one work tab at a time).
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Per-session page-load spacing (anti-ban, per exit IP).
    space_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_load: float = 0.0


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


# --- Windows fingerprint spoof (see BrowserManager._apply_stealth) --------- #
# UA pinned to the real Chrome major running on the server (149) so version
# checks stay consistent; only the OS axis is masked.
_WIN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)


def _win_ua_metadata():
    """Client Hints (navigator.userAgentData) matching _WIN_UA — Windows 11."""
    bv = cdp.emulation.UserAgentBrandVersion
    brands = [
        bv(brand="Chromium", version="149"),
        bv(brand="Google Chrome", version="149"),
        bv(brand="Not?A_Brand", version="24"),
    ]
    full = [
        bv(brand="Chromium", version="149.0.0.0"),
        bv(brand="Google Chrome", version="149.0.0.0"),
        bv(brand="Not?A_Brand", version="24.0.0.0"),
    ]
    return cdp.emulation.UserAgentMetadata(
        brands=brands,
        full_version_list=full,
        full_version="149.0.0.0",
        platform="Windows",
        platform_version="15.0.0",
        architecture="x86",
        model="",
        mobile=False,
        bitness="64",
        wow64=False,
    )


# Runs before page scripts on every navigation: align the JS-visible signals
# that CDP doesn't cover (core count, memory, languages) and spoof the WebGL
# renderer away from SwiftShader to a common consumer GPU.
_SPOOF_JS = """
(() => {
  const def = (o, p, v) => { try { Object.defineProperty(o, p, {get: () => v}); } catch (e) {} };
  def(navigator, 'hardwareConcurrency', 8);
  def(navigator, 'deviceMemory', 8);
  def(navigator, 'languages', ['ru-RU', 'ru']);
  const V = 'Google Inc. (NVIDIA)';
  const R = 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)';
  const protos = [];
  if (self.WebGLRenderingContext) protos.push(WebGLRenderingContext.prototype);
  if (self.WebGL2RenderingContext) protos.push(WebGL2RenderingContext.prototype);
  for (const pr of protos) {
    const gp = pr.getParameter;
    pr.getParameter = function (p) {
      if (p === 37445) return V;
      if (p === 37446) return R;
      return gp.call(this, p);
    };
  }
})();
"""


class BrowserManager:
    def __init__(self) -> None:
        self._shared: Optional[uc.Browser] = None
        self._shared_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(settings.max_concurrency)
        self._space_lock = asyncio.Lock()
        self._last_load = 0.0
        # Warm proxy sessions, keyed by id.
        self._sessions: dict[str, _Session] = {}
        self._reaper_task: Optional[asyncio.Task] = None

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
        """Mask the headless-Linux fingerprint as a real Russian Windows user,
        applied before navigation. Ozon's antibot challenges our default
        fingerprint (Linux UA, SwiftShader GPU, UTC, en-US, low core count)
        with a captcha even on a clean IP; a consistent Windows profile passes.
        Each piece must agree (UA ⇄ platform ⇄ Client Hints ⇄ WebGL)."""
        with contextlib.suppress(Exception):
            await tab.send(
                cdp.emulation.set_timezone_override(
                    timezone_id=settings.browser_timezone
                )
            )
        with contextlib.suppress(Exception):
            await tab.send(cdp.emulation.set_locale_override(locale=settings.lang))
        with contextlib.suppress(Exception):
            await tab.send(
                cdp.emulation.set_user_agent_override(
                    user_agent=_WIN_UA,
                    accept_language=settings.accept_lang,
                    platform="Win32",
                    user_agent_metadata=_win_ua_metadata(),
                )
            )
        with contextlib.suppress(Exception):
            await tab.send(cdp.page.enable())
            await tab.send(
                cdp.page.add_script_to_evaluate_on_new_document(source=_SPOOF_JS)
            )

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

    async def _prepare_tab(self, browser: uc.Browser, proxy: Optional[Proxy], new_tab: bool):
        """Open a blank tab with stealth applied and, for authenticated proxies,
        the CDP 407-auth handler installed (must be in place before navigation)."""
        tab = await browser.get("about:blank", new_tab=new_tab)
        await self._apply_stealth(tab)
        if proxy is not None and (proxy.username or proxy.password):
            await self._install_proxy_auth(tab, proxy)
        return tab

    async def _warm_browser(
        self,
        browser: uc.Browser,
        proxy: Optional[Proxy],
        warmup_url: Optional[str] = None,
        settle_s: Optional[float] = None,
    ) -> None:
        """Visit the warmup URL once (through the proxy, with stealth) so the
        browser holds valid anti-bot cookies before any cold product hit."""
        url = warmup_url if warmup_url is not None else settings.warmup_url
        if not url:
            return
        with contextlib.suppress(Exception):
            tab = await self._prepare_tab(browser, proxy, new_tab=False)
            await tab.get(url)
            await tab.sleep(
                settings.warmup_settle_s if settle_s is None else settle_s
            )

    # -- throttle ---------------------------------------------------------- #

    async def _throttle(self) -> None:
        async with self._space_lock:
            wait = settings.min_interval_s + random.uniform(0, settings.jitter_s)
            delta = time.monotonic() - self._last_load
            if delta < wait:
                await asyncio.sleep(wait - delta)
            self._last_load = time.monotonic()

    @staticmethod
    async def _throttle_session(s: _Session) -> None:
        """Per-session (per exit IP) page-load spacing — the anti-ban lever when
        several sessions run in parallel, each pacing its own IP independently."""
        async with s.space_lock:
            wait = settings.min_interval_s + random.uniform(0, settings.jitter_s)
            delta = time.monotonic() - s.last_load
            if delta < wait:
                await asyncio.sleep(wait - delta)
            s.last_load = time.monotonic()

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
                    # Warm up (proxy auth installed on the warmup tab), then open
                    # a fresh work tab — auth is per-CDP-session, so re-install.
                    await self._warm_browser(browser, proxy)
                    tab = await self._prepare_tab(browser, proxy, new_tab=True)
                else:
                    browser = await self._get_shared(headless)
                    tab = await self._prepare_tab(browser, None, new_tab=True)
                yield browser, tab
            finally:
                if ephemeral is not None:
                    with contextlib.suppress(Exception):
                        await ephemeral.stop()
                elif tab is not None:
                    with contextlib.suppress(Exception):
                        await tab.close()

    # -- warm sessions ----------------------------------------------------- #

    async def create_session(
        self,
        label: Optional[str],
        country: Optional[str],
        warmup: bool,
        proxy_type_id: Optional[int] = None,
        warmup_url: Optional[str] = None,
        warmup_settle_s: Optional[float] = None,
    ) -> _Session:
        """Launch a proxied browser, warm it, and keep it alive for reuse.
        Each session gets a fresh Asocks exit IP (fresh=True) so a pool spreads
        load across IPs. `proxy_type_id` pins the Asocks proxy type (e.g. mobile
        for Yandex.Market); None uses the server default. Falls back to a direct
        (no-proxy) browser when Asocks isn't configured (local dev)."""
        proxy: Optional[Proxy] = None
        if asocks_client.configured:
            proxy = await asocks_client.acquire(
                country or settings.default_proxy_country,
                fresh=True,
                proxy_type_id=proxy_type_id,
            )
        server = proxy.server if proxy else None
        bus.emit(
            "info", "system", "sessions", "creating session",
            f"{label or '-'} · {server or 'direct'}",
        )
        browser = await self._launch(
            headless=settings.default_headless,
            proxy_server=_normalize_proxy_server(proxy.server) if proxy else None,
        )
        if warmup:
            await self._warm_browser(browser, proxy, warmup_url, warmup_settle_s)
        now_m = time.monotonic()
        sid = uuid.uuid4().hex[:12]
        session = _Session(
            id=sid,
            browser=browser,
            proxy=proxy,
            label=label,
            created_at=now_m,
            created_ts=time.time(),
            last_used=now_m,
        )
        self._sessions[sid] = session
        bus.emit(
            "success", "system", "sessions", "session ready",
            f"{sid} · {label or '-'} · {server or 'direct'}",
        )
        return session

    @contextlib.asynccontextmanager
    async def acquire_session(
        self, session_id: str
    ) -> AsyncIterator[tuple[uc.Browser, object]]:
        """Yield (browser, work_tab) on an existing warm session. No launch, no
        warmup, no teardown — just a fresh tab + per-session throttle. Raises
        SessionNotFound if the id is unknown/expired."""
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound(session_id)
        async with self._sem:
            # One run per session browser at a time (single work tab).
            async with session.lock:
                await self._throttle_session(session)
                tab = None
                try:
                    tab = await self._prepare_tab(
                        session.browser, session.proxy, new_tab=True
                    )
                    yield session.browser, tab
                finally:
                    session.last_used = time.monotonic()
                    session.runs += 1
                    if tab is not None:
                        with contextlib.suppress(Exception):
                            await tab.close()

    async def close_session(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        with contextlib.suppress(Exception):
            await session.browser.stop()
        bus.emit("info", "system", "sessions", "session closed", session_id)
        return True

    def session_infos(self) -> list[SessionInfo]:
        now_m = time.monotonic()
        out: list[SessionInfo] = []
        for s in self._sessions.values():
            out.append(
                SessionInfo(
                    id=s.id,
                    label=s.label,
                    exit_server=s.proxy.server if s.proxy else None,
                    created_ts=s.created_ts,
                    last_used_ts=s.created_ts + (s.last_used - s.created_at),
                    age_s=int(now_m - s.created_at),
                    idle_s=int(now_m - s.last_used),
                    runs=s.runs,
                )
            )
        return out

    def session_count(self) -> int:
        return len(self._sessions)

    # -- reaper ------------------------------------------------------------ #

    async def _reap_once(self) -> None:
        now = time.monotonic()
        doomed = [
            s.id
            for s in self._sessions.values()
            if (now - s.last_used) > settings.session_idle_ttl_s
            or (now - s.created_at) > settings.session_max_age_s
        ]
        for sid in doomed:
            s = self._sessions.get(sid)
            if s is None or s.lock.locked():  # don't evict mid-run
                continue
            await self.close_session(sid)
            bus.emit("info", "system", "sessions", "session reaped", sid)

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(settings.session_reap_interval_s)
            with contextlib.suppress(Exception):
                await self._reap_once()

    def start_reaper(self) -> None:
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reaper_loop())

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
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None
        for sid in list(self._sessions):
            await self.close_session(sid)
        if self._shared is not None:
            with contextlib.suppress(Exception):
                await self._shared.stop()
            self._shared = None
            bus.emit("info", "system", "system", "browser stopped", "")


manager = BrowserManager()
