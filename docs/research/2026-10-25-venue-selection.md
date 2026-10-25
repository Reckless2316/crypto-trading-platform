# Primary trading venue: selection

Researched 2026-10-25. The operator's rule: **among the exchanges on the API-status list, pick the
one with the lowest fees that serves US residents.** It becomes the platform's primary trading
venue. Fees change often, so the platform should read the account's real tier from the
exchange's fee endpoint once keys are configured, not trust this table.

## Decision: OKX US (`okxus`)

On a like-for-like spot comparison with confirmed fee schedules, OKX US is the cheapest
US-accessible exchange on the list. It has a working trading API (CCXT `okxus`, `us.okx.com`,
verified live today), and the operator already has an OKX account and keys. It is now the
default `SURGE_LIVE_EXCHANGE`. Live trading stays disabled and unarmed.

**Two caveats that could change the answer:**

1. **OKX US does not serve New York, Texas or US territories.** If the operator lives there,
   the fallback is Kraken (spot).
2. **US-regulated perpetual futures are 10–100× cheaper than any US spot market**, but they are
   futures, not spot (see the table). Kraken Derivatives US charges about $0.0113 per 0.01 BTC
   contract per side, roughly 0.0014%. That makes it the lowest-fee US venue overall. But no
   API for those contracts is documented, so the platform cannot trade them today. If Kraken
   publishes one, or if Coinbase's US perpetual-style futures (reported "as low as 0.02%"
   taker, through the Advanced Trade API) are confirmed, the platform should re-run this
   decision for any strategy that trades often.

**What the fee level means for strategy design.** OKX US entry-tier spot is 0.20% maker and
0.35% taker, so a market-in, market-out round trip costs 0.70%, or 0.40% with limit orders on
both sides. The fade-the-flush study died at about 0.19% round-trip cost. Only low-turnover
strategies (daily bars, few trades) are plausible at these fees. Use maker (limit) orders
by default. VIP 1 (0.10% / 0.20%) roughly halves costs.

## US-accessible venues on the list

| Venue | Product | Maker | Taker | Trading API | Source |
|---|---|---|---|---|---|
| **OKX US** | spot | **0.20%** | **0.35%** | yes (`okxus`) | OKX US schedule dated 2026-09-15, as reported by [CryptoSlate](https://cryptoslate.com/crypto-exchanges/okx-us-exchange-review/); VIP 1 0.10/0.20, VIP 9 −0.005/0.015. Stablecoin pairs differ ([OKX notice](https://www.okx.com/en-us/help/notice-fee-update-for-usdt-usd-usdg-usdt-rlusd-usdt-effective-sept-1)) |
| Kraken | spot | 0.40% | 0.80% | yes | [Kraken fee schedule](https://www.kraken.com/features/fee-schedule) (official): $0+ tier; 0.22/0.38 from $10K; 0/0.10 from $10M |
| Kraken Derivatives US | perps (Bitnomial) | ≈0.0014% | ≈0.0014% | **not documented** | [Kraken US futures fees](https://support.kraken.com/articles/us-futures-fees): $0.0113/contract/side all-in; [contract specs](https://support.kraken.com/articles/contract-specifications): BTC 0.01, ETH 0.5, SOL 5. Launched 2026-06-15 ([CoinDesk](https://www.coindesk.com/markets/2026/06/15/kraken-debuts-u-s-perpetual-futures-as-crypto-derivatives-move-onshore)) |
| Coinbase (Advanced) | spot | ~0.50% | ~0.90% | yes | Not confirmed from Coinbase (pages refuse automated fetches). Newest reviews say 0.50/0.90 ([Altrady](https://www.altrady.com/blog/crypto-trading-platforms/coinbase-advanced-trade)); older ones say 0.40/0.60 |
| Coinbase Financial Markets | US perp-style futures | ? | "as low as 0.02%" | Advanced Trade API (reported) | [CryptoSlate, 2025-07-21](https://cryptoslate.com/coinbase-starts-cftc-regulated-perpetuals-for-us-traders-offering-10x-leverage-and-0-02-fees/); no official schedule found |
| Binance Spot | spot | — | — | — | binance.com excludes the US; Binance.US is a separate exchange and is not on the list |

## Exchanges on the list that exclude US residents

These are listed for completeness. None can be the primary venue under the operator's rule.
"Unverified" means no current official or consistent figure was found.

| Exchange | Spot maker / taker | Perp maker / taker | US status |
|---|---|---|---|
| Binance | 0.10 / 0.10 | 0.02 / 0.05 | excluded (API returns HTTP 451 to US IPs) |
| Bybit | 0.10 / 0.10 | 0.02 / 0.055 | excluded (API returns HTTP 403 to US IPs) |
| OKX (global) | 0.08 / 0.10 | 0.02 / 0.05 | excluded; US users go to OKX US |
| Bitget | 0.10 / 0.10 | 0.02 / 0.06 | excluded |
| KuCoin | 0.10 / 0.10 | 0.02 / 0.06 | excluded |
| Gate.io | ~0.10 / ~0.10 (sources disagree) | 0.02 / 0.05 | excluded |
| MEXC | 0 / 0.05 (MEXC's own claim) | 0 / 0.02 | excluded |
| Phemex | — / 0.10 | — / 0.06 | excluded |
| Hyperliquid | 0.04 / 0.07 | 0.015 / 0.045 | US persons barred by terms |
| Deribit | n/a | unverified | excluded |
| dYdX | n/a | unverified | excluded; front end geoblocks the US |
| BitMEX | unverified | unverified | excluded (API returns HTTP 403 to US IPs) |
| Bitfinex | unverified | unverified | excluded |
| HTX | unverified | unverified | excluded |
| WEEX | 0 / 0.10 (reported) | unverified | excluded |

Non-US rates come from third-party comparisons ([Coin Bureau](https://coinbureau.com/analysis/best-crypto-exchange-with-lowest-fees),
[crypto.news](https://crypto.news/maker-and-taker-fees-compared-across-8-crypto-exchanges/)) and
[Hyperliquid fee summaries](https://eco.com/support/en/articles/15039715-hyperliquid-fees-explained-maker-taker-funding).
US status draws on the same sources plus
[Kevin Wu's restricted-countries list](https://kevin-wu.org/en/exchange-restricted-countries-en/), and on HTTP 403/451
responses observed from this machine's US IP on 2026-10-25.

## Not exchanges

CoinGecko, Fear & Greed, Coinalyze, ForexFactory, CFTC, Farside, SoSoValue, Polymarket and
mempool.space are data sources, not trading venues. "Bentley" and "Market Intel Feed" could
not be identified. See [DATA_SOURCES.md](../DATA_SOURCES.md).
