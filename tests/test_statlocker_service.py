import asyncio
import copy
from datetime import datetime, timezone
import importlib
import tempfile
import unittest
from pathlib import Path
from test_history_consistency import PACKAGE

module = importlib.import_module(PACKAGE + ".statlocker_service")


def fixture(mid="123"):
    parts = [f"{label}\nExample:\n+1.0 pts\nCATEGORY TOTAL: 1.0 PTS\nTotal Points: 6.0 pts"
             for label in ["DAMAGE DEALER", "EARLY IMPACT", "PUSHER", "TANK", "SUPPORT", "DEATHS"]]
    return dict(match_id=mid, source="https://statlocker.gg", captured_at=datetime.now(timezone.utc).isoformat(),
                players=[dict(account_id=str(i), name=f"Player{i}", hero="Hero", rank=f"{i}th",
                              score=71.3, grade="B+", breakdown=parts[:]) for i in range(1, 13)])


class StatLockerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = []

        async def fetch(mid, account):
            self.calls.append((mid, account))
            await asyncio.sleep(.01)
            return fixture(mid)
        self.fetch = fetch
        self.service = module.StatLockerService(self.temp.name, fetch=fetch, delay=0)

    async def asyncTearDown(self):
        await self.service.close()

    async def test_same_match_coalesces_and_persistent_cache_is_shared(self):
        results = await asyncio.gather(*(self.service.get("123", str(i)) for i in range(1, 6)))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(r[0]["match_id"] == "123" for r in results))
        other = module.StatLockerService(self.temp.name, fetch=self.fetch, delay=0)
        _, cached = await other.get("123", "2")
        self.assertTrue(cached)
        self.assertEqual(len(self.calls), 1)
        await other.close()

    async def test_refresh_failure_never_returns_old_or_local_score(self):
        await self.service.get("123", "1")
        async def failed(*args):
            raise RuntimeError("site failed")
        self.service.fetch = failed
        with self.assertRaises(module.StatLockerError):
            await self.service.get("123", "1", refresh=True)
        with self.assertRaisesRegex(module.StatLockerError, "一分钟"):
            await self.service.get("123", "1", refresh=True)

    async def test_queue_is_bounded_and_distinct_matches_run_serially(self):
        entered, release = asyncio.Event(), asyncio.Event()
        active = 0
        async def blocked(mid, account):
            nonlocal active
            active += 1
            self.assertEqual(active, 1)
            entered.set()
            await release.wait()
            active -= 1
            return fixture(mid)
        self.service.fetch = blocked
        self.service.capacity = 1
        first = asyncio.create_task(self.service.get("123", "1"))
        await entered.wait()
        try:
            with self.assertRaisesRegex(module.StatLockerError, "队列已满"):
                await self.service.get("124", "1")
        finally:
            release.set()
        await first

    async def test_invalid_page_never_enters_cache(self):
        async def incomplete(mid, account):
            data = fixture(mid)
            data["players"].pop()
            return data
        self.service.fetch = incomplete
        with self.assertRaises(module.StatLockerError):
            await self.service.get("123", "1")
        self.assertFalse((Path(self.temp.name) / "123.json").exists())

    async def test_expired_or_corrupt_cache_is_refetched(self):
        await self.service.get("123", "1")
        self.service.ttl = 0
        await self.service.get("123", "1")
        self.assertEqual(len(self.calls), 2)
        (Path(self.temp.name) / "123.json").write_text("broken", encoding="utf-8")
        await self.service.get("123", "1")
        self.assertEqual(len(self.calls), 3)

    def test_render_uses_website_grade_and_only_requested_player(self):
        data = fixture()
        text = module.render(data, "1", True)
        self.assertIn("71.3 / B+", text)
        self.assertIn("Player1 ·", text)
        self.assertNotIn("Player2 ·", text)
        self.assertIn("评分构成：暂未获取", text)
        self.assertIn("没有查询账号", module.render(data, "99", False))

    def test_validation_rejects_wrong_match_duplicates_and_mixed_tooltips(self):
        for change in ("match", "duplicate", "tooltip", "nan"):
            data = copy.deepcopy(fixture())
            if change == "match": data["match_id"] = "999"
            if change == "duplicate": data["players"][1]["account_id"] = "1"
            if change == "tooltip": data["players"][0]["breakdown"][0] = data["players"][0]["breakdown"][0].replace("6.0 pts", "9.0 pts")
            if change == "nan": data["players"][0]["score"] = float("nan")
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.validate(data, "123")


