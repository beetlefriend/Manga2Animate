"""Refresh the public ledger the demo site reads in live mode.

    python treasury/sweep.py --wallet <treasury pubkey> --out site/data/ledger.json

Reports only; never signs or moves anything. What it does:

  * reads the treasury wallet's SOL balance from a public Solana JSON-RPC node
    (`--rpc`, default mainnet-beta), and a USD estimate only when you pass
    `--sol-usd` (no price is ever invented);
  * counts the finished cuts under `results/` (any `best_video.mp4` /
    `final_story.mp4`) and adds the ones the ledger does not know yet as
    `done` entries with their run folder, so the site's cut list grows as the
    kit produces;
  * keeps everything you edited by hand (fees_in_total_usd, credits_bought_usd,
    cost_usd per cut, the queue) -- those come from your receipts, not from here.

Standard library only, so it can run from a cron job on any box.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAMPORTS = 1_000_000_000


def rpc_balance(wallet: str, rpc: str) -> float:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}).encode()
    req = urllib.request.Request(rpc, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data["result"]["value"] / LAMPORTS


def find_cuts(results_root: Path) -> list[dict]:
    """Every finished video under results/<case>/video/<model>/run_*/."""
    cuts = []
    if not results_root.is_dir():
        return cuts
    for video in sorted(results_root.glob("*/video/*/run_*/*.mp4")):
        if video.name not in {"best_video.mp4", "final_story.mp4"}:
            continue
        case = video.parts[-5]
        model = video.parts[-3]
        run = video.parts[-2]
        cuts.append({
            "id": f"{case}/{model}/{run}",
            "title": case,
            "manga": case,
            "status": "done",
            "model": model,
            "creative": model.endswith("-creative"),
            "path": str(video.relative_to(ROOT)),
            "produced_at": datetime.fromtimestamp(video.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
        })
    return cuts


def load(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Write the Production Fund ledger for the site.")
    ap.add_argument("--wallet", default=None, help="Treasury wallet (Solana pubkey). Omit to skip the balance.")
    ap.add_argument("--rpc", default="https://api.mainnet-beta.solana.com")
    ap.add_argument("--sol-usd", type=float, default=None, help="SOL price to estimate USD; omitted = no estimate")
    ap.add_argument("--results-root", default=str(ROOT / "results"))
    ap.add_argument("--out", default=str(ROOT / "site" / "data" / "ledger.json"))
    args = ap.parse_args()

    out = Path(args.out)
    ledger = load(out)
    ledger.setdefault("treasury", {"sol": None, "usd_estimate": None})
    ledger.setdefault("fees_in_total_usd", 0)
    ledger.setdefault("credits_bought_usd", 0)
    ledger.setdefault("cuts", [])
    ledger.setdefault("queue", [])

    if args.wallet:
        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", args.wallet):
            print("wallet does not look like a Solana pubkey", file=sys.stderr)
            return 2
        sol = rpc_balance(args.wallet, args.rpc)
        ledger["treasury"]["sol"] = round(sol, 6)
        ledger["treasury"]["usd_estimate"] = round(sol * args.sol_usd, 2) if args.sol_usd else None
        ledger["treasury"]["wallet"] = args.wallet
        print(f"treasury: {sol:.4f} SOL" + (f" (~${sol * args.sol_usd:,.2f})" if args.sol_usd else ""))

    known = {c.get("id") for c in ledger["cuts"]}
    added = 0
    for cut in find_cuts(Path(args.results_root)):
        if cut["id"] in known:
            continue
        cut.setdefault("cost_usd", None)
        cut.setdefault("funded_by", "fees")
        cut.setdefault("video_url", "")
        ledger["cuts"].append(cut)
        added += 1
    print(f"cuts: {len(ledger['cuts'])} ({added} new)")

    ledger["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
