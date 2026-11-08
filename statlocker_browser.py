"""Shared visible-page collector; no undocumented endpoints or local scoring."""
import argparse
import asyncio
import time
from datetime import datetime, timezone
import re


async def setup_route_blocking(page):
    if not hasattr(page, "route"):
        return
    blocked_types = {"image", "media", "font"}
    ad_keywords = ("eyeota", "carbon-cdn", "hadron", "amazon-adsystem", "googletagmanager",
                   "confiant", "doubleclick", "adnxs", "criteo", "rubiconproject")

    async def handle_route(route):
        try:
            req = route.request
            if req.resource_type in blocked_types:
                await route.abort()
                return
            url_lower = req.url.lower()
            if any(k in url_lower for k in ad_keywords):
                await route.abort()
                return
            await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    try:
        await page.route("**/*", handle_route)
    except Exception:
        pass


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
    await setup_route_blocking(page)
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
    farm = {}
    farm_error = None
    try:
        farm = await collect_farm(page, url, result)
    except Exception:
        farm_error = "资源占用暂未获取"
    highlights = {}
    highlight_errors = {}
    focus = url.split('/profile/')[1].split('/')[0]
    focus_player = next((p for p in result if p['account_id'] == focus), None)
    if focus_player:
        try:
            highlights[focus] = await asyncio.wait_for(
                collect_highlights(page, url, focus_player), timeout=35)
        except Exception as exc:
            highlight_errors[focus] = type(exc).__name__
    return dict(highlight_errors=highlight_errors, highlights=highlights, farm_impact=farm, farm_error=farm_error, source=url, match_id=url.split("/matches/")[1].split("/")[0],
                captured_at=datetime.now(timezone.utc).isoformat(),
                method="visible DOM and hovered contribution tooltips", players=result)


async def collect_farm(page, url, players):
    farm_url = url.rsplit('/', 1)[0] + '/farm-impact'
    await page.goto(farm_url, wait_until='domcontentloaded', timeout=20000)
    await page.locator('.fiv-p-row').first.wait_for(state='visible', timeout=15000)
    if page.url.split('?')[0].rstrip('/') != farm_url:
        raise ValueError('资源页比赛地址不匹配')
    result = {}
    expected = {p['account_id']:p for p in players}
    async def read_rows():
        for row in await page.locator('.fiv-p-row').all():
            href = await row.locator('.fiv-p-link').get_attribute('href')
            acc = href.rsplit('/', 1)[-1]
            if acc not in expected:
                raise ValueError('资源页账号不匹配')
            tag = (await row.locator('.fiv-p-farm-tag').inner_text()).upper()
            label = next((v for v in ['HIGHEST FARM','HIGH FARM','MEDIUM FARM','LOW FARM'] if tag.startswith(v)), None)
            grades = await row.locator('.tier-badge').all_text_contents()
            scores = await row.locator('.fiv-p-lift-score').all_text_contents()
            if not label or len(grades)!=2 or len(scores)!=2:
                raise ValueError('资源页结构变化')
            if float(scores[1]) != expected[acc]['score'] or grades[1].strip()!=expected[acc]['grade']:
                raise ValueError('资源页评分不一致')
            bonus_el = row.locator('.fiv-p-lift-bonus')
            bonus = (await bonus_el.inner_text()).strip() if await bonus_el.count() else '0'
            if bonus in ('—','–'): bonus='0'
            if not re.fullmatch(r'\+?\d+(?:\.\d+)?', bonus):
                raise ValueError('奖励值不明确')
            result[acc] = dict(label=label, farm=(await row.locator('.fiv-p-farm-amt').inner_text()).strip(),
                before_grade=grades[0].strip(), before_score=float(scores[0]), bonus=bonus,
                score=float(scores[1]), grade=grades[1].strip())
    await read_rows()
    if len(result)==6:
        for team in ['Hidden King','Archmother']:
            button=page.get_by_role('button',name=team,exact=True)
            if await button.count():
                await button.click()
                await read_rows()
    if set(result)!=set(expected):
        raise ValueError('资源页玩家不完整')
    return result


