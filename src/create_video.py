"""Storyboard → video, for every supported model, on the route that model can run.

Two routes, picked from the model rather than from a flag:

  ref route  (`--model seedance | minimax-local | minimax-api`)
      The whole `bestStoryboard.png` goes in as one reference image and the model
      returns one continuous video for the entire story. Two candidates, one
      Gemini review (`video_eval_first.txt`), best one wins.

  i2v route  (`--model wan | anisora`)
      The model only accepts a first/conditioning frame, so the storyboard is cut
      into panels (`crop_storyboard_panels.py`) and each panel drives its own
      clip. Two candidates per panel; panel 1 is reviewed with
      `video_eval_first.txt`, panels 2..N with `video_eval_sequential.txt`, which
      also sees the previous panel's winner and weights coherency at 0.4. The
      winners are concatenated into the final video.

Both routes read from  <results-root>/<target>/[<run>/]iter<N>/  — by default the
newest iteration that has a storyboard, `--iter` to pin one.

Creative Expansion (`--creative`) reads `iter<N>/creative/` instead: the expanded
shot list (`expandedScript.json`, 6/9/12 shots) and its storyboard. Three things
change on top of the paper's routes:

  * `--route auto|ref|i2v` picks the route per run instead of per model. A model
    that takes both a first frame and reference images (Seedance) can run either;
    `auto` keeps the ref route when the shot list fits one request, splits it
    into segments when it does not, and switches to i2v for `aggressive` shot
    lists where every shot has its own camera move.
  * Character model sheets (src/character_sheets.py) ride along as extra
    reference images on backends that accept them (`--no-character-refs` to opt
    out), so identity survives low angles and reverse angles.
  * The creative prompts (`prompts/video_generation_creative*.txt`) ask for the
    camera and the motion language each shot specifies; on the i2v route each
    clip is trimmed to its `duration_hint` before the concat so a 1.5s hold is a
    1.5s hold.

`-m manual` writes `manifest.json` instead of calling a provider: generate the
clips elsewhere (a web UI, the Higgsfield MCP in an agent session), drop them at
the listed paths and re-run with `--auto-resume` to review and concatenate.

The per-model API details live in `video_models.py`; adding a model means adding
an entry there, not editing this file.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from PIL import Image

load_dotenv()

from src import creative_config, log
from src.runs import resolve_run_dir
from src.utils import find_character_ref_images, generated_character_sheets
from src.video_models import (
    BACKEND_NAMES,
    ManualGenerationPending,
    PolicyViolationError,
    fetch_result,
    get_backend,
    model_from_env,
    upload_image_to_imgbb,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GEMINI_API_KEY      = os.getenv("GEMINI_API_KEY")
GEMINI_REVIEW_MODEL = os.getenv("VIDEO_REVIEW_MODEL", "gemini-3.1-pro-preview")

PROMPT_PANEL         = PROJECT_ROOT / "prompts" / "video_generation.txt"
PROMPT_STORYBOARD    = PROJECT_ROOT / "prompts" / "video_generation_storyboard.txt"
PROMPT_PANEL_CREATIVE      = PROJECT_ROOT / "prompts" / "video_generation_creative.txt"
PROMPT_STORYBOARD_CREATIVE = PROJECT_ROOT / "prompts" / "video_generation_creative_storyboard.txt"
EVAL_FIRST_FILE      = PROJECT_ROOT / "prompts" / "video_eval_first.txt"
EVAL_SEQUENTIAL_FILE = PROJECT_ROOT / "prompts" / "video_eval_sequential.txt"

# Jobs a `-m manual` run could not generate here; written to manifest.json at the end.
MANUAL_JOBS = []

IMG_EXTS = ("png", "jpg", "jpeg", "webp")
# anisora's tested runs used --base_seed 4096, so candidate 1 reproduces them
# exactly and further candidates step off from there. Ignored by the hosted
# providers, which never took a seed.
BASE_SEED = 4096
# The iteration a full pipeline run ends on (NUM_ITERATIONS=2 → iter0, then the
# refined iter1). Only used to word the fallback message: dropping below it means
# generating from an unrefined storyboard.
EXPECTED_LAST_ITER = 1


# ======================
# UTILITIES
# ======================
def save_file(content, folder, filename, is_json=False):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, filename)
    mode = "w" if not isinstance(content, bytes) else "wb"
    encoding = None if isinstance(content, bytes) else "utf-8"
    with open(path, mode, encoding=encoding) as f:
        if is_json:
            json.dump(content, f, indent=4, ensure_ascii=False)
        else:
            f.write(content)
    return path


def read_state(state_path):
    """The task-id checkpoint next to a candidate. Missing or corrupt reads as empty."""
    path = Path(state_path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state_path, state):
    path = Path(state_path)
    save_file(state, str(path.parent), path.name, True)


def extract_json_from_response(text):
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return json.loads(text)


def get_panel_data(full_json, clip_index):
    """One clip plus only the characters that clip actually references."""
    clip = next((c for c in full_json.get("clips", []) if c["id"] == clip_index), None)
    if not clip:
        raise ValueError(f"Clip id {clip_index} not found in the script.")
    desc = clip.get("description", "")
    char_ids = set(int(x) for x in re.findall(r"Character\[(\d+)\]", desc))
    # Creative shots also list their cast explicitly (a reaction insert may not
    # name the character in prose).
    for x in clip.get("characters_in_panel", []) or []:
        if str(x).isdigit():
            char_ids.add(int(x))
    filtered_chars = [c for c in full_json.get("characters", []) if c["id"] in char_ids]
    return {"characters": filtered_chars, "clip": clip}


# ======================
# CREATIVE HELPERS
# ======================
def creative_level(iter_dir):
    """The creativity level the creative stage ran with, from its config file."""
    cfg = Path(iter_dir) / "creative" / "creative_config.json"
    try:
        return json.loads(cfg.read_text(encoding="utf-8")).get("level", "balanced")
    except (OSError, json.JSONDecodeError):
        return "balanced"


def choose_route(args, backend, n_shots, level):
    """Pick ref or i2v for this run.

    An explicit --route (or VIDEO_ROUTE) wins if the backend supports it. `auto`
    keeps the paper's route when creative mode is off. In creative mode:
    aggressive shot lists go i2v when the model allows it (every shot has its
    own camera move, and one clip per shot realises it best); otherwise the ref
    route keeps continuity, segmented when the list does not fit one request.
    """
    requested = args.route or creative_config.video_route()
    if requested != "auto":
        if backend.supports(requested):
            return requested
        log.warn(f"{backend.name} cannot run the {requested} route; using {backend.mode}")
        return backend.mode
    if not args.creative:
        return backend.mode
    if level == "aggressive" and backend.supports("i2v"):
        return "i2v"
    if backend.supports("ref"):
        return "ref"
    return backend.mode


def ref_duration_for(n_shots, backend):
    """Seconds to request on the ref route for an n-shot list."""
    want = int(round(n_shots * creative_config.seconds_per_shot()))
    return max(4, min(want, backend.max_ref_duration))


def resolve_extra_refs(args, backend, dry_run):
    """Character model sheets (or the shipped refs) as extra reference inputs.

    Returns URLs/paths ready for backend.submit, or [] when the backend does not
    take them, the user opted out, or there is nothing to send. Generated sheets
    are preferred; the shipped reference is the fallback so the video model
    always sees a clean character image next to the storyboard.
    """
    if args.no_character_refs or not backend.supports_extra_refs:
        return []
    if not creative_config.video_character_refs(default=bool(args.creative)):
        return []
    sheets = [p for _, p in generated_character_sheets()]
    if not sheets:
        target_dir = PROJECT_ROOT / args.target_root / args.target
        try:
            sheets = [p for _, p in find_character_ref_images(target_dir)]
        except FileNotFoundError:
            sheets = []
    sheets = sheets[: backend.max_extra_refs]
    if not sheets:
        return []
    log.info(f"character refs : {', '.join(p.name for p in sheets)}")
    if backend.needs_public_url and not dry_run:
        return [upload_image_to_imgbb(str(p)) for p in sheets]
    return [str(p) for p in sheets]


def calculate_optimal_duration(panel_image_path, total_panels_area, clip_data, clamp):
    """Longer clips for panels that are visually bigger and carry more dialogue."""
    try:
        with Image.open(panel_image_path) as panel_img:
            panel_area = panel_img.width * panel_img.height
        area_ratio = panel_area / total_panels_area if total_panels_area else 0.2
        visual_bonus = area_ratio * 10.0
    except Exception as e:
        log.warn(f"Area calculation failed ({e}), using default 2.0s")
        visual_bonus = 2.0
        area_ratio = 0

    # flux2-backend scripts carry a structured "dialogue" array; gemini-backend
    # ones embed "Dialogue(Character[N]): '...'" inside "description".
    dialogue_field = clip_data.get("dialogue")
    if isinstance(dialogue_field, list) and dialogue_field:
        total_words = sum(len(str(d.get("text", "")).split()) for d in dialogue_field if isinstance(d, dict))
    else:
        dialogues = re.findall(r"Dialogue\([^)]+\):\s*'([^']+)'", clip_data.get("description", ""))
        total_words = sum(len(d.split()) for d in dialogues)
    text_bonus = total_words / 10.0

    total = visual_bonus + text_bonus + 1.0
    lo, hi = clamp
    optimal = max(lo, min(hi, int(round(total))))
    log.info(
        f"Duration: visual({area_ratio*100:.1f}%)={visual_bonus:.2f}s"
        f" + dialogue({total_words}w)={text_bonus:.2f}s + base=1.00s → [bold]{optimal}s[/bold]"
        f"  [dim](clamp {lo}-{hi})[/dim]"
    )
    return optimal


# ======================
# GEMINI REVIEW
# ======================
class Reviewer:
    """Gemini video comparison.

    Two transports: the Files API (developer key) uploads each video and is the
    only one that works without Vertex ADC, which is why it is preferred here;
    the Vertex client has no Files API and gets the videos inline as bytes.
    """

    def __init__(self):
        from google import genai
        self.genai = genai
        if GEMINI_API_KEY:
            self.client = genai.Client(api_key=GEMINI_API_KEY)
            self.transport = "files"
        elif os.getenv("GOOGLE_CLOUD_PROJECT"):
            self.client = genai.Client(
                vertexai=True,
                project=os.getenv("GOOGLE_CLOUD_PROJECT"),
                location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
            )
            self.transport = "inline"
        else:
            raise RuntimeError("Video review needs GEMINI_API_KEY or GOOGLE_CLOUD_PROJECT")

    def _upload(self, path):
        log.step(f"Uploading to Gemini: {os.path.basename(path)}")
        f = self.client.files.upload(file=path)
        while f.state.name == "PROCESSING":
            time.sleep(5)
            f = self.client.files.get(name=f.name)
        if f.state.name == "FAILED":
            raise RuntimeError(f"Gemini file processing failed: {path}")
        return f

    def compare(self, user_prompt, video_a, video_b, prev_video=None):
        """Returns the raw response text. prev_video switches to the sequential
        rubric, which adds Coherency at weight 0.4."""
        template = EVAL_FIRST_FILE if prev_video is None else EVAL_SEQUENTIAL_FILE
        eval_prompt = template.read_text(encoding="utf-8").replace("<user_prompt>", user_prompt)

        if self.transport == "files":
            uploaded = []
            try:
                file_a = self._upload(video_a); uploaded.append(file_a)
                file_b = self._upload(video_b); uploaded.append(file_b)
                if prev_video is None:
                    contents = ["Video A:", file_a, "Video B:", file_b, eval_prompt]
                else:
                    file_prev = self._upload(prev_video); uploaded.append(file_prev)
                    contents = ["Reference Previous Video:", file_prev,
                                "Video A:", file_a, "Video B:", file_b, eval_prompt]
                log.step("Requesting Gemini evaluation...")
                resp = self.client.models.generate_content(
                    model=GEMINI_REVIEW_MODEL, contents=contents
                )
            finally:
                for f in uploaded:
                    try:
                        self.client.files.delete(name=f.name)
                    except Exception:
                        pass
            return resp.text.strip()

        from google.genai import types
        parts = []
        if prev_video is not None:
            parts += [types.Part.from_text(text="Reference Previous Video:"),
                      types.Part.from_bytes(data=Path(prev_video).read_bytes(), mime_type="video/mp4")]
        parts += [
            types.Part.from_text(text="Video A:"),
            types.Part.from_bytes(data=Path(video_a).read_bytes(), mime_type="video/mp4"),
            types.Part.from_text(text="Video B:"),
            types.Part.from_bytes(data=Path(video_b).read_bytes(), mime_type="video/mp4"),
            types.Part.from_text(text=eval_prompt),
        ]
        log.step("Requesting Gemini evaluation...")
        resp = self.client.models.generate_content(
            model=GEMINI_REVIEW_MODEL,
            contents=[types.Content(role="user", parts=parts)],
        )
        return resp.text.strip()


def pick_winner(reviewer, prompt, candidates, out_dir, prev_best=None):
    """Review candidates and return the winning path.

    One candidate means nothing to compare, so the review is skipped entirely —
    that is the normal case for local models, where a second candidate costs
    another full GPU run.
    """
    if len(candidates) == 1:
        return candidates[0]

    log.subsection("Gemini Evaluation")
    try:
        text = reviewer.compare(prompt, candidates[0], candidates[1], prev_best)
        save_file(text, out_dir, "gemini_eval_response.txt")
        decision = extract_json_from_response(text).get("Decision", "A_BETTER")
    except Exception as e:
        log.warn(f"Evaluation failed, defaulting to A. Error: {e}")
        decision = "A_BETTER"

    winner = "B" if decision == "B_BETTER" else "A"
    log.success(f"Decision: {decision} → using candidate {winner}")
    return candidates[1] if decision == "B_BETTER" else candidates[0]


# ======================
# GENERATION DRIVER
# ======================
def submit_candidate(backend, out_path, prompt, image, duration, seed, state_path, slot,
                     dry_run=False, extra_refs=()):
    """Submit one candidate and checkpoint its task id.

    Returns the task handle, or None when there is nothing to wait for — the
    video is already downloaded, or this is a dry run. Task ids are checkpointed
    next to the video so an interrupted run polls the existing job instead of
    paying for a second one; backends whose handle is not a string simply don't
    checkpoint.
    """
    out_path = Path(out_path)
    if out_path.exists():
        log.info(f"{out_path.name} already exists — skipping")
        return None

    # Check before submitting, and before a dry run reports success: a prompt over
    # the provider's limit is rejected at the door, and the point of --dry-run is to
    # find that out without spending anything.
    if backend.prompt_limit and len(prompt) > backend.prompt_limit:
        raise RuntimeError(
            f"{backend.name}: prompt is {len(prompt)} chars, over its {backend.prompt_limit}-char "
            f"limit. That is the hosted API's limit, not the model's — the self-hosted backend "
            f"has none, so `-m minimax-local` runs this prompt as it is."
        )

    if dry_run:
        log.warn(
            f"--dry-run: would submit {slot} to {backend.name} "
            f"({duration}s, seed {seed}, prompt {len(prompt)} chars, image {Path(image).name if not str(image).startswith('http') else image}"
            + (f", +{len(extra_refs)} character ref(s)" if extra_refs else "") + ")"
        )
        return None

    state = read_state(state_path)
    if state.get("status") == "failed":
        raise PolicyViolationError(state.get("task_id"), state.get("message", "previous run failed"))

    task = state.get(slot)
    if task:
        log.info(f"Resuming {slot}: ...{str(task)[-8:]}")
        return task

    log.step(f"Submitting {slot} to {backend.name} ({duration}s)...")
    task = backend.submit(prompt, image, duration, seed=seed, extra_refs=extra_refs)
    if isinstance(task, str):
        state[slot] = task
        write_state(state_path, state)
    log.info(f"Submitted {slot}: ...{str(task)[-8:]}")
    return task


def collect_candidate(backend, out_path, task, state_path):
    """Poll a submitted task and save the video."""
    out_path = Path(out_path)
    if task is None:
        return out_path if out_path.exists() else None
    try:
        result = backend.poll(task)
    except PolicyViolationError as e:
        state = read_state(state_path)
        state.update({"status": "failed", "message": str(e), "task_id": e.task_id})
        write_state(state_path, state)
        raise
    except ManualGenerationPending as e:
        # `-m manual`: nothing to download; the job goes to manifest.json instead.
        MANUAL_JOBS.append({"out": str(out_path), **e.job})
        log.warn(f"{out_path.name}: queued for manual generation (see manifest.json)")
        return None
    fetch_result(result, out_path)
    log.success(f"{out_path.name} downloaded")
    return out_path


def generate_candidates(backend, specs, state_path, dry_run=False):
    """Every candidate is submitted before any is polled.

    The providers are asynchronous, so submitting both and then waiting lets them
    run at the same time; submitting one, waiting for it, then submitting the next
    doubles the wall clock for no benefit. This is what both original scripts did.
    """
    tasks = [
        submit_candidate(backend, s["out"], s["prompt"], s["image"], s["duration"],
                         s["seed"], state_path, s["slot"], dry_run,
                         extra_refs=s.get("extra_refs", ()))
        for s in specs
    ]
    if any(t is not None for t in tasks):
        log.step("Waiting for videos...")
    produced = [collect_candidate(backend, s["out"], t, state_path) for s, t in zip(specs, tasks)]
    return [p for p in produced if p is not None]


def candidate_specs(out_dir, prompt, image, duration, name="candidate", extra_refs=()):
    """The two candidates every video is generated as.

    Two is the method, not a tuning knob: the reviewer's whole job is a pairwise
    A/B comparison, so one candidate would mean no review at all and results that
    cannot be compared with a model that did get reviewed.
    """
    return [
        {"out": Path(out_dir) / f"{name}_{c}.mp4", "prompt": prompt, "image": image,
         "duration": duration, "seed": BASE_SEED + c - 1, "slot": f"task_id_{c}",
         "extra_refs": tuple(extra_refs)}
        for c in (1, 2)
    ]


def write_manifest(run_dir, backend, route):
    """`-m manual`: everything needed to generate the queued clips elsewhere."""
    if not MANUAL_JOBS:
        return None
    path = Path(run_dir) / "manifest.json"
    payload = {
        "backend": backend.name, "route": route,
        "instructions": (
            "Generate each job with any video model (web UI, Higgsfield MCP, another "
            "provider): use `prompt` as the text, `image` as the first frame (i2v) or the "
            "reference storyboard (ref), `extra_refs` as additional character references, "
            "and `duration` in seconds. Save the result at `out`, then re-run the same "
            "create_video.py command with --auto-resume to review and concatenate."
        ),
        "jobs": MANUAL_JOBS,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.warn(f"{len(MANUAL_JOBS)} job(s) written to {path} - generate them, then re-run with --auto-resume")
    return path


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def trim_clip(src, dst, seconds):
    """Cut a clip to `seconds` (re-encoded so every trimmed clip concatenates cleanly)."""
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-t", f"{seconds:.2f}",
         "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-movflags", "+faststart", str(dst)],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return dst


def concat_clips(clips, run_dir, final_name="final_story.mp4"):
    run_dir = Path(run_dir)
    list_path = run_dir / "concat_list.txt"
    list_path.write_text(
        "".join(f"file '{Path(p).resolve()}'\n" for p in clips), encoding="utf-8"
    )
    final_path = run_dir / final_name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
             "-c", "copy", str(final_path)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        log.success(f"Final video: {final_path}")
    except Exception as e:
        log.error(f"ffmpeg concat failed: {e}")
    return final_path


# ======================
# ROUTE: ref  (one video from the whole storyboard)
# ======================
def run_ref_route(args, backend, paths, run_dir, reviewer):
    script = json.loads(paths["script"].read_text(encoding="utf-8"))
    n_shots = len(script.get("clips", []))
    template = PROMPT_STORYBOARD_CREATIVE if args.creative else PROMPT_STORYBOARD

    extra_refs = resolve_extra_refs(args, backend, args.dry_run)

    # A long creative shot list that does not fit one request is generated as
    # consecutive segments, each from its own sub-storyboard, then concatenated.
    if args.creative and n_shots > creative_config.max_shots_per_segment():
        return run_ref_segments(args, backend, paths, run_dir, reviewer, script, template, extra_refs)

    prompt = template.read_text(encoding="utf-8").replace(
        "<script>", json.dumps(script, ensure_ascii=False)
    )
    save_file(prompt, run_dir, "prompt.txt")

    best_path = Path(run_dir) / "best_video.mp4"
    if best_path.exists():
        log.success("best_video.mp4 already exists — done")
        return best_path

    image = (upload_image_to_imgbb(str(paths["storyboard"]))
             if backend.needs_public_url and not args.dry_run else paths["storyboard"])
    duration = ref_duration_for(n_shots, backend) if args.creative else backend.ref_duration

    candidates = generate_candidates(
        backend,
        candidate_specs(run_dir, prompt, image, duration, extra_refs=extra_refs),
        Path(run_dir) / "tasks_info.json",
        dry_run=args.dry_run,
    )
    if not candidates:
        log.warn("Nothing generated (dry run?) — stopping before review.")
        return None

    winner = pick_winner(reviewer, prompt, candidates, run_dir)
    shutil.copy(winner, best_path)
    return best_path


def run_ref_segments(args, backend, paths, run_dir, reviewer, script, template, extra_refs):
    """Ref route for long shot lists: one reference-conditioned video per segment.

    The creative storyboard is cut into panels, the panels of each segment are
    re-tiled into a small sub-storyboard (same 3-column grid), and that
    sub-storyboard + the segment's shots drive one request. The reviewer judges
    each segment against the previous winner (sequential rubric) so identity and
    style carry across the cuts; the winners are concatenated.
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    from opensource_backends.flux2.grid_compose import compose_grid  # pure PIL

    run_dir = Path(run_dir)
    panels_dir = ensure_panels(args, paths["iter_dir"], paths["storyboard"], len(script["clips"]))
    per_segment = creative_config.max_shots_per_segment()
    clips = script["clips"]
    segments = [clips[i:i + per_segment] for i in range(0, len(clips), per_segment)]
    log.info(f"segments     : {len(segments)} x up to {per_segment} shots")

    best_videos = []
    prev_best = None
    for s_idx, seg_clips in enumerate(segments, start=1):
        seg_dir = run_dir / f"segment_{s_idx}"
        seg_dir.mkdir(parents=True, exist_ok=True)
        best_path = seg_dir / f"best_segment_{s_idx}.mp4"

        # Sub-storyboard: the segment's panels in a fresh grid, renumbered 1..k.
        sub_sb = seg_dir / "storyboard.png"
        if not sub_sb.exists():
            panels = []
            for c in seg_clips:
                p = panels_dir / f"panel{c['id']}.png"
                if not p.exists():
                    raise FileNotFoundError(f"storyboard panel for shot {c['id']} missing: {p}")
                panels.append(Image.open(p).convert("RGB"))
            w = max(im.width for im in panels)
            h = max(im.height for im in panels)
            compose_grid(panels, columns=creative_config.STORYBOARD_COLUMNS, cell_size=(w, h)).save(sub_sb)
        sub_script = {"characters": script.get("characters", []), "clips": []}
        for k, c in enumerate(seg_clips, start=1):
            sub_script["clips"].append({**c, "id": k, "global_id": c["id"]})
        prompt = template.read_text(encoding="utf-8").replace(
            "<script>", json.dumps(sub_script, ensure_ascii=False)
        )
        save_file(prompt, str(seg_dir), "prompt.txt")

        if best_path.exists():
            log.info(f"{best_path.name} already exists — skipping")
        else:
            log.section(f"Segment {s_idx}/{len(segments)} — shots {seg_clips[0]['id']}..{seg_clips[-1]['id']}")
            image = (upload_image_to_imgbb(str(sub_sb))
                     if backend.needs_public_url and not args.dry_run else sub_sb)
            duration = ref_duration_for(len(seg_clips), backend)
            candidates = generate_candidates(
                backend,
                candidate_specs(seg_dir, prompt, image, duration, extra_refs=extra_refs),
                seg_dir / "tasks_info.json",
                dry_run=args.dry_run,
            )
            if not candidates:
                log.warn("Nothing generated (dry run?) — stopping before review.")
                return None
            winner = pick_winner(reviewer, prompt, candidates, seg_dir, prev_best)
            shutil.copy(winner, best_path)
        best_videos.append(best_path)
        prev_best = best_path

    log.section("Concatenating Segments")
    return concat_clips(best_videos, run_dir)


