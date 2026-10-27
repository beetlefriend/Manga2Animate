"""Creative Expansion, stage 2: expanded shot list -> creative storyboard.

Draws ``CREATIVE_NUM_STORYBOARDS`` candidate sheets for the expanded shot list
(``prompts/create_storyboard_creative.txt``: a 3 x R grid, one panel per shot,
each panel drawn from the shot's own camera), then picks the best candidate
with pairwise comparisons on the creative rubric
(``prompts/storyboard_reviewer_creative.txt``: camera realisation, motion
readability, spatial continuity under new angles, source fidelity ...).

Output under ``iter<K>/creative/``:

    storyboard/storyboard_<k>.png     the candidates
    bestStoryboard.png                the winner (what create_video.py reads)
    intermediate/sb_compare_<a>_<b>.json, bestStoryboard_meta.json,
    storyboard_feedback.json

The faithful stages are untouched: this module has its own prompt, its own
grid shape and its own reviewer so nothing about the paper's pipeline changes
when creative mode is off.
"""

from __future__ import annotations

import json
import mimetypes
import os
import shutil
from itertools import combinations
from pathlib import Path

from dotenv import load_dotenv
from google.genai import types

from src import creative_config
from src.creative_expansion import creative_dir, expanded_script_path, panel_paths
from src.log import error, info, step, subsection, success, warn
from src.runs import results_dir_from_env
from src.utils import (
    character_ref_images,
    image_part,
    is_dry_run,
    make_genai_client,
    parse_json_object,
    with_retry,
)

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
_target = os.environ.get("TARGET", "")
_target_root = os.environ.get("TARGET_ROOT", "target")
TARGET_DIR = BASE_DIR / _target_root / _target if _target else BASE_DIR / _target_root
RESULTS_DIR = results_dir_from_env(BASE_DIR)
PROMPTS_DIR = BASE_DIR / "prompts"

CREATE_PROMPT = PROMPTS_DIR / "create_storyboard_creative.txt"
REVIEW_PROMPT = PROMPTS_DIR / "storyboard_reviewer_creative.txt"

IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gemini-3-pro-image")
TEXT_MODEL = os.environ.get("TEXT_MODEL", "gemini-3.1-pro-preview")
NUM_CLIPS = int(os.environ.get("NUM_CLIPS", "6"))
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# Pass/fail criteria are scored per side; the rest are comparative.
INDEPENDENT = {"Color Presence & Visual Variety", "Grid Integrity", "Only Existing Characters",
               "Shot Order Consistency", "No Dialogue Boxes"}
TIE_ORDER = ["Grid Integrity", "Camera Realisation", "Character Identity Preservation",
             "Motion & Impact Readability", "Source Fidelity", "Spatial Continuity",
             "Overall Art Style Consistency", "Left/Right Feature Consistency",
             "Background Consistency", "Only Existing Characters", "Shot Order Consistency",
             "No Dialogue Boxes", "Color Presence & Visual Variety"]

_client = None


def client():
    global _client
    if _client is None:
        _client = make_genai_client()
    return _client


# --- Paths -------------------------------------------------------------------

def storyboard_dir(iteration: int) -> Path:
    return creative_dir(iteration) / "storyboard"


def candidate_path(iteration: int, index: int) -> Path | None:
    for p in sorted(storyboard_dir(iteration).glob(f"storyboard_{index}.*")):
        if p.suffix.lower() in IMAGE_EXTS and p.stat().st_size > 0:
            return p
    return None


def best_path(iteration: int) -> Path | None:
    for p in sorted(creative_dir(iteration).glob("bestStoryboard.*")):
        if p.suffix.lower() in IMAGE_EXTS and p.stat().st_size > 0:
            return p
    return None


def done(iteration: int) -> bool:
    interim = creative_dir(iteration) / "intermediate"
    return best_path(iteration) is not None and (interim / "bestStoryboard_meta.json").is_file()


# --- Prompts -----------------------------------------------------------------

def _fill(template: str, expanded: dict, settings: creative_config.CreativeSettings) -> str:
    n_panels = len(panel_paths())
    return (
        template.replace("{Columns}", str(settings.columns))
        .replace("{Rows}", str(rows_for(expanded, settings)))
        .replace("{Shot Count}", str(len(expanded["clips"])))
        .replace("{Panel Count Plus One}", str(n_panels + 1))
        .replace("{Panel Count}", str(n_panels))
        .replace("{Expanded Shot List}", json.dumps(expanded, ensure_ascii=False, indent=2))
    )


