# Autonomous crypto agent — independent research refresh

Prepared **2026-10-17 UTC**, using fresh public retrievals beginning **07:07 UTC** (October 17, 01:07 America/Denver). Advisory research only. **All six candidates remain `eligible=false`; zero qualified finalists.** No production, configuration, exchange account, or candidate-admission state was changed. The approved specification and October 16 research were read for requirements; neither is treated as independent evidence for a market claim below.

## Provenance and timing

Raw responses and SHA-256 hashes are in [research-evidence](research-evidence/). The two main source ledgers are [public market captures](research-evidence/retrieval-ledger.json) and [official documents and six tickers](research-evidence/primary-retrieval-ledger.json); [supplemental sources](research-evidence/supplement-retrieval-ledger.json) includes an explicit AERO asset-fetch failure. [Observations](research-evidence/observations.json) contains derived market observations and null candidate fundamentals.

Official protocol/issuer documents establish what their authors disclose, not independent proof of transaction execution. Exchange public APIs are primary for that exchange's observations. CoinGecko and DefiLlama are explicitly **secondary aggregates**. No figures below were sourced from the existing research report. No authenticated or paid API, exchange key, `.env`, order, or external message was used.

The first market-capture script recorded request start in its original timestamp field. The ledger now names this `request_started_at`, leaves exact `received_at=null`, and preserves file-write time as `received_at_upper_bound`. That bound is not an exact receipt event. Later document/ticker captures record actual post-read `retrieved_at` separately. These artifacts are research captures, not automatically admissible production events. Publication time, accounting period/as-of time, observation event time, and local receipt must remain separate. Unknown publication times remain null. A current webpage cannot prove what was known before retrieval.

## Market regime: verified observations and remaining unknowns

| Component | Fresh evidence | Interpretation and gap |
|---|---|---|
| BTC trend | Coinbase daily candle October 16 00:00–October 17 00:00 UTC closes **$81,500.04**. SMA20 **$83,557.45**, SMA50 **$81,249.70**; 7d **−1.27%**, 30d **+7.03%**. | Mixed: below SMA20, slightly above SMA50. Not a validated trend-regime classification. |
| ETH trend | Same completed interval closes **$2,419.92**. SMA20 **$2,585.89**, SMA50 **$2,552.29**; 7d **−2.63%**, 30d **+0.14%**. | Below both averages, weaker than BTC; no broad altcoin participation claim. |
| Realized volatility | Last 30 completed daily log-return sample standard deviations × √365: BTC **37.86%**, ETH **43.61%** annualized. | Historical estimator, not forecast. Percentile history/calibrated high-vol thresholds missing. |
| Stablecoin issuer | Circle HTML dated **October 15** gives USDC **$72.48B**. 7d issued/redeemed **$10.90B/$11.43B**; 30d **$43.85B/$44.98B**. | Approximate calculated net issuance **−$0.53B/−$1.13B** from rounded disclosed inputs. One issuer, not all-stablecoin liquidity or buying demand. |
| Dominance, secondary | CoinGecko source timestamp **07:04:43 UTC October 17**: BTC **59.3063%**, ETH **10.7420%** of its aggregate. | Single level, no independently measured 7/30d change; no rotation inference. Market-cap dominance requires offchain price/universe conventions and is not chain-primary. |
| Stablecoins, secondary | DefiLlama October 17 00:00 USD-pegged circulating USD value **$304.859B**; 7d **−0.366%**, 30d **+0.622%**. | USD-pegged category only. Do not sum native EUR/JPY/token units. Revisions, universe, bridge de-duplication and issuer reconciliation remain gaps. |
| BTC derivatives | Deribit BTC-PERPETUAL, event **07:07:01.009 UTC**: OI **$841,151,960**; `funding_8h=0.00001487` (rate fraction; **0.001487%**). | Single venue/instant. Current-window projected funding, not historical settled funding. No OI growth/crowding conclusion. |
| ETH derivatives | Deribit ETH-PERPETUAL, event **07:07:00.590 UTC**: OI **$244,451,991**; `funding_8h=0.000015` (**0.0015%**). | Same limitations. Venue aggregation, multi-day history and funding settlement history missing. |
| Macro | September 16 Fed statement raises target to **3.75–4.00%**; official calendar lists **October 27–28** next FOMC meeting. | Tightening is a plausible headwind, not a return forecast. Exact event timestamps, other releases, yields/USD, and historical calendar versions are missing. |
| Narratives | Primary mechanisms map candidates to lending, perpetuals, yield trading, Base liquidity, staking and RWA governance. | Taxonomy only. Timestamped breadth, organic flows, positioning and news diffusion are absent. No claimed active narrative rotation. |