# ======================
# ROUTE: i2v  (one clip per storyboard panel)
# ======================
def build_panel_jobs(args, backend, paths, script):
    """Prompt, image and duration for each panel, in panel order."""
    panels_dir = paths["panels"]
    manga_dir = paths["manga_panels"]

    total_area = 0
    if manga_dir and manga_dir.is_dir():
        for i in range(1, args.panel_count + 1):
            p = manga_dir / f"panel{i}.png"
            if p.exists():
                with Image.open(p) as im:
                    total_area += im.width * im.height

    template = (PROMPT_PANEL_CREATIVE if args.creative else PROMPT_PANEL).read_text(encoding="utf-8")
    jobs = []
    for i in range(1, args.panel_count + 1):
        image = panels_dir / f"panel{i}.png"
        if not image.exists():
            log.warn(f"storyboard panel{i}.png not found, skipping.")
            continue
        try:
            panel_data = get_panel_data(script, i)
        except ValueError as e:
            log.warn(f"{e} — skipping panel {i}")
            continue

        if args.creative:
            # The director already decided how long each shot holds; the
            # provider's clamp is the only correction (a 1.5s hold is trimmed
            # back down after generation, see run_i2v_route).
            lo, hi = backend.panel_clamp
            hint = float(panel_data["clip"].get("duration_hint", 2.5))
            duration = max(lo, min(hi, int(round(hint))))
            log.info(f"Shot {i}: duration_hint {hint:.1f}s → request {duration}s (clamp {lo}-{hi})")
        else:
            manga_img = manga_dir / f"panel{i}.png" if manga_dir else None
            duration = calculate_optimal_duration(
                str(manga_img) if manga_img and manga_img.exists() else str(image),
                total_area, panel_data["clip"], backend.panel_clamp,
            )

        # A backend with a small text budget builds its own condensed prompt;
        # everything else gets the pipeline's per-panel template + the clip JSON.
        prompt = backend.build_panel_prompt(panel_data["clip"], panel_data["characters"])
        if prompt is None:
            prompt = template.replace("<script>", json.dumps(panel_data, ensure_ascii=False))

        jobs.append({"index": i, "prompt": prompt, "image": image, "duration": duration,
                     "duration_hint": panel_data["clip"].get("duration_hint")})
    return jobs


