"""Refresh one player's history, with bounded load and shared in-flight work."""
import asyncio
import time
from contextlib import asynccontextmanager

import aiohttp


@asynccontextmanager
async def report_slot(lock):
    """Let a concurrent query reuse the first query's cached detail."""
    try:
        await asyncio.wait_for(lock.acquire(), timeout=30)
    except TimeoutError:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


class LatestHistory:
    def __init__(self, library):
        self.library = library
        self.pending = {}
        self.retry_at = {}
        self.global_retry_at = 0
        self.gate = asyncio.Semaphore(3)

    async def refresh(self, account):
        task = self.pending.get(account)
        if task is None:
            if len(self.pending) >= 30:
                return "查询人数较多，暂用本地记录，请稍后重试。"
            task = asyncio.create_task(self._refresh(account))
            self.pending[account] = task
            task.add_done_callback(lambda done: self.pending.pop(account, None))
        return await asyncio.shield(task)

    async def _refresh(self, account):
        async with self.gate:
            last = await asyncio.to_thread(self.library.last_sync, account)
            if 0 <= time.time() - last < 120:
                return "历史已在 2 分钟内更新，本次复用缓存。"
            now = time.monotonic()
            self.retry_at = {a: t for a, t in self.retry_at.items() if t > now}
            if now < max(self.global_retry_at, self.retry_at.get(account, 0)):
                return "接口冷却中，暂用本地记录。"
            self.retry_at[account] = now + 60
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                    async with session.get(
                        f"https://api.deadlock-api.com/v1/players/{account}/match-history",
                        allow_redirects=False,
                    ) as response:
                        if response.status == 429:
                            self.global_retry_at = time.monotonic() + 60
                        if response.status != 200:
                            return f"历史刷新失败（HTTP {response.status}），暂用本地记录。"
                        payload = await response.json()
                await asyncio.to_thread(self.library.import_history, account, payload)
                return "已更新接口收录的历史；正在进行或尚未收录的比赛不会显示。"
            except (TimeoutError, aiohttp.ClientError, ValueError):
                return "历史刷新失败，暂用本地记录；请稍后重试。"
