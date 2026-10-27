<h1 align="center">
  <img src="assets/logo.png" width="40" alt="logo" style="vertical-align: middle;">
  Manga2Animate: An Agentic Framework for Faithful Manga Animation with Script and Storyboard Planning
</h1>

<p align="center">
  <a href="https://github.com/bbbguji">Sheng-Wei Zhang</a><sup>*</sup> ·
  <a href="https://www.linkedin.com/in/annchen1234/">Hsi-An Chen</a><sup>*</sup> ·
  <a href="https://github.com/timidtimmm">Yan-Ting Liou</a><sup>*</sup> ·
  <a href="https://www.linkedin.com/in/chchen825/">Chih-Hsuan Chen</a> ·
  <a href="https://www.linkedin.com/in/%E5%A3%AB%E7%8F%8D-%E6%9B%BE-125166331/">Shih-Chen Tseng</a> ·
  <a href="https://yulunalexliu.github.io/">Yu-Lun Liu</a>
  <br>
  National Yang Ming Chiao Tung University · <sup>*</sup>Equal contribution
</p>

<h3 align="center">
  🌐 <a href="https://annchenn.github.io/Manga2Animate/">Project Page</a> |
  📄 <a href="https://arxiv.org/abs/2610.26851">Paper</a> |
  🤗 <a href="https://huggingface.co/papers/2610.26851">Hugging Face</a> |
  📁 <a href="https://drive.google.com/drive/folders/1CTwp8wCjtqUCDPo5QmUIPYL7WjWh3VCP?usp=sharing">Evaluation Data</a>
</h3>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/teaser_dark.webp">
    <img src="assets/teaser.webp" width="100%" alt="Manga2Animate teaser">
  </picture>
</p>