def run_i2v_route(args, backend, paths, run_dir, reviewer):
    run_dir = Path(run_dir)
    script = json.loads(paths["script"].read_text(encoding="utf-8"))
    jobs = build_panel_jobs(args, backend, paths, script)
    if not jobs:
        raise RuntimeError("No usable storyboard panels found.")

    # candidate_paths[c][panel index] -> mp4
    candidate_paths = {1: {}, 2: {}}
    extra_refs = resolve_extra_refs(args, backend, args.dry_run)

    if backend.batch:
        # One invocation per candidate: the model loads once and does every panel,
        # instead of paying the load cost per clip.
        for c in (1, 2):
            cand_dir = run_dir / f"candidate_{c}"
            existing = backend.collect_outputs(jobs, cand_dir) if cand_dir.is_dir() else []
            if existing and all(p is not None for p in existing):
                log.info(f"candidate_{c}: all {len(jobs)} clips exist — skipping generation")
                produced = existing
            else:
                log.section(f"Generating candidate {c} / 2 — {len(jobs)} panels")
                produced = backend.generate_batch(jobs, cand_dir, seed=BASE_SEED + c - 1)
            for job, path in zip(jobs, produced):
                if path is not None:
                    candidate_paths[c][job["index"]] = Path(path)
        if not candidate_paths[1]:
            log.warn("No clips produced (dry run?) — stopping before review.")
            return None
    else:
        for job in jobs:
            log.section(f"Panel {job['index']}")
            panel_dir = run_dir / f"panel_{job['index']}"
            panel_dir.mkdir(parents=True, exist_ok=True)
            image = (upload_image_to_imgbb(str(job["image"]))
                     if backend.needs_public_url and not args.dry_run else job["image"])
            specs = candidate_specs(panel_dir, job["prompt"], image, job["duration"],
                                    extra_refs=extra_refs)
            generate_candidates(backend, specs, panel_dir / "tasks_info.json", dry_run=args.dry_run)
            for c, spec in enumerate(specs, start=1):
                if spec["out"].exists():
                    candidate_paths[c][job["index"]] = spec["out"]

    if not any(candidate_paths[1].values()) and not any(candidate_paths[2].values()):
        if args.dry_run:
            log.warn("--dry-run: nothing generated, stopping before review.")
        elif MANUAL_JOBS:
            log.warn("All clips are queued for manual generation; review runs on the next --auto-resume.")
        else:
            log.warn("No candidate clip was produced; nothing to review.")
        return None

    # Review each panel against the previous panel's winner (sequential rubric).
    best_videos = []
    prev_best = None
    for job in jobs:
        i = job["index"]
        panel_dir = run_dir / f"panel_{i}"
        panel_dir.mkdir(parents=True, exist_ok=True)
        best_path = panel_dir / f"best_panel_{i}.mp4"

        if best_path.exists():
            log.info(f"best_panel_{i}.mp4 already exists — skipping review")
        else:
            cands = [candidate_paths[c].get(i) for c in (1, 2)]
            cands = [c for c in cands if c is not None and Path(c).exists()]
            if not cands:
                log.warn(f"panel {i}: no candidate video, skipping")
                continue
            log.section(f"Reviewing panel {i}")
            winner = pick_winner(reviewer, job["prompt"], cands, panel_dir, prev_best)
            shutil.copy(winner, best_path)

        best_videos.append(best_path)
        prev_best = best_path

    if not best_videos:
        raise RuntimeError("No panel videos to concatenate.")

    # Creative mode: cut each winner back to the director's duration_hint so the
    # timing of holds and impact frames is the director's, not the provider's
    # minimum clip length. Every trimmed clip is re-encoded with the same settings
    # so the concat below can still stream-copy.
    if args.creative and args.trim_to_hint:
        if not ffmpeg_available():
            log.warn("ffmpeg not found — clips are concatenated untrimmed")
        else:
            log.section("Trimming clips to duration_hint")
            trimmed = []
            hints = {j["index"]: j.get("duration_hint") for j in jobs}
            for p in best_videos:
                m = re.search(r"best_panel_(\d+)\.mp4$", p.name)
                hint = hints.get(int(m.group(1))) if m else None
                dst = p.with_name(p.stem + "_cut.mp4")
                if not dst.exists():
                    try:
                        trim_clip(p, dst, float(hint) if hint else 10 ** 6)
                        log.info(f"{p.name} → {dst.name} ({float(hint):.1f}s)" if hint else f"{p.name} → {dst.name}")
                    except Exception as e:  # keep the untrimmed clip rather than lose the shot
                        log.warn(f"trim failed for {p.name} ({e}); using the full clip")
                        dst = p
                trimmed.append(dst)
            best_videos = trimmed

    log.section("Concatenating Clips")
    return concat_clips(best_videos, run_dir)