class DisplaySafetyTests(unittest.TestCase):
    def test_missing_account_never_substitutes_friend(self):
        self.assertIn('没有查询账号', module.render(fixture(), '99', False, {'match_info': {'match_id':123, 'players':[]}}, {'1':'friend'}))

    def test_wrong_match_drops_api(self):
        text=module.render(fixture(), '1', False, {'match_info':{'match_id':999, 'players':[{'account_id':1,'kills':88}]}})
        self.assertIn('详情暂不可用',text)
        self.assertNotIn('88/',text)

    def test_unknown_not_zero_or_loss(self):
        text=module.render(fixture(), '1', False, {'match_info':{'match_id':123,'players':[{'account_id':1,'team':0}]}})
        self.assertIn('结果：未知',text)
        self.assertIn('未知/未知/未知',text)
        self.assertNotIn('结果：失败',text)

    def test_equal_shares_and_farm(self):
        data=fixture()
        data['farm_impact']={'1':dict(label='LOW FARM',farm='38k',bonus='+7.8',before_grade='B-')}
        text=module.render(data,'1',False)
        self.assertIn('输出8.3%', '\n'.join(module.contribution_shares(data,data['players'][0])))
        self.assertIn('评分奖励 +7.8 · B- → B+',text)
        self.assertNotIn('points',text)

    def test_zero_category_and_invalid_farm(self):
        data=fixture()
        for p in data['players']:
            p['breakdown']=[b.replace('CATEGORY TOTAL: 1.0', 'CATEGORY TOTAL: 0.0') for b in p['breakdown']]
        self.assertIn('输出无贡献','\n'.join(module.contribution_shares(data,data['players'][0])))
        data=fixture()
        data['farm_impact']={'1':{}}
        with self.assertRaises(ValueError): module.validate(data,'123')


class HighlightTests(unittest.TestCase):
    def data(self):
        data=fixture()
        comp={'DAMAGE DEALER':41.4,'SUPPORT':14.3,'PUSHER':12.2,'TEAM FARM BONUS':6.0,'TANK':5.6,'EARLY IMPACT':5.3,'DEATHS':-13.6}
        data['highlights']={'1':dict(account_id='1',match_id='123',score=71.3,components=comp,facts=[dict(title='Killed every enemy',value='6/6',description='Player1 killed every enemy hero at least once')])}
        return data

    def test_original_scores_and_facts_render_without_shares(self):
        data=self.data(); module.validate(data,'123')
        text=module.render(data,'1',False)
        self.assertIn('输出 +41.4',text)
        self.assertIn('死亡扣分 -13.6',text)
        self.assertIn('击杀过所有敌方英雄：6/6',text)
        self.assertNotIn('贡献占比',text)
        self.assertIn('71.3 / B+',text)

    def test_mixed_account_match_and_invalid_components_rejected(self):
        for key,value in [('account_id','2'),('match_id','999'),('score',99)]:
            data=self.data();data['highlights']['1'][key]=value
            with self.assertRaises(ValueError):module.validate(data,'123')
        data=self.data();data['highlights']['1']['components']['TANK']=float('nan')
        with self.assertRaises(ValueError):module.validate(data,'123')

    def test_another_player_never_gets_focus_highlights(self):
        text=module.render(self.data(),'2',False)
        self.assertIn('本局亮点：暂未获取',text)
        self.assertNotIn('6/6',text)

    def test_api_only_still_works(self):
        text=module.render(None,'1',False,{'match_info':{'match_id':123,'players':[{'account_id':1,'kills':3}]}},stat_error='采集失败')
        self.assertIn('采集失败',text)
        self.assertIn('评分：暂未就绪',text)

    def test_card_manifest_catches_missing_and_cross_player_facts(self):
        data=self.data();item=data['highlights']['1']
        item['cards_status']='settled'
        item['cards']=[dict(id='every-enemy',title='Killed every enemy',value='6/6',description='Player1 killed every enemy hero at least once')]
        item['facts'][0]['card_id']='every-enemy'
        module.validate(data,'123')
        item['facts']=[]
        with self.assertRaises(ValueError):module.validate(data,'123')

    def test_converted_components_have_provenance_in_report(self):
        data=self.data();data['highlights']['1']['component_source']='stat_display_formula'
        self.assertIn('按原站展示公式换算',module.render(data,'1',False))

    def test_original_card_labels_translate_without_award_rules(self):
        text=module.format_highlight(dict(title='Lobby 1st places',value='',description='3 stats led Ally healing, Accuracy, CC score Ally healing 28k Accuracy 69% CC score 8.9k'))
        self.assertEqual(text,'🥇 全场第一项：3项——队友治疗 28k、命中率 69%、控制分 8.9k')
        text=module.format_highlight(dict(title='Elite Pair',value='',description='Top 2% Denies 14 You 3.0 Avg Top 3% Crowd Control 8.9k You 1.1k Avg'))
        self.assertIn('反补前 2%（14次），控制前 3%',text)

    def test_junk_dom_cards_are_filtered(self):
        # 验证图表坐标轴刻度、对线表格与散点图 legend 等无意义 DOM 文本被正确滤除
        self.assertIsNone(module.format_highlight(dict(
            title='Damage/min', value='', description='2.6k 3:00 6:00 9:00 12:00 15:00', card_id='per-minute-damage'
        )))
        self.assertIsNone(module.format_highlight(dict(
            title="Tetrahedron's lane · Green", value='',
            description='Souls Paige icon 6.3 k Vyper icon 5.5 k Last hits Paige icon 24 Denies Paige icon 3',
            card_id='lane-matchup'
        )))
        self.assertIsNone(module.format_highlight(dict(
            title='Kills vs assists', value='',
            description='Assists Kills equals assists Solomon icon Abrams icon Infernus icon',
            card_id='kills-scatter'
        )))

    def test_extended_highlight_cards_translate_to_chinese(self):
        # 远距离击杀
        t1 = module.format_highlight(dict(
            title='Most long-range kills', value='4',
            description='kills from long range, the most in the lobby Next most: Holliday, 2.',
            card_id='kill-range'
        ))
        self.assertEqual(t1, '🏹 远距离击杀最多：4次（全场第一；第二名哈雷黛 2次）')

        # 命中率
        t2 = module.format_highlight(dict(
            title='Accuracy vs usual', value='54%',
            description='54% accuracy this game, roughly 1.2x their average of 44% Their average is over 454 games. Also past their average: deaths -42%.',
            card_id='personal-average'
        ))
        self.assertEqual(t2, '🎯 命中率：54%，约为平时44%的1.2倍（原站454场样本）')

        # 重点交火 / 伤害往来
        t3 = module.format_highlight(dict(
            title='Damaged most', value='',
            description='Tetrahedron damaged Abrams icon 31% 10k 31% of total Damaged them Solomon icon 24% 4.5k 24% of total',
            card_id='damaged-most'
        ))
        self.assertEqual(t3, '⚔️ 重点交火：主要输出 亚伯兰 10k（31%）；主要受击 所罗门 4.5k（24%）')

        # 伤害交换比
        t4 = module.format_highlight(dict(
            title='Damage traded', value='',
            description='3 rd 1.76x of 12 players Damage Efficiency (Last 200) 1.76 dealt : taken 992 562',
            card_id='damage-efficiency'
        ))
        self.assertEqual(t4, '⚖️ 伤害交换比：1.76x（全场第3/12，输出/承伤 992:562）')

        # 团队伤害爆发峰值
        t5 = module.format_highlight(dict(
            title="Tetrahedron's damage peak", value='',
            description="27% 0:00 to 3:00 of their team's damage 0:00 to 3:00 Peak share of team damage 27%, 0:00 to 3:00",
            card_id='peak-window'
        ))
        self.assertEqual(t5, '💥 团队伤害爆发峰值：0:00-3:00 贡献全队 27% 伤害')