> **Sakuga Kit.** This fork adds an optional **Creative Expansion** layer on top
> of the paper's faithful pipeline: a Shot Expansion Agent that re-stages the
> verified script with new camera angles and inserted sakuga moments, a
> Continuity Reviewer that checks every new shot against the manga, generated
> character model sheets, smarter video routing, extra backends and a single
> entry point. The faithful pipeline below runs unchanged when the layer is off.
> See [Creative Expansion Mode](#creative-expansion-mode-sakuga-kit).

**TL;DR** Manga2Animate turns a few manga pages and a character reference
into an animation that stays faithful to the source manga.

Feeding manga pages straight into a video model often misreads the panel
layout and gets the story wrong, while working from text alone loses the
characters and art style. Manga2Animate follows real animation production
instead: it first writes an **animation script**, then draws a **visual
storyboard**, and only then generates the video. Each step is produced by a
creator agent and checked against the source panels by a reviewer agent, and
both the script and the storyboard are refined over two rounds of feedback.
In experiments and a user study, the resulting animations are more faithful to
the manga and more consistent across shots than prior methods.

## Table of Contents

- [Repository Structure](#repository-structure)
- [Installation](#installation)
- [Dataset](#dataset)
- [Running the Pipeline](#running-the-pipeline)
  - [Stage 1: Pre-processing](#stage-1-pre-processing)
  - [Stage 2: Script and Storyboard Refinement](#stage-2-script-and-storyboard-refinement)
  - [Stage 3: Video Generation](#stage-3-video-generation)
- [Creative Expansion Mode (Sakuga Kit)](#creative-expansion-mode-sakuga-kit)
- [Open-source Backends](#open-source-backends)
- [Evaluation](#evaluation)
- [Configuration](#configuration)
- [Acknowledgement](#acknowledgement)
- [Citation](#citation)

## Repository Structure

```
.
├── src/                    # pipeline
│   ├── croppanels.py       # stage 1: panels
│   ├── magi.py             # stage 1: transcript
│   ├── run_all.py          # stage 2: refinement (+ --creative)
│   ├── create_video.py     # stage 3: video (+ --creative)
│   ├── full_pipeline.py    # one command for stages 1-3
│   ├── creative_expansion.py, creative_storyboard.py, creative_config.py
│   ├── character_sheets.py # per-character model sheets
│   └── ...
├── prompts/                # agent prompts (shot_expansion_*, *_creative, character_sheet_*)
├── docs/                   # MODELS.md, FUNDING.md, BRAND.md
├── site/                   # Sakuga Kit demo page
├── treasury/               # fee-to-credits ledger tooling
├── opensource_backends/    # open-model setup
├── mangaeval/              # evaluation
│   ├── vistorybench/       # storyboard metrics
│   ├── vbench/             # video metrics
│   └── aesthetic/          # aesthetic scores
├── assets/
├── requirements.txt
└── .env.example
```

## Installation

1. Create the environment:

   ```bash
   conda create -n manga2animate python=3.12 -y
   conda activate manga2animate
   pip install -r requirements.txt
   ```

2. Set up Gemini access. The creators and reviewers call Gemini through
   Vertex AI, or through a plain API key if no GCP project is set:

   ```bash
   gcloud auth application-default login   # only for Vertex AI
   ```

3. Copy the configuration file and fill in your credentials:

   ```bash
   cp .env.example .env
   ```

   ```env
   GOOGLE_CLOUD_PROJECT=<your-gcp-project>   # Vertex AI; leave empty to use GEMINI_API_KEY
   GEMINI_API_KEY=<your-gemini-api-key>

   # only the video models you use need a key
   ARK_API_KEY=<key>        # -m seedance
   MINIMAX_API_KEY=<key>    # -m minimax-api
   DASHSCOPE_API_KEY=<key>  # -m wan
   IMGBB_API_KEY=<key>      # image hosting for seedance / wan
   ```

   See [Configuration](#configuration) for the remaining settings.

## Dataset

Each case is a folder under `target/`:

```
target/<case>/
├── manga_page.jpg          # one page, or manga_page1.jpg, manga_page2.jpg, ... in reading order
├── ref.png                 # character reference sheet (all characters in one image)
└── characters/             # optional, replaces ref.png: one image per character
    ├── char1.png           #   matches Character[1] in the script
    └── char2.png
```

The pipeline uses 6 panels per case (`NUM_CLIPS=6`).

## Running the Pipeline

The full pipeline for one case is three commands:

```bash
# 1. Pre-processing: crop the pages into panels in reading order
#    -> target/<case>/manga_panels/panel1.png ... panel6.png
python src/croppanels.py -t <case>

# 2. Transcript, character descriptions, and two rounds of script/storyboard refinement
python src/run_all.py -t <case>

# 3. Video generation from the best script and storyboard
python src/create_video.py -t <case> -m seedance
```

Every `run_all.py` run writes to its own folder,
`results/<case>/<YYYYMMDD_HHMMSS>/`. Useful options:

| option | effect |
|---|---|
| `--auto-resume` | continue the newest run, skipping completed stages |
| `--time <YYYYMMDD_HHMMSS>` | write into (or resume) that run folder |
| `-n 3` / `--to-iter 5` / `--from-iter 3` | change the number of refinement rounds |
| `--storyboard-backend flux2` | draw the storyboard with FLUX.2-dev instead of Gemini |

`create_video.py` takes the same `--time`, plus `--iter` to choose the round;
by default it uses the newest run and round.

### Stage 1: Pre-processing

`src/croppanels.py` detects the panels on each page with MAGI and saves them in
reading order to `target/<case>/manga_panels/`, numbered across pages. The
transcript (MAGI OCR, `src/magi.py`) and the character descriptions
(`src/create_character_description.py`) are produced by `run_all.py` as its
first step, in `results/<case>/<run>/character/`.

### Stage 2: Script and Storyboard Refinement

`run_all.py` runs the following per case (default settings in `.env`):

| step | agent | output (`results/<case>/<run>/`) |
|---|---|---|
| transcript | MAGI | `character/transcript.txt` |
| character descriptions | Character Description Creator | `character/character_description.json` |
| round `k`: `NUM_SCRIPTS=3` scripts | Script Creator | `iter<k>/scripts/script_*.json` |
| best script + script feedback | Character Reviewer, Clip Reviewer | `iter<k>/bestScript.json`, `iter<k>/intermediate/` |
| `NUM_STORYBOARDS=3` storyboards | Storyboard Creator | `iter<k>/storyboard/` |
| best storyboard + storyboard feedback | Storyboard Reviewer | `iter<k>/bestStoryboard.png`, `iter<k>/intermediate/` |

Round 0 generates from the manga alone. From round 1 on, the Script Creator
also receives the previous best script and both kinds of feedback, and new
storyboards are compared against the previous best. `NUM_ITERATIONS=2` gives
the two rounds used in the paper, and the output of the last round
(`iter1/`) goes to video generation.

**Script Reviewer.** The Character Reviewer checks appearance and clothing
against the manga and the style against the reference; the Clip Reviewer
checks each panel's content and layout. The best character description and the
best description for each clip are chosen independently across candidates and
reassembled into the final script.

**Storyboard Reviewer.** Candidates are compared in pairs against the manga
panels, the character reference, and the script, on 13 criteria covering
character identity, art style, cross-panel consistency, and script alignment
(five of them, such as no extra characters and no speech bubbles, are pass/fail
checks). In round 0 the three candidates are compared with each other; from
round 1 on, each is compared with the previous best storyboard.

### Stage 3: Video Generation

```bash
python src/create_video.py -t <case> -m <model>
```

The model decides the mode:

| `-m` | model | mode | runs on |
|---|---|---|---|
| `seedance` | Seedance 2.0 | reference-conditioned | BytePlus Ark API |
| `minimax-api` | MiniMax-H3 | reference-conditioned | MiniMax API |
| `minimax-local` | MiniMax-H3 | reference-conditioned | self-hosted ([setup](opensource_backends/README.md)) |
| `wan` | Wan 2.7 I2V | first-frame | DashScope API |
| `anisora` | Index-AniSora V3.2 | first-frame | self-hosted ([setup](opensource_backends/README.md)) |

- **Reference-conditioned mode** (`ref` in the code). The whole best storyboard
  and the best script go in as references, and the model returns one continuous
  video for the story. Two candidates are generated and the Video Reviewer
  keeps the better one (`best_video.mp4`).
- **First-frame mode** (`i2v` in the code). The storyboard is cut into its 6
  panels and each panel conditions its own clip. For each clip, two candidates
  are generated and the Video Reviewer picks one, judging continuity with the
  previously selected clip from the second clip onward. The selected clips are
  concatenated (`final_story.mp4`).

Output goes to `results/<case>/video/<model>/run_<YYYYMMDD_HHMMSS>/`.
`--dry-run` builds every prompt and prints what would be submitted without
calling any model.

## Creative Expansion Mode (Sakuga Kit)

The faithful pipeline reproduces the manga's own panel compositions. Creative
Expansion keeps that faithful script as ground truth and adds a fourth layer on
top of it: an anime **director** that re-stages the verified clips with new
camera angles and inserts the cuts the manga never drew, and a **continuity
supervisor** that rejects anything the manga does not support.

```
faithful script ──► Shot Expansion Agent ──► Continuity Reviewer ──► creative storyboard ──► video
(iter1/bestScript)  (new angles, inserts)    (keep / revise / drop)  (3×R grid, one panel    (creative prompts,
                                                                      per shot, reviewed)      per-shot camera)
```

Everything new is additive and off by default. Turn it on with `--creative` or
`CREATIVE_MODE=true`.

### One command

```bash
# panels -> transcript -> character sheets -> faithful rounds -> creative layer -> video
python src/full_pipeline.py -t <case> --creative -m seedance

# pick the creativity level (conservative | balanced | aggressive; aliases faithful / sakuga)
python src/full_pipeline.py -t <case> --creative --creativity sakuga -m seedance --route i2v

# no credentials, no model calls: prints the plan and writes every prompt to disk
python src/full_pipeline.py -t <case> --creative --dry-run

# continue the newest run after an interruption (every stage is resumable)
python src/full_pipeline.py -t <case> --creative --resume
```

`full_pipeline.py` wraps the three scripts documented above; each stage still
works on its own:

```bash
python src/run_all.py -t <case> --creative --creativity balanced   # adds iter1/creative/
python src/create_video.py -t <case> --creative -m seedance         # reads iter1/creative/
```

### Creativity levels

| level | shots (6 panels) | what the director may do |
|---|---|---|
| `conservative` | 6 | one shot per panel; up to 2 re-angled for stronger composition; no inserts |
| `balanced` (default) | 9 | every panel kept, plus reaction close-ups, one impact frame, one establishing shot; at most one showpiece cut |
| `aggressive` | 12 | the full sakuga treatment: inserts on every action beat, crash zooms, whip pans, orbital moves, Dutch angles, first-person flashes |

Shot counts are rounded up to a full 3-column storyboard row (`CREATIVE_SHOTS_*`
to override). Whatever the level, the director may change the **camera**, never
the **world**: no new characters, wardrobe, events, order or dialogue. Every line
of the faithful script stays on exactly one shot of its own panel; inserts are
silent.

### What runs

| step | agent | prompt | output (`results/<case>/<run>/iter<last>/creative/`) |
|---|---|---|---|
| shot expansion | Shot Expansion Agent | `prompts/shot_expansion_creator.txt` | `intermediate/expanded_r<N>.json` |
| continuity review | Continuity Reviewer | `prompts/shot_expansion_reviewer.txt` | `intermediate/shot_review_r<N>.json` |
| revise / drop / pad | (code) | — | `expandedScript.json`, `expansion_summary.json` |
| creative storyboard | Storyboard Creator | `prompts/create_storyboard_creative.txt` | `storyboard/storyboard_<k>.png` |
| storyboard review | Creative Storyboard Reviewer | `prompts/storyboard_reviewer_creative.txt` | `bestStoryboard.png`, `intermediate/sb_compare_*.json` |

Each shot in `expandedScript.json` carries `source_clip`, `shot_kind`
(`faithful` / `reangle` / `insert` / `hold`), a `camera` block (angle, framing,
movement, lens), `motion` notes in anime terms (smears, impact frame, speed
lines, follow-through, debris), a `sakuga_tier` (0–3) and a `duration_hint`.
The reviewer checks cast, appearance, event, order, dialogue placement, spatial
continuity under the new angle, drawability and coverage, and returns
keep / revise / drop per shot. `CREATIVE_REVIEW_ROUNDS` (default 2) sets how
many review passes run; shots still marked `drop` after the last pass are
removed and the list is padded with static `hold` shots so the grid stays
regular.

The creative storyboard reviewer uses the same pairwise mechanics as the paper's
reviewer with a rubric built for camera work: Camera Realisation, Motion &
Impact Readability, Spatial Continuity under angle changes and Source Fidelity
join the identity / style / grid / text checks.

### Character model sheets

When a target ships only a composite `ref.png`, `src/character_sheets.py`
generates a per-character model sheet (four-view turnaround, six expressions,
three detail call-outs) from the panels and the reference, produces
`CHARACTER_SHEET_CANDIDATES` candidates and keeps the one the sheet reviewer
(`prompts/character_sheet_reviewer.txt`) prefers. Sheets live in
`results/<case>/<run>/character/sheets/` and are appended to the shipped
references for the storyboard, creative and video stages (`CHARACTER_SHEETS=auto|true|false`).
The script reviewers keep judging against the shipped reference only.

### Video in creative mode

`create_video.py --creative` reads the expanded shot list and its storyboard and:

- **picks the route per run** (`--route auto|ref|i2v`, `VIDEO_ROUTE`). Models
  that take both a first frame and reference images (Seedance) can run either.
  `auto` sends `aggressive` lists down the i2v route (one clip per shot, each
  with its own camera move) and keeps the reference-conditioned route otherwise,
  **segmenting** lists longer than `VIDEO_MAX_SHOTS_PER_SEGMENT` into
  consecutive sub-storyboards that are reviewed against each other and
  concatenated;
- **sizes the request** to the shot list (`VIDEO_SECONDS_PER_SHOT` × shots,
  clamped to the model's `*_MAX_DURATION`);
- **sends the character sheets** as extra reference images on backends that
  accept them (`--no-character-refs` to opt out);
- uses `prompts/video_generation_creative*.txt`, which ask for the specified
  camera move and the anime motion language of each shot (anticipation, smears,
  impact frames, speed lines, follow-through, particles), cel-shaded rendering
  and no text;
- on the i2v route **trims each winning clip to its `duration_hint`** before the
  concat (`--no-trim` to keep the provider's length), so a 1.5 s hold is a 1.5 s hold.

Output goes to `results/<case>/video/<model>-creative/run_*/`.

Two backends were added for the models you may have access to without an Ark
key:

| `-m` | what it does |
|---|---|
| `higgsfield` | One API (`api.higgsfield.ai`) in front of Seedance 2.x, Kling, MiniMax H3, Veo, Wan. Set `HIGGSFIELD_API_KEY/SECRET`, `HIGGSFIELD_MODEL_PATH` and, if the model's body differs, `HIGGSFIELD_BODY_TEMPLATE` from the model page on docs.higgsfield.ai. Polls `status_url`, reads `video.url`. |
| `manual` | Writes `manifest.json` (prompt, image, extra refs, duration, expected output path per clip) instead of calling anything. Generate the clips anywhere (a web UI, the Higgsfield MCP in an agent session), drop them at the listed paths, re-run with `--auto-resume`: the Video Reviewer and the concat pick up from there. |

`--crop-mode grid` slices a regular storyboard into panels geometrically (no
MAGI / torch), which is what a `--dry-run` falls back to.

### Configuration

| variable | meaning |
|---|---|
| `CREATIVE_MODE`, `CREATIVITY_LEVEL` | enable the layer / default level |
| `CREATIVE_SHOTS_<LEVEL>`, `CREATIVE_MAX_INSERTS`, `CREATIVE_MAX_REANGLES` | shot budget per level |
| `CREATIVE_REVIEW_ROUNDS`, `CREATIVE_NUM_STORYBOARDS`, `CREATIVE_IMAGE_SIZE`, `CREATIVE_NOTES` | review passes, storyboard candidates, image size, free-text direction |
| `CHARACTER_SHEETS`, `CHARACTER_SHEET_CANDIDATES`, `CHARACTER_SHEET_ASPECT/SIZE` | model-sheet stage |
| `VIDEO_ROUTE`, `VIDEO_SECONDS_PER_SHOT`, `VIDEO_MAX_SHOTS_PER_SEGMENT`, `VIDEO_CHARACTER_REFS` | creative video routing |
| `SEEDANCE_MAX_DURATION`, `MINIMAX_MAX_DURATION`, `*_MAX_EXTRA_REFS`, `SEEDANCE_RESOLUTION` | per-backend limits |
| `HIGGSFIELD_*`, `MANUAL_*`, `CROP_MODE` | the new backends and the panel cropper |

See [`docs/MODELS.md`](docs/MODELS.md) for which model to put behind each
stage, [`docs/FUNDING.md`](docs/FUNDING.md) for how the Sakuga Studio
Production Fund turns token fees into generation credits, and
[`site/`](site/) for the demo page.

### Limitations

- The expanded shots are checked by a VLM reviewer, not by a human; `revise`
  instructions are applied by the same creator model, so a stubborn
  hallucination can survive two rounds. Read `expansion_summary.json`.
- Gemini draws the 9- and 12-panel grids less reliably than the 3×2 sheet;
  `CREATIVE_IMAGE_SIZE=2K` and two candidates are the default for that reason.
- Camera moves are *requested* from the video model; hosted models realise
  crash zooms and whip pans better than orbitals. The i2v route gives the
  most control, the ref route the best continuity.
- The `higgsfield` backend's request body is configured, not hard-coded (the
  lifecycle is from the docs; model bodies differ). Verify one request with
  `--dry-run` and the model page before a batch.

## Open-source Backends

The pipeline also runs on open models only: FLUX.2-dev for the storyboard, and
MiniMax-H3 or Index-AniSora for the video. Each needs its own conda
environment; setup steps and weight downloads are in
[`opensource_backends/README.md`](opensource_backends/README.md).

```bash
bash opensource_backends/flux2/setup_env.sh      # once
bash opensource_backends/flux2/verify_setup.sh   # env + conda + weights + GPU
python src/run_all.py -t <case> --storyboard-backend flux2

bash opensource_backends/minimax_h3/serve.sh   # leave running, then in another shell:
python src/create_video.py -t <case> -m minimax-local
```

With FLUX.2-dev, each storyboard panel is generated separately (3 variants per
panel, conditioned on the manga panel and the character references), a Gemini
reviewer scores each variant, and the best panels are tiled into the same 3×2
grid as the Gemini storyboard.

FLUX.2-dev needs most of a 97GB-class GPU (~105GB of weights, with the text
encoder released after prompt encoding so transformer+VAE stay resident). On a
shared machine set `FLUX2_CUDA_DEVICE` to an idle card — a co-tenant process on
the same GPU is the most common cause of failure. `FLUX2_OFFLOAD_MODE=cpu_offload`
trades speed for a smaller VRAM footprint. Every run writes `metrics.json` with
per-stage timings and the Gemini API call count. See
[`opensource_backends/flux2/README.md`](opensource_backends/flux2/README.md).

## Evaluation

[`mangaeval/`](mangaeval/) contains the evaluation protocol of the paper
(Sec. 4.1.3): storyboard metrics built on ViStoryBench (CSD, CIDS, PA, OCCM),
video metrics built on VBench++ (I2VS, I2VB, SC, BC, MS, DD), and anime aesthetic
scores (Shadow-v2, Waifu-v3), with an example case. See
[`mangaeval/README.md`](mangaeval/README.md).

## Configuration

All settings are read from `.env` (see [`.env.example`](.env.example)).

| variable | meaning |
|---|---|
| `TEXT_MODEL` | Gemini model for the script, character, and reviewer agents |
| `IMAGE_MODEL` | Gemini model for the storyboard |
| `VIDEO_REVIEW_MODEL` | Gemini model for the Video Reviewer |
| `NUM_SCRIPTS`, `NUM_STORYBOARDS` | candidates per round (3 and 3) |
| `NUM_CLIPS` | panels per case (6) |
| `NUM_ITERATIONS` | refinement rounds (2) |
| `STORYBOARD_BACKEND` | `gemini` or `flux2` |
| `SEEDANCE_MODEL`, `MINIMAX_MODEL`, `MINIMAX_LOCAL_MODEL`, `WAN_MODEL` | exact model id submitted for each `-m` |
| `VIDEO_GENERATION_MODEL` | model used when `-m` is omitted |
| `FLUX2_*` | FLUX.2-dev settings (variants per panel, resolution, steps, guidance, offload) |
| `OSVG_MODELS_DIR`, `MINIMAX_LOCAL_*`, `ANISORA_*` | self-hosted video models, see [`opensource_backends/`](opensource_backends/README.md) |

## Acknowledgement

This project builds on
[MAGI](https://github.com/ragavsachdeva/magi) for panel detection and OCR,
[ViStoryBench](https://github.com/ViStoryBench/vistorybench) and
[VBench](https://github.com/Vchitect/VBench) for evaluation,
[FLUX.2](https://huggingface.co/black-forest-labs/FLUX.2-dev),
[MiniMax-H3](https://huggingface.co/MiniMaxAI/MiniMax-H3) and
[Index-AniSora](https://github.com/bilibili/Index-anisora) as open generation
models, and uses manga from [Manga109](http://www.manga109.org/).

## Citation

```bibtex
@misc{manga2animate2026,
  title={{Manga2Animate}: An Agentic Framework for Faithful Manga Animation with Script and Storyboard Planning},
  author={Sheng-Wei Zhang and Hsi-An Chen and Yan-Ting Liou and Chih-Hsuan Chen and Shih-Chen Tseng and Yu-Lun Liu},
  year={2026},
  eprint={2610.26851},
  archivePrefix={arXiv},
  primaryClass={cs.CV}
}
```
