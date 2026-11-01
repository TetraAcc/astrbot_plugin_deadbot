"""Optional research utility; reads rendered pages, never private endpoints.

Install separately: python -m pip install playwright
Uses installed Microsoft Edge and a dedicated profile, never the daily profile.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re


# This script can be run directly without importing AstrBot.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from statlocker_browser import match_url, collect


async def run(args):
    from playwright.async_api import async_playwright
    args.output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(args.profile), channel="msedge", headless=args.headless,
            viewport={"width": 1920, "height": 2160})
        try:
            page = await context.new_page()
            if args.login:
                await page.goto("https://statlocker.gg/", wait_until="domcontentloaded")
                await asyncio.to_thread(input, "请在独立窗口手动登录，完成后回车；不需要把密码交给脚本。")
            for url in dict.fromkeys(args.urls):
                mid = url.split("/matches/")[1].split("/")[0]
                target = args.output / f"{mid}.json"
                if target.exists():
                    print(f"{mid}: 已采集，跳过")
                    continue
                try:
                    data = await asyncio.wait_for(collect(page, url), timeout=110)
                    await page.screenshot(path=str(args.output / f"{mid}-verified.png"))
                    temporary = target.with_suffix(".json.tmp")
                    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    temporary.replace(target)
                    print(f"{mid}: 保存12人评分及悬浮明细")
                except Exception as exc:
                    print(f"{mid}: 未完成，停止队列：{type(exc).__name__}: {exc}")
                    await page.screenshot(path=str(args.output / f"{mid}-error.png"))
                    raise  # Never bypass login, access checks or repeat failed requests.
                await asyncio.sleep(5)
        finally:
            await context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", type=match_url, nargs="*")
    parser.add_argument("--profile", type=Path, default=Path.home() / ".deadbot-collector-profile")
    parser.add_argument("--output", type=Path, default=Path("statlocker-samples"))
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    if args.login and args.headless:
        parser.error("首次登录需要可见窗口，请勿同时使用 --headless")
    if not args.urls and not args.login:
        parser.error("请提供比赛链接或使用 --login")
    asyncio.run(run(args))
