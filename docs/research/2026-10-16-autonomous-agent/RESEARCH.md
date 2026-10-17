# Autonomous crypto agent: research gate

Research date: **2026-10-16 America/Denver**. Public API evidence was captured at approximately **2026-10-17 05:41 UTC**, still October 16 in Denver. This is a research shortlist, not an approved trading universe. **Zero trade-ready finalists.** Current prices, fully diluted valuations, forward supply, concentration, and net cash-flow yields have not all been verified for any candidate. No asymmetric valuation disconnect is established yet.

## Evidence and market regime

Raw public responses, retrieval metadata, extracted vendor documentation, and SHA-256 hashes are in `evidence/`. Calculations below use the captured responses, not moving web-page values. API failures are recorded. These observations are only available after their retrieval time; they must never be backfilled into a historical decision as previously known information.

| Input | Observation | Interpretation and limitation |
|---|---|---|
| BTC/USD, Coinbase | Last completed daily candle starts October 16 00:00 UTC, closes October 17 00:00 UTC: $81,500.04. SMA20 $83,557.45; SMA50 $81,249.70. Seven-day return −1.27%; 30-day +7.03%. | Recovery has weakened; mixed trend, not established momentum continuation. |
| ETH/USD, Coinbase | Same completed interval: $2,419.92. SMA20 $2,585.89; SMA50 $2,552.29. Seven-day return −2.63%; 30-day +0.14%. | Below both averages; weaker than BTC. |
| Realized volatility | Computed sample standard deviation of the last 30 daily log returns × sqrt(365): BTC 37.86%, ETH 43.61% annualized. | Historical estimates, not forecasts. No percentile history collected to establish a high-volatility regime. |
| Dominance | CoinGecko's own aggregate reports BTC 59.31%, ETH 10.72%, source timestamp October 17 05:34:32 UTC. | Aggregator estimate, **not chain-primary evidence**; a single level does not establish rotation. |
| Stablecoin liquidity, issuer | Circle reports USDC $72.7B circulating as of October 12; seven-day and 30-day net redemptions each $1.5B. | Primary issuer disclosure, not a contemporaneous all-stablecoin measure. |
| Stablecoin liquidity, aggregate | DefiLlama USD-pegged circulating USD value: $304.84B at October 17 00:00 UTC; −0.37% versus seven days earlier, +0.62% versus 30 days earlier. | Secondary aggregate; minting is not proof of buying demand. Other peg currencies are not added as native units. |
| Derivatives | Deribit BTC-PERPETUAL, October 17 05:41:28.479 UTC: reported open interest 841,649,160; funding_8h 0.00000704 (0.000704% as a rate). | One venue, one observation. Preserve raw OI units until checked against the instrument/API contract; no leverage-growth inference. Bybit requests returned 403; Binance web requests failed. |
| Macro | September 16, 2026 FOMC statement raised target range to 3.75–4.00%. | Tightening headwind is an inference; it is not a complete macro forecast or next-meeting prediction. |
| Narratives | Candidate mechanisms span lending, perpetuals, yield trading, staking, Base liquidity, and RWA governance. | A research taxonomy only. No timestamped breadth/flow series establishes current narrative rotation. |

