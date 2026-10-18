"""Paper runtime orders through the shared OrderAuthority/Ledger path. No network.

Fixture approvals (release row, permit, lease) stand in for what release.py/control.py grant in production;
they are written only here, as the existing authority tests do. The runtime itself never writes them.
"""
import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from bot.autonomous.contracts import AdvisoryRisk, DecisionReference, RiskLimits, content_hash
from bot.autonomous.ledger import Ledger
from bot.autonomous.runtime import MarketRules as RuntimeRules, PaperRuntime, RuntimeConfig
from bot.autonomous.runtime_authority import RuntimeAuthority, open_paper_account
from tests.autonomous.test_runtime import GOOD, Clock, FakeBooks, FakeRules, Fixed, online_rules

MIDNIGHT_US = 1760054400 * 1_000_000  # 2025-10-10T00:00:00Z: the ledger's first mark must sit on a UTC boundary
RELEASE = content_hash({})  # the release row binds its manifest hash; "{}" is the fixture manifest
SNAPSHOT = "f" * 64
HASH = "a" * 64
ACCOUNT = "paper-rt"


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.clock = Clock(MIDNIGHT_US)
        self.price = {"v": 100.0}
        self.books = FakeBooks(self.clock, {"AAVE-USD": lambda: self.price["v"]})
        self.ledger = self.open_ledger()
        open_paper_account(self.ledger, ACCOUNT, D("10000"))

    def open_ledger(self):
        ledger = Ledger.open(self.dir / "ledger.sqlite3")
        ledger._clock_us = self.clock
        self.addCleanup(ledger.close)
        return ledger

    def approve(self, ledger=None):
        """Test-only stand-in for an activated release and an operator permit/lease."""
        ledger = ledger or self.ledger
        now = self.clock.us
        with ledger.transaction() as c:
            c.execute("INSERT INTO release_bundles(release_hash,manifest_json,expires_at_us,recorded_at_us) VALUES (?, '{}', ?, ?)",
                      (RELEASE, now + 10 * 86_400_000_000, now))
            c.execute("INSERT INTO snapshots(snapshot_id,account_id,release_hash,candle_close_us,knowledge_cutoff_us,field_hash,state_json,"
                      "lineage_hash,account_revision,valid,tokenizer_hash,token_count,recorded_at_us) "
                      "VALUES (?,?,?,?,?,?,'{}',?,'{}',1,?,10,?)", (SNAPSHOT, ACCOUNT, RELEASE, now, now, HASH, HASH, HASH, now))
            c.execute("INSERT INTO decisions(decision_id,snapshot_id,request_id,deadline_us,model_id,status,recorded_at_us) "
                      "VALUES ('decision-rt',?,'request-rt',?,'fixture','completed',?)", (SNAPSHOT, now + 10**9, now))
            c.execute("INSERT INTO candidate_results(decision_id,candidate_id,status,veto_codes_json,recorded_at_us) "
                      "VALUES ('decision-rt',1,'eligible','[]',?)", (now,))
            c.execute("UPDATE account_control SET permit='entry',permit_epoch=1,lease_boot_id='rt-boot',lease_deadline_mono_ns=?,"
                      "active_release_hash=?,version=version+1", (10**15, RELEASE))
        self.decision = DecisionReference("decision-rt", "request-rt", SNAPSHOT, RELEASE)
        self.entry_close = now

    def adapter(self, ledger=None, **kw):
        kw.setdefault("limits", RiskLimits(armed=True, candidate_eligible=True, tokenizer_config_hash=HASH, calibration_config_hash=HASH))
        kw.setdefault("entry_verifier", lambda *a: True)  # test-only: the fixture decision stands in for attested sizing
        kw.setdefault("monotonic_ns", lambda: 1)
        return RuntimeAuthority(ledger or self.ledger, account_id=ACCOUNT, clock_us=self.clock,
                                boot_id="rt-boot", fee_rate=D("0.006"), **kw)

    def observe(self, adapter, **rules):
        book = self.books("AAVE-USD")
        adapter.observe("AAVE-USD", book["bids"], book["asks"], self.clock.us - self.books.lag_us,
                        RuntimeRules.parse(online_rules(**rules), self.clock.us, "AAVE-USD"))

    def open_entry(self, adapter, notional=D("100")):
        self.observe(adapter)
        outcome = adapter.entry("AAVE-USD", notional, self.entry_close, decision=self.decision)
        self.assertTrue(outcome.ok, outcome.reasons)
        return outcome

    def usd(self, ledger=None):
        return (ledger or self.ledger).account_view(ACCOUNT).balances