def rows_for(expanded: dict, settings: creative_config.CreativeSettings) -> int:
    return max(1, -(-len(expanded["clips"]) // settings.columns))


def grid_aspect(expanded: dict, settings: creative_config.CreativeSettings) -> str:
    return creative_config.GRID_ASPECT_BY_ROWS.get(rows_for(expanded, settings), "3:4")


# --- Generation --------------------------------------------------------------

def generate_candidate(expanded: dict, settings: creative_config.CreativeSettings,
                       iteration: int, index: int) -> Path | None:
    prompt = _fill(CREATE_PROMPT.read_text(encoding="utf-8"), expanded, settings)
    images = [*panel_paths(), *(p for _, p in character_ref_images(TARGET_DIR))]
    out_dir = storyboard_dir(iteration)
    out_dir.mkdir(parents=True, exist_ok=True)

    if is_dry_run():
        p = out_dir / f"dryrun_storyboard_prompt_{index}.txt"
        p.write_text(prompt, encoding="utf-8")
        warn(f"--dry-run: wrote storyboard prompt ({len(prompt)} chars, {len(images)} images, "
             f"aspect {grid_aspect(expanded, settings)}, size {settings.image_size}) to {p.name}")
        return None

    parts = [image_part(p) for p in images]
    parts.append(types.Part.from_text(text=prompt))
    config = types.GenerateContentConfig(
        image_config=types.ImageConfig(
            aspect_ratio=grid_aspect(expanded, settings),
            image_size=settings.image_size,
        ),
        response_modalities=["IMAGE"],
    )
    saved: list[Path] = []

    def _stream():
        for chunk in client().models.generate_content_stream(
            model=IMAGE_MODEL, contents=[types.Content(role="user", parts=parts)], config=config
        ):
            if chunk.parts is None:
                continue
            part = chunk.parts[0]
            if part.inline_data and part.inline_data.data:
                ext = mimetypes.guess_extension(part.inline_data.mime_type) or ".png"
                out = out_dir / f"storyboard_{index}{ext}"
                out.write_bytes(part.inline_data.data)
                saved.append(out)

    with_retry(_stream, label=f"Creative storyboard {index} ({IMAGE_MODEL})")
    for p in saved:
        success(f"Saved {p}")
    return saved[-1] if saved else None


# --- Review ------------------------------------------------------------------

def compare(expanded: dict, settings: creative_config.CreativeSettings,
            sb_a: Path, sb_b: Path, out_path: Path) -> dict:
    prompt = _fill(REVIEW_PROMPT.read_text(encoding="utf-8"), expanded, settings)
    images = [*panel_paths(), *(p for _, p in character_ref_images(TARGET_DIR)), sb_a, sb_b]
    parts = [image_part(p) for p in images]
    parts.append(types.Part.from_text(text=prompt))
    raw = with_retry(
        lambda: client().models.generate_content(
            model=TEXT_MODEL,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level="HIGH"),
            ),
        ).text,
        label=f"Creative storyboard reviewer ({TEXT_MODEL})",
    )
    result = parse_json_object(raw) if raw else None
    if result is None:
        result = {"_raw": raw or ""}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    success(f"Saved {out_path.name}")
    return result


def winner_of(result: dict) -> str | None:
    label = (result.get("overall_winner") or {}).get("storyboard")
    if label in ("A", "B"):
        return label
    scores = result.get("scores") or {}
    a, b = scores.get("A"), scores.get("B")
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a != b:
        return "A" if a > b else "B"
    return None


def _passes(v) -> bool:
    if isinstance(v, dict):
        v = v.get("pass")
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"pass", "1", "true", "yes"}


