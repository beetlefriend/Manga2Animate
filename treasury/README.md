# treasury/

Tooling for the Sakuga Studio Production Fund. Design in
[`../docs/FUNDING.md`](../docs/FUNDING.md).

| file | purpose |
|---|---|
| `sweep.py` | refresh `site/data/ledger.json`: treasury balance from a public RPC, finished cuts from `results/`. Report-only; moves nothing. |
| `ledger.json` | (git-ignored) a local copy of the ledger when you keep it outside `site/` |

```bash
# balance + cuts, with a USD estimate from a price you supply
python treasury/sweep.py --wallet <pubkey> --sol-usd 150 --out site/data/ledger.json

# cuts only (no wallet yet)
python treasury/sweep.py --out site/data/ledger.json
```

Hand-maintained fields in the ledger (from receipts): `fees_in_total_usd`,
`credits_bought_usd`, each cut's `cost_usd` and `video_url`, and the `queue`.
The site shows them only in `live` mode (`site/token.config.js`).
