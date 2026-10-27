// Sakuga Kit — site configuration.
// Everything the page says about the token comes from here. Nothing is invented in the page.
//
// mode:
//   "prelaunch"  no mint yet: the funding section shows "nothing to show yet", no numbers at all
//   "live"       numbers come ONLY from data/ledger.json (written by treasury/sweep.py)
//   "sample"     numbers come from data/sample.json and every one of them wears a SAMPLE badge
// The URL parameter ?mode=prelaunch|live|sample overrides this for previewing.
window.TOKEN = {
  mode: "sample",
  name: "Sakuga",
  symbol: "SAKUGA",
  chain: "solana",
  mint: "",             // token mint address once launched
  launchpad: "",        // e.g. "pump.fun", "bonk", "raydium"
  treasuryWallet: "",   // the Production Fund wallet; fees land here
  explorerUrl: "",      // link to the wallet on an explorer
  feeBps: 100,          // 100 bps = 1.00% of every trade
  feeSplit: { credits: 80, operations: 10, liquidity: 10 },  // percent of each fee
  links: { github: "#", x: "#", telegram: "#" },
};
