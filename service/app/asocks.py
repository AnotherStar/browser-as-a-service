"""Asocks proxy provider (https://api.asocks.com/v2).

Resolves a usable proxy (host:port + login/password) from the Asocks API so a
browser run can be routed through a residential IP without managing ports by
hand.

Behaviour:
- Auth: every request carries `?apikey=<ASOCKS_API_KEY>` (query param, as the
  official PHP/Go examples do). The key is never put in log lines or errors.
- We prefer reusing an existing active port (optionally filtered by country);
  only when none match do we create one (`POST proxy/create-port`) and poll
  `GET proxy/ports` until it appears.
- Resolved proxies are cached per country for `asocks_pool_ttl_s`, so a burst
  of requests neither hammers the API nor spawns duplicate ports.

HTTP is done with stdlib urllib in a worker thread to avoid adding an async
HTTP client dependency (matching events.py's stdlib-only ethos).
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .events import bus
from .models import Proxy
from .settings import settings
from .socks_bridge import manager as bridge_manager


class AsocksError(RuntimeError):
    """Any failure talking to the Asocks API or resolving a port."""


# Asocks logins end with a sticky-session token (`...-session-<hex>`). Minting a
# new token routes the next connection through a different exit IP — the lever we
# use to rotate away from an IP that a target site has blocked.
_SESSION_RE = re.compile(r"(session-)[0-9a-fA-F]+$")


def _rotate_login(login: Optional[str]) -> Optional[str]:
    """Return `login` with a fresh sticky-session token so Asocks hands out a
    different exit IP. No-op if the login has no recognizable session token."""
    if not login or not _SESSION_RE.search(login):
        return login
    return _SESSION_RE.sub("session-" + secrets.token_hex(8), login)


def _num(value: Any) -> Optional[float]:
    """Coerce an API value to a number; None when missing/unparseable.
    The Asocks API sometimes returns numbers as strings."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _blocking_call(
    url: str, method: str, body: Optional[dict], timeout: float
) -> Any:
    """Synchronous HTTP call (run off-loop via asyncio.to_thread)."""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class AsocksClient:
    def __init__(
        self,
        api_key: Optional[str],
        base_url: str,
        timeout_s: float,
        pool_ttl_s: float,
        balance_ttl_s: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self._pool_ttl = pool_ttl_s
        self._balance_ttl = balance_ttl_s
        # country code ("" == any) -> (monotonic_ts, Proxy)
        self._cache: dict[str, tuple[float, Proxy]] = {}
        # (monotonic_ts, normalised balance dict)
        self._balance_cache: Optional[tuple[float, dict]] = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    # -- raw API ----------------------------------------------------------- #

    async def _call(
        self, method: str, path: str, body: Optional[dict] = None
    ) -> dict:
        if not self._api_key:
            raise AsocksError("ASOCKS_API_KEY is not set")
        qs = urllib.parse.urlencode({"apikey": self._api_key})
        url = f"{self._base}/{path}?{qs}"
        try:
            payload = await asyncio.to_thread(
                _blocking_call, url, method, body, self._timeout
            )
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise AsocksError(
                f"asocks {method} {path} -> HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AsocksError(f"asocks {method} {path} failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AsocksError(f"asocks {method} {path}: bad JSON response") from exc
        if isinstance(payload, dict) and payload.get("success") is False:
            raise AsocksError(f"asocks {path}: {payload.get('message') or 'error'}")
        return payload if isinstance(payload, dict) else {}

    async def balance(self) -> dict:
        """Account balance snapshot: money `balance` plus `balance_traffic`
        (bytes). Creating a port needs money balance even when traffic is left."""
        return await self._call("GET", "user/balance")

    async def balance_info(self) -> dict:
        """Normalised, briefly-cached balance for the admin panel:
        `{balance, currency, traffic_bytes}`. Cached for `asocks_balance_ttl_s`
        so polling the panel doesn't hammer the Asocks API. Values are `None`
        when the API omits them; raises AsocksError on a failed call."""
        now = time.monotonic()
        if self._balance_cache and (now - self._balance_cache[0]) < self._balance_ttl:
            return self._balance_cache[1]
        raw = await self.balance()
        # The API may nest the real payload under "message" (as proxy/ports does).
        body = raw.get("message") if isinstance(raw.get("message"), dict) else raw
        if not isinstance(body, dict):
            body = {}
        info = {
            "balance": _num(body.get("balance")),
            "currency": body.get("currency") or "USD",
            "traffic_bytes": _num(body.get("balance_traffic")),
        }
        self._balance_cache = (now, info)
        return info

    async def list_ports(self) -> list[dict]:
        data = await self._call("GET", "proxy/ports")
        return (data.get("message") or {}).get("proxies") or []

    async def create_port(
        self, country_code: str, name: Optional[str] = None
    ) -> None:
        # type_id / proxy_type_id / server_port_type_id are required by the API
        # (a missing one is a 422). Defaults yield a SOCKS5 port with auth.
        body: dict[str, Any] = {
            "country_code": country_code,
            "type_id": settings.asocks_type_id,
            "proxy_type_id": settings.asocks_proxy_type_id,
            "server_port_type_id": settings.asocks_server_port_type_id,
        }
        if name:
            body["name"] = name
        await self._call("POST", "proxy/create-port", body)

    # -- port selection ---------------------------------------------------- #

    @staticmethod
    def _matches_country(port: dict, country: Optional[str]) -> bool:
        if not country:
            return True
        return str(port.get("countryCode") or "").upper() == country.upper()

    def _pick(self, ports: list[dict], country: Optional[str]) -> Optional[dict]:
        """Choose a port matching the country, preferring active ones. Picks at
        random within that set so we don't keep hitting the same (possibly
        blocked) port across requests."""
        cands = [p for p in ports if self._matches_country(p, country)]
        if not cands:
            return None
        active = [p for p in cands if str(p.get("status")) in ("1", "active")]
        return random.choice(active or cands)

    @staticmethod
    def _to_upstream(port: dict, rotate: bool = False) -> Proxy:
        """The raw Asocks proxy: SOCKS5 with user/pass auth (see asocks-api
        notes). Not usable by Chrome directly — wrapped by the local bridge.
        With `rotate`, the login's session token is re-rolled for a new exit IP."""
        raw = str(port.get("proxy") or "")
        host, sep, port_no = raw.rpartition(":")
        if not sep or not host or not port_no:
            raise AsocksError(f"unexpected proxy format from asocks: {raw!r}")
        login = port.get("login")
        if rotate:
            login = _rotate_login(login)
        return Proxy(
            server=f"socks5://{host}:{port_no}",
            username=login,
            password=port.get("password"),
        )

    # -- public ------------------------------------------------------------ #

    async def acquire(
        self, country: Optional[str] = None, fresh: bool = False
    ) -> Proxy:
        """Return a ready-to-use Proxy for `country` (None = any available).

        Reuses a cached/existing port when possible; otherwise creates one and
        waits for it to come up. With `fresh=True` the cache is bypassed and the
        session token re-rolled, yielding a new exit IP — used to rotate away
        from an IP a target site has blocked. Raises AsocksError on
        misconfiguration or API failure."""
        key = (country or "").strip().upper()
        if not fresh:
            cached = self._fresh(key)
            if cached is not None:
                return cached

        async with self._lock:
            if not fresh:
                cached = self._fresh(key)  # re-check: another task may have filled it
                if cached is not None:
                    return cached

            ports = await self.list_ports()
            port = self._pick(ports, country)
            if port is None:
                if not country:
                    raise AsocksError(
                        "no asocks ports available; pass proxy_country to "
                        "create one (or create a port in the dashboard)"
                    )
                bus.emit("info", "system", "asocks", "creating proxy port", country)
                await self.create_port(country, name="browser-as-a-service")
                port = await self._await_new_port(country)

            upstream = self._to_upstream(port, rotate=fresh)
            # Chrome can't authenticate SOCKS5, so route it through a local
            # no-auth bridge that does the upstream user/pass handshake.
            local = await bridge_manager.local_proxy_for(upstream)
            if not fresh:
                self._cache[key] = (time.monotonic(), local)
            bus.emit(
                "success", "system", "asocks",
                "proxy rotated" if fresh else "proxy ready",
                f"{port.get('countryCode') or '?'} · {upstream.server} "
                f"via {local.server}",
            )
            return local

    def _fresh(self, key: str) -> Optional[Proxy]:
        hit = self._cache.get(key)
        if hit and (time.monotonic() - hit[0]) < self._pool_ttl:
            return hit[1]
        return None

    async def _await_new_port(
        self, country: str, attempts: int = 10, delay: float = 2.0
    ) -> dict:
        """Poll until a port for `country` shows up after a create."""
        for _ in range(attempts):
            await asyncio.sleep(delay)
            port = self._pick(await self.list_ports(), country)
            if port is not None:
                return port
        raise AsocksError(f"asocks port for {country} not ready after create")


client = AsocksClient(
    api_key=settings.asocks_api_key,
    base_url=settings.asocks_base_url,
    timeout_s=settings.asocks_timeout_s,
    pool_ttl_s=settings.asocks_pool_ttl_s,
    balance_ttl_s=settings.asocks_balance_ttl_s,
)
