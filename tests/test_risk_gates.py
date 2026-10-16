import unittest
import time

from bot.risk_gates import RiskGates


class RiskGateTests(unittest.TestCase):
    def setUp(self):
        self.gates = RiskGates({"max_position_pct":5,"max_daily_loss_pct":3,"max_open_positions":3})
        self.base = {"kill_switch":False,"armed_until":time.time()+600,"now":time.time(),
                     "starting_equity":10_000,"daily_realized_pnl":0}

    def test_disarmed_and_killed_fail_closed(self):
        for change in ({"kill_switch":True},{"armed_until":0}):
            allowed,_,_=self.gates.evaluate({**self.base,**change})
            self.assertFalse(allowed)

    def test_position_limits_are_enforced(self):
        allowed,_,_=self.gates.evaluate({**self.base,"side":"buy","is_new_position":True,"open_positions":3,
                                         "current_position_value":0,"order_value":10,"available_cash":1000})
        self.assertFalse(allowed)
        allowed,_,_=self.gates.evaluate({**self.base,"side":"buy","is_new_position":False,"open_positions":1,
                                         "current_position_value":450,"order_value":60,"available_cash":1000})
        self.assertFalse(allowed)

    def test_daily_loss_and_cash_gates(self):
        allowed,_,_=self.gates.evaluate({**self.base,"daily_realized_pnl":-300})
        self.assertFalse(allowed)
        allowed,_,_=self.gates.evaluate({**self.base,"side":"buy","is_new_position":False,"open_positions":1,
                                         "current_position_value":0,"order_value":20,"available_cash":10})
        self.assertFalse(allowed)

if __name__ == "__main__":
    unittest.main()
