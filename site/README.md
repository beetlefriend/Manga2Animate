# site/ — the Sakuga Kit page

Static, no build step. `index.html` + `token.config.js` + `data/` + `assets/pd/`.

## Modes

`token.config.js` sets `mode`:

| mode | what the funding section shows |
|---|---|
| `prelaunch` | no numbers at all; "the Fund opens at launch" |
| `sample` (default) | `data/sample.json`, every number stamped **sample** |
| `live` | `data/ledger.json` only, written by `treasury/sweep.py`; a missing file shows "ledger unavailable" |

`?mode=prelaunch|sample|live` on the URL overrides it for previewing. Set `mint`,
`treasuryWallet`, `explorerUrl`, `launchpad`, `feeBps`, `feeSplit` and `links` there;
nothing about the token is written anywhere else on the page.

## Run locally

```bash
python -m http.server 8765 --directory site
```

Opening `index.html` by double-click works but blocks the JSON fetch (the page
falls back to the embedded sample) and may block the Google Fonts request; serve
it to see the real thing.

## Deploy

`.github/workflows/deploy-site.yml` publishes this folder to GitHub Pages on
every push to `master` that touches `site/`. For live mode, run the treasury
sweep in CI and commit `data/ledger.json` (it is git-ignored by default).

## Images

`assets/pd/` holds public-domain prints (Hokusai, Kuniyoshi) from The Met Open
Access and the Art Institute of Chicago, used for the camera demo and the shot
cards. They are not model output and the page says so. Credits in `CREDITS.md`.
