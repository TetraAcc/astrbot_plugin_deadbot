"""Shared visible-page collector; no undocumented endpoints or local scoring."""
import argparse
from datetime import datetime, timezone
import re


async def close_ad(page):
    # Only close the observed sticky advertisement, never the match dialog.
    close = page.locator(".desktop-horizontal-sticky-slot").get_by_role("button", name="Close", exact=True)
    if await close.count() and await close.first.is_visible():
        await close.first.click(timeout=3000)


def match_url(value):
    if not re.fullmatch(r"https://statlocker\.gg/profile/[0-9]+/matches/[0-9]+/mvp/mvp-breakdown", value):
        raise argparse.ArgumentTypeError("需要完整 StatLocker 单局 MVP Breakdown 链接")
    return value


async def collect(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    rows = page.locator(".player-row")
    await rows.first.wait_for(state="visible", timeout=30000)
    count = await rows.count()
    if count != 12:
        raise ValueError(f"预期12位玩家，实际{count}；不保存不完整比赛")
    if page.url.split("?", 1)[0].rstrip("/") != url.rstrip("/"):
        raise ValueError("网页跳转到其他地址，未确认目标比赛")
    await close_ad(page)
    result = []
    seen = set()
    for index in range(count):
        await close_ad(page)
        row = rows.nth(index)
        link = row.locator(".player-name")
        href = await link.get_attribute("href")
        if not href or not re.fullmatch(r"/profile/[0-9]+", href):
            raise ValueError("玩家身份格式变化")
        account = href.rsplit("/", 1)[-1]
        if account in seen:
            raise ValueError("重复玩家，停止本场采集")
        seen.add(account)
        score = float(await row.locator(".total-score").inner_text())
        if not 0 <= score <= 100:
            raise ValueError("评分超出预期范围")
        record = dict(account_id=account, name=await link.inner_text(),
                      hero=await row.locator(".hero-icon").get_attribute("alt"),
                      rank=await row.locator(".rank-badge").inner_text(),
                      grade=await row.locator(".tier-badge").inner_text(), score=score,
                      breakdown=[])
        segments = row.locator(".bar-segment")
        segment_count = await segments.count()
        if not 6 <= segment_count <= 7:
            raise ValueError("贡献条结构变化，需人工检查")
        for segment in await segments.all():
            await close_ad(page)
            await segment.hover(timeout=10000)
            tooltip = page.locator(".mvp-breakdown-tooltip")
            await tooltip.wait_for(state="visible", timeout=5000)
            text = await tooltip.inner_text()
            if "Total Points:" not in text or "CATEGORY TOTAL:" not in text:
                raise ValueError("悬浮明细未完整显示")
            record["breakdown"].append(text)
        if len(set(record["breakdown"])) != segment_count:
            raise ValueError("悬浮框重复，可能未更新；不接受该场数据")
        result.append(record)
    return dict(source=url, match_id=url.split("/matches/")[1].split("/")[0],
                captured_at=datetime.now(timezone.utc).isoformat(),
                method="visible DOM and hovered contribution tooltips", players=result)