Primary links: [Coinbase BTC](https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400), [Coinbase ETH](https://api.exchange.coinbase.com/products/ETH-USD/candles?granularity=86400), [Circle](https://www.circle.com/transparency), [Deribit BTC](https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=future), [Deribit ETH](https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=ETH&kind=future), [Fed statement](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm), [Fed calendar](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm). Secondary links: [CoinGecko](https://api.coingecko.com/api/v3/global), [DefiLlama](https://stablecoins.llama.fi/stablecoincharts/all).

The candle extraction sorts timestamps, excludes unfinished candles, and finds no duplicate timestamps or gaps in the last 51 completed rows. It does not certify historical availability, absence of revisions, or a production dataset. Formulae and raw inputs are retained in the observation artifact.

**Regime conclusion:** mixed momentum, weak ETH participation and issuer-level liquidity contraction. `regime_valid=false`; missing derivatives history, breadth and calibrated classification prevent assigning a validated Jev class. Research supports **HOLD / no new exposure** while those inputs and candidate admission remain incomplete.

### Source-adapter corrections

**Deribit:** the [official endpoint contract](https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency) defines perpetual/inverse-future OI in **USD amount units**. It distinguishes instantaneous `current_funding` from **projected current-window** `funding_8h`. Persist raw field name, fraction unit, window and semantic type. A historical settled-funding field must not be populated from this response. Snapshot OI must not be transformed into growth without a comparable earlier observation. Raw docs: `deribit_docs.raw`, SHA-256 `401fb3e788c14f740f9e0181b16c3c76fcbcc90ed49276bc72fa7267d0272d54`.

**Circle correction:** an initial research status marked balances missing because browser-extracted text omitted them. Fresh raw HTML recovers them; this supersedes that status and the old October 12 figures. `circle_transparency.raw`, SHA-256 **`03fdf2ff9a34ef87d66afdf1ecd201b3e1db75aa2b44f497345057055ca304bb`**, retrieved **2026-10-17T07:07:36.311635Z**. Page as-of label is **October 15, 2026**; exact publication time is unknown. HTML selectors are `span#usdc-in-circulation[data-point="72.48"]`, `#usdc-issued-7[data-point="10.9"]`, `#usdc-redeemed-7[data-point="11.43"]`, `#usdc-issued-30[data-point="43.85"]`, `#usdc-redeemed-30[data-point="44.98"]`, each with `data-coin="usdc"`. Inline formatter maps `case "usdc"` to `letter="B"`, `symbol="$"`; do not substitute chart canvas attributes for the circulation span. Net calculations use rounded issuer disclosures, so **−$0.53B and −$1.13B are approximate derived amounts**, not more precise observed net flows.

## Candidate valuation snapshots and complete gap matrix

Coinbase last-trade tickers were independently retrieved at **07:07:36 UTC**. They are price observations with different event times, not a synchronized valuation cutoff or executable depth guarantee.

| Candidate | USD last trade | Trade event UTC October 17 | Market cap / FDV |
|---|---:|---|---|
| AAVE | 156.08 | 07:06:20.113893659 | null / null |
| HYPE | 79.92 | 07:07:03.517998729 | null / null |
| PENDLE | 2.1232 | 07:07:01.721802603 | null / null |
| AERO | 0.89211 | 07:07:29.274464697 | null / null |
| LDO | 0.3988 | 07:03:22.360234891 | null / null |
| ONDO | 0.45555 | 07:06:58.871779935 | null / null |

Source URL pattern: `https://api.exchange.coinbase.com/products/{SYMBOL}-USD/ticker`; exact links, raw hashes and response bodies are recorded in `primary-retrieval-ledger.json` and `{symbol}_ticker.raw`. A ticker response does not establish account availability, approved listing, tradable size, compliance status or exchange adapter compatibility.

All **current circulating/total supply**, 30/90d **sellable unlocks/emissions**, 30/90d **cash fees/net revenue**, present **net TVL/AUM**, **retained active users** and **entity-adjusted concentration** remain null for every candidate. Historical policy/disclosures below must not fill those current fields. Consequently no conservative multiple, cash-flow yield, discount or net absorption edge is established.

| Candidate | Supply/unlock evidence and exact missing reconciliation | Revenue/usage/ownership gaps blocking admission |
|---|---|---|
| AAVE | Need current token state, treasury/reserve holdings, grant/incentive outflows and forward sellable supply. No fresh float reconciliation. | Resolve operative buyback budget through executed governance; reconcile 30/90d purchases, recurring surplus after costs/bad debt, gross fees, net TVL, retained borrowers and classified holders. |
| HYPE | Need current issuance/burn state, contributor vesting contracts, staking emissions, 30/90d net sellable supply. No accepted third-party allocation estimate. | Separate net protocol fees from HLP/deployer/referral/rebate flows, verify assistance-fund conversions/burns, retained fee-paying traders, withdrawable collateral and beneficial owners. |
| PENDLE | Team/investor vesting completion is documented historically; terminal 2% policy is not a measured emission series. Reconcile current total, staking/fund balances, actual emissions and old ve-lock expiries. | Realized cash versus offchain points, executed purchases/distributions, post-maturity retained users, collateral-adjusted TVL and entity-adjusted token/voting concentration. |
| AERO | Need deployed minter parameters, actual epoch emissions/rebases, lock expiry queues, float and migration terms. Repository descriptions are not live chain state. | Isolate swap fees from external incentives, wash/circular volume, repeat traders, fee-generating reserves and managed-NFT ownership/voting power. Migration validation incomplete. |
| LDO | Need current total/float, treasury grants/distributions, acquisitions and next 90d sellable supply. Treasury receipts are not burns. | Live NEST budget/deficit and purchase transactions; 30/90d fees/revenue/costs; current ETH staking deposits/queue treatment; retained depositors and custody-cluster concentration. H1 metrics are stale for current admission. |
| ONDO | 10B **initial** supply/no planned inflation documented. Current float, treasury/vesting state and next 30/90d releases still unknown. | Verify token entitlement, token-attributable fees/revenue, product AUM-to-token bridge, distinct product users and holder entities. Any short additionally needs borrow/funding/depth; outside initial spot scope. |

## Primary source ledger, theses and invalidation

The following are conditional hypotheses. Confirmed documentation and issuer reports are identified separately from unconfirmed executions/future events. There is no independently verified new dated catalyst that admits a candidate.

### AAVE

Fresh [Aave token docs](https://aave.com/docs/ecosystem/aave) describe revenue-funded purchases begun April 2025, a $50M annual budget and reserve reuse rather than burning. The [finance forum first post](https://governance.aave.com/t/aave-dao-funding-insights/24192), created **February 28, 2026 17:03:46 UTC** in fresh forum JSON, proposes ~$30M instead. This is a proposal/docs conflict until enacted governance and executions are reconciled, not proof of either live budget. Both retrieved October 17 07:07 UTC.

Hypothesis: conservative recurring net surplus plus verified purchases can justify undervaluation. Conditional catalyst: sustained realized surplus and net reserve accumulation; expansion is speculative until enacted. Bear case: losses, oracle failure, subsidies, spending and reserve redistribution absorb cash. Invalidate if durable surplus cannot fund purchases, suspension occurs, or dilution/losses overwhelm purchases. **Admission false.**

### HYPE

Fresh [official fees documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees) states fees flow among HLP, assistance fund and deployers; assistance-fund conversions are automated and its HYPE is classified as burned. It also documents discounts/rebates and growth-mode fee reductions. Thus total volume or headline fees cannot stand in for token purchases. Retrieved October 17 07:07 UTC; document publication time unknown. No primary forward issuance series was obtained.

Hypothesis: organic fee-funded absorption can exceed sellable issuance at conservative valuation. Conditional catalyst: verified net absorption and retention; no accepted future event date. Bear case: subsidized/concentrated activity, fee compression, contributor selling, validator/venue or liquidation losses. Invalidate on persistent absorption deficit, fee-paying trader churn or venue integrity failure. **Admission false.**

### PENDLE

Fresh [tokenomics](https://docs.pendle.finance/pendle-v2/ProtocolMechanics/Mechanisms/Tokenomics) documents team/investor vesting complete September 2024 and a terminal 2% annual incentive policy from April 2026; its supply definition excludes named staking/fund addresses. [Fees](https://docs.pendle.finance/pendle-v2/ProtocolMechanics/Mechanisms/Fees) assigns 20% of swap fees to LPs and 80% of the remaining swap fees plus YT fees to buybacks. Points can be offchain. [sPENDLE](https://docs.pendle.finance/pendle-v2/ProtocolMechanics/Mechanisms/sPENDLE) describes active-staker rewards, 14-day withdrawal/5% immediate-exit fee and purchase contract `0x9e08C5499f953C6297A7755BcBcEd383b606896b`. These are documented mechanics, not verified transactions. Retrieved October 17 07:07–07:08 UTC; publication unknown.

Hypothesis: nonpoints fee demand persists after maturities and incentives. Conditional catalyst: retained users/cash fees and executed purchases; no new dated event confirmed. Bear case: rented TVL, yield compression, underlying failure and recycled rollovers. Invalidate if retention/cash fees collapse or dilution exceeds sustainable purchases. Liquid PENDLE does not automatically receive sPENDLE distributions. **Admission false.**

### AERO

Fresh [official website](https://aerodrome.finance/docs) identifies NFT voting and fees in metadata but serves an application shell; linked JavaScript returned **403**. Lookalike search domains were excluded. The website explicitly links the official GitHub organization; its [protocol specification](https://github.com/aerodrome-finance/contracts/blob/main/SPECIFICATION.md), freshly retrieved **07:08 UTC**, describes minter emissions, vote escrow, and separate fee/bribe rewards. Repository policy/history is not deployed parameter evidence. Current migration terms/date are unverified; the old research's transition description is not re-certified here.

Hypothesis: normalized organic swap demand can outgrow liquidity subsidies. Conditional catalyst: sustained repeat-user fees after incentives decline; migration benefit remains speculation. Bear case: circular bribes/emissions, wash volume, Base dependence, voting capture and adverse migration. Invalidate if organic economics fail after dilution or conversion terms destroy expected entitlement. Spot AERO does not earn veAERO income automatically. **Admission false.**

### LDO

Fresh [official H1 report](https://lido.fi/ldo-hub/reports/h1-2026), retrieved **07:07 UTC**, discloses unaudited H1 net DAO revenue **$15.9M**, expenses **$14.3M**, pre-nonrecurring surplus **$1.6M**, overall loss **$4.5M** and June 30 staking TVL **9.13M ETH**. It describes NEST requiring revenue above a **$40M annualized baseline** and deficit clearance; one-time stETH/LDO trades differ from NEST. A note says a second July batch is reflected in the June reconciliation. Publication/receipt, not June accounting date, governs causal availability. Historical purchases do not prove October NEST activation.

Hypothesis: recurring surplus eventually funds durable purchases. Conditional catalyst: onchain hurdle/deficit clearance and actual purchases; timing unconfirmed. Bear case: market-share loss, waivers, weak ETH and expenses; report numbers are not current run rates. Invalidate if the hurdle stays unmet or purchases exceed durable surplus. **Admission false.**

### ONDO

Fresh [foundation token docs](https://docs.ondo.foundation/ondo-token) document governance over DAO/Flux, **10B initial supply**, no planned inflation and voting by locked tokens. The [distribution proposal](https://blog.ondo.foundation/unlocking-ondo-a-proposal-from-the-ondo-foundation/), revised **December 6, 2024**, is a historical framework, not verified current vesting execution. Both retrieved October 17 07:07 UTC. No fresh enforceable entitlement to product/company cash flow was established; that absence of verification must not be asserted as a proven universal absence of rights.

Hypothesis: product adoption can be overpriced into a governance token without matching entitlement, creating avoidance/short research. Conditional catalyst: verified sellable release plus weak demand, not an accepted release date/amount. Bear case for a short: binding accrual change, accumulation and squeeze. Invalidate when executable entitlement changes or valuation already discounts dilution/rights. Product AUM is not ONDO revenue. **Admission false; neutral in initial spot scope.**

## Task 6 machine-ingestion contracts

These are advisory contract requirements for the implementation owner; they do not enable ingestion or admission automatically.

1. **Evidence envelope:** stable source ID/URL, primary/secondary classification, raw SHA-256/path, parser version, metric definition, unit, chain/contract/instrument, event/as-of time, publication time (nullable), request/received times, period start/end, block hash/height/finality or exchange identity, revision knowledge time and expiry. Missing receipt/publication must not be silently replaced with period dates. Unknown is null plus reason/validity false, never zero.
2. **Price/supply:** decimal price and quote currency, source event time, synchronized cutoff tolerance; independently observed circulating/total/maximum supply with exclusions and bridge/staking reconciliation. Derive market cap/FDV only when denominators and timestamps are valid. Finite current supply is not automatically maximum supply; inflationary assets need explicit FDV convention/horizon. No stale initial allocation multiplied by a fresh ticker as certified FDV.
3. **Forward supply:** per-event unlock/mint/burn/relock records with amount/base-token units, effective timestamp, holder class, already-circulating flag, contract/governance execution evidence and certainty. Compute 30/90d totals only from complete coverage; separate maximum authorized, scheduled, released and sellable. Buybacks into reusable reserves do not equal burns.
4. **Fundamentals:** 30/90d gross fees, rebates/referrals, incentives, protocol-retained revenue, recurring expenses/bad debt and token-attributable cash flow; each separate from offchain points. Transaction IDs must reconcile purchases, burns and redistribution. Conservative valuation requires bear/base assumptions, dilution, costs and no automatic annualization of a strong week.
5. **TVL/users/concentration:** chain/asset balances with common cutoff and price marks; exclude recursive claims and bridge duplication; distinguish AUM/collateral from fee-bearing assets. Specify user activity/retention window and entity-clustering method. Holder classes must separate custody, bridges, escrow, treasuries, vesting and beneficial owners; wallet count/top-wallet share cannot substitute for users/concentration.
6. **Regime adapters:** completed-candle windows and fixed-cadence return formula; derivatives distinguish projected/instantaneous/settled rates and explicit fraction/window units; comparable OI snapshots and history; secondary dominance/stablecoin universe versions and revisions; macro event publication, timezone and confirmed schedule. Unknown components leave regime validity false.
7. **Catalyst/admission:** proposed, approved, scheduled and executed are separate states backed by primary evidence with expiry/invalidation. Deterministic admission requires all approved evidence, conservative discount, priced-in-catalyst analysis, execution depth/costs, calibration and release approval. The six observation rows are hypotheses only; a document, model judgment or ticker cannot turn `eligible` on. ONDO short remains outside spot execution.

No concentration, unlock, current revenue or TVL estimate was filled by inference. That incompleteness is the intended gap output, not a successful candidate screen.
