"""Operator-facing research cards. Status is copied from the 2026-10-17 research file.

Figures that were null there stay null. This module does not fetch markets or admit a trade.
"""
from __future__ import annotations

AS_OF = "2026-10-17"
SOURCE = "docs/research/2026-10-17-autonomous-agent/RESEARCH.md"

CANDIDATES = (
    {
        "symbol": "AAVE",
        "eligible": False,
        "hypothesis": "Recurring net surplus plus verified purchases could justify undervaluation.",
        "catalyst": "Conditional on enacted governance and executed purchases. The docs and the February 28, 2026 forum post disagree on the budget.",
        "bear_case": "Losses, oracle failure, subsidies, spending, and reserve redistribution can absorb the cash.",
        "invalidation": "Durable surplus cannot fund purchases, purchases are suspended, or dilution and losses overwhelm them.",
        "value_accrual": "Not established. Current supply, fees, TVL, users, and holder concentration are null.",
    },
    {
        "symbol": "HYPE",
        "eligible": False,
        "hypothesis": "Organic fee-funded absorption could exceed sellable issuance.",
        "catalyst": "No accepted future event date. Needs verified net absorption and retention.",
        "bear_case": "Subsidized activity, fee compression, contributor selling, or venue losses.",
        "invalidation": "Persistent absorption deficit, fee-paying trader churn, or venue integrity failure.",
        "value_accrual": "Not established. Headline fees are shared with HLP, the assistance fund, and deployers.",
    },
    {
        "symbol": "PENDLE",
        "eligible": False,
        "hypothesis": "Non-points fee demand persists after maturities and incentives.",
        "catalyst": "No new dated event confirmed. Needs retained users, cash fees, and executed purchases.",
        "bear_case": "Rented TVL, yield compression, underlying failure, and recycled rollovers.",
        "invalidation": "Retention or cash fees collapse, or dilution exceeds sustainable purchases.",
        "value_accrual": "Not established. Liquid PENDLE does not automatically receive sPENDLE distributions.",
    },
    {
        "symbol": "AERO",
        "eligible": False,
        "hypothesis": "Normalized organic swap demand could outgrow liquidity subsidies.",
        "catalyst": "Migration benefit is speculation. The docs site JavaScript returned 403 on retrieval.",
        "bear_case": "Circular bribes and emissions, wash volume, Base dependence, and voting capture.",
        "invalidation": "Organic economics fail after dilution, or conversion terms destroy the expected entitlement.",
        "value_accrual": "Not established. Spot AERO does not earn veAERO income automatically.",
    },
    {
        "symbol": "LDO",
        "eligible": False,
        "hypothesis": "Recurring surplus eventually funds durable purchases.",
        "catalyst": "H1 2026 report is historical. October NEST activation was not proven.",
        "bear_case": "Market-share loss, waivers, weak ETH, and expenses. Report numbers are not a current run rate.",
        "invalidation": "The revenue hurdle stays unmet, or purchases exceed durable surplus.",
        "value_accrual": "Not established. Treasury receipts are not burns.",
    },
    {
        "symbol": "ONDO",
        "eligible": False,
        "hypothesis": "Product adoption can be priced into a governance token without a matching cash-flow entitlement.",
        "catalyst": "No accepted release date or amount. The December 6, 2024 distribution note is a historical framework.",
        "bear_case": "A short is exposed to a binding accrual change, accumulation, and a squeeze.",
        "invalidation": "Executable entitlement changes, or the price already discounts dilution and rights.",
        "value_accrual": "Not established for spot. Product AUM is not ONDO revenue. Neutral in the initial spot scope.",
    },
)


def candidate_cards() -> dict:
    return {
        "as_of": AS_OF,
        "source": SOURCE,
        "regime_valid": False,
        "qualified_finalists": 0,
        "note": "All six candidates were admission-false on the cited research date. Null fundamentals were not filled in.",
        "candidates": list(CANDIDATES),
    }