# ======================
# PATHS
# ======================
def find_iter_dir(run_dir, iteration):
    """The iter pinned by --iter, or the newest one that actually has a storyboard.

    Auto-selection means a run that never produced its last iteration still generates from the one it
    did. Every iteration passed over on the way down is logged: falling back to
    an earlier storyboard changes what gets generated, so it must not be silent.
    """
    if iteration is not None:
        d = run_dir / f"iter{iteration}"
        if not d.is_dir():
            available = sorted(p.name for p in run_dir.glob("iter*") if p.is_dir())
            raise FileNotFoundError(
                f"iter{iteration} not found under {run_dir}. Available: "
                f"{', '.join(available) or 'none'} (--iter pins one, omit it for the newest)"
            )
        return d

    existing = {
        int(p.name[4:]): p
        for p in run_dir.glob("iter*")
        if p.is_dir() and re.fullmatch(r"iter\d+", p.name)
    }
    if not existing:
        raise FileNotFoundError(f"No iter* directory under {run_dir}")

    for idx in sorted(existing, reverse=True):
        d = existing[idx]
        if any((d / f"bestStoryboard.{e}").exists() for e in IMG_EXTS):
            # Report each iteration above the winner, up to the one the pipeline
            # normally ends on, so "generated from iter0" always says why.
            for skipped in range(idx + 1, max(max(existing), EXPECTED_LAST_ITER) + 1):
                reason = "has no bestStoryboard" if skipped in existing else "does not exist"
                log.warn(f"iter{skipped} {reason} — using {d.name} instead")
            return d
    raise FileNotFoundError(f"No iter* with a bestStoryboard under {run_dir}")