class MultiplayerHighlightTests(unittest.IsolatedAsyncioTestCase):
    async def test_absent_score_tile_keeps_facts_and_shared_cache(self):
        import tempfile
        data=fixture()
        data['highlights']={p['account_id']:dict(account_id=p['account_id'],match_id='123',score=None,components={},components_status='not_shown',facts=[dict(title='Most ally healing',value='28k',description='healing on teammates, the most in the lobby')]) for p in data['players']}
        module.validate(data,'123')
        calls=[]
        async def fetch(mid,account):
            calls.append(account)
            await asyncio.sleep(.01)
            return copy.deepcopy(data)
        with tempfile.TemporaryDirectory() as directory:
            service=module.StatLockerService(directory,fetch=fetch,delay=0)
            a,b=await asyncio.gather(service.get('123','1'),service.get('123','2'))
            self.assertEqual(len(calls),1)
            for account in ('1','2'):
                result,cached=await service.get('123',account)
                self.assertTrue(cached)
                text=module.render(result,account,cached)
                self.assertIn('评分构成暂未获取',text)
                self.assertIn('28k',text)
            await service.close()

    async def test_missing_player_triggers_fill_and_preserves_previous(self):
        import tempfile
        data=fixture();data['highlights']={}
        async def fetch(mid,account):
            out=copy.deepcopy(data)
            out['highlights'][account]=dict(account_id=account,match_id=mid,score=None,components={},components_status='not_shown',facts=[])
            return out
        with tempfile.TemporaryDirectory() as directory:
            service=module.StatLockerService(directory,fetch=fetch,delay=0)
            await service.get('123','1')
            result,cached=await service.get('123','2')
            self.assertFalse(cached)
            self.assertEqual(set(result['highlights']),{'1','2'})
            await service.close()
