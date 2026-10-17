## Task 9: fractional Kelly sizing with deterministic capacity caps

**Files:** create `bot/autonomous/sizing.py`, `tests/autonomous/test_sizing.py`.

**Consumes:** validated `EdgeEstimate`, current `AccountView`, `MarketRules`, `RiskLimits`. **Produces:** `size_entry(edge,account,market,limits) -> SizedProposal | SizeRejection` and `kelly_loss_budget(equity,p,win,loss) -> Decimal`.

- [ ] RED: show quarter-Kelly and the harder per-entry cap.

```python
import unittest
from decimal import Decimal as D
from bot.autonomous.sizing import kelly_loss_budget

class SizingTests(unittest.TestCase):
    def test_loss_budget_is_capped(self):
        self.assertEqual(kelly_loss_budget(D("10000"), D(".6"), D(".02"), D(".01")), D("25"))
        self.assertEqual(kelly_loss_budget(D("10000"), D(".2"), D(".02"), D(".01")), D("0"))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_sizing -v`; expect RED.
- [ ] Implement exact loss-budget convention and convert to notional only after validated positive payoff/cost support.

```python
from decimal import Decimal, localcontext

def kelly_loss_budget(equity, p, win, loss):
    if not all(x.is_finite() for x in (equity, p, win, loss)):
        raise ValueError("nonfinite Kelly input")
    if equity <= 0 or not 0 <= p <= 1 or win <= 0 or loss <= 0:
        raise ValueError("invalid Kelly input")
    with localcontext() as ctx:
        ctx.prec = 50
        fraction = max(Decimal(0), p-(1-p)/(win/loss))
        return equity*min(Decimal(".25")*fraction, Decimal(".0025"))
```

`size_entry` caps loss budget by remaining 1% pending loss, notional by 5% asset/20% gross residual room, current free cash minus reservations and max fees, and verified depth. Convert to base, round quantity down to lot, round buy price up/sell down within a bounded permissible limit, then recompute all caps; rounding must not expand authorization. Reject missing calibration/tail suitability, negative expectation, costs counted twice, unknown fee or minimum notional violations. Proposed initial entry execution guardrails: spread <=10bps, modeled additional impact <=10bps and participation <=1% of displayed executable depth inside the bound. These conservative plan parameters require measured validation before live admission; no liquidity numbers are assumed today.
- [ ] GREEN: cover exact caps, tiny L, nonfinite, unsupported edges, rounding/minimums, already-reserved capacity and insufficient depth. Commit: `feat: cap calibrated Kelly allocations`.