def find_storyboard(iter_dir):
    for ext in IMG_EXTS:
        p = iter_dir / f"bestStoryboard.{ext}"
        if p.exists():
            return p
    raise FileNotFoundError(f"bestStoryboard not found in {iter_dir}")


def ensure_panels(args, iter_dir, storyboard=None, expect=None):
    """Cut the storyboard into panels if that hasn't been done yet.

    Cropping is a subprocess rather than an import because it pulls in MAGI's
    torch/transformers stack, which nothing else in this script needs.

    `storyboard` / `expect` point the cropper at a creative storyboard (which
    lives in iter<N>/creative/ and has 6, 9 or 12 panels) instead of the
    faithful one.
    """
    base = Path(storyboard).parent if storyboard is not None else iter_dir
    panels_dir = base / "storyboard_panels"
    if panels_dir.is_dir() and any(panels_dir.glob("panel*.png")):
        return panels_dir

    log.section("Cropping storyboard panels")
    n_panels = expect or args.panel_count or 6
    sb_path = Path(storyboard) if storyboard is not None else find_storyboard(iter_dir)

    if args.crop_mode == "grid":
        grid_slice(sb_path, panels_dir, n_panels)
        return panels_dir

    cmd = [
        sys.executable, str(PROJECT_ROOT / "src" / "crop_storyboard_panels.py"),
        "-t", args.target,
        "--results-root", str(PROJECT_ROOT / args.results_root),
        "--iter", iter_dir.name[len("iter"):],
    ]
    if args.time:
        cmd += ["--time", args.time]
    if storyboard is not None:
        cmd += ["--storyboard", str(storyboard), "--out", str(panels_dir)]
    if expect:
        cmd += ["--expect", str(expect)]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        if not args.dry_run:
            raise
        # A dry run should not need MAGI's torch stack just to show the prompts.
        log.warn(f"MAGI cropping failed in --dry-run ({e}); slicing the grid geometrically instead")
        grid_slice(sb_path, panels_dir, n_panels)

    if not (panels_dir.is_dir() and any(panels_dir.glob("panel*.png"))):
        raise FileNotFoundError(f"Panel cropping produced nothing in {panels_dir}")
    return panels_dir


