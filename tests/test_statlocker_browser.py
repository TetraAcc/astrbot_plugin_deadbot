import importlib
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from test_history_consistency import PACKAGE

browser = importlib.import_module(PACKAGE + '.statlocker_browser')


class DisplayComponentsTests(unittest.TestCase):
    def test_real_match_conversion_matches_all_seven_visible_card_values(self):
        data=json.loads((Path(__file__).parent/'fixtures/statlocker_129078319_display.json').read_text(encoding='utf-8'))
        self.assertEqual(browser.stat_display_components(data['player']),data['expected'])

    def player(self):
        return dict(score=50, breakdown=[
            'DAMAGE DEALER\nCATEGORY TOTAL: 120.0 PTS\nTotal Points: 100.0 pts',
            'DEATHS\nCATEGORY TOTAL: -20.0 PTS\nTotal Points: 100.0 pts'])

    def test_negative_contribution_preserved_not_reweighted(self):
        self.assertEqual(browser.stat_display_components(self.player()),
                         {'DAMAGE DEALER':60.0, 'DEATHS':-10.0})

    def test_mixed_player_tooltips_rejected(self):
        p=self.player();p['breakdown'][1]=p['breakdown'][1].replace('100.0','99.0')
        with self.assertRaises(ValueError): browser.stat_display_components(p)

    def test_inconsistent_or_zero_total_rejected(self):
        p=self.player();p['breakdown'][1]=p['breakdown'][1].replace('-20.0','-30.0')
        with self.assertRaises(ValueError): browser.stat_display_components(p)
        p=self.player();p['breakdown']=[s.replace('100.0','0.0') for s in p['breakdown']]
        with self.assertRaises(ValueError): browser.stat_display_components(p)


class SettleTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_cards_are_not_mistaken_for_complete(self):
        first=[dict(id='a',title='First',description='initial')]
        complete=first+[dict(id='b',title='Late',description='late loaded')]
        clock=[0.0]
        async def sleep(seconds): clock[0]+=seconds
        async def read(page): return first if clock[0]<7 else complete
        with patch.object(browser.time,'monotonic',side_effect=lambda:clock[0]), patch.object(browser.asyncio,'sleep',side_effect=sleep), patch.object(browser,'read_highlight_cards',side_effect=read):
            self.assertEqual(await browser.wait_highlight_cards(None),complete)
            self.assertGreaterEqual(clock[0],10)

    async def test_empty_card_fails_instead_of_caching_absence(self):
        clock=[0.0]
        async def sleep(seconds): clock[0]+=seconds
        with patch.object(browser.time,'monotonic',side_effect=lambda:clock[0]), patch.object(browser.asyncio,'sleep',side_effect=sleep), patch.object(browser,'read_highlight_cards',new=AsyncMock(return_value=[dict(id='a',title='A',description='')])):
            with self.assertRaises(TimeoutError): await browser.wait_highlight_cards(None)
