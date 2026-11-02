"""Isolated browser worker, usable from Windows AstrBot's event loop."""
import asyncio
import json
import subprocess
import sys
from statlocker_browser import collect, match_url


async def launch_browser(pw):
    if sys.platform == "win32":
        try:
            return await pw.chromium.launch(channel="msedge", headless=True)
        except Exception:
            pass
    try:
        return await pw.chromium.launch(headless=True)
    except Exception as exc:
        err_str = str(exc).lower()
        if "executable doesn't exist" in err_str or "playwright install" in err_str:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                           capture_output=True, timeout=300)
            return await pw.chromium.launch(headless=True)
        raise


async def run(url):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await launch_browser(pw)
        try:
            page = await browser.new_page(viewport={"width": 1920, "height": 2160})
            return await asyncio.wait_for(collect(page, url), timeout=110)
        finally:
            await browser.close()


if __name__ == "__main__":
    try:
        result = asyncio.run(run(match_url(sys.argv[1])))
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({"ok": False, "kind": type(exc).__name__}, ensure_ascii=True))
        sys.exit(1)
