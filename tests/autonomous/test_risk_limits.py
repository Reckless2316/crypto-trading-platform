"""Local deterministic accounting/risk evidence, never venue proof."""
import unittest
from decimal import Decimal as D, localcontext
from fractions import Fraction as F


class FlowTests(unittest.TestCase):
    def test_external_flow_fixture_and_adjacent_exact_boundaries(self):
        import json
        from pathlib import Path
        flow,loss,triggers=self.api()
        for line in (Path(__file__).parent/'fixtures/external_flows.jsonl').read_text().splitlines():
            p=json.loads(line)
            with self.subTest(case=p['case']):
                self.assertEqual(flow(*(D(p[k]) for k in ('equity','units','flow'))),
                    tuple(D(p[k]) for k in ('after_equity','after_units','nav')))
                self.assertEqual(loss(*(D(p[k]) for k in ('nav','high_water','day_open'))),
                    tuple(D(p[k]) for k in ('drawdown','daily_loss')))
        for baseline,boundary,index in [(F(100),F(85),0),(F(100),F(98),1)]:
            for delta,expected in [(F(-1,10**100),True),(F(0),True),(F(1,10**100),False)]:
                self.assertEqual(triggers(boundary+delta,baseline,baseline)[index],expected)

    def api(self):
        from bot.autonomous import ledger
        self.assertTrue(hasattr(ledger, 'nav_after_flow'), 'flow NAV implementation missing')
        from bot.autonomous.risk import loss_fractions, loss_triggers
        return ledger.nav_after_flow, loss_fractions, loss_triggers

    def test_deposit_cannot_hide_drawdown(self):
        flow, loss, _ = self.api()
        self.assertEqual(flow(D('8500'), D('100'), D('8500')), (D('17000'),D('200'),D('85')))
        self.assertEqual(loss(D('85'), D('100'), D('100')), (D('.15'), D('.15')))

    def test_exact_thresholds_ignore_decimal_context(self):
        _, loss, triggers = self.api()
        with localcontext() as ctx:
            ctx.prec=3
            for nav, expected in [('85.001', (False, True)), ('85', (True, True)), ('98.001',(False,False)), ('98',(False,True))]:
                self.assertEqual(triggers(F(D(nav)),F(100),F(100)), expected)
            self.assertEqual(loss(D('85.001'),D(100),D(100))[0],D('.14999'))
            self.assertEqual(triggers(F(85)+F(1,10**100),F(100),F(100)),(False,True))

    def test_invalid_and_closure_reject_before_division(self):
        flow, loss, _ = self.api()
        for inputs in [(D(0),D(1),D(1)),(D(1),D(0),D(1)),(D(1),D(1),D(-1)),(D('NaN'),D(1),D(1)),(1,D(1),D(1))]:
            with self.assertRaises(ValueError): flow(*inputs)
        with self.assertRaises(ValueError): loss(D(0),D(1),D(1))
