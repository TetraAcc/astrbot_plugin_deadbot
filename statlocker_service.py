"""StatLocker-only reports: bounded queue, shared requests and validated disk cache."""
import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time


class StatLockerError(Exception):
    pass


def validate(data, match_id):
    if not isinstance(data, dict) or data.get("match_id") != str(match_id):
        raise ValueError("比赛不匹配")
    rows = data.get("players")
    if not isinstance(rows, list) or len(rows) != 12:
        raise ValueError("玩家数据不完整")
    accounts, ranks = set(), set()
    for p in rows:
        if not isinstance(p, dict):
            raise ValueError("玩家记录无效")
        account = p.get("account_id", "")
        if not re.fullmatch(r"[0-9]+", account) or account in accounts:
            raise ValueError("玩家身份异常")
        accounts.add(account)
        score = p.get("score")
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 100:
            raise ValueError("评分无效")
        rank = re.match(r"^\s*(\d+)", str(p.get("rank", "")))
        if not rank or not 1 <= int(rank[1]) <= 12 or int(rank[1]) in ranks:
            raise ValueError("排名无效")
        ranks.add(int(rank[1]))
        if p.get("grade") not in {"S+", "S", "S-", "A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "E+", "E", "E-", "F"}:
            raise ValueError("评级无效")
        parts = p.get("breakdown")
        if not isinstance(parts, list) or not 6 <= len(parts) <= 7 or len(set(parts)) != len(parts):
            raise ValueError("贡献明细不完整")
        totals = []
        categories = set()
        category_sum = 0
        for part in parts:
            category = part.splitlines()[0].strip().upper()
            if category not in CATEGORY_NAMES or category in categories:
                raise ValueError("贡献类别重复或无法识别")
            categories.add(category)
            total = re.search(r"Total Points:\s*(-?[\d,.]+)\s*pts", part)
            subtotal = re.search(r"CATEGORY TOTAL:\s*(-?[\d,.]+)\s*PTS", part, re.I)
            if not total or not subtotal:
                raise ValueError("贡献明细无效")
            totals.append(float(total[1].replace(",", "")))
            category_sum += float(subtotal[1].replace(",", ""))
        if max(totals) != min(totals):
            raise ValueError("悬浮明细来自不同玩家")
        if abs(category_sum - totals[0]) > .6:
            raise ValueError("贡献分项与总分不一致")
    datetime.fromisoformat(data["captured_at"])
    return data


class StatLockerService:
    def __init__(self, directory, python=None, fetch=None, ttl=86400, capacity=8, delay=3):
        self.directory = Path(directory)
        self.python = python or sys.executable
        self.fetch = fetch or self._fetch
        self.ttl, self.capacity, self.delay = ttl, capacity, delay
        self.pending = {}
        self.failed_until = {}
        self.gate = asyncio.Lock()
        self.next_start = 0
        self.closed = False

    def _read(self, mid):
        try:
            envelope = json.loads((self.directory / f"{mid}.json").read_text(encoding="utf-8"))
            if envelope.get("schema") != 1 or not 0 <= time.time() - envelope["stored_at"] < self.ttl:
                return None
            return validate(envelope["data"], mid)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    async def get(self, mid, account, refresh=False):
        mid, account = str(mid), str(account)
        if not re.fullmatch(r"[0-9]{1,15}", mid) or not re.fullmatch(r"[0-9]{1,10}", account):
            raise StatLockerError("比赛或账号编号无效。")
        if self.closed:
            raise StatLockerError("采集服务正在关闭，请稍后重试。")
        if not refresh:
            cached = await asyncio.to_thread(self._read, mid)
            if cached is not None:
                return cached, True
        task = self.pending.get(mid)
        if task is None:
            if len(self.pending) >= self.capacity:
                raise StatLockerError("采集队列已满，请稍后重试。")
            if time.monotonic() < self.failed_until.get(mid, 0):
                raise StatLockerError("该场刚刚采集失败，请一分钟后再试。")
            task = asyncio.create_task(self._job(mid, account))
            self.pending[mid] = task
            def done(finished):
                self.pending.pop(mid, None)
                if not finished.cancelled():
                    finished.exception()  # Consume errors if all clients disconnected.
            task.add_done_callback(done)
        return await asyncio.shield(task), False

    async def _job(self, mid, account):
        async with self.gate:
            if self.closed:
                raise StatLockerError("插件正在关闭，尚未开始的采集已取消。")
            await asyncio.sleep(max(0, self.next_start - time.monotonic()))
            try:
                data = validate(await self.fetch(mid, account), mid)
                await asyncio.to_thread(self._save, mid, data)
                return data
            except Exception as exc:
                now = time.monotonic()
                self.failed_until = {k: v for k, v in self.failed_until.items() if v > now}
                self.failed_until[mid] = now + 60
                if isinstance(exc, StatLockerError):
                    raise
                raise StatLockerError("StatLocker 数据不完整或缓存保存失败，本次无法提供评分。") from exc
            finally:
                self.next_start = time.monotonic() + self.delay

    def _save(self, mid, data):
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{mid}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(schema=1, stored_at=time.time(), data=data),
                                        ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)

    async def _fetch(self, mid, account):
        url = f"https://statlocker.gg/profile/{account}/matches/{mid}/mvp/mvp-breakdown"
        def run():
            try:
                result = subprocess.run([self.python, str(Path(__file__).with_name("statlocker_worker.py")), url],
                    capture_output=True, text=True, encoding="utf-8", timeout=150,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                reply = json.loads(result.stdout)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                raise StatLockerError("采集进程启动失败或超时，请检查浏览器和 Python 配置。") from exc
            if not reply.get("ok"):
                if reply.get("kind") == "ModuleNotFoundError":
                    raise StatLockerError("采集环境未安装 Playwright，请按说明配置后重试。")
                raise StatLockerError("StatLocker 页面采集失败：可能尚未收录、访问受限或页面结构变化，请稍后重试。")
            return reply["data"]
        return await asyncio.to_thread(run)

    async def close(self):
        self.closed = True
        # Allow the bounded worker to close its browser rather than abandon it.
        if self.pending:
            await asyncio.gather(*list(self.pending.values()), return_exceptions=True)


CATEGORY_NAMES = {"DAMAGE DEALER": "输出", "EARLY IMPACT": "前期", "PUSHER": "推进",
                  "TANK": "承伤", "SUPPORT": "辅助", "TEAM FARM BONUS": "经济奖励", "DEATHS": "死亡扣分"}


def render(data, account, cached):
    player = next((p for p in data["players"] if p["account_id"] == str(account)), None)
    if player is None:
        return "本场没有查询账号，请核对绑定或比赛编号；不会替换为其他玩家的评分。"
    name = str(player["name"]).replace("\n", " ")[:60]
    rank = re.match(r"^\s*(\d+)", player["rank"])[1]
    lines = [f"StatLocker 战报 · {data['match_id']}",
             f"{name}｜{player['hero']}｜账号 {account}",
             f"评分 {player['score']:.1f} / {player['grade']}｜全场第 {rank} / 12",
             "贡献明细（原站贡献点，不是百分制评分）："]
    for part in player["breakdown"]:
        title = part.splitlines()[0].strip().upper()
        total = re.search(r"CATEGORY TOTAL:\s*(-?[\d,.]+)\s*PTS", part, re.I)
        lines.append(f"{CATEGORY_NAMES.get(title, title)}：{total[1]}")
    lines += [f"数据来源：StatLocker 网页｜{'缓存' if cached else '本次采集'}",
              f"采集时间：{data['captured_at']}（UTC）", "评分、评级和排名直接取自原站，未使用本地评分。"]
    return "\n".join(lines)