class RuntimeAuthorityTests(LedgerCase):
    def test_entry_without_formal_decision_is_refused_not_forged(self):
        self.approve()
        a = self.adapter(entry_verifier=None)
        self.observe(a)
        outcome = a.entry("AAVE-USD", D("100"), self.entry_close, decision=None)
        self.assertFalse(outcome.ok)
        self.assertEqual(("DECISION_PROVENANCE_UNAVAILABLE",), outcome.reasons)
        self.assertEqual(0, self.ledger.connection.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0])

    def test_entry_and_exit_through_authority_update_the_ledger(self):
        self.approve()
        a = self.adapter()
        entry = self.open_entry(a)
        self.assertGreater(a.view()["positions"]["AAVE-USD"], 0)
        self.clock.us += 1_000_000
        self.observe(a)
        exit_ = a.exit("AAVE-USD", self.entry_close + 60_000_000)
        self.assertTrue(exit_.ok, exit_.reasons)
        self.assertEqual(entry.qty, exit_.qty)
        view = a.view()
        self.assertEqual({}, view["positions"])
        self.assertLess(view["cash"], D("10000"))  # two fees and the spread were paid
        sides = [r[0] for r in self.ledger.connection.execute("SELECT side FROM fills ORDER BY recorded_at_us")]
        self.assertEqual(["buy", "sell"], sides)

    def test_reconcile_releases_the_reservation(self):
        self.approve()
        a = self.adapter()
        self.open_entry(a)
        reserved = self.ledger.connection.execute(
            "SELECT SUM(cash_usd_u)+SUM(gross_usd_u)+SUM(loss_budget_usd_u) FROM reservations").fetchone()[0]
        self.assertEqual(0, reserved)
        self.assertEqual((), self.ledger.account_view(ACCOUNT).reservations)
        settled = self.ledger.connection.execute("SELECT COUNT(*) FROM control_events WHERE reason LIKE '%' AND kind='recovery'").fetchone()[0]
        self.assertGreaterEqual(settled, 1)

    def test_operator_kill_in_the_ledger_blocks_exits(self):
        self.approve()
        a = self.adapter()
        self.open_entry(a)
        a.kill()
        self.observe(a)
        outcome = a.exit("AAVE-USD", self.entry_close + 60_000_000)
        self.assertFalse(outcome.ok)
        self.assertIn("HARD_KILL", outcome.reasons)
        self.assertIn("AAVE-USD", a.view()["positions"])

    def test_restart_recovers_state_from_the_ledger(self):
        self.approve()
        a = self.adapter()
        self.open_entry(a)
        before = a.view()
        self.ledger.close()
        reopened = self.open_ledger()
        after = self.adapter(reopened).view()
        self.assertEqual(before["positions"], after["positions"])
        self.assertEqual(before["cash"], after["cash"])

    def test_production_refusals_are_explicit(self):
        a = self.adapter()
        self.observe(a)
        self.assertIn("PERMIT_NONE", a.exit("AAVE-USD", self.clock.us).reasons)
        self.assertIn("NO_ACTIVE_RELEASE", a.exit("AAVE-USD", self.clock.us).reasons)
        self.assertEqual(("NOT_A_CANDIDATE",), a.exit("BTC-USD", self.clock.us).reasons)
        self.approve()
        self.assertEqual(("NO_LEDGER_ENTRY",), a.exit("AAVE-USD", self.clock.us).reasons)


