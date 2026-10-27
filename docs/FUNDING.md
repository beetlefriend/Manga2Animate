# Sakuga Studio Production Fund: fees to frames

The kit costs money to run: every cut is a few dozen Gemini calls and a handful
of video-model requests. The funding idea is simple and should stay simple:
**trading fees on $SAKUGA flow into a treasury, the treasury buys generation
credits, the credits make cuts, the cuts are public, the ledger is public.**

This document is the design. Nothing in it is live until `site/token.config.js`
says `mode: "live"` and `treasury/ledger.json` has real entries.

## 1. Where the fee comes from

Pick one; they are not exclusive but one is enough to launch.

| mechanism | how fees arrive | pros | cons |
|---|---|---|---|
| **Launchpad creator fee** (pump.fun-style creator revenue share on Solana) | the launchpad streams a share of trading volume to the creator wallet | zero contract work, familiar to traders, no tax on holders | fee rate is the platform's, not ours; stops if the platform changes terms |
| **Token-2022 transfer fee** (`feeBps` in `token.config.js`, e.g. 100 = 1%) | every transfer withholds the fee into the mint's fee account; the treasury harvests it | fee rate is ours; works on every DEX | some wallets / aggregators mishandle Token-2022; a "tax token" label |
| **Own liquidity position** (Meteora / Raydium LP owned by the treasury) | swap fees accrue to the LP position | aligned with liquidity depth | needs seed capital; fee income scales with volume only |

Recommendation: launch with the launchpad creator fee (fastest, cleanest), keep
the Token-2022 option for a v2 token if the platform share proves too small.

## 2. Where it goes

```
trade ──► fee ──► Production Fund wallet (TOKEN.treasuryWallet)
                        │
                        ├── 80%  credits  (Gemini / Higgsfield / Ark)   feeSplit.credits
                        ├── 10%  operations (hosting, domains, ffmpeg box) feeSplit.operations
                        └── 10%  liquidity (kept as SOL/LP)               feeSplit.liquidity
```

The split is a published number in `site/token.config.js` and is enforced by
hand on each sweep until it is worth automating (see §5).

## 3. The off-ramp problem (be honest about it)

The providers the pipeline uses take **cards**, not SOL:

| provider | pays with | crypto path |
|---|---|---|
| Google AI Studio / Vertex (Gemini) | card / GCP billing | none native; some resellers (OpenRouter) accept crypto for Gemini text and image models, and the kit's text agents can point at an OpenAI-compatible endpoint with a small adapter (not written yet) |
| Higgsfield credits | card | none native |
| BytePlus Ark (Seedance) | card / invoice | none native |

So the conversion is: fee (SOL) -> swap to USDC -> off-ramp to a card or a
crypto-funded prepaid card -> buy credits. That last hop is manual and done by
whoever holds the treasury key. The ledger makes it auditable: every top-up is a
row with the on-chain outflow and the provider receipt total.

Automation candidates once volume justifies it: an x402 / agent-payments
provider for Gemini-class models, or a GPU host that accepts USDC for a
self-hosted open model (MiniMax-H3 / AniSora on rented 95 GB cards).

## 4. The ledger

`treasury/ledger.json` (schema in `site/README.md`, sample in `site/data/sample.json`):

- `treasury`: current SOL balance and, when a price source is configured, a USD estimate
- `fees_in_total_usd`, `credits_bought_usd`: running totals
- `cuts[]`: every produced episode with its cost and video link
- `queue[]`: what holders voted to animate next

`treasury/sweep.py` refreshes the balance from a public Solana RPC, counts the
finished cuts under `results/`, and writes the ledger the site reads in live
mode. It never moves funds; it only reports.

## 5. Governance-lite

- Holders vote on the **next manga** from an eligible list (see §6). A vote is a
  signed message, tallied off-chain and shown in the queue; no on-chain
  governance contract for v0.
- The Production Fund publishes a **cost per cut** after each run so the
  community can see what one episode costs at each creativity level.
- Treasury key: a 2-of-3 multisig (Squads) from day one.

## 6. Licensing: the part that can sink the whole thing

The paper evaluates on **Manga109**, which is licensed for academic research
only. A token-funded studio publishing cuts is not academic research. Eligible
sources are:

- public-domain manga and comics (pre-1920s works, expired copyrights),
- original or community-submitted manga where the author grants a licence
  (a simple written grant stored with the cut),
- works under permissive licences (CC BY / CC BY-SA with attribution),
- anything the rights holder signs off on.

Keep a `LICENSE.txt` next to every cut's source pages. A cut without one does
not get published.

## 7. What is and is not a promise

- The token is a **funding mechanism for compute**. It does not entitle holders
  to revenue, equity, or the cuts; the cuts are released publicly under the
  source's licence.
- No market numbers on the site are ever invented; sample mode is labelled, live
  mode reads the ledger only.
- Not financial advice. Say so everywhere the token is mentioned.

## 8. Roadmap

| version | funding side | kit side |
|---|---|---|
| v0 (now) | token config, sample ledger, sweep script | creative mode, sheets, routing, manual/Higgsfield backends |
| v1 | launch, multisig treasury, first sweeps, published cost per cut | unattended Higgsfield runs, ffmpeg finishing pass |
| v2 | holder voting on the queue, automated credit top-ups where a provider accepts crypto | self-hosted open models on rented GPUs paid from the fund |