async def collect_highlights(page, url, player):
    mid = url.split('/matches/')[1].split('/')[0]
    target = f"https://statlocker.gg/profile/{player['account_id']}/matches/{mid}/player-stats/highlights"
    await page.goto(target, wait_until='domcontentloaded', timeout=15000)
    await page.locator('.match-hub-profile-button').wait_for(state='attached', timeout=8000)
    await page.locator('.match-hub-tile').first.wait_for(state='attached', timeout=8000)
    if page.url.split('?')[0].rstrip('/') != target:
        raise ValueError('亮点比赛地址不匹配')
    profile = await page.locator('.match-hub-profile-button').get_attribute('href')
    if profile != '/profile/' + player['account_id']:
        raise ValueError('亮点账号不匹配')
    cards = await wait_highlight_cards(page)
    total = page.locator('.mh-mvpbuild-total-value')
    score = float(await total.inner_text()) if await total.count() else None
    if score is not None and score != player['score']:
        raise ValueError('亮点评分不匹配')
    components = {}
    for row in await page.locator('.mh-mvpbuild-row[data-category]').all():
        category = (await row.get_attribute('data-category')).upper()
        if category in components:
            raise ValueError('重复评分分项')
        components[category] = float((await row.locator('.mh-mvpbuild-points').text_content()).replace('−','-'))
    component_source = 'page_card' if components else 'stat_display_formula'
    if not components:
        components = stat_display_components(player)
    facts = [dict(title=c['title'], value=c['value'], description=c['description'], card_id=c['id'])
             for c in cards if c['id'] != 'mvp-build']
    return dict(account_id=player['account_id'], match_id=mid, score=score, components_status='available' if components else 'not_shown',
                component_source=component_source, components=components, facts=facts, cards=cards,
                cards_status='settled', source=target)


def stat_display_components(player):
    """Stat's public MVP-card display normalization; never a replacement score.

    Source tooltips round points, so these are explicitly marked as converted,
    not falsely reported as values directly read from the score card.
    """
    totals, components = [], {}
    for part in player['breakdown']:
        category = part.splitlines()[0].strip().upper()
        total = re.search(r'Total Points:\s*(-?[\d,.]+)\s*pts', part)
        subtotal = re.search(r'CATEGORY TOTAL:\s*(-?[\d,.]+)\s*PTS', part, re.I)
        if not total or not subtotal or category in components:
            raise ValueError('原站分项不完整')
        totals.append(float(total[1].replace(',', '')))
        components[category] = float(subtotal[1].replace(',', ''))
    if not totals or len(set(totals)) != 1 or totals[0] <= 0 or abs(sum(components.values()) - totals[0]) > .6:
        raise ValueError('原站 points 总计不一致')
    return {k:round(v * player['score'] / totals[0], 1) for k,v in components.items()}


async def read_highlight_cards(page):
    return await page.locator('.match-hub-tile').evaluate_all(r'''tiles => tiles.map(tile => {
        const clean = el => {
            if (!el) return '';
            const clone = el.cloneNode(true);
            clone.querySelectorAll('[aria-hidden="true"],script,style').forEach(x=>x.remove());
            clone.querySelectorAll('img[alt]').forEach(x=>x.replaceWith(document.createTextNode(' '+x.alt+' ')));
            clone.querySelectorAll('[role="img"][aria-label]').forEach(x=>x.replaceWith(document.createTextNode(' '+x.getAttribute('aria-label')+' ')));
            const walker=document.createTreeWalker(clone,NodeFilter.SHOW_TEXT);
            const parts=[]; let node;
            while (node=walker.nextNode()) parts.push(node.textContent);
            return parts.join(' ').replace(/\s+/g,' ').trim();
        };
        const outer=tile.closest('[data-track-seen]');
        const id=(outer?.getAttribute('data-track-seen') || '').replace('match.hub.highlight.','');
        const description=[...tile.querySelectorAll('.mh-fact-sentence,.mh-fact-detail')].map(clean).join(' ');
        return {id, title:clean(tile.querySelector('h2')), value:clean(tile.querySelector('.mh-fact-number')),
                description:description || clean(tile.querySelector('.match-hub-tile-body'))};
    })''')


async def wait_highlight_cards(page):
    # The first card renders before asynchronous facts and layout selection settle.
    # Compare complete accessible card content, excluding animated duplicate digits.
    start = time.monotonic()
    previous, unchanged = None, 0
    while time.monotonic() - start < 22:
        cards = await read_highlight_cards(page)
        if cards and all(c['id'] and c['title'] and c['description'] for c in cards) and cards == previous:
            unchanged += 1
        else:
            unchanged = 0
        if time.monotonic() - start >= 12 and unchanged >= 4:
            return cards
        previous = cards
        await asyncio.sleep(.75)
    raise TimeoutError('亮点卡片仍在变化或内容不完整，不缓存不完整结果')