class LedgerRuntimeTests(LedgerCase):
    def runtime(self, adapter, judgment=GOOD, **config):
        config.setdefault("instruments", ("AAVE-USD",))
        config.setdefault("paper_armed", True)
        rt = PaperRuntime(RuntimeConfig(data_dir=self.dir / "rt", accounting="ledger", **config), book_source=self.books,
                          clock_us=self.clock, reflex=Fixed(judgment), rules_source=FakeRules(), authority=adapter)
        self.addCleanup(rt.close)
        return rt

    def advance(self, rt, n):
        out = None
        for _ in range(n):
            self.clock.us += 60_000_000
            out = rt.cycle()
        return out

    def test_runtime_entry_is_refused_by_the_authority_without_forging(self):
        self.approve()
        rt = self.runtime(self.adapter(entry_verifier=None), explore_fraction=0.001)
        self.clock.us += 1_000_000
        result = self.advance(rt, 21)["instruments"]["AAVE-USD"]
        self.assertIn("AUTHORITY_REFUSED:FORMAL_JUDGMENT_UNAVAILABLE", result["reasons"])  # offline rules have no distribution
        self.assertEqual(0, self.ledger.connection.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0])
        self.assertEqual(0, rt.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0])

    def test_runtime_exit_goes_through_the_authority_with_cash_parity(self):
        self.approve()
        a = self.adapter()
        self.open_entry(a)
        rt = self.runtime(a, judgment=replace(GOOD, risk_state=AdvisoryRisk.REDUCE))
        self.clock.us += 1_000_000
        self.assertIn("AAVE-USD", rt.positions())  # positions are read back from the ledger
        result = self.advance(rt, 1)["instruments"]["AAVE-USD"]  # exits act even during warmup
        self.assertEqual("sell", result["status"], result["reasons"])
        self.assertIn("LEDGER", result["reasons"])
        self.assertEqual({}, rt.positions())
        ledger_cash = a.view()["cash"]
        self.assertEqual(ledger_cash, rt.cash())
        # Parity: the runtime-accounting formula over the same scripted ledger fills gives the same cash.
        cash = D("10000")
        for side, quote, fee in self.ledger.connection.execute("SELECT side, quote_qty, fee_qty FROM fills"):
            cash += (-D(quote) - D(fee)) if side == "buy" else (D(quote) - D(fee))
        self.assertEqual(cash, ledger_cash)
        mirrored = rt.db.execute("SELECT side, notional_usd, fee_usd FROM fills").fetchall()
        self.assertEqual(["sell"], [r["side"] for r in mirrored])
        status = json.loads((self.dir / "rt" / "status.json").read_text())
        self.assertEqual("ledger", status["accounting"])

    def test_runtime_kill_file_latches_the_ledger_kill(self):
        self.approve()
        a = self.adapter()
        self.open_entry(a)
        rt = self.runtime(a, judgment=replace(GOOD, risk_state=AdvisoryRisk.REDUCE))
        (self.dir / "rt" / "KILL").write_text("operator")
        self.clock.us += 1_000_000
        self.advance(rt, 1)
        self.assertEqual(1, self.ledger.connection.execute("SELECT hard_killed FROM account_control").fetchone()[0])
        (self.dir / "rt" / "KILL").unlink()  # removing the file does not clear the durable ledger kill
        result = self.advance(rt, 1)["instruments"]["AAVE-USD"]
        self.assertIn("AUTHORITY_REFUSED:HARD_KILL", result["reasons"])
        self.assertIn("AAVE-USD", rt.positions())

    def test_accounting_mode_is_bound_in_meta(self):
        rt = self.runtime(self.adapter())
        rt.close()
        with self.assertRaisesRegex(ValueError, "config mismatch"):
            PaperRuntime(RuntimeConfig(data_dir=self.dir / "rt", instruments=("AAVE-USD",)), book_source=self.books,
                         clock_us=self.clock, rules_source=FakeRules())


if __name__ == "__main__":
    unittest.main()