Sources: [Coinbase BTC candles](https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400), [Coinbase ETH candles](https://api.exchange.coinbase.com/products/ETH-USD/candles?granularity=86400), [Circle transparency, as of October 12](https://www.circle.com/transparency), [Deribit public summaries](https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=future), [Federal Reserve statement, September 16](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm). Secondary cross-checks: [CoinGecko global](https://api.coingecko.com/api/v3/global), [DefiLlama stablecoin history](https://stablecoins.llama.fi/stablecoincharts/all).

**Regime assessment:** mixed trend with weak ETH participation and a liquidity warning. A four-class Jev regime is not yet validated. Operational action: **HOLD / block new exposure**, rather than converting missing data into a fabricated regime. The local validity flag can be false even though Jev's output enum has only four choices. Funding/OI history, dominance changes, breadth, and a causally available macro calendar remain required.

The captured daily histories have no duplicate timestamps or gaps after sorting completed candles. This is a limited data inspection, not acceptance of a historical trading dataset: revisions, exchange outages, and historical availability still need separate treatment.

## Six candidates and adversarial theses

All six are **hypotheses**. Documented mechanisms are facts about documentation; execution, current parameters, and future catalysts require transaction-level confirmation. No price target, probability of profit, current multiple, or expected return is asserted.

### AAVE — lending cash-flow durability

**Hypothesis:** a valuation discount could be justified as an opportunity if recurring net protocol surplus supports durable AAVE purchases after operating costs and incentives. Governance alone does not establish that discount.

**Documented:** Aave describes revenue-funded buybacks begun in April 2025, with acquired tokens returned to the ecosystem reserve rather than burned. Its token page describes a $50M annual budget. A March 2026 finance discussion proposes reducing that budget to $30M. Treat the conflict as unresolved until votes and executed purchases are checked. [Aave token documentation, accessed October 16](https://www.aave.com/docs/ecosystem/aave), [Aave DAO Funding Insights, March 2026 discussion](https://governance.aave.com/t/aave-dao-funding-insights/24192).

**Supply/unlocks:** historical governance documentation specifies 16M AAVE total supply; this is not a current circulating-supply measurement. Measure reserve distributions, incentives, and attributable sellable supply over the next 30/90 days. [Aave governance introduction, accessed October 16](https://governance.aave.com/t/welcome-to-aaves-governance-discussion/7).

**Catalyst:** recurring executed purchases and sustained net-surplus improvement are conditional triggers, not a newly announced dated event. Budget expansion is speculation until enacted.

**Bear case:** credit losses, oracle failure, subsidized borrowing, higher costs, reserve redistribution, and an already priced buyback. **Invalidate:** observed net surplus cannot fund purchases, governance suspends them, or bad debt overwhelms risk reserves. A current governance contradiction blocks eligibility.

**Missing:** current price/FDV, circulating supply, 30/90-day executed purchases, net revenue, gross fees, chain-adjusted TVL, distinct active borrowers, and entity-adjusted top-holder concentration.

### HYPE — fee demand versus forward issuance

**Hypothesis:** retained trading activity and fee-funded token demand might exceed sellable issuance at an attractive valuation.

**Documented:** Hyperliquid describes automated conversion of assistance-fund fees into HYPE and classifies fund HYPE as burned; fees also support HLP and deployers. Do not equate total venue fees with assistance-fund purchases. [Hyperliquid fees documentation, accessed October 16](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees).

**Supply/unlocks:** current circulating/total supply, contributor releases, staking emissions, and actual burn treatment require primary contract/state verification. No allocation percentages are accepted from third-party tokenomics summaries.

**Catalyst:** verified expansion in durable fee-paying activity and net token absorption. No new future event date verified. **Bear case:** incentive farming, volume concentration, venue/validator risk, liquidation losses, competitor fee compression, and contributor selling. **Invalidate:** fee-funded purchases consistently trail forward sellable issuance, organic traders churn, or venue integrity fails.

**Missing:** current valuation, issuance schedule, 30/90-day fees net of rebates, fund purchases, withdrawable collateral rather than double-counted TVL, active fee-paying traders, and beneficial-owner concentration. No eligibility until measured.

### PENDLE — durable yield demand beyond points

**Hypothesis:** yield-trading revenue can persist after partner points programs end, supporting token demand at a discount to conservative cash-flow value.

**Documented:** team/investor vesting completed in September 2024; documentation describes terminal incentive inflation of 2% annually from April 2026. Current tokenomics also says vePENDLE is winding down. [Pendle tokenomics, accessed October 16](https://docs.pendle.finance/pendle-v2/ProtocolMechanics/Mechanisms/Tokenomics).

**Accrual:** fee documentation assigns 20% of swap fees to LPs, then 80% of remaining swap fees and YT fees to buybacks. sPENDLE documentation describes rewards to eligible active stakers, a 14-day withdrawal period, and a 5% immediate-exit fee. Buyback redistribution is not permanent burning, and liquid spot holdings do not automatically earn staking rewards. [Pendle fees](https://docs.pendle.finance/pendle-v2/ProtocolMechanics/Mechanisms/Fees), [sPENDLE](https://docs.pendle.finance/pendle-v2/ProtocolMechanics/Mechanisms/sPENDLE), accessed October 16.

**Catalyst:** post-maturity retention and executed fee purchases; no new dated catalyst verified. **Bear case:** TVL is rented through points, yield compression cuts revenue, underlying collateral fails, and repeated rollovers are miscounted as adoption. **Invalidate:** fee retention collapses after incentives, net dilution overwhelms purchases, or underlying losses impair demand.

**Missing:** current valuation/supply, actual emissions, cash versus unmonetized points revenue, buyback transactions, net TVL excluding recursive claims, users retained across maturities, and entity-adjusted holder concentration.

### AERO — Base fee growth net of liquidity subsidies

**Hypothesis:** organic fees could grow faster than the dilution required to attract liquidity; the liquid-token valuation might then understate attainable value.

**Documented:** liquid AERO rewards liquidity providers, while locked voting positions receive exchange revenue and external voting incentives. External incentives and swap revenue must be separated. Current documentation also discusses a future Aero transition; do not assume continuity of contracts or economics without checking migration details. [Aerodrome documentation, accessed October 16](https://aerodrome.finance/docs).

**Supply/unlocks:** weekly emissions, locked balances, unlock queues, concentrated voting rights, and migration conversion terms need fresh onchain verification. Historical emissions schedules are not accepted as current values.

**Catalyst:** verified organic volume retention after incentives decline. No migration date is accepted here. **Bear case:** circular emissions/bribes, wash volume, Base concentration, governance capture, and liquid AERO holders receiving none of locked-position income. **Invalidate:** normalized organic fees do not cover economic dilution, repeat users disappear without incentives, or migration terms dilute expected value.

**Missing:** current FDV/float, emissions, unlocks, incentive-adjusted revenue/fees, fee-generating TVL, independent active traders, and top voting/beneficial-holder shares. Initial execution would trade spot only; it must not book veAERO income.

### LDO — conditional buybacks with a revenue hurdle

**Hypothesis:** a discount might exist if net staking revenue recovers enough to fund repeat purchases despite institutional competition.

**Documented historical evidence:** Lido's unaudited H1 2026 report gives $15.9M net DAO revenue, $14.3M foundation expenses, $1.6M operating surplus before nonrecurring costs, and a $4.5M overall loss. Staking TVL was 9.13M ETH at June 30, while staking share fell from 23.93% to 21.18%. The report describes NEST purchases as conditional on clearing a revenue baseline and accrued deficit, not an unconditional current bid. These are period-specific observations, not October run rates. [Lido H1 2026 report, accessed October 16](https://lido.fi/ldo-hub/reports/h1-2026).

**Supply/unlocks:** verify current total/float, treasury acquisitions, grants, and sellable supply; treasury acquisitions are not burns. **Catalyst:** transaction-confirmed NEST activation after the actual deficit clears; timing is speculation. **Bear case:** staking share shrinks, ETH weakness suppresses dollar revenue, fee waivers inflate adoption, and token purchases remain dormant. **Invalidate:** the baseline remains unmet, treasury spend exceeds durable surplus, or paid adoption fails to offset market-share loss.

**Missing:** October valuation, updated supply/unlocks, 30/90-day cash fees/revenue, current net TVL, distinct depositors with custody clustering, concentration, and the live NEST budget balance. H1 TVL cannot stand in for these.

### ONDO — RWA adoption versus token entitlement; avoidance/short research

**Hypothesis:** a token may be overvalued if company/product adoption is incorrectly priced as tokenholder cash flow. This is a potential avoidance or short thesis, not a long finalist.

**Documented:** ONDO governs the DAO/Flux; foundation documentation gives 10B initial supply and no planned inflation. Locked tokens can vote. The token sale risk factors describe limited governance rights rather than shareholder rights. [ONDO token](https://docs.ondo.foundation/ondo-token), [token sale risk factors](https://docs.ondo.foundation/coinlist/coinlist-risk-factors), accessed October 16.

**Supply/unlocks:** no inflation does not mean no unlock selling. The foundation published a distribution framework; verify current vesting contracts and the exact next releases before using dates or quantities. [Foundation distribution proposal](https://blog.ondo.foundation/unlocking-ondo-a-proposal-from-the-ondo-foundation/), accessed October 16.

**Catalyst:** a verified supply release combined with weak marginal demand would be a trigger; no current release quantity is claimed. **Bear case for the short:** a future enforceable value-accrual change, institutional accumulation, scarce borrow, and a narrative squeeze. **Invalidate:** binding tokenholder accrual is implemented or the valuation already discounts absent entitlement and dilution. Product AUM is not ONDO revenue.

**Missing:** valuation, next 90-day supply, token-attributable revenue/fees, distinct product users versus token holders, AUM/TVL bridge, concentration, executable borrow, funding, and squeeze depth. **Spot-only first release cannot open this short.**

## Research admission and finalist compilation

Each candidate requires a versioned evidence record covering all of: price and market cap/FDV at the same cutoff, circulating/total supply reconciliation, 30/90-day unlocks and emissions, fees versus net revenue, incentives, actual token accrual, nonduplicated TVL/AUM, retained active users, and entity-adjusted holder concentration. Unknowns remain null, never zero. Exchange custody, treasury, bridge escrow, and vesting contracts must be identified before interpreting top wallets as whales.

Conservative valuation uses recurring **token-attributable** cash flow after costs, purchase redistribution, and dilution, with sensitivity ranges and no automatic annualization of a good week. A finalist needs a discount under bear assumptions, a priced-in-catalyst check, executable depth and costs, and a documented invalidation. At present none meets that standard.

The spec defines a common typed Jev contract plus a candidate-specific template for each of these six. Their deterministic `eligible` flag is false until the evidence and trading validations pass. Jev cannot turn eligibility on, infer missing fundamentals from order-book shape, or invent catalyst completion.

## WHAT COULD I BE WRONG ABOUT?

- All six ideas may be obvious and already priced. A mechanism is not a valuation edge.
- Reported activity can be circular, subsidized, or controlled by a few entities. TVL and wallets can overstate demand.
- Buybacks may recycle tokens, fall below issuance, pause, or reward locked holders rather than the liquid instrument traded.
- Daily trends and one-venue derivatives observations may miss a sharp intraday change; secondary aggregates can contain bad units or revisions.
- A long-horizon fundamental thesis may have no measurable candle-level timing edge.
- Costs, nonfills, borrow, and regime shifts can erase apparent backtest performance. The acceptable result of this research is no trade.
