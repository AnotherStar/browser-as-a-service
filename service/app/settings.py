"""Runtime configuration, sourced from environment variables."""
from __future__ import annotations

import os
import pathlib


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
    # Browser language.
    lang: str = os.environ.get("BROWSER_LANG", "ru-RU")
    # URL visited once right after a browser launches, to acquire anti-bot
    # session cookies before any "cold" product hit. Empty disables it.
    warmup_url: str = os.environ.get("WARMUP_URL", "https://www.ozon.ru/")
    warmup_settle_s: float = float(os.environ.get("WARMUP_SETTLE_S", "4.0"))
    # Per-run hard timeout.
    run_timeout_s: float = float(os.environ.get("RUN_TIMEOUT_S", "90"))


settings = Settings()