def criterion_points(result: dict, side: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for name, crit in (result.get("criteria_results") or {}).items():
        if not isinstance(crit, dict):
            continue
        if name in INDEPENDENT:
            out[name] = 1 if _passes(crit.get(side)) else 0
        else:
            out[name] = 1 if crit.get("winner") == side else 0
    return out


def pick_best(iteration: int, expanded: dict, settings: creative_config.CreativeSettings,
              candidates: dict[int, Path]) -> tuple[int, list[dict], str]:
    """Round-robin comparison; returns (winner index, winner feedbacks, branch label)."""
    interim = creative_dir(iteration) / "intermediate"
    interim.mkdir(parents=True, exist_ok=True)
    indices = sorted(candidates)
    if len(indices) == 1:
        return indices[0], [], "single_candidate"

    results: dict[tuple[int, int], dict] = {}
    wins = {i: 0 for i in indices}
    for a, b in combinations(indices, 2):
        step(f"creative reviewer: sb {a} vs sb {b}")
        res = compare(expanded, settings, candidates[a], candidates[b],
                      interim / f"sb_compare_{a}_{b}.json")
        results[(a, b)] = res
        w = winner_of(res)
        if w == "A":
            wins[a] += 1
            info(f"sb {a} beats sb {b}")
        elif w == "B":
            wins[b] += 1
            info(f"sb {b} beats sb {a}")
        else:
            info(f"sb {a} vs sb {b}: tie / unparseable")

    info(f"Pairwise win tally: {wins}")
    top = max(wins.values())
    leaders = [i for i in indices if wins[i] == top]
    branch = "clean"
    if len(leaders) > 1:
        branch = "tiebreak"
        per_crit = {i: {} for i in indices}
        for (a, b), res in results.items():
            for name, pts in criterion_points(res, "A").items():
                per_crit[a][name] = per_crit[a].get(name, 0) + pts
            for name, pts in criterion_points(res, "B").items():
                per_crit[b][name] = per_crit[b].get(name, 0) + pts
        for name in TIE_ORDER:
            scores = {i: per_crit[i].get(name, 0) for i in leaders}
            best = max(scores.values())
            lead = [i for i in leaders if scores[i] == best]
            if len(lead) == 1:
                info(f"Tie broken by '{name}': sb {lead[0]}")
                leaders = lead
                break
    winner = leaders[0]

    feedbacks = []
    for (a, b), res in results.items():
        if winner not in (a, b):
            continue
        side = "A" if winner == a else "B"
        if winner_of(res) == side:
            fb = res.get("improvement_suggestions_for_winner") or {}
            if fb:
                feedbacks.append(fb)
    return winner, feedbacks, branch


# --- Main --------------------------------------------------------------------

def main(iteration: int, settings: creative_config.CreativeSettings | None = None) -> Path | None:
    settings = settings or creative_config.load_settings(enabled=True, num_clips=NUM_CLIPS)
    script_path = expanded_script_path(iteration)
    if not script_path.is_file():
        if is_dry_run():
            warn(f"--dry-run: {script_path.name} does not exist yet; the creative storyboard "
                 f"prompt is built from it once the shot expansion has run")
            return None
        error(f"{script_path} not found. Run creative_expansion first.")
        return None
    expanded = json.loads(script_path.read_text(encoding="utf-8"))
    n = len(expanded.get("clips", []))
    subsection(
        f"creative_storyboard (iter={iteration}, shots={n}, grid={settings.columns}x{rows_for(expanded, settings)}, "
        f"candidates={settings.num_storyboards}, backend=gemini)"
    )

    candidates: dict[int, Path] = {}
    for k in range(settings.num_storyboards):
        existing = candidate_path(iteration, k)
        if existing is not None:
            success(f"Storyboard {k + 1}/{settings.num_storyboards} already exists ({existing.name}) - skipping")
            candidates[k] = existing
            continue
        step(f"Creative storyboard {k + 1}/{settings.num_storyboards}")
        p = generate_candidate(expanded, settings, iteration, k)
        if p is not None:
            candidates[k] = p

    if not candidates:
        if is_dry_run():
            warn("--dry-run: no storyboard generated, stopping before review.")
        else:
            error("No creative storyboard candidate was produced.")
        return None

    winner, feedbacks, branch = pick_best(iteration, expanded, settings, candidates)
    src = candidates[winner]
    dst = creative_dir(iteration) / f"bestStoryboard{src.suffix}"
    shutil.copy2(src, dst)
    interim = creative_dir(iteration) / "intermediate"
    interim.mkdir(parents=True, exist_ok=True)
    (interim / "bestStoryboard_meta.json").write_text(
        json.dumps({"branch": branch, "winner_idx": winner, "candidates": sorted(candidates),
                    "shots": n, "grid": f"{settings.columns}x{rows_for(expanded, settings)}"},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (interim / "storyboard_feedback.json").write_text(
        json.dumps({"feedbacks": feedbacks}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    success(f"Best creative storyboard: sb {winner} -> {dst.name} ({branch})")
    return dst


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Draw and pick the creative storyboard for one iteration.")
    ap.add_argument("--iter", type=int, default=int(os.environ.get("NUM_ITERATIONS", "2")) - 1)
    ap.add_argument("--creativity", default=None)
    a = ap.parse_args()
    main(a.iter, creative_config.load_settings(level=a.creativity, enabled=True, num_clips=NUM_CLIPS))
