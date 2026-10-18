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


class RiskAdmissionTests(unittest.TestCase):
    def setUp(self):
        from bot.autonomous.contracts import (
            ControlState, MarketRules, Mode, Permit, RiskLimits,
        )
        from tests.autonomous.support import account_view, order_proposal
        self.account = account_view()
        self.proposal = order_proposal()
        self.control = ControlState(mode=Mode.PAPER, permit=Permit.ENTRY,
            control_epoch=0, boot_id='boot-test', deadman_deadline_mono_ns=40_000_000_000,
            active_release_hash=self.proposal.release_hash, reconciliation_ready=True, armed=True)
        self.limits = RiskLimits(mode=Mode.PAPER, armed=True, candidate_eligible=True,
            tokenizer_config_hash='1'*64, calibration_config_hash='2'*64)
        self.market = MarketRules(venue='test', instrument='AAVE-USD', base_asset='AAVE',
            quote_asset='USD', tick_size=D('0.01'), lot_size=D('0.001'),
            minimum_quantity=D('0.001'), minimum_notional=D('1'), fee_bound=D('0.01'),
            max_price_precision=2, capability_hash='f'*64, expires_at_us=30_000_000,
            spot_allowed=True, limit_ioc_verified=True, book_at_us=1_000_000,
            available_buy_notional_usd_u=1_000_000_000)

    def assess(self, **changes):
        from bot.autonomous.risk import assess
        args = dict(proposal=self.proposal, account=self.account, control=self.control,
            limits=self.limits, market=self.market, now_us=1_000_000,
            now_mono_ns=1_000_000_000, current_boot_id='boot-test')
        args.update(changes)
        return assess(**args)

    def test_allowed_entry_returns_bound_verdict(self):
        from bot.autonomous.contracts import content_hash
        verdict = self.assess()
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.reason_codes, ())
        self.assertEqual(verdict.account_revision, self.account.revision)
        self.assertEqual(verdict.proposal_hash, content_hash(self.proposal))

    def test_loss_limit_reason_kernel_is_inclusive(self):
        from bot.autonomous.risk import loss_limit_reasons
        self.assertEqual(loss_limit_reasons(D('.14999'), D('.01999')), ())
        self.assertEqual(loss_limit_reasons(D('.15'), D('0')), ('DRAWDOWN_LATCH',))
        self.assertEqual(loss_limit_reasons(D('0'), D('.02')), ('DAILY_HALT',))

    def test_hard_kill_disarm_and_expired_or_missing_lease_veto_buys_and_reductions(self):
        from dataclasses import replace
        from bot.autonomous.contracts import Permit, Side
        from tests.autonomous.support import order_proposal
        from bot.autonomous.risk import assess
        reduce = replace(order_proposal(), side=Side.SELL, reducing_only=True, decision=None,
            base_quantity=D('0.1'))
        cases = [
            ('killed-buy', self.proposal, replace(self.control, hard_killed=True, armed=False), 'HARD_KILL'),
            ('killed-reduce', reduce, replace(self.control, hard_killed=True, armed=False), 'HARD_KILL'),
            ('disarmed-buy', self.proposal, replace(self.control, armed=False), 'DISARMED'),
            ('expired-lease', self.proposal, replace(self.control, deadman_deadline_mono_ns=999_999_999), 'LEASE_EXPIRED'),
            ('missing-boot', self.proposal, replace(self.control, boot_id=None, armed=False), 'LEASE_BOOT_UNKNOWN'),
            ('new-boot', self.proposal, self.control, 'LEASE_BOOT_MISMATCH'),
        ]
        for name, proposal, control, reason in cases:
            with self.subTest(name=name):
                verdict = assess(proposal, self.account, control, self.limits, self.market, 1_000_000,
                    now_mono_ns=1_000_000_000,
                    current_boot_id='boot-new' if name == 'new-boot' else 'boot-test')
                self.assertFalse(verdict.allowed)
                self.assertIn(reason, verdict.reason_codes)

    def test_entry_freshness_and_capability_vetoes_are_stable(self):
        from dataclasses import replace
        cases = [
            ('stale-book', {'market': replace(self.market, book_at_us=0), 'now_us': 3_000_001}, 'STALE_BOOK'),
            ('bad-private-heartbeat', {'account': replace(self.account, private_heartbeat_us=0), 'now_us': 6_000_001}, 'STALE_PRIVATE_HEARTBEAT'),
            ('bad-reconciliation', {'account': replace(self.account, reconciled_at_us=0), 'now_us': 16_000_001}, 'STALE_RECONCILIATION'),
            ('unknown-orders', {'account': replace(self.account, unknown_intent_ids=('unknown',))}, 'UNKNOWN_ORDERS'),
            ('unverified-bounded-order', {'market': replace(self.market, limit_ioc_verified=False)}, 'UNVERIFIED_ORDER_CAPABILITY'),
        ]
        for name, kwargs, reason in cases:
            with self.subTest(name=name):
                verdict = self.assess(**kwargs)
                self.assertFalse(verdict.allowed)
                self.assertIn(reason, verdict.reason_codes)

    def test_loss_threshold_and_missing_baseline_vetoes(self):
        from dataclasses import replace
        from fractions import Fraction as F
        from bot.autonomous.contracts import ExactNavEvidence, ExactRatio
        from bot.autonomous.risk import assess
        for label, baseline, reason in [('drawdown', F(20,17), 'DRAWDOWN_LATCH'), ('daily', F(50,49), 'DAILY_HALT')]:
            with self.subTest(label=label):
                exact = ExactNavEvidence(self.account.account_id, 'mark', self.account.revision, 1,
                    self.account.liquidation_equity_usd_u, ExactRatio('10000','1'),
                    ExactRatio('1','1'),
                    ExactRatio(str(baseline.numerator),str(baseline.denominator)) if label == 'drawdown' else ExactRatio('1','1'),
                    ExactRatio(str(baseline.numerator),str(baseline.denominator)) if label == 'daily' else ExactRatio('1','1'),
                    'a'*64)
                # Keep display values exactly bound to rational evidence.
                from decimal import Decimal
                changed = replace(self.account, nav=exact.nav.projection(),
                    high_water_nav=exact.high_water_nav.projection(), day_open_nav=exact.day_open_nav.projection(), exact_nav=exact)
                verdict = assess(self.proposal, changed, self.control, self.limits, self.market, 1_000_000,
                    now_mono_ns=1_000_000_000, current_boot_id='boot-test')
                self.assertFalse(verdict.allowed)
                self.assertIn(reason, verdict.reason_codes)
        unready = replace(self.account, ready=False, exact_nav=None, day_open_nav=D(0))
        verdict = assess(self.proposal, unready, self.control, self.limits, self.market, 1_000_000,
            now_mono_ns=1_000_000_000, current_boot_id='boot-test')
        self.assertFalse(verdict.allowed)
        self.assertIn('ACCOUNT_UNREADY', verdict.reason_codes)

    def test_drawdown_and_daily_latches_block_buys_and_allow_reducing_sells(self):
        from dataclasses import replace
        from bot.autonomous.contracts import Balance, Holding, Permit, Side
        from tests.autonomous.support import order_proposal
        holding = Holding('AAVE-USD', 'AAVE', D('1'), 100_000_000, 100_000_000, 1_000_000, 'a'*64)
        account = replace(self.account, balances=(Balance('USD', D('9000'), 9_000_000_000),
            Balance('AAVE', D('1'), 100_000_000)), holdings=(holding,))
        reduce = replace(order_proposal(), side=Side.SELL, reducing_only=True, decision=None,
            base_quantity=D('0.1'), max_fee_usd_u=2_000_000)
        latched = replace(self.control, permit=Permit.REDUCE_ONLY, drawdown_latched=True, daily_halted=True)
        verdict = self.assess(proposal=reduce, account=account, control=latched)
        self.assertTrue(verdict.allowed, verdict.reason_codes)
        self.assertEqual(verdict.halt_transitions, ())
        killed = self.assess(proposal=reduce, account=account, control=replace(latched, hard_killed=True, armed=False))
        self.assertFalse(killed.allowed)
        self.assertIn('HARD_KILL', killed.reason_codes)
        buy = self.assess(control=replace(self.control, drawdown_latched=True, daily_halted=True))
        self.assertFalse(buy.allowed)
        self.assertIn('DRAWDOWN_LATCH', buy.reason_codes)
        self.assertIn('DAILY_HALT', buy.reason_codes)

    def test_sell_cannot_exceed_owned_or_reserved_inventory(self):
        from dataclasses import replace
        from bot.autonomous.contracts import Balance, Holding, IntentState, Reservation, Side
        from tests.autonomous.support import order_proposal
        from bot.autonomous.risk import assess
        proposal = replace(order_proposal(), side=Side.SELL, reducing_only=True, decision=None,
            base_quantity=D('1.1'), max_fee_usd_u=2_000_000)
        account = replace(self.account, balances=(Balance('USD', D('9000'), 9_000_000_000),
            Balance('AAVE', D('1'), 1_000_000_000)))
        from bot.autonomous.contracts import Permit
        verdict = assess(proposal, account, replace(self.control, permit=Permit.REDUCE_ONLY),
            self.limits, self.market, 1_000_000, now_mono_ns=1_000_000_000,
            current_boot_id='boot-test')
        self.assertFalse(verdict.allowed)
        self.assertIn('INSUFFICIENT_UNRESERVED_BASE', verdict.reason_codes)
        foreign = Reservation('btc-buy', 0, D('1'), 100_000_000, 0, D('1'), IntentState.RESERVED, instrument='BTC-USD', side='buy')
        owned = replace(account, holdings=(Holding('AAVE-USD', 'AAVE', D('1'), 100_000_000, 100_000_000, 1_000_000, 'a'*64),),
            reservations=(foreign,), open_intent_ids=('btc-buy',))
        smaller = replace(proposal, base_quantity=D('0.1'))
        verdict = assess(smaller, owned, replace(self.control, permit=Permit.REDUCE_ONLY),
            self.limits, self.market, 1_000_000, now_mono_ns=1_000_000_000, current_boot_id='boot-test')
        self.assertTrue(verdict.allowed, verdict.reason_codes)
        same = replace(foreign, intent_id='aave-sell', instrument='AAVE-USD', side='sell', cash_usd_u=0, gross_usd_u=0)
        blocked = assess(smaller, replace(owned, reservations=(same,), open_intent_ids=('aave-sell',)),
            replace(self.control, permit=Permit.REDUCE_ONLY), self.limits, self.market, 1_000_000,
            now_mono_ns=1_000_000_000, current_boot_id='boot-test')
        self.assertFalse(blocked.allowed)
        self.assertIn('INSUFFICIENT_UNRESERVED_BASE', blocked.reason_codes)

    def test_asset_gross_and_pending_loss_caps_include_reservations(self):
        from dataclasses import replace
        from bot.autonomous.contracts import Holding, IntentState, Reservation
        holding = Holding('AAVE-USD', 'AAVE', D('4.1'), 410_000_000, 410_000_000,
            1_000_000, 'a'*64)
        account = replace(self.account, holdings=(holding,))
        verdict = self.assess(account=account)
        self.assertFalse(verdict.allowed)
        self.assertIn('ASSET_CAP', verdict.reason_codes)
        cross_book = replace(holding, instrument='AAVE-USDC')
        verdict = self.assess(account=replace(self.account, holdings=(cross_book,)))
        self.assertFalse(verdict.allowed)
        self.assertIn('ASSET_CAP', verdict.reason_codes)
        holding = replace(holding, instrument='OTHER-USD', base_asset='OTHER',
            liquidation_value_usd_u=1_950_000_000)
        verdict = self.assess(account=replace(self.account, holdings=(holding,)))
        self.assertFalse(verdict.allowed)
        self.assertIn('GROSS_CAP', verdict.reason_codes)
        reservation = Reservation('pending', 0, D('0'), 0, 99_000_000, D('0'), IntentState.OPEN)
        verdict = self.assess(account=replace(self.account, reservations=(reservation,), open_intent_ids=('pending',)))
        self.assertFalse(verdict.allowed)
        self.assertIn('PENDING_LOSS_CAP', verdict.reason_codes)

        # An unfilled sell reservation never reduces current gross exposure.
        holding = Holding('AAVE-USD', 'AAVE', D('6'), 600_000_000, 600_000_000,
            1_000_000, 'a'*64)
        reducing = Reservation('reduce', 0, D('1'), 0, 0, D('1'), IntentState.OPEN)
        verdict = self.assess(account=replace(self.account, holdings=(holding,),
            reservations=(reducing,), open_intent_ids=('reduce',)))
        self.assertFalse(verdict.allowed)
        self.assertIn('ASSET_CAP', verdict.reason_codes)

        # A competing buy consumes pending gross capacity before a second proposal.
        pending_buy = Reservation('buy-pending', 0, D('0'), 1_950_000_000, 0, D('0'), IntentState.OPEN)
        verdict = self.assess(account=replace(self.account, reservations=(pending_buy,),
            open_intent_ids=('buy-pending',)))
        self.assertFalse(verdict.allowed)
        self.assertIn('GROSS_CAP', verdict.reason_codes)

    def test_pending_sell_gives_no_buy_capacity_credit(self):
        from dataclasses import replace
        from bot.autonomous.contracts import Balance, Holding, IntentState, Reservation
        held = replace(self.account,
            holdings=(Holding('AAVE-USD','AAVE',D('4.5'),450_000_000,450_000_000,1_000_000,'f'*64),),
            balances=self.account.balances+(Balance('AAVE',D('4.5'),450_000_000),),
            reservations=(Reservation('sell-all',0,D('4.5'),450_000_000,0,D('4.5'),IntentState.RESERVED),),
            open_intent_ids=('sell-all',))
        verdict = self.assess(account=held)
        self.assertFalse(verdict.allowed)
        self.assertIn('ASSET_CAP', verdict.reason_codes)

    def test_loss_threshold_neighbors_use_exact_nav_not_display_ratios(self):
        from dataclasses import replace
        from fractions import Fraction as F
        from bot.autonomous.contracts import ExactNavEvidence, ExactRatio
        from bot.autonomous.risk import assess
        # At equality, NAV/HWM is .85. A positive rational perturbation is just above it.
        for offset, blocked in ((F(0), True), (F(1, 10**80), False)):
            nav = F(1) + offset
            units = F(self.account.liquidation_equity_usd_u, 1_000_000) / nav
            exact = ExactNavEvidence(self.account.account_id, 'mark', self.account.revision, 1,
                self.account.liquidation_equity_usd_u, ExactRatio.of(units), ExactRatio.of(nav),
                ExactRatio('20','17'), ExactRatio('1','1'), 'b'*64)
            account = replace(self.account, units=exact.units.projection(), nav=exact.nav.projection(),
                high_water_nav=exact.high_water_nav.projection(), exact_nav=exact)
            verdict = assess(self.proposal, account, self.control, self.limits, self.market, 1_000_000,
                now_mono_ns=1_000_000_000, current_boot_id='boot-test')
            with self.subTest(offset=offset):
                self.assertEqual('DRAWDOWN_LATCH' in verdict.reason_codes, blocked)
