"""StatLocker-only reports: bounded queue, shared requests and validated disk cache."""
import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
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
    farm = data.get('farm_impact', {})
    if not isinstance(farm, dict):
        raise ValueError('资源占用格式错误')
    if farm:
        if set(farm) != accounts:
            raise ValueError('资源占用账号不完整')
        for player in rows:
            item = farm[player['account_id']]
            if not isinstance(item, dict) or item.get('label') not in {'LOW FARM','MEDIUM FARM','HIGH FARM','HIGHEST FARM'}:
                raise ValueError('资源标签无效')
            if item.get('score') != player['score'] or item.get('grade') != player['grade']:
                raise ValueError('资源评分不匹配')
            if not re.fullmatch(r'\+?\d+(?:\.\d+)?', str(item.get('bonus',''))):
                raise ValueError('资源奖励无效')
            if not re.fullmatch(r'\d+(?:\.\d+)?[kKmM]?', str(item.get('farm',''))):
                raise ValueError('资源数量无效')
            if not isinstance(item.get('before_grade'),str):
                raise ValueError('资源评级缺失')
    for account, item in data.get('highlights', {}).items():
        player = next((p for p in rows if p['account_id'] == account), None)
        if not player or item.get('account_id') != account or item.get('match_id') != str(match_id) or (item.get('score') is not None and item.get('score') != player['score']):
            raise ValueError('亮点身份或评分不匹配')
        comp = item.get('components', {})
        required = set(CATEGORY_NAMES) - {'TEAM FARM BONUS'}
        if (comp and not required <= set(comp)) or set(comp) - set(CATEGORY_NAMES) or (not comp and item.get('components_status') != 'not_shown'):
            raise ValueError('亮点评分分项不完整')
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in comp.values()) or (comp and abs(sum(comp.values()) - player['score']) > .5):
            raise ValueError('亮点评分分项不一致')
        if not isinstance(item.get('facts'), list) or any(not isinstance(f,dict) or any(not isinstance(f.get(k),str) for k in ('title','value','description')) for f in item['facts']):
            raise ValueError('亮点格式无效')
        if 'cards' in item:
            cards = item['cards']
            if not isinstance(cards, list) or not cards or any(not isinstance(c, dict) or any(not isinstance(c.get(k), str) or not c[k] for k in ('id','title','description')) for c in cards):
                raise ValueError('亮点卡片不完整')
            ids = [c['id'] for c in cards]
            if len(ids) != len(set(ids)) or item.get('cards_status') != 'settled':
                raise ValueError('亮点卡片未稳定或重复')
            expected = [dict(title=c['title'], value=c['value'], description=c['description'], card_id=c['id']) for c in cards if c['id'] != 'mvp-build']
            if item['facts'] != expected:
                raise ValueError('亮点卡片有漏采或串位')
    datetime.fromisoformat(data["captured_at"])
    return data


