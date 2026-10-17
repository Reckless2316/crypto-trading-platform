# AAVE / PENDLE focused proof and gap handoff

Bounded follow-up closed October 17, 2026. Public read-only research; **AAVE and PENDLE remain `eligible=false`**. No source, configuration, tests, commits or activation. This supplement preserves the original [broad report](../research-2026-10-17.md) and its evidence unchanged. No more network retrieval is required for this handoff.

## Findings that can be carried forward

| Question | Captured evidence | What remains unproven |
|---|---|---|
| AAVE purchase continuity | Official TokenLogic governance post discloses a buyback pause effective April 19, 2026. | October pause/resumption status, enacted vote/execution and complete 30/90d purchase/cash reconciliation. |
| AAVE/PENDLE total supply | Keyless Ethereum RPC returned a provider-labelled finalized Ethereum block and ERC-20 call results. | Token-call batch reused request IDs, so results are rejected for validated ingestion. No current total or circulating supply is certified. |
| PENDLE fees/purchases | Official public `/core/v1/spendle/data` returned epoch arrays for fees, revenues, purchases and airdrops. | Explicit units/accounting definitions for generic arrays, transaction settlement, coverage/finality and true publication time. API values are issuer-reported aggregates, not independently proved executions. |
| PENDLE staking | API returned three separate staking strings. | Scaling and physical-token versus virtual-voting reconciliation; none is a validated float denominator. |

## AAVE: historical pause materially qualifies the buyback thesis

