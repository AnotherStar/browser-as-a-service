"""Runtime configuration, sourced from environment variables."""
from __future__ import annotations

import os
import pathlib

# Load the repo-root .env before reading any settings. The service is started
# from `service/`, but `.env` lives at the project root, so a bare `load_dotenv`
# (which searches the CWD) would miss it — we point at the root explicitly.
# Real environment variables still win (override=False), so deployments can
# override without editing the file. Degrades gracefully if python-dotenv is
# absent.
try:
    from dotenv import load_dotenv

    load_dotenv(pathlib.Path(__file__).resolve().parents[2] / ".env")
except ImportError:  # pragma: no cover - optional dependency
    pass


def _detect_chrome() -> str | None:
    """Find a real Chrome/Chromium. We deliberately avoid the homebrew
    `chromium` symlink, which is often unsigned/broken on macOS."""
    env = os.environ.get("CHROME_PATH")
    if env:
        return env
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for c in candidates:
        if pathlib.Path(c).exists():
            return c
    return None


class Settings:
    chrome_path: str | None = _detect_chrome()
    # Max concurrent browser operations. Anti-ban favours a low number.
    max_concurrency: int = int(os.environ.get("MAX_CONCURRENCY", "1"))
    # Minimum seconds between consecutive page loads (politeness / anti-ban).
    min_interval_s: float = float(os.environ.get("MIN_INTERVAL_S", "2.0"))
    # Extra random jitter (0..jitter) added to the interval.
    jitter_s: float = float(os.environ.get("JITTER_S", "1.5"))
    # Default headless mode (Ozon requires headful -> False).
    default_headless: bool = os.environ.get("HEADLESS", "0") == "1"
    # Ozon price cache TTL.
    price_cache_ttl_s: float = float(os.environ.get("PRICE_CACHE_TTL_S", "900"))
    # Browser language / locale (drives navigator.language(s) + Intl).
    lang: str = os.environ.get("BROWSER_LANG", "ru-RU")
    accept_lang: str = os.environ.get("ACCEPT_LANG", "ru-RU,ru;q=0.9,en;q=0.8")
    # Timezone the browser reports (CDP override). A headless Linux box defaults
    # to UTC, which on a Russian site + Russian proxy IP reads as a bot.
    browser_timezone: str = os.environ.get("BROWSER_TIMEZONE", "Europe/Moscow")
    # URL visited once right after a browser launches, to acquire anti-bot
    # session cookies before any "cold" product hit. Empty disables it.
    warmup_url: str = os.environ.get("WARMUP_URL", "https://www.ozon.ru/")
    warmup_settle_s: float = float(os.environ.get("WARMUP_SETTLE_S", "4.0"))
    # Per-run hard timeout.
    run_timeout_s: float = float(os.environ.get("RUN_TIMEOUT_S", "90"))
    # Max attempts for an Ozon scrape behind an Asocks proxy. On an antibot wall
    # (IP block or captcha) the service rotates to a fresh proxy session (a new
    # exit IP) and retries, up to this many times. 1 disables rotation/retry.
    # Each extra attempt spends proxy traffic, so keep it low.
    scrape_max_attempts: int = int(os.environ.get("SCRAPE_MAX_ATTEMPTS", "2"))

    # -- Asocks proxy provider (https://api.asocks.com/v2) ------------------ #
    # API key. When set, callers can opt a request into a residential proxy
    # with `use_proxy: true` instead of supplying a `proxy` by hand.
    asocks_api_key: str | None = os.environ.get("ASOCKS_API_KEY")
    asocks_base_url: str = os.environ.get(
        "ASOCKS_BASE_URL", "https://api.asocks.com/v2"
    )
    # Per-call HTTP timeout against the Asocks API.
    asocks_timeout_s: float = float(os.environ.get("ASOCKS_TIMEOUT_S", "30"))
    # How long a resolved proxy is reused before we re-query the API. Keeps a
    # burst of requests from hammering the API or spawning duplicate ports.
    asocks_pool_ttl_s: float = float(os.environ.get("ASOCKS_POOL_TTL_S", "300"))
    # How long a fetched balance/traffic snapshot is reused. The admin panel
    # polls it, so this caps how often the Asocks API is hit for it.
    asocks_balance_ttl_s: float = float(os.environ.get("ASOCKS_BALANCE_TTL_S", "60"))
    # create-port required fields (a missing one is a 422). Defaults mirror
    # Asocks' official PHP example. The gateway speaks HTTP and SOCKS5 on the
    # same port; we use it as an HTTP proxy so Chrome can authenticate natively.
    # Exposed as env in case a plan needs different ids.
    asocks_type_id: int = int(os.environ.get("ASOCKS_TYPE_ID", "1"))
    asocks_proxy_type_id: int = int(os.environ.get("ASOCKS_PROXY_TYPE_ID", "2"))
    asocks_server_port_type_id: int = int(
        os.environ.get("ASOCKS_SERVER_PORT_TYPE_ID", "1")
    )


settings = Settings()
