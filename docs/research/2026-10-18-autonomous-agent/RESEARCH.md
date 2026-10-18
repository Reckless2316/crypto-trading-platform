# Autonomous crypto agent: primary-source research refresh, 2026-10-18

Prepared **2026-10-18 UTC**. All sources were retrieved live in one pass, **2026-10-18 12:55:55 – 12:57:33 UTC**. This is advisory research only. **All six candidates remain `eligible=false`; there are zero qualified finalists.** No code, configuration, exchange account or candidate-admission state was changed. No authenticated or paid API, exchange key, order or private data was used.

**Regime classification: `unclassified`** (reasons in §1.4).

## 0. Provenance rules

- Every raw response is saved under [`evidence/`](evidence/), with SHA-256 hashes in [`evidence/MANIFEST.json`](evidence/MANIFEST.json). Per-request URL, HTTP status, request start and receipt time are in [`evidence/retrieval-ledger.json`](evidence/retrieval-ledger.json). Raw files over 150 KB are stored gzip-compressed (mtime 0); the manifest records both the raw and the compressed hash.
- Derived numbers (SMAs, returns, volatility, funding means, ratios) are in [`evidence/observations.json`](evidence/observations.json) and can be recomputed from the raw files.
- In the tables below, **"@hh:mm:ss"** is the UTC receipt time on 2026-10-18 from the ledger. Each row links its source URL.
- **Source classes:**
  - **Primary:** exchange public APIs for their own trades, books and derivatives; chain RPC reads; the issuer's own pages (Circle, Federal Reserve, protocol documentation).
  - **Secondary aggregates**, labelled wherever used: CoinGecko, DefiLlama and Ethplorer.
- Values marked **(derived)** are my arithmetic on cited inputs. Values marked **(estimate)** or **(speculation)** are not observations.
- A blank or `null` means the value was not obtained. Gaps are never filled with estimates presented as fact.

## 1. Regime inputs

### 1.1 Trend: completed daily candles

Only candles whose 24 h interval had fully elapsed at retrieval are used. The last completed candle is **2026-10-17 00:00–24:00 UTC** on both venues. Coinbase showed 349 completed contiguous daily rows with 0 gaps (BTC and ETH). Returns are close-to-close over completed candles. Volatility is the sample standard deviation of the last 30 daily log returns × √365.

| Metric | BTC Coinbase | BTC Kraken | ETH Coinbase | ETH Kraken |
|---|---:|---:|---:|---:|
| Close, Oct 17 | 81,378.21 | 81,382.0 | 2,399.68 | 2,399.70 |
| SMA20 (derived) | 83,403.26 | 83,404.82 | 2,571.46 | 2,571.47 |
| SMA50 (derived) | 81,320.48 | 81,322.02 | 2,551.43 | 2,551.44 |
| SMA200 (derived) | 72,424.51 | 72,425.02 | 2,146.19 | 2,146.18 |
| 7d return (derived) | −1.85% | −1.85% | −4.16% | −4.17% |
| 30d return (derived) | +6.59% | +6.58% | −1.87% | −1.87% |
| 90d return (derived) | +25.81% | – | +28.26% | – |
| 30d realized vol, annualized (derived) | 37.88% | – | 43.49% | – |
| Max close, last 90 days | 86,594.94 | – | 2,775.58 | – |

