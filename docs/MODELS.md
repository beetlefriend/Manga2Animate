# Models: what to use, and what is already reachable

Written 2026-10-27 from this machine's session. Prices and model ids drift; treat
the table as the starting lineup, not scripture.

## What this machine can reach today

| access | what it gives | notes |
|---|---|---|
| **Higgsfield** (Plus plan, ~504 credits at the time of writing) | video: Seedance 2.0 / 2.0 Mini / 2.5 / 1.5 Pro, Kling 4.0 / 3.0 Omni / 2.6, MiniMax H3 / H3 Max / Hailuo, Veo 3.1 / Lite, Wan 2.6 / 2.7 / 3.0, Grok Video 1.5, FLUX 3 Video, Cinema Studio 3.0-4.0; image: Nano Banana Pro / 2 / 2.1 (Gemini image), Seedream 4.5 / 5.0, FLUX.2 / FLUX 3, GPT Image 2 / 2.5, Ideogram 4.5; plus Topaz video upscale, FPS boost, lipsync, dubbing, a character-sheet workflow | reachable now through the Higgsfield MCP (agent session) and through the REST API with console keys (`-m higgsfield`). Credit-based, so cost per clip is visible before submitting (`get_cost`). |
| **imgmcp** | Nano Banana Pro (Gemini 3 Pro Image), Nano Banana 2 (Gemini 3.1 Flash Image), DALL-E 3, SDXL, Flux; providers fal / openai / replicate / gemini / anthropic active | the keys live inside that MCP server, not in the shell, so the repo cannot use them directly; fine for sheets and one-off panels from an agent session |
| shell environment | **no** `GEMINI_API_KEY`, `ARK_API_KEY`, `MINIMAX_API_KEY`, `DASHSCOPE_API_KEY`, `HIGGSFIELD_API_KEY` set | the pipeline's agents need a Gemini key (AI Studio) or a GCP project; the video backends need theirs |
| local compute | no NVIDIA GPU, no ffmpeg, Python 3.14 system / 3.12 venv in the repo | MAGI runs on CPU (slow but fine); FLUX.2-dev, MiniMax-H3 local and AniSora need 48-97 GB cards and are **not** an option here; install ffmpeg for the concat / trim steps |

## Recommended lineup per stage

### Text agents (script creator, reviewers, shot expansion, continuity, storyboard reviewers)

| role | pick | why |
|---|---|---|
| creators (script, shot expansion) | `gemini-3.7-flash` (repo default) | fast, cheap, `thinking_level=HIGH` is enough for structured JSON; the reviewers catch the slips |
| reviewers (character / clip / storyboard / continuity / video) | `gemini-3.1-pro-preview` | the review calls carry 6-20 images each and the verdicts drive everything downstream; spend here, not on the creator |
| budget run | flash everywhere | ~3x fewer tokens; expect one extra review round |

Set `TEXT_MODEL` for the agents and `VIDEO_REVIEW_MODEL` for the video judge. The
creative stages read the same two variables.

### Storyboards and character sheets (image)

| need | pick | why |
|---|---|---|
| faithful 3x2 storyboard | `gemini-3.1-flash-image` (repo default) | what the paper measured |
| creative 3x3 / 3x4 sheets, model sheets | `gemini-3-pro-image` (Nano Banana Pro) at `2K` | 9-12 cells need resolution for faces; set `IMAGE_MODEL` or just `CREATIVE_IMAGE_SIZE=2K` |
| open-source | FLUX.2-dev via `--storyboard-backend flux2` | only with a 97 GB-class GPU |

If a Gemini image quota is the bottleneck, Seedream 5.0 Pro or FLUX 3 Image on
Higgsfield can draw the sheets (`-m manual` route for the storyboard is not
wired; generate there and drop the file in as `storyboard_0.png`).

### Video

| goal | pick | route | notes |
|---|---|---|---|
| paper-faithful, best continuity | **Seedance 2.0** via BytePlus Ark (`-m seedance`) | ref, 15 s | the paper's main model; reference-conditioned; audio |
| creative balanced (9 shots) | Seedance 2.0 / 2.5 (`-m seedance` or `-m higgsfield`) | ref, segmented 6+3 | continuity within segments, sheets keep identity across |
| creative aggressive (12 shots, per-shot camera moves) | Seedance 2.0 i2v, or Kling 4.0 (3-30 s, keyframes), via Higgsfield | i2v | one clip per shot realises crash zooms / whip pans best; trimmed to `duration_hint` |
| long single take | Seedance 2.5 (4-30 s) or Kling 4.0 (up to 30 s) on Higgsfield | ref | raise `HIGGSFIELD_MAX_DURATION` / `VIDEO_MAX_SHOTS_PER_SEGMENT` |
| 2K hero cut | MiniMax H3 (2K, references) | ref | slower; the paper's open alternative, hosted |
| cheap drafts | Seedance 2.0 Mini, Grok Imagine 1.5 Lite | either | iterate on prompts before spending on the final |
| dialogue with lip sync / sound | Veo 3.1, or any clip + Higgsfield lipsync | ref | the pipeline requests English audio only |
| upscale / smooth | Topaz Hyperion, FPS Boost (Higgsfield) | post | optional finishing pass on `final_story.mp4` |

### Practical path for this machine right now

1. Get a Gemini API key (AI Studio) -> `.env`: `GEMINI_API_KEY`. That unlocks every
   agent stage, the storyboards and the model sheets.
2. Video without an Ark key: run `create_video.py --creative -m manual`, then
   generate the manifest's jobs through the Higgsfield MCP (Seedance 2.5 /
   Kling 4.0 with the storyboard panel as `start_image` and the sheets as
   `image_references`), drop the mp4s at the listed paths and re-run with
   `--auto-resume`. The reviewer and concat finish the cut.
3. When the Production Fund covers it, add Higgsfield console keys and switch
   to `-m higgsfield` for unattended runs, or an Ark key for `-m seedance`.

### Cost shape (per 6-panel case, balanced creative run)

Rough call counts, so the budget can be reasoned about; look at
`metrics.json` of a real run for the numbers.

| stage | Gemini text calls | Gemini image calls | video requests |
|---|---|---|---|
| faithful (2 rounds, 3 scripts, 3 storyboards) | ~40 | 6 | 0 |
| character sheets (2 chars x 2 candidates) | 2 | 4 | 0 |
| shot expansion + 2 review passes | 3-4 | 0 | 0 |
| creative storyboard (2 candidates) | 1 | 2 | 0 |
| video, ref segmented (9 shots) | 2 reviews | 0 | 4 |
| video, i2v (12 shots) | 12 reviews | 0 | 24 |

The video requests dominate the bill; the i2v route costs ~6x the ref route in
clips. `--dry-run` prints every request before anything is spent.