class StatLockerService:
    @staticmethod
    def _find_python():
        candidates = []
        if sys.executable:
            candidates.append(sys.executable)
        for kv in [
            os.path.expanduser("~/.local/share/webclass-sync/venv/bin/python"),
            os.path.expanduser("~/.astrbot/data/venv/bin/python"),
            os.path.expanduser("~/.local/share/astrbot/venv/bin/python"),
            os.path.expanduser("~/venv/bin/python"),
            os.path.expanduser("~/.venv/bin/python"),
        ]:
            if os.path.exists(kv) and kv not in candidates:
                candidates.append(kv)
        for name in ["python3", "python"]:
            w = shutil.which(name)
            if w and w not in candidates:
                candidates.append(w)
        for py in candidates:
            try:
                # 纯净隔离测试：验证解释器是否能正常独立导入核心库与 playwright，杜绝污染环境
                clean_env = os.environ.copy()
                clean_env.pop("PYTHONPATH", None)
                r = subprocess.run(
                    [py, "-c", "import re, json, asyncio; import playwright"],
                    capture_output=True, timeout=5, env=clean_env
                )
                if r.returncode == 0:
                    return py
            except Exception:
                pass
        return candidates[0] if candidates else sys.executable

    def __init__(self, directory, python=None, fetch=None, ttl=86400, capacity=8, delay=3):
        self.directory = Path(directory)
        self.python = python or self._find_python()
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
            if envelope.get("schema") != 5 or not 0 <= time.time() - envelope["stored_at"] < self.ttl:
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
            if cached is not None and ('highlights' not in cached or account in cached['highlights'] or account not in {p['account_id'] for p in cached['players']}):
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
                old = await asyncio.to_thread(self._read, mid)
                if old and 'highlights' in data:
                    scores = {p['account_id']:p['score'] for p in data['players']}
                    old_scores = {p['account_id']:p['score'] for p in old['players']}
                    if scores == old_scores:
                        data['highlights'] = {**old.get('highlights', {}), **data['highlights']}
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
        temporary.write_text(json.dumps(dict(schema=5, stored_at=time.time(), data=data),
                                        ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)

    async def _fetch(self, mid, account):
        url = f"https://statlocker.gg/profile/{account}/matches/{mid}/mvp/mvp-breakdown"
        def run():
            try:
                env = os.environ.copy()
                is_same_interpreter = False
                try:
                    is_same_interpreter = os.path.samefile(self.python, sys.executable)
                except Exception:
                    is_same_interpreter = (self.python == sys.executable)

                if is_same_interpreter:
                    pythonpath_parts = []
                    user_site = os.path.expanduser("~/.astrbot/data/site-packages")
                    if os.path.exists(user_site):
                        pythonpath_parts.append(user_site)
                    if env.get("PYTHONPATH"):
                        pythonpath_parts.append(env["PYTHONPATH"])
                    if pythonpath_parts:
                        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
                else:
                    # 跨解释器彻底隔离：显式清除父进程残留的 PYTHONPATH，
                    # 严防主程序不同版本的标准库/C扩展倒灌进子进程引发 SRE module mismatch
                    env.pop("PYTHONPATH", None)

                result = subprocess.run([self.python, str(Path(__file__).with_name("statlocker_worker.py")), url],
                    capture_output=True, text=True, encoding="utf-8", timeout=100,
                    env=env,
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

HEROES_ZH = {}
try:
    _hpath = Path(__file__).with_name("heroes_zh.json")
    if _hpath.exists():
        HEROES_ZH = json.loads(_hpath.read_text(encoding="utf-8"))
except Exception:
    pass


def hero_name(hid):
    return HEROES_ZH.get(str(hid), f"英雄（ID {hid}）")


def display_num(value):
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return f"{value:,.0f}" if int(value) == value else f"{value:,.1f}"
    return "未知"


def contribution_shares(data, player):
    categories = ["DAMAGE DEALER", "PUSHER", "SUPPORT", "EARLY IMPACT", "TANK"]
    def points(p, cat):
        for part in p.get("breakdown", []):
            if part.splitlines()[0].strip().upper() == cat:
                m = re.search(r"CATEGORY TOTAL:\s*(-?[\d,.]+)", part, re.I)
                return float(m[1].replace(",", "")) if m else None
        return None
    items = []
    for cat in categories:
        vals = [points(p, cat) for p in data["players"]]
        value = points(player, cat)
        if len(vals) != 12 or any(v is None or v < 0 for v in vals):
            text = "未知"
        elif sum(vals) == 0:
            text = "无贡献"
        else:
            text = f"{value / sum(vals):.1%}"
        items.append(CATEGORY_NAMES[cat] + text)
    return [" · ".join(items[:3]), " · ".join(items[3:])]


def farm_amount(text):
    if text.lower().endswith('k'):
        return f"{float(text[:-1])/10:g}万"
    if text.lower().endswith('m'):
        return f"{float(text[:-1])*100:g}万"
    return text


def format_highlight(fact):
    title = fact['title'].strip().lower()
    titles = {'most low-hp kills':'低血量击杀最多', 'top healing duo':'最高治疗搭档',
              'killed every enemy':'击杀过所有敌方英雄', 'souls/min vs usual':'每分钟灵魂对比平时',
              'damage traded':'伤害交换', 'most ally healing':'队友治疗最多', 'accuracy vs usual':'命中率对比平时', 'mvp lead over next':'领先第二名'}
    value = fact['value']
    desc = fact['description']
    titles.update({'lobby 1st places':'全场第一项', 'elite pair':'社区对比',
                   'kills vs assists':'击杀与助攻', 'damaged most':'伤害往来',
                   'head to head':'相邻评分对比', 'kill types':'击杀类型'})
    if title == 'lobby 1st places':
        m = re.fullmatch(r'(\d+) stats led (.*?) Ally healing ([\d.k]+) Accuracy ([\d.]+)% CC score ([\d.k]+)', desc)
        if m:
            return f'🥇 全场第一项：{m[1]}项——队友治疗 {m[3]}、命中率 {m[4]}%、控制分 {m[5]}'
    if title == 'elite pair':
        m = re.fullmatch(r'Top ([\d.]+)% Denies (\d+) You ([\d.]+) Avg Top ([\d.]+)% Crowd Control ([\d.k]+) You ([\d.k]+) Avg', desc)
        if m:
            return f'✨ 原站社区对比：反补前 {m[1]}%（{m[2]}次），控制前 {m[4]}%（{m[5]}）'
    # Translate confirmed website prose only; never infer awards from statistics.
    if title == 'killed every enemy':
        return f"🎯 {titles[title]}：{value}（每位至少一次）"
    if title == 'most low-hp kills':
        m = re.search(r'of their (\d+) kills came while they were on (\d+)% health or less',desc)
        if m:
            return f"🔥 {titles[title]}：{m[1]}次击杀中，{value}次发生在自身血量≤{m[2]}%时"
    if title == 'top healing duo':
        m = re.search(r'healing to them from (.+?), (\d+)% of the healing they got from teammates',desc)
        if m:
            hero = {'Nurse Harrow':'护士'}.get(m[1],m[1])
            return f"💚 {titles[title]}：{hero}提供约{value}治疗，占队友治疗的{m[2]}%"
    if title == 'most ally healing':
        m = re.search(r'(\d+)% of their healing went to allies. Next most: (.+?), ([\d.k]+)',desc)
        if m:
            hero = {'McGinnis':'麦金尼斯'}.get(m[2],m[2])
            return f"💚 队友治疗最多：约{value}；{m[1]}%治疗用于队友，第二名{hero}约{m[3]}"
        return f"💚 队友治疗最多：约{value}，全场最高；{desc}"
    if title == 'accuracy vs usual':
        m = re.search(r'(.+?) accuracy this game, roughly (.+?)x their average of (.+?) Their average is over (\d+) games', desc)
        if m:
            return f"🎯 命中率：{m[1]}，约为平时{m[3]}的{m[2]}倍（原站{m[4]}场样本）"
    if title == 'souls/min vs usual':
        m = re.search(r'(.+?) souls per minute this game, roughly (.+?)x their average of (.+?) Their average is over (\d+) games',desc)
        if m:
            return f"🌱 每分钟灵魂：{m[1]}，约为平时{m[3]}的{m[2]}倍（原站{m[4]}场样本）"
    return f"• {titles.get(title, fact['title'])}：" + (value + '；' if value else '') + desc


def render(data, account, cached, raw_match=None, roster=None, stat_error=None):
    roster = roster or {}
    account = str(account)
    if not data and not raw_match:
        return "本场没有查询账号，请核对绑定或比赛编号；不会替换为其他玩家的评分。"

    info = raw_match.get("match_info", {}) if isinstance(raw_match, dict) else {}
    sl = {str(p["account_id"]): p for p in data["players"]} if (data and "players" in data) else {}
    mid = str(info.get("match_id", data.get("match_id", "?") if data else "?"))

    if not isinstance(info, dict) or (data and str(info.get("match_id")) != str(data["match_id"])):
        info = {}
    api = {str(p["account_id"]): p for p in info.get("players", [])
           if isinstance(p, dict) and p.get("account_id") is not None}

    if sl and account not in sl:
        return "本场没有查询账号，请核对绑定或比赛编号；不会替换为其他玩家的评分。"
    if not sl and api and account not in api:
        return "本场没有查询账号，请核对绑定或比赛编号；不会替换为其他玩家的评分。"

    if sl:
        targets = [account] + [a for a in sl if a != account and a in roster]
    elif api:
        targets = [account] + [a for a in api if a != account and a in roster]
    else:
        return "本场没有查询账号，请核对绑定或比赛编号；不会替换为其他玩家的评分。"

    duration = info.get("duration_s")
    lines = [f"🏆 StatLocker 战报 · {data['match_id']}"] if data else [f"🏆 Deadlock 战报 · {mid}"]
    if type(duration) is int and duration >= 0:
        lines.append(f"⏱️ 时长：{duration // 60:02d}:{duration % 60:02d}")
    if len(targets) > 1:
        lines.append(f"👥 本场群友：{len(targets)}人")
    if data and not info:
        lines.append("⚠️ 对局详情暂不可用，仅展示原站数据。")
    elif not data and stat_error:
        lines.append(f"⚠️ {stat_error}")

    for acc in targets:
        ps, pa = sl.get(acc, {}), api.get(acc, {})
        name = str(roster.get(acc, ps.get('name', f"玩家 {acc}"))).replace("\n", " ")[:40]
        hero = hero_name(pa['hero_id']) if pa.get('hero_id') else ps.get('hero', '未知英雄')
        
        lines += ["", "➖➖➖➖➖➖➖➖", f"👤 {name} · 🕹️ {hero}"]
        if ps:
            rank = re.match(r"^\s*(\d+)", str(ps.get('rank', '')))
            lines.append(f"⭐ 评分：{ps['score']:.1f} / {ps['grade']} · 全场第 {rank[1] if rank else '?'}/12")
        else:
            lines.append("⭐ 评分：暂未就绪（StatLocker 评分未获取）")

        if pa:
            team, winner = pa.get('team'), info.get('winning_team')
            outcome = ('胜利' if team == winner else '失败') if team in (0, 1) and winner in (0, 1) else '未知'
            lines.append(f"🏁 结果：{outcome}")
            k, d, a = (pa.get(x) for x in ('kills', 'deaths', 'assists'))
            teammates = [p for p in api.values() if p.get('team') == team]
            kills = [p.get('kills') for p in teammates]
            kp = ''
            if len(teammates) == 6 and all(type(v) is int and v >= 0 for v in kills) and sum(kills) > 0 and all(type(v) is int and v >= 0 for v in (k, a)):
                kp = f" · 参团 {(k+a)/sum(kills):.0%}"
            lines += [f"⚔️ {display_num(k)}/{display_num(d)}/{display_num(a)}{kp}",
                      f"💰 灵魂 {display_num(pa.get('net_worth'))} · 补刀 {display_num(pa.get('last_hits'))} · 反补 {display_num(pa.get('denies'))}"]
            snaps = [v for v in (pa.get('stats') or []) if isinstance(v, dict) and type(v.get('time_stamp_s')) in (int, float)]
            last = max(snaps, key=lambda v:v['time_stamp_s']) if snaps else {}
            lines += [f"💥 英雄伤害 {display_num(last.get('player_damage'))} · 🛡️ 承伤 {display_num(last.get('player_damage_taken'))}",
                      f"🚩 建筑伤害 {display_num(last.get('boss_damage'))} · 清兵 {display_num(last.get('creep_kills'))}"]
            mitigation = [last.get('damage_mitigated'), last.get('damage_absorbed')]
            if all(type(v) in (int,float) and math.isfinite(v) and v >= 0 for v in mitigation):
                lines.append('🛡️ 减免/吸收 ' + display_num(sum(mitigation)))
            support = [label + ' ' + display_num(last[key]) for key, label in [('teammate_healing', '治疗'), ('teammate_barriering', '护盾')]
                       if type(last.get(key)) in (int, float) and last[key] > 0]
            if support:
                lines.append('💚 ' + ' · '.join(support))
                
        if ps and data:
            highlight = data.get('highlights', {}).get(acc)
            if highlight:
                lines += ['', '📊 评分构成']
                if not highlight['components']:
                    lines.append('评分构成暂未获取')
                parts = [CATEGORY_NAMES[k] + f" {v:+.1f}" for k,v in highlight['components'].items()]
                lines += [' · '.join(parts[i:i+3]) for i in range(0,len(parts),3)]
                if parts:
                    lines.append('分项为原站总分构成，非各项满分100分。')
                    if highlight.get('component_source') == 'stat_display_formula':
                        lines.append('按原站展示公式换算；points 舍入可能产生约0.1分误差。')
                lines += ['', '✨ 本局亮点']
                facts = highlight['facts']
                if not facts:
                    lines.append('原站未展示文字亮点')
                for fact in facts:
                    lines.append(format_highlight(fact))
            else:
                lines += ['', '📊 评分构成：暂未获取', '✨ 本局亮点：暂未获取']
            farm = data.get('farm_impact', {}).get(acc)
            lines += ['', '🌾 资源占用']
            if farm:
                labels = {'LOW FARM':'低资源占用', 'MEDIUM FARM':'中等资源占用', 'HIGH FARM':'高资源占用', 'HIGHEST FARM':'队内最高资源占用'}
                lines.append(labels.get(farm['label'], farm['label']) + ' · 发育获取约' + farm_amount(farm['farm']) + '灵魂')
                lines.append(f"评分奖励 {farm['bonus']} · {farm['before_grade']} → {ps['grade']}")
            else:
                lines.append('暂未获取')

    if data and info:
        src = '数据来源：Deadlock API 和 Statlocker.gg'
    elif data:
        src = '数据来源：Statlocker.gg（Deadlock API 详情暂不可用）'
    else:
        src = '数据来源：Deadlock API（StatLocker 评分暂未获取）'
    lines += ['', src]
    return '\n'.join(lines)