Primary [ARFC: Pause AAVE Buybacks](https://governance.aave.com/t/arfc-pause-aave-buybacks/24686) first-post JSON records **created/updated 2026-04-22T18:51:13.688Z**, version 1, author TokenLogic. It states buybacks paused effective April 19 following the rsETH bridge incident, with any resumption to be announced through a standard funding update. Raw `aave_pause_forum.raw`, received **2026-10-17T07:28:20.482511Z**, SHA-256 `a0540a24805327129a9064323256bdb3e6b69bdce6fb7f82180a2e4844622401`.

This is a historical service-provider disclosure within a governance proposal. It does not independently establish executed governance or an ongoing October pause. The fresh token-document narrative about a recurring program must not be interpreted as proof of continuous purchases. No subsequent funding update, governance receipt, purchase logs or complete transaction series was certified during this follow-up.

The [officially linked TokenLogic buybacks route](https://aave.tokenlogic.xyz/buybacks) returned **404** at **07:28:20.093113Z**; its failure is retained in `retrieval-ledger-1.json`. This does not establish absence of a dashboard or purchases. A new dashboard route, its source address definitions and accounting methodology remain access/discovery gaps.

Official token identity from the existing captured [Aave documentation](https://aave.com/docs/ecosystem/aave) is Ethereum `0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9`. Identity alone does not prove float, emissions or treasury inventory. Acquisitions returned to a reusable reserve must remain separate from burning and subsequent distributions.

## Chain snapshot: preserved, rejected token batch

Keyless `https://ethereum-rpc.publicnode.com` returned chain ID `0x1` and a provider-labelled finalized block:

- Height `0x18ff18d` = **26,210,701**.
- Hash `0x0a6f964c48948d5a52f7134cacd9da26aded9cb95d0660d2579153f49a62bf6b`.
- Header timestamp `0x6ad31fd7` = **2026-10-17T07:12:23Z**.
- Local response receipt **2026-10-17T07:28:53.143328Z**.
- Raw `eth_finalized_0.raw`, SHA-256 `6ad98aa9272936e443dcf821217ec26a97a879e0428ae1524b665fc74841436f`.

This is one RPC provider's observation, not a separately verified consensus/state proof. A later batch called `totalSupply()` (`0x18160ddd`), `decimals()` (`0x313ce567`) and `symbol()` (`0x95d89b41`) for AAVE and PENDLE, pinned by block number. **The research script mistakenly reused ID 10 for all AAVE calls and ID 13 for all PENDLE calls.** Response ordering cannot safely associate results with methods; the batch fails strict request/response identity validation. Do not repair it by guessing positional order or magnitude. Raw `eth_token_calls.raw`, received **07:28:53.272101Z**, SHA-256 `3267174e325ce30f2e6a2289480ea81530eb12d240ed89d5611e87b46f4ea771`; complete requests are in `rpc-retrieval-ledger.json`.

A corrective request using unique IDs, a canonical block-hash pin, five PENDLE excluded-address balances and an official address-reference retrieval was submitted for approval. The tool call was **aborted after 324.3 seconds**. No corrective response, ledger or address-reference file exists among the captured files. It is unknown whether any remote request ran; no success, error body or exact abort/publication timestamp is available. The tool's elapsed duration is not a data-source timestamp. Following the explicit stop, no further retrieval was attempted. Total supply, float and the five-address reconciliation therefore remain **null**.

## PENDLE: useful public API, underdefined accounting

The [official API overview](https://docs.pendle.finance/pendle-v2-dev/Backend/ApiOverview) directs third-party integrations to public `/core`, and distinguishes unstable app `/bff` endpoints. The [API documentation](https://api-v2.pendle.finance/core/docs) embeds an OpenAPI document, extracted locally into `extracted-openapi.json` without executing page JavaScript. It identifies `GET /v1/spendle/data`, one computing unit per request. No authenticated, paid or transaction-generation endpoint was used.

Capture metadata:

| Artifact | Receipt October 17 UTC | SHA-256 |
|---|---|---|
| `pendle_api_spendle.raw` | 07:28:21.362671 | `0a4db4e8883514dbdab8ea9af43735a1251f162a7676ea9ab53e465e12e4d73e` |
| `pendle_api_docs.raw` | 07:28:21.320634 | `e38bef8529d0fce33b5aa8eb7d78f1509c2054af940975655ed8c5f84f7f82c6` |
| `pendle_api_overview.raw` | 07:28:20.271368 | `bc5a28bc0914ab766ba48706e6ddb27cf4b68183711148d5cbc5af5ed0a29bbe` |
| `pendle_investor_app.raw` | 07:28:20.824039 | `8d63df4c531cd1071f3fafed98745d9b0fa5fed9b003f917fc33d3f0275c5366` |

The overview describes 12 historical epochs, whereas the live OpenAPI says every epoch and the response includes **19**. This contract drift must be versioned; do not truncate to a hardcoded 12 or infer complete history merely from array length. Neither response publication time nor source block is supplied.

The response gives the following **raw field values**, not certified cash-flow observations:

| Epoch timestamp | `revenues` | `fees` | `buybackAmounts` | `ptLoopingFees` |
|---|---:|---:|---:|---:|
| 1791244800 (October 6 00:00 UTC) | 240563.33313297638 | 184530.6724696618 | 0 | 2397.748740101124 |
| 1790035200 (September 22 00:00 UTC) | 191655.04789715516 | 142718.44193812035 | 0 | 2976.9686808128263 |
| 1788825600 (September 8 00:00 UTC) | 272996.95636830706 | 206000 | 82545 | 1285.761368886188 |

OpenAPI explicitly denominates `ptLoopingFees` in **USD** and says it is already included in `fees` unless a buyback value overrides it. Do not add it again. The generic `revenues`, `fees` and `buybackAmounts` schema lacks adequate units and settlement/accounting definitions for certification. The table intentionally assigns no invented currency/token units to those columns. Zero purchase entries and `lastEpochBuybackAmount=0` cannot establish that no onchain buying occurred or that future buying is halted.

Airdrop data is separate: `airdrops` is deprecated in favor of `airdropInUSDs`; breakdowns identify individual tokens/amounts/valueInUSD. Do not book offchain points or in-kind token distributions as cash, and do not infer `revenues = fees + airdropInUSDs` without a reconciled definition. No exact rolling 30/90d fees or net revenue can be compiled safely from unqualified epoch timestamps.

Raw staking strings are `totalPendleStaked="92247017076059887543224574"`, `totalStakedInSpendle="28821429878398092829756579"`, `virtualSpendleFromVependle="167983274836185775258544847"`. OpenAPI describes physical locked PENDLE, circulating sPENDLE and virtual units separately. Decimals/scaling and the reconciled token balances were not validated. In particular, virtual voting power must not be added to physical token locks or subtracted from total supply as if it were circulating token inventory.

Existing official [sPENDLE documentation](https://docs.pendle.finance/pendle-v2/ProtocolMechanics/Mechanisms/sPENDLE) names purchase contract `0x9e08C5499f953C6297A7755BcBcEd383b606896b`; receipt, funding, swap and redistribution logs were not retrieved here. The Ethereum token address used in the failed RPC validation was `0x808507121B80c02388fAd14726482e061B8da827`; an official bridge-document search result supplied the reference, but the immutable HTTP capture was part of the interrupted correction. Preserve that address-identity evidence gap before admitting chain-derived observations.

## Task 6 / 22 requirements and residual gaps

1. **Supply proof:** unique JSON-RPC IDs; independent canonical block identity; verified official contract identity/proxy implementation; explicit decimals; total supply plus each documented excluded balance at the same block; current bridge escrow/redeemed representations, mint/emission and vesting reconciliation. Current total is not float or maximum future supply.
2. **Purchase proof:** authoritative current governance parameters and executed receipts; complete funded/settled swap logs; token receipt destinations; distinguish reserve acquisitions, permanent destruction and later redistribution. Establish coverage before treating an empty query or zero API field as zero purchases.
3. **Cash-flow proof:** source schema/version/hash, explicit units and accrual/cash treatment; epoch interval and finalization/publication semantics; cash, in-kind distributions, points, rebates and costs in separate fields. Reconcile API aggregate to transaction amounts before conservative valuation.
4. **Causality:** retain header/as-of, publication, local request and receipt times independently. An October API response describing a September epoch was not automatically known in September. The finalized block's 07:12 timestamp is not the 07:28 receipt time.
5. **Admission:** no current total/float, verified 30/90d purchases or net revenue was certified. Keep every unresolved field null with a reason, and both eligibility flags false. These captures improve source discovery and reveal contradictions; they cannot activate a thesis or executable trade.