Sources:
- [Coinbase BTC-USD](https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400) @12:55:55
- [Coinbase ETH-USD](https://api.exchange.coinbase.com/products/ETH-USD/candles?granularity=86400) @12:55:56
- [Kraken XBTUSD](https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1440) @12:55:57
- [Kraken ETHUSD](https://api.kraken.com/0/public/OHLC?pair=ETHUSD&interval=1440) @12:55:57

Kraken agrees with Coinbase to within 0.01% on every shared metric, so the trend inputs are not venue-specific artifacts.

Reading:
- **BTC** is below its SMA20 but sits about 0.07% above its SMA50 (derived), and is about 12% above its SMA200 (derived).
- **ETH** is below both SMA20 and SMA50 and above its SMA200. Its 7d and 30d returns are weaker than BTC's.

### 1.2 Dominance and stablecoin liquidity

| Input | Value | Source |
|---|---|---|
| BTC dominance (secondary) | **60.06%**; source timestamp 12:47:54 UTC | [CoinGecko global](https://api.coingecko.com/api/v3/global) @12:55:58 |
| ETH dominance (secondary) | **10.76%** | same |
| Total crypto market cap (secondary) | $2.731T; 24h **−3.50%** | same |
| USD-pegged stablecoins (secondary) | **$304.584B**, as of 2026-10-18 00:00 UTC | [DefiLlama stablecoincharts/all](https://stablecoins.llama.fi/stablecoincharts/all) @12:55:58 |
| Stablecoins 7d / 30d / 90d change (derived) | **−0.49% / +0.53% / +1.14%** | same |
| USDC in circulation (issuer) | **$72.48B**; page label **"Oct 15, 2026"** | [Circle transparency](https://www.circle.com/transparency) @12:55:59 |
| USDC issued / redeemed, 7d | $10.90B / $11.43B; net **−$0.53B** (derived from rounded figures) | same |
| USDC issued / redeemed, 30d | $43.85B / $44.98B; net **−$1.13B** (derived from rounded figures) | same |

Limits of these inputs:
- **Dominance:** a single secondary snapshot. Without an independently measured 7d/30d series it supports no rotation claim.
- **DefiLlama:** USD-pegged category only. The universe and revisions are outside my control.
- **Circle:** the page has **not updated since the 2026-10-17 research**; it still shows the Oct 15 label and the identical figures. It covers one issuer, not all stablecoin liquidity.

### 1.3 Derivatives: funding and open interest

**Unit convention.** Funding is quoted as a fraction per 8 h, with percent in brackets. A positive rate means longs pay shorts.

| Venue / instrument | Funding now | Funding history (derived means) | Open interest | Sources |
|---|---|---|---|---|
| Deribit BTC-PERPETUAL | `funding_8h` **0.00004091 (0.0041%)**, projected current window | Hourly `interest_8h`, 720 points from Sep 18 13:00 to Oct 18 12:00 UTC: 30d mean 0.0000457, 7d mean 0.0000503 | **$842,914,190** (USD-amount units) | [summary](https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=future) @12:55:59; [history](https://www.deribit.com/api/v2/public/get_funding_rate_history?instrument_name=BTC-PERPETUAL) @12:56:00 (query with start/end timestamps in the ledger) |
| Deribit ETH-PERPETUAL | `funding_8h` **0.000073 (0.0073%)**; `current_funding` 0.000123 | 30d mean 0.0000337; 7d mean 0.0000363 | **$244,469,054** | [summary](https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=ETH&kind=future) @12:56:01; [history](https://www.deribit.com/api/v2/public/get_funding_rate_history?instrument_name=ETH-PERPETUAL) @12:56:01 |
| OKX BTC-USDT-SWAP | current period **0.0000443 (0.0044%)**; next funding 16:00 UTC | Settled `realizedRate`, 100 periods from Sep 15 08:00 to Oct 18 08:00: last-21 mean 0.0000433, all-100 mean 0.0000427 | **$2.495B** now. Daily OI history: last full day $3.331B; 7d **+0.85%**, 30d **+14.2%** (derived) | [funding](https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP) @12:56:05; [history](https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100) @12:56:05; [OI](https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP) @12:56:06; [OI history](https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=BTC&period=1D) @12:56:08 |
| OKX ETH-USDT-SWAP | **0.0000925 (0.0092%)** | last-21 mean 0.0000329; all-100 mean 0.0000330 | **$1.425B** now. Daily history: last full day $1.732B; 7d **−4.51%**, 30d **−2.56%** (derived) | [funding](https://www.okx.com/api/v5/public/funding-rate?instId=ETH-USDT-SWAP) @12:56:06; [history](https://www.okx.com/api/v5/public/funding-rate-history?instId=ETH-USDT-SWAP&limit=100) @12:56:07; [OI](https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=ETH-USDT-SWAP) @12:56:07; [OI history](https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=ETH&period=1D) @12:56:08 |
| Binance USDⓈ-M | **not obtained**: HTTP **451** ("restricted location") on premiumIndex, openInterest and fundingRate for BTCUSDT and ETHUSDT | – | – | e.g. [premiumIndex](https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT) @12:56:02; all six attempts are in the ledger |

Reading:
- Funding is mildly positive on both venues.
- **Derived (estimate, not a forecast):** OKX's BTC settled mean of about 0.0043% per 8 h annualizes to roughly 4.7% per year at constant rate.
- ETH current-window funding is above its own 30-day settled mean on OKX and Deribit. A single window does not establish crowding.

Two comparability caveats:
- **OKX current vs. history OI:** the instantaneous figure (instrument OI) and the daily history series (`rubik` stat, currency-level aggregate) are different measures and are not compared with each other.
- **Venue coverage:** without Binance, cross-venue OI and funding is missing its largest venue.

### 1.4 Macro

| Input | Value | Source |
|---|---|---|
| Effective federal funds rate | **3.88%** on Oct 9, 12, 13, 14, 15 | [H.15](https://www.federalreserve.gov/releases/h15/), release date **Oct 16, 2026** @12:56:10 |
| 2-year Treasury (CMT) | 4.80 → 4.79 → 4.82 → **4.77%** (Oct 9, 13, 14, 15; Oct 12 markets closed) | same |
| 10-year Treasury (CMT) | 5.24 → 5.21 → 5.19 → **5.10%** | same |
| 10-year TIPS | 2.91 → 2.88 → 2.84 → **2.78%** | same |
| FOMC calendar | Sept 15–16 meeting held (minutes released Oct 7). **Next: Oct 27–28.** Then Dec 8–9 (with a Summary of Economic Projections). | [FOMC calendars](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) @12:56:09 |

The EFFR of 3.88% sits inside the 3.75–4.00% target range from the September statement cited in the 10-17 research. I did not re-fetch that statement today. Long yields fell over the week to Oct 15 (derived from the table); I make no claim about the cause. The Oct 27–28 meeting is a **confirmed scheduled event**. Its outcome is unknown.

### 1.5 Regime call

**`unclassified`.** These observations support a descriptive reading, not a validated class:
- **Mixed trend:** BTC is below its SMA20, at its SMA50 and above its SMA200. ETH is below both SMA20 and SMA50.
- **Mild short-term risk-off:** 7d returns are negative and total market cap fell 3.5% over 24h.
- **Flat-to-shrinking stablecoin supply** over 7 days.
- **Mildly positive funding.**
- **Divergent open interest:** BTC OI on OKX grew 30d while ETH OI shrank.

None of this maps to a calibrated `trending` / `mean_reverting` / `high_vol` / `crisis` label, because:
- no calibrated thresholds or percentile history exist;
- Binance is missing;
- dominance is a single secondary snapshot.

Research supports **HOLD / no new exposure**. `regime_valid=false`.

## 2. Candidates

### 2.1 Status table

**Price:** Coinbase last trade. **Total supply:** a primary chain read; Hyperliquid's own API for HYPE. **Fees / revenue / holder revenue / TVL:** DefiLlama (secondary). **Market cap:** CoinGecko (secondary) unless marked derived.

| | AAVE | HYPE | PENDLE | AERO | LDO | ONDO |
|---|---:|---:|---:|---:|---:|---:|
| Last trade (USD) | 153.62 | 78.66 | 2.0199 | 0.83497 | 0.3842 | 0.43747 |
| 7d / 30d return (derived, completed candles) | −9.9% / +18.0% | −8.7% / −8.3% | −6.1% / −11.8% | −10.2% / +43.8% | −9.7% / +8.1% | −11.6% / +17.7% |
| 30d realized vol, annualized (derived) | 83.6% | 61.3% | 82.7% | 116.1% | 122.2% | 125.5% |
| Total supply (primary) | 16,000,000 | 998,847,686 (max 1.0B) | 281,527,448 | 2,002,379,494 (Base) | 1,000,000,000 | 10,000,000,000 |
| Circulating (secondary unless noted) | 15.44M | 302.08M per Hyperliquid API vs **222.45M per CoinGecko**: conflict | 173.85M | 1,003.89M | 829.57M | **not obtained** (CoinGecko 429) |
| Market cap (secondary) | $2.37B | $17.49B (CG); **$23.76B derived** from the Hyperliquid circulating figure | $351M | $836M | $319M | not obtained |
| Fees, 30d (secondary) | $38.45M | $86.83M | $0.49M | $11.89M | $51.96M (staking rewards) | $5.73M |
| Revenue, 30d (secondary) | $5.03M | $69.94M | $0.48M | $7.95M | $3.20M | **$0** |
| Holder revenue, 30d / 1y (secondary) | **$0** / $17.79M | $69.94M / $689.1M | $0.36M / $11.29M | $7.95M / $87.78M | $2.25M / $7.06M | $0 / $0 |
| TVL (secondary) | $18.00B (sum of 7 deployments) | $7.07B (bridge $6.76B; perps/outcomes null) | $1.11B | $393M | $23.44B | **null** for all 3 children |
| Top-10 address share (secondary, not entity-adjusted) | 44.07% (top-1 15.46%) | no source | 67.35% (top-1 22.53%) | no source | 50.46% (top-1 12.15%) | 67.82% (**top-1 51.79%**) |
| Admission | **false** | **false** | **false** | **false** | **false** | **false**; outside spot scope as a short |

Sources:
- **Tickers:** `https://api.exchange.coinbase.com/products/{SYM}-USD/ticker` and `.../candles?granularity=86400`, @12:56:10–12:56:15 (per-symbol times in the ledger).
- **Fees:** `https://api.llama.fi/summary/fees/{aave|hyperliquid|pendle|aerodrome|lido|ondo-finance}?dataType={dailyFees|dailyRevenue|dailyHoldersRevenue}`, @12:56:23–12:56:42.
- **TVL:** [api.llama.fi/protocols](https://api.llama.fi/protocols) @12:56:42. TVL is summed over children sharing `parentProtocol`; for Lido it is the single `lido` entry.
- **Total supply:** `eth_call` `totalSupply()` / `symbol()` / `decimals()` against [ethereum-rpc.publicnode.com](https://ethereum-rpc.publicnode.com) and [base-rpc.publicnode.com](https://base-rpc.publicnode.com) (AERO), @12:56:46–12:56:52. Each returned symbol matched the expected ticker. Contracts: AAVE `0x7Fc6…DaE9`, LDO `0x5A98…1B32`, PENDLE `0x8085…8827`, ONDO `0xfAbA…9BE3` (the same address is quoted in Ondo's own docs), AERO `0x9401…8631`.
- **HYPE supply:** [Hyperliquid info `tokenDetails`](https://api.hyperliquid.xyz/info) (POST `{"type":"tokenDetails","tokenId":"0x0d01dc56dcaaca66ad901c959b4011ec"}`) @12:56:58.
- **CoinGecko:** `https://api.coingecko.com/api/v3/coins/{id}`, @12:56:43–12:56:45.
- **Top holders:** `https://api.ethplorer.io/getTopTokenHolders/{address}?limit=10`, @12:56:53–12:56:55.

**Holder concentration:**
- **No primary source was obtained for any candidate.** A primary measure needs a full-chain balance scan plus entity labelling.
- The Ethplorer figures are address-level shares from a secondary indexer. The largest addresses are typically treasuries, vesting, staking or bridge contracts, or exchange custody. I **did not label them**, so they are not evidence of beneficial-owner concentration.

### 2.2 Per-candidate analysis

Each section separates **confirmed** facts (observed or documented today) from **speculative** ones.

#### AAVE

**Value accrual (documented).** [Aave token docs](https://aave.com/docs/ecosystem/aave) @12:56:59 state:
- a governance-approved buyback program (August 2024), executing since April 2025;
- a **$50M annual budget**, with weekly purchases of **$250k–$1.75M**;
- execution by the Aave Finance Committee multisig.

**Observed:**
- DefiLlama 1y holder revenue is **$17.79M**, but **$0 over 24h, 7d and 30d**.
- 30d revenue is $5.03M.
- **Derived:** about $61M per year at the 30d rate; about 26.9x 1y revenue and 38.7x the 30d run rate, at the CoinGecko market cap.
- Total supply on chain is 16,000,000 (= max).

**Thesis.** Recurring protocol surplus funds sustained open-market purchases against a fixed supply.

**Catalysts:**
- Confirmed: a documented buyback program with a stated budget.
- Speculative: whether purchases are currently executing. The $0 30d holder-revenue figure might mean paused purchases, a DefiLlama attribution lag, or a reclassification; I cannot tell which without the AFC's on-chain transactions.

**Aggressive bear case.** Buybacks are paused or cut, as the 10-17 report flagged with a February ~$30M forum proposal. A bad-debt or oracle event consumes reserves. V4 migration splits liquidity. The 30d +18% move (derived) reverses with 83.6% realized vol. A −40% drawdown is consistent with that volatility and would not require a new event (**speculation**).

**Invalidation:**
- On-chain AFC purchases absent for a full quarter.
- Governance cutting the budget below recurring revenue.
- A reserve loss larger than the 1y holder revenue.

#### HYPE

**Value accrual (documented).** [Hyperliquid fees docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees) @12:56:59:
- Fees go to HLP, the assistance fund and deployers. Deployers may keep up to 50% on spot/HIP-3 assets.
- The assistance fund (system address `0xfefe…fefe`) automatically converts fees to HYPE.
- "HYPE in the assistance fund is burned, removing the tokens permanently from the circulating and total supply."

**Observed (primary API):**
- maxSupply 1.0B; totalSupply **998,847,686**; circulatingSupply **302,078,232**.
- **futureEmissions 410,657,627** (41.1% of max, derived).
- nonCirculatingUserBalances include `0xfefe…fefe` at **48,109,216** and `0x43e9…f251` at **238,000,935** (unlabelled).

**Inconsistency (unresolved).** The docs say assistance-fund HYPE leaves *total* supply. The API still lists the 48.1M balance, and total supply is only about 1.15M below max (derived). Either total supply already excludes it differently, or the "burn" is an accounting classification. I cannot reconcile this from the fetched data. The circulating figure also differs by about 80M between Hyperliquid (302.1M) and CoinGecko (222.4M).

**Fundamentals (secondary):**
- 30d fees $86.83M; 30d revenue = holder revenue = **$69.94M**. DefiLlama classifies assistance-fund buys as holder revenue.
- **Derived:** about $851M per year at the 30d rate. Market cap is $17.5–23.8B depending on the circulating definition.

**Thesis.** Fee-funded automated buy-and-retire outruns sellable issuance.

**Catalysts:**
- Confirmed: an automated, protocol-level fee conversion mechanism.
- Speculative: the release schedule of the 410.7M future emissions. The [tokenomics page](https://hyperliquid.gitbook.io/hyperliquid-docs/hype/hype-tokenomics) returned **HTTP 404** @12:57:01, so **no primary emission schedule was obtained**.

**Aggressive bear case:**
- Emissions or contributor releases of the 410.7M overhang outpace the roughly $70M/month buy.
- Fee compression from HIP-3 deployer share or growth-mode discounts.
- A venue, validator or liquidation incident.
- −8.3% over 30d already shows relative weakness (derived).

**Invalidation:**
- 30d holder revenue falling below the dollar value of 30d net emissions.
- Any loss of assistance-fund automation.
- A venue integrity failure.

#### PENDLE

**Value accrual (documented):**
- [Pendle fees](https://docs.pendle.finance/pendle-v2/ProtocolMechanics/Mechanisms/Fees) @12:57:02: after the LP share, swap fees and all YT fees split **80% PENDLE buyback / 10% treasury / 10% operations**.
- [Tokenomics](https://docs.pendle.finance/pendle-v2/ProtocolMechanics/Mechanisms/Tokenomics) @12:57:02: weekly emission decays 1.1% per week until **April 2026**, then a **terminal 2% per annum**. Circulating supply excludes sPENDLE/vePENDLE contract balances.

**Observed:**
- Total supply 281,527,448 on chain.
- **Estimate:** 2% per year of total supply ≈ 5.6M PENDLE/yr ≈ $11.4M/yr at today's price. This is derived from a policy statement, not a measured emission series.
- 30d holder revenue $0.36M; 1y $11.29M. 30d fees are only $0.49M.

**Thesis.** Fee-funded buybacks exceed terminal emissions once yield-trading demand recovers.

**Catalysts:**
- Confirmed: the documented 80% buyback share and the 2% terminal rate.
- Speculative: any demand recovery. Today's 30d run rate of about $4.4M/yr holder revenue (derived) is **below** the estimated emission value.

**Aggressive bear case.** Fees have collapsed relative to the 1y figure: the 30d run rate is about 39% of 1y (derived). Emissions then dilute faster than buybacks absorb, and the 30d −11.8% trend continues. TVL ($1.11B) is rate-sensitive capital that leaves when yields compress.

**Invalidation:**
- 90d holder revenue staying below the value of terminal emissions.
- A further TVL decline as rate markets mature.

#### AERO

**Value accrual (documented).** The [Aerodrome specification](https://raw.githubusercontent.com/aerodrome-finance/contracts/main/SPECIFICATION.md) @12:57:03 says:
- veAERO NFT votes direct weekly emissions;
- locks last up to four years;
- once tail emissions are on, each epoch can vote to increase, hold or decrease emissions.

This is repository text, not proof of deployed parameters.

**Observed:**
- Total supply **2,002,379,494** on Base.
- CoinGecko circulating 1,003.89M, so about 50% is locked or uncirculating (derived from secondary data).
- 30d holder revenue (to veAERO voters) **$7.95M**; 1y $87.78M; TVL $393M.

**Thesis.** Organic swap fees on Base outgrow emissions.

**Catalysts:**
- Confirmed: a fee-to-voter mechanism.
- Speculative: Base volume growth. Any migration or merger terms remain **unverified** today, as in 10-17.

**Aggressive bear case:**
- Emissions are voted up.
- Volume is incentive-driven or circular.
- The +43.8% 30d move (derived) mean-reverts at 116% realized vol.
- Liquid AERO earns nothing unless locked.

**Invalidation:**
- Voter fees falling while emissions hold or rise.
- Adverse migration terms.
- Base activity decline.

#### LDO

**Value accrual (documented).** The [Lido H1 2026 report](https://lido.fi/ldo-hub/reports/h1-2026) @12:57:03 describes **NEST** (LIP-36):
- When DAO staking revenue exceeds a **$40M annualized baseline**, half the daily surplus buys LDO.
- Buys are capped at **$50k/day and $10M/year**.
- Purchases pause below the baseline.
- H1 operations had a $1.61M surplus before a one-off expense, turning into a **$4.45M loss**.

**Observed (secondary):**
- 1y revenue **$35.01M**; 30d $3.20M (≈ $38.9M/yr derived).
- Both are **below the $40M baseline on DefiLlama's revenue definition**, which may differ from LIP-36's.
- Holder revenue: 30d **$2.25M**, but 7d **$0**.
- TVL $23.44B. Total supply 1.0B on chain.

**Thesis.** Revenue crosses the NEST hurdle and buybacks resume or scale.

**Catalysts:**
- Confirmed: the NEST mechanism and its caps.
- Speculative: crossing the hurdle. The $0 7d holder revenue is *consistent with* purchases pausing below baseline, but that is **inference, not observation**.

**Aggressive bear case:**
- Buybacks are capped at $10M/yr even in success, against a $319M market cap.
- Staking market-share loss.
- Weak ETH: −4.2% 7d, below SMA20 and SMA50.
- A repeat of the H1 one-off expense.

**Invalidation:**
- Revenue staying below the baseline for a full quarter.
- Treasury distributions exceeding acquisitions.

#### ONDO

**Value accrual (documented):**
- [Ondo token docs](https://docs.ondo.foundation/ondo-token) @12:57:04 give the token address, matching the chain read.
- **"The initial token supply of ONDO is 10 billion. There is no scheduled or planned inflation."**
- Transfer restrictions lifted January 18, 2024, with a structured distribution schedule (FIP-08).

**Observed:**
- Total supply exactly 10,000,000,000 on chain.
- DefiLlama fees 30d $5.73M with **revenue and holder revenue $0**. Fees appear product-level and do not accrue to the token on DefiLlama's classification.
- TVL **null** for all three Ondo children.
- Largest address holds **51.79%** of supply (secondary, unlabelled; plausibly a treasury or vesting contract, but **unverified**).
- Circulating supply not obtained (CoinGecko 429).

**Thesis (avoid/short research).** Product adoption may be priced into a governance token with no observed cash accrual.

**Catalysts:**
- Confirmed: no documented inflation; zero token revenue on DefiLlama.
- Speculative: distribution releases from the large address, whose schedule and timing were not obtained.

**Bear case for a short.** Governance enacts fee accrual or a buyback, a treasury lock-up is announced, or RWA momentum squeezes holders. Volatility is high (125.5%). Shorts are outside spot scope.

**Invalidation:** an executable token entitlement to product cash flow, or verified long-dated locks on the top address.

## 3. What changed since 2026-10-17

Compared with [the 10-17 research](../2026-10-17-autonomous-agent/RESEARCH.md):

| Item | 2026-10-17 | 2026-10-18 | Change |
|---|---|---|---|
| Last completed day | Oct 16 | Oct 17 | +1 day |
| BTC close / SMA20 / SMA50 | 81,500.04 / 83,557.45 / 81,249.70 | 81,378.21 / 83,403.26 / 81,320.48 | Close −0.15%; now at the SMA50 rather than clearly above it |
| BTC 7d / 30d | −1.27% / +7.03% | −1.85% / +6.59% | weaker |
| ETH close / SMA20 / SMA50 | 2,419.92 / 2,585.89 / 2,552.29 | 2,399.68 / 2,571.46 / 2,551.43 | close −0.84% |
| ETH 7d / 30d | −2.63% / +0.14% | −4.16% / −1.87% | 30d turned negative |
| Realized vol BTC / ETH | 37.86% / 43.61% | 37.88% / 43.49% | unchanged |
| BTC / ETH dominance (secondary) | 59.31% / 10.74% | 60.06% / 10.76% | BTC +0.75 pp |
| USD-pegged stablecoins (secondary) | $304.859B; 7d −0.366%; 30d +0.622% | $304.584B; 7d −0.486%; 30d +0.529% | slightly lower |
| Circle USDC | $72.48B, Oct 15 label | identical, still Oct 15 | **page not updated** |
| Deribit BTC OI / funding_8h | $841.15M / 0.001487% | $842.91M / 0.0041% | funding higher; OI flat |
| Deribit ETH OI / funding_8h | $244.45M / 0.0015% | $244.47M / 0.0073% | funding higher |
| New inputs | – | Kraken cross-check; Deribit 30d funding history; OKX settled funding and daily OI history; H.15 yields | wider coverage |
| Binance | not used | HTTP 451 | coverage gap recorded |
| Candidate prices | AAVE 156.08, HYPE 79.92, PENDLE 2.1232, AERO 0.89211, LDO 0.3988, ONDO 0.45555 | 153.62, 78.66, 2.0199, 0.83497, 0.3842, 0.43747 | all lower (−1.6% to −6.4%) |
| Candidate supply / fees / TVL | all null | primary total supply for all six; secondary fees, revenue and TVL for all six; address-level holders for four | **gaps narrowed** but are not closed (no emission schedules, no entity-adjusted ownership, no executed-buyback reconciliation) |
| Regime | "mixed", `regime_valid=false` | `unclassified`, `regime_valid=false` | unchanged in substance |
| Admission | all false | all false | unchanged |

New findings that affect the theses:
- AAVE 30d holder revenue is $0.
- HYPE has a 410.7M future-emissions overhang, plus the circulating-supply and burn-accounting inconsistencies.
- PENDLE's 30d buyback run rate is below the estimated terminal emission value.
- LDO's DefiLlama revenue is below the NEST baseline.
- ONDO shows $0 token revenue and a 51.8% top address.

## 4. Gaps that still block admission

- **Emissions and unlocks:** no primary per-event schedule for any candidate. The HYPE tokenomics page returned 404. No DefiLlama emissions API data was used.
- **Executed buybacks:** no transaction-level reconciliation (AAVE AFC, HYPE assistance fund, PENDLE buyback, LDO NEST).
- **Entity-adjusted holder concentration:** no primary source. The Ethplorer data is address-level and secondary.
- **ONDO** circulating supply and TVL are missing.
- **Binance derivatives** are missing. Dominance is a single secondary snapshot.
- **No calibrated regime thresholds.**

## WHAT COULD I BE WRONG ABOUT?

- **The "unclassified" call may be too cautious.** BTC above its SMA200 with 90d returns of +25.8% could be read as an intact uptrend in a pullback. Equally, BTC −1.85% 7d, ETH below its SMA50 and a −3.5% 24h total-cap move could be the start of a breakdown that a calibrated classifier would label risk-off. I am declining to classify, not observing "no regime".
- **Secondary aggregates may be wrong or revised.** DefiLlama attribution is a methodology choice, not an on-chain fact. This covers AAVE's $0 30d holder revenue, the HYPE revenue/holder-revenue identity, ONDO's $0 revenue and the null ONDO TVL. CoinGecko's HYPE circulating supply disagrees with Hyperliquid's by about 80M tokens. I may be over-reading data artifacts as fundamentals.
- **The HYPE burn interpretation.** I flagged an inconsistency between "burned … from total supply" and an API that still lists the assistance-fund balance. The API's `totalSupply` may already net it out by another route, or the docs may describe economic rather than ledger removal. My supply ratios for HYPE could be off by tens of millions of tokens.
- **Address concentration is not ownership.** The 51.79% ONDO address and the large AAVE/LDO/PENDLE addresses are probably contracts (treasury, vesting, staking, bridges) or exchange custody. Treating them as concentrated owners would be wrong. Treating them as harmless would also be unverified.
- **Funding and OI comparability.** Deribit `funding_8h` is a projected window, OKX `realizedRate` is settled, and OKX's daily OI history is a currency-level aggregate. Means across these are not the same quantity. A 30d OI rise on one venue does not imply market-wide leverage growth, especially with Binance missing.
- **Timing.** Every figure is a snapshot from about two minutes of fetching on 2026-10-18. Circle's page has not updated since Oct 15. H.15 is through Oct 15. Daily candles end Oct 17. A material move after 12:57 UTC is not reflected.
- **The PENDLE emission estimate.** It applies a policy rate (2% p.a.) to on-chain total supply. The documented base may be circulating supply or a different reference, and governance may have changed it.
- **Macro direction.** I noted falling long yields but drew no causal conclusion. The Oct 27–28 FOMC outcome could dominate every crypto-specific input here.
- **Survivorship in the candidate list.** These six were chosen before today. Better or worse candidates may exist. An empty eligible universe remains a valid outcome.
