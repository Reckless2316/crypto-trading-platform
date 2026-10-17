## Task 8: immutable outcome labels and calibrated edge artifacts

**Files:** create `bot/autonomous/calibration.py`, `tests/autonomous/test_calibration.py`; modify `bot/autonomous/ledger.py` for label events/projections.

**Consumes:** recorded judgments/probabilities, executable postdecision observations and fixed four-candle policy. **Produces:** `estimate_edge(forecast,snapshot,artifact) -> EdgeEstimate | UnavailableEdge`, `binary_brier(probabilities: tuple[float,...], outcomes: tuple[int,...]) -> float`, versioned matured label events and supported cohort estimates.

- [ ] RED: exact Brier and invalid/pending labels.

```python
import unittest
from bot.autonomous.calibration import binary_brier

class CalibrationTests(unittest.TestCase):
    def test_brier_uses_outcome_probability(self):
        self.assertAlmostEqual(binary_brier((.8, .3), (1, 0)), .065)
        with self.assertRaises(ValueError):
            binary_brier((.8,), ())
        with self.assertRaises(ValueError):
            binary_brier((.8,), (2,))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_calibration -v`; expect RED.
- [ ] Implement strict metrics, frozen horizon labels, matching version support and chronological calibration.

```python
import math

def binary_brier(probabilities, outcomes):
    if not probabilities or len(probabilities) != len(outcomes):
        raise ValueError("incomplete outcomes")
    if any(type(y) is not int or y not in (0, 1) for y in outcomes):
        raise ValueError("invalid outcome")
    if any(type(p) not in (int, float) or not math.isfinite(p)
           or not 0 <= p <= 1 for p in probabilities):
        raise ValueError("invalid probability")
    return math.fsum((p-y)**2 for p, y in zip(probabilities, outcomes))/len(outcomes)
```

Initial calibrator is a versioned bin map from Jev's probability of the proposed direction and approved regime/instrument cohort to net-positive outcomes. Define at most four quantile bins using training observations only, freeze boundaries before calibration/test, and require at least 100 independent calibration outcomes per admitted bin; unsupported bins remain disabled with no interpolation/extrapolation. Conservative p is the 95% one-sided Wilson lower bound; conservative W is a lower conditional-win estimate, L an upper conditional-loss estimate from independent training/calibration blocks. Store method, sample count, payoff tails/cost convention and exact artifact compatibility. Separate binary policy labels from terminal direction labels. Outcome revisions append label_events, preserve original session versions and recompute current projections. Missing quote/depth/fees, short borrow or matured data stays unresolved. No valid artifact means `UnavailableEdge`, including at bootstrap; shadow forecasts can still collect labels without trades.
- [ ] GREEN: future labels excluded, label corrections retained, no confidence-as-p, all HOLD/veto forecasts included, version mismatch rejects, no-supported-bin rejects, net fees/slippage affect labels and Brier denominators include only resolved labels with missingness reported. Commit: `feat: record outcome calibration evidence`.