def grid_slice(storyboard, out_dir, n_panels, columns=creative_config.STORYBOARD_COLUMNS, inset=0.015):
    """Cut a regular storyboard grid into equal cells without MAGI.

    Good enough for the creative sheets (Gemini draws them as an exact 3 x R
    grid) and for dry runs; `--crop-mode magi` (the default) keeps the paper's
    detector + border trim for the faithful route.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = max(1, -(-n_panels // columns))
    with Image.open(storyboard) as im:
        im = im.convert("RGB")
        w, h = im.size
        cw, ch = w / columns, h / rows
        dx, dy = cw * inset, ch * inset
        k = 0
        for r in range(rows):
            for c in range(columns):
                k += 1
                if k > n_panels:
                    break
                box = (int(c * cw + dx), int(r * ch + dy), int((c + 1) * cw - dx), int((r + 1) * ch - dy))
                im.crop(box).save(out_dir / f"panel{k}.png")
    log.info(f"grid-sliced {storyboard.name if hasattr(storyboard, 'name') else storyboard} into {n_panels} panels ({columns}x{rows})")
    return out_dir


def build_paths(args, backend):
    results_target = PROJECT_ROOT / args.results_root / args.target
    run_dir = resolve_run_dir(results_target, args.time)
    iter_dir = find_iter_dir(run_dir, args.iter)

    if args.creative:
        creative_dir = iter_dir / "creative"
        script = creative_dir / "expandedScript.json"
        if not script.exists():
            raise FileNotFoundError(
                f"{script} not found. Run `python src/run_all.py -t {args.target} --creative "
                f"--auto-resume` first (or drop --creative to use the faithful storyboard)."
            )
        storyboard = find_storyboard(creative_dir)
        output_name = f"{args.model}-creative"
    else:
        script = iter_dir / "bestScript.json"
        if not script.exists():
            raise FileNotFoundError(f"bestScript.json not found: {script}")
        storyboard = find_storyboard(iter_dir)
        output_name = args.model

    manga_panels = PROJECT_ROOT / args.target_root / args.target / "manga_panels"
    paths = {
        "pipeline_run_dir": run_dir,
        "iter_dir": iter_dir,
        "storyboard": storyboard,
        "script": script,
        "manga_panels": manga_panels if manga_panels.is_dir() else None,
        "base_output_dir": results_target / "video" / output_name,
        "panels": None,
    }
    return paths


# ======================
# ARGS
# ======================
def parse_args():
    parser = argparse.ArgumentParser(
        description="Storyboard → video. The model decides the route: reference-image "
                    "models generate the whole story in one video, i2v models generate "
                    "one clip per storyboard panel."
    )
    parser.add_argument("-t", "--target", required=True, help="Target folder name under the target root")
    parser.add_argument("-m", "--model", default=None, choices=BACKEND_NAMES,
                        help="Video model. ref-route: seedance, minimax-local, minimax-api. "
                             "i2v-route: wan, anisora. Omit to detect it from "
                             "VIDEO_GENERATION_MODEL. The exact model id always comes "
                             "from that backend's own variable, e.g. SEEDANCE_MODEL.")
    parser.add_argument("--target-root", default="target", help="Directory holding the targets")
    parser.add_argument("--results-root", default="results", help="Directory holding the results")
    parser.add_argument("--time", default=None,
                        help="Pipeline run timestamp under <results-root>/<target>/ (default: newest)")
    parser.add_argument("--iter", type=int, default=None,
                        help="Iteration to read bestStoryboard/bestScript from "
                             "(default: the newest iter that has a storyboard; a "
                             "fallback to an earlier one is logged)")
    parser.add_argument("--panel-count", type=int, default=None,
                        help="i2v route: number of panels (default: 6, or the shot count in --creative)")
    parser.add_argument("--auto-resume", action="store_true",
                        help="Continue the newest run_* folder instead of starting a new one")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve paths, build every prompt and print what would be submitted, without calling any model (anisora also writes its prompt_list)")
    parser.add_argument("--creative", action="store_true",
                        help="Generate from iter<N>/creative/ (the expanded shot list and its "
                             "storyboard) with the creative video prompts")
    parser.add_argument("--route", choices=["auto", "ref", "i2v"], default=None,
                        help="Force the ref or i2v route when the model supports both "
                             "(default: VIDEO_ROUTE from .env, or auto)")
    parser.add_argument("--no-character-refs", action="store_true",
                        help="Do not send character model sheets as extra reference images")
    parser.add_argument("--no-trim", dest="trim_to_hint", action="store_false",
                        help="Creative i2v: keep the provider's clip length instead of trimming "
                             "each clip to its duration_hint")
    parser.add_argument("--crop-mode", choices=["magi", "grid"], default=os.getenv("CROP_MODE", "magi"),
                        help="How to cut the storyboard into panels: MAGI detection + border trim "
                             "(default) or a geometric grid slice that needs no GPU/torch")
    args = parser.parse_args()
    if args.iter is not None and args.iter < 0:
        args.iter = None
    return args


# ======================
# MAIN
# ======================
def main():
    args = parse_args()

    from_env = args.model is None
    if from_env:
        args.model = model_from_env()

    backend = get_backend(args.model)
    if hasattr(backend, "dry_run"):
        backend.dry_run = args.dry_run

    paths = build_paths(args, backend)
    script = json.loads(paths["script"].read_text(encoding="utf-8"))
    n_shots = len(script.get("clips", []))
    if args.panel_count is None:
        args.panel_count = n_shots if args.creative else 6

    # The route used to be a property of the model alone. It still is for models
    # that only take one kind of image; for the others `--route` / auto decides.
    level = creative_level(paths["iter_dir"]) if args.creative else None
    mode = choose_route(args, backend, n_shots, level)
    backend.mode = mode

    label = f" · creative/{level} ({n_shots} shots)" if args.creative else ""
    log.section(f"Video Pipeline — {backend.name} ({mode} route) — {args.target}{label}")

    if from_env:
        log.info("model        : from VIDEO_GENERATION_MODEL (no -m given)")
    log.info(f"pipeline run : {paths['pipeline_run_dir'].name}")
    log.info(f"iteration    : {paths['iter_dir'].name}")
    log.info(f"storyboard   : {paths['storyboard']}")
    log.info(f"script       : {paths['script']}")
    log.info(f"model id     : {backend.model_id}")
    log.info(f"review model : {GEMINI_REVIEW_MODEL}")
    log.info(f"route        : {mode} ({'forced' if (args.route or creative_config.video_route()) != 'auto' else 'auto'})")

    # A dry run touches no provider, so it must not need their credentials —
    # inspecting the prompts is exactly what you do before you have the keys.
    if not args.dry_run:
        backend.check_env()

    if mode == "i2v":
        if args.creative:
            paths["panels"] = ensure_panels(args, paths["iter_dir"], paths["storyboard"], n_shots)
        else:
            paths["panels"] = ensure_panels(args, paths["iter_dir"])
        log.info(f"panels       : {paths['panels']}")

    base_output_dir = paths["base_output_dir"]
    base_output_dir.mkdir(parents=True, exist_ok=True)

    run_dir = None
    if args.auto_resume:
        runs = sorted(d for d in os.listdir(base_output_dir) if d.startswith("run_"))
        if runs:
            run_dir = base_output_dir / runs[-1]
            log.info(f"Resuming: {run_dir}")
    if run_dir is None:
        run_dir = base_output_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        run_dir.mkdir(parents=True, exist_ok=True)
        log.info(f"New run: {run_dir}")

    # A dry run never produces a video, so it must not require review credentials.
    reviewer = None if args.dry_run else Reviewer()

    try:
        if mode == "ref":
            result = run_ref_route(args, backend, paths, run_dir, reviewer)
        else:
            result = run_i2v_route(args, backend, paths, run_dir, reviewer)
    except PolicyViolationError as e:
        log.error(f"Policy violation — stopping: {e}")
        raise SystemExit(0)

    manifest = write_manifest(run_dir, backend, mode)
    log.success(f"DONE → {result or manifest or run_dir}")


if __name__ == "__main__":
    main()
