"""Smoke test: launch Chrome via zendriver, open Ozon, check for anti-bot wall."""
import asyncio
import sys
import zendriver as uc


async def main(url: str, headless: bool):
    browser = await uc.start(
        headless=headless,
        browser_executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        browser_args=["--lang=ru-RU"],
    )
    try:
        tab = await browser.get(url)
        await tab.sleep(4)  # let JS settle
        title = await tab.evaluate("document.title")
        body_text = await tab.evaluate(
            "document.body ? document.body.innerText.slice(0, 600) : '<no body>'"
        )
        await tab.save_screenshot("smoke.png")
        print("=== TITLE ===")
        print(title)
        print("=== BODY (first 600 chars) ===")
        print(body_text)
        # crude anti-bot detection
        markers = ["Доступ ограничен", "Antibot", "проверк", "captcha", "Ой!", "робот"]
        hit = [m for m in markers if m.lower() in (str(body_text) + str(title)).lower()]
        print("=== ANTIBOT MARKERS HIT ===", hit or "none")
    finally:
        browser.stop()


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.ozon.ru/"
    headless = "--headless" in sys.argv
    asyncio.run(main(url, headless))
