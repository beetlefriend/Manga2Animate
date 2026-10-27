"""Creative Expansion, stage 1: faithful script -> expanded cinematic shot list.

Runs after the faithful refinement loop has produced ``iter<K>/bestScript.json``.
Two agents, both reading the manga panels and the character references:

  Shot Expansion Agent   (prompts/shot_expansion_creator.txt)
      Re-stages the faithful clips with new camera angles and inserts silent
      in-between shots (reaction close-ups, impact frames, crash zooms, whip
      pans ...). It may change the camera, never the world: no new cast,
      wardrobe, events, order or dialogue.

  Continuity Reviewer    (prompts/shot_expansion_reviewer.txt)
      Checks every expanded shot against the source panels and the faithful
      script and returns keep / revise / drop verdicts. Revisions go back to
      the creator; shots that are still ``drop`` after the last round are
      removed here.

Output (all under ``iter<K>/creative/``):

    expandedScript.json          the final shot list, same top-level shape as a
                                 faithful script ({"characters", "clips"}) so the
                                 storyboard and video stages can consume it
                                 unchanged; each clip carries the extra creative
                                 fields (source_clip, shot_kind, camera, motion,
                                 sakuga_tier, duration_hint)
    expansion_summary.json       counts, verdict history, padding
    creative_config.json         the resolved CreativeSettings
    intermediate/expanded_r<N>.json, shot_review_r<N>.json, and the prompts
    (dryrun_*.txt) when DRY_RUN is set

The shot count is always a multiple of the storyboard column count, so the
creative storyboard is a full regular grid. If the reviewer drops shots, the
list is padded with ``hold`` shots (a static hold on the last shot of the same
source clip), which are legitimate anime beats and keep the grid regular.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

from dotenv import load_dotenv
from google.genai import types

from src import creative_config
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
PANELS_DIR = TARGET_DIR / "manga_panels"
RESULTS_DIR = results_dir_from_env(BASE_DIR)
PROMPTS_DIR = BASE_DIR / "prompts"

CREATOR_PROMPT = PROMPTS_DIR / "shot_expansion_creator.txt"
REVIEWER_PROMPT = PROMPTS_DIR / "shot_expansion_reviewer.txt"

MODEL = os.environ.get("TEXT_MODEL", "gemini-3.1-pro-preview")
NUM_CLIPS = int(os.environ.get("NUM_CLIPS", "6"))

SHOT_KINDS = {"faithful", "reangle", "insert", "hold"}

_client = None


def client():
    """Lazy Gemini client so a dry run (and `import`) never needs credentials."""
    global _client
    if _client is None:
        _client = make_genai_client()
    return _client


# --- Paths -------------------------------------------------------------------

def iter_dir(iteration: int) -> Path:
    return RESULTS_DIR / f"iter{iteration}"


def creative_dir(iteration: int) -> Path:
    return iter_dir(iteration) / "creative"


def expanded_script_path(iteration: int) -> Path:
    return creative_dir(iteration) / "expandedScript.json"


def panel_paths() -> list[Path]:
    return [PANELS_DIR / f"panel{i}.png" for i in range(1, NUM_CLIPS + 1)]


def reference_images() -> list[Path]:
    """Manga panels first, then every character reference (sheets included)."""
    return [*panel_paths(), *(p for _, p in character_ref_images(TARGET_DIR))]


# --- Prompt building ---------------------------------------------------------

def build_creator_prompt(settings: creative_config.CreativeSettings, faithful: dict,
                         verdicts: dict | None) -> str:
    template = CREATOR_PROMPT.read_text(encoding="utf-8")
    verdict_text = (
        json.dumps(verdicts, ensure_ascii=False, indent=2)
        if verdicts else "(none - this is the first draft)"
    )
    return (
        template.replace("{Creativity Level}", settings.level)
        .replace("{Shot Count}", str(settings.shot_count))
        .replace("{Max Inserts}", str(settings.max_inserts))
        .replace("{Max Reangles}", str(settings.max_reangles))
        .replace("{Creativity Notes}", settings.notes)
        .replace("{Faithful Script}", json.dumps(faithful, ensure_ascii=False, indent=2))
        .replace("{Reviewer Verdicts}", verdict_text)
    )


def build_reviewer_prompt(faithful: dict, expanded: dict) -> str:
    template = REVIEWER_PROMPT.read_text(encoding="utf-8")
    return (
        template.replace("{Faithful Script}", json.dumps(faithful, ensure_ascii=False, indent=2))
        .replace("{Expanded Shot List}", json.dumps(expanded, ensure_ascii=False, indent=2))
    )


def call_agent(prompt: str, images: list[Path], label: str) -> dict | None:
    parts = [image_part(p) for p in images]
    parts.append(types.Part.from_text(text=prompt))
    raw = with_retry(
        lambda: client().models.generate_content(
            model=MODEL,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level="HIGH"),
            ),
        ).text,
        label=f"{label} ({MODEL})",
    )
    if raw is None:
        return None
    parsed = parse_json_object(raw)
    if parsed is None:
        warn(f"{label}: could not parse JSON from the response")
    return parsed


# --- Validation / normalisation ----------------------------------------------

def _faithful_ids(faithful: dict) -> list[int]:
    return [int(c.get("id", i + 1)) for i, c in enumerate(faithful.get("clips", []))]


def normalize_expanded(expanded: dict, faithful: dict,
                       settings: creative_config.CreativeSettings) -> tuple[dict, list[str]]:
    """Make the creator's output safe for the downstream stages.

    * ``characters`` is always the faithful array (the creator must not touch it).
    * ids are renumbered 1..N in output order.
    * every clip gets the creative fields with sane defaults.
    Returns the cleaned script and a list of human-readable problems.
    """
    problems: list[str] = []
    clips = [c for c in expanded.get("clips", []) if isinstance(c, dict)]
    if not clips:
        return {"characters": faithful.get("characters", []), "clips": []}, ["creator returned no clips"]

    valid_source = set(_faithful_ids(faithful))
    valid_chars = {int(c["id"]) for c in faithful.get("characters", []) if "id" in c}

    cleaned = []
    for i, c in enumerate(clips, start=1):
        shot = dict(c)
        shot["id"] = i
        try:
            shot["source_clip"] = int(shot.get("source_clip", 0))
        except (TypeError, ValueError):
            shot["source_clip"] = 0
        if shot["source_clip"] not in valid_source:
            problems.append(f"shot {i}: source_clip {shot['source_clip']} is not a faithful clip")
            shot["source_clip"] = min(valid_source, key=lambda v: abs(v - shot["source_clip"])) if valid_source else 1
        kind = str(shot.get("shot_kind", "faithful")).strip().lower()
        if kind not in SHOT_KINDS:
            problems.append(f"shot {i}: unknown shot_kind '{kind}', treating as reangle")
            kind = "reangle"
        shot["shot_kind"] = kind
        shot.setdefault("placement", "replaces" if kind in {"faithful", "reangle"} else "after")
        cam = shot.get("camera") if isinstance(shot.get("camera"), dict) else {}
        shot["camera"] = {
            "angle": str(cam.get("angle", "eye level")),
            "framing": str(cam.get("framing", "medium")),
            "movement": str(cam.get("movement", "static hold")),
            "lens": str(cam.get("lens", "normal")),
        }
        shot["motion"] = str(shot.get("motion", "")).strip()
        try:
            shot["sakuga_tier"] = max(0, min(3, int(shot.get("sakuga_tier", 0))))
        except (TypeError, ValueError):
            shot["sakuga_tier"] = 0
        try:
            shot["duration_hint"] = float(shot.get("duration_hint", 2.5))
        except (TypeError, ValueError):
            shot["duration_hint"] = 2.5
        shot["duration_hint"] = max(1.5, min(5.0, shot["duration_hint"]))
        shot["environment"] = str(shot.get("environment", "")).strip()
        shot["description"] = str(shot.get("description", "")).strip()
        ids = shot.get("characters_in_panel")
        if not isinstance(ids, list):
            ids = []
        ids = sorted({int(x) for x in ids if str(x).lstrip("-").isdigit()} & valid_chars)
        shot["characters_in_panel"] = ids
        cleaned.append(shot)

    covered = {c["source_clip"] for c in cleaned if c["shot_kind"] in {"faithful", "reangle"}}
    for sid in sorted(valid_source - covered):
        problems.append(f"faithful clip {sid} has no faithful/reangle shot (coverage gap)")

    if len(cleaned) != settings.shot_count:
        problems.append(f"creator returned {len(cleaned)} shots, {settings.shot_count} requested")

    return {
        "creativity": settings.level,
        "source_clip_count": len(valid_source),
        "characters": deepcopy(faithful.get("characters", [])),
        "clips": cleaned,
    }, problems


def apply_drops(expanded: dict, review: dict) -> tuple[dict, list[int]]:
    """Remove shots the reviewer marked ``drop`` and renumber."""
    drops = {
        int(s["id"]) for s in review.get("shots", [])
        if isinstance(s, dict) and str(s.get("verdict", "")).lower() == "drop" and "id" in s
    }
    if not drops:
        return expanded, []
    kept = [c for c in expanded["clips"] if int(c["id"]) not in drops]
    for i, c in enumerate(kept, start=1):
        c["id"] = i
    out = dict(expanded)
    out["clips"] = kept
    return out, sorted(drops)


def make_hold_shot(after: dict) -> dict:
    """A static hold on the shot before it: keeps the grid regular without adding story."""
    hold = deepcopy(after)
    hold["shot_kind"] = "hold"
    hold["placement"] = "after"
    hold["camera"] = {**after.get("camera", {}), "movement": "static hold"}
    hold["motion"] = "Hold frame: almost no motion; a breath, a blink, hair settling."
    hold["sakuga_tier"] = 0
    hold["duration_hint"] = 1.5
    hold["description"] = "Static hold on the previous shot. " + _strip_dialogue_sentences(after.get("description", ""))
    hold["rationale"] = "Padding hold so the storyboard grid stays regular."
    return hold


def _strip_dialogue_sentences(text: str) -> str:
    """Drop 'Dialogue: ...' / 'Thoughts: ...' sentences so a hold never repeats a line."""
    out = []
    for sentence in text.replace("\n", " ").split(". "):
        s = sentence.strip()
        if not s or s.lower().startswith(("dialogue", "thought")):
            continue
        out.append(s)
    return ". ".join(out).strip()


def pad_to_grid(expanded: dict, columns: int) -> tuple[dict, int]:
    clips = expanded["clips"]
    if not clips:
        return expanded, 0
    target = creative_config.round_up_to_columns(len(clips), columns)
    added = 0
    while len(clips) < target:
        clips.append(make_hold_shot(clips[-1]))
        clips[-1]["id"] = len(clips)
        added += 1
    return expanded, added


def kind_counts(expanded: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in expanded.get("clips", []):
        counts[c.get("shot_kind", "?")] = counts.get(c.get("shot_kind", "?"), 0) + 1
    return counts


# --- Completion check (used by run_all's resume logic) -----------------------

def done(iteration: int, columns: int = creative_config.STORYBOARD_COLUMNS) -> bool:
    p = expanded_script_path(iteration)
    if not p.is_file() or p.stat().st_size == 0:
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    clips = data.get("clips", [])
    return bool(clips) and len(clips) % columns == 0


# --- Main --------------------------------------------------------------------

def main(iteration: int, settings: creative_config.CreativeSettings | None = None) -> Path | None:
    settings = settings or creative_config.load_settings(enabled=True, num_clips=NUM_CLIPS)
    faithful_path = iter_dir(iteration) / "bestScript.json"
    if not faithful_path.is_file():
        error(f"{faithful_path} not found. The faithful pipeline must finish iter{iteration} first.")
        return None
    faithful = json.loads(faithful_path.read_text(encoding="utf-8"))

    out_dir = creative_dir(iteration)
    interim = out_dir / "intermediate"
    interim.mkdir(parents=True, exist_ok=True)
    (out_dir / "creative_config.json").write_text(
        json.dumps(settings.as_dict(), indent=2), encoding="utf-8"
    )

    subsection(
        f"creative_expansion (iter={iteration}, level={settings.level}, "
        f"shots={settings.shot_count}, grid={settings.columns}x{settings.rows}, "
        f"review_rounds={settings.review_rounds})"
    )
    images = reference_images()
    info(f"{len(panel_paths())} panel(s) + {len(images) - len(panel_paths())} reference image(s)")

    history: list[dict] = []

    def create(verdicts: dict | None, round_no: int) -> dict | None:
        prompt = build_creator_prompt(settings, faithful, verdicts)
        if is_dry_run():
            p = interim / f"dryrun_creator_r{round_no}.txt"
            p.write_text(prompt, encoding="utf-8")
            warn(f"--dry-run: wrote creator prompt ({len(prompt)} chars, {len(images)} images) to {p.name}")
            return None
        step(f"Shot Expansion Agent (round {round_no})")
        raw = call_agent(prompt, images, "Shot expansion")
        if raw is None:
            return None
        expanded, problems = normalize_expanded(raw, faithful, settings)
        for pr in problems:
            warn(pr)
        (interim / f"expanded_r{round_no}.json").write_text(
            json.dumps(expanded, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        history.append({"round": round_no, "stage": "create", "shots": len(expanded["clips"]),
                        "kinds": kind_counts(expanded), "problems": problems})
        success(f"Expanded to {len(expanded['clips'])} shots: {kind_counts(expanded)}")
        return expanded

    def review(expanded: dict, round_no: int) -> dict | None:
        prompt = build_reviewer_prompt(faithful, expanded)
        if is_dry_run():
            p = interim / f"dryrun_reviewer_r{round_no}.txt"
            p.write_text(prompt, encoding="utf-8")
            warn(f"--dry-run: wrote reviewer prompt to {p.name}")
            return None
        step(f"Continuity Reviewer (round {round_no})")
        result = call_agent(prompt, images, "Shot review")
        if result is None:
            warn("Reviewer failed; keeping the current shot list as-is.")
            return None
        (interim / f"shot_review_r{round_no}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        verdicts = [str(s.get("verdict", "?")).lower() for s in result.get("shots", []) if isinstance(s, dict)]
        tally = {v: verdicts.count(v) for v in sorted(set(verdicts))}
        history.append({"round": round_no, "stage": "review", "approved": bool(result.get("approved")),
                        "coverage_ok": bool(result.get("coverage_ok", True)), "verdicts": tally})
        (success if result.get("approved") else warn)(
            f"Review: approved={bool(result.get('approved'))} verdicts={tally}"
        )
        return result

    expanded = create(None, 0)
    if expanded is None:
        if is_dry_run():
            # Still write the reviewer prompt against the faithful script so the
            # whole round trip can be inspected without a model call.
            review({"characters": faithful.get("characters", []), "clips": faithful.get("clips", [])}, 1)
            return None
        error("Shot expansion failed; no expanded script written.")
        return None

    dropped: list[int] = []
    last_review = None
    for r in range(1, settings.review_rounds + 1):
        last_review = review(expanded, r)
        if last_review is None or last_review.get("approved"):
            break
        if r < settings.review_rounds:
            revised = create(last_review, r)
            if revised is not None:
                expanded = revised
        else:
            expanded, dropped = apply_drops(expanded, last_review)
            if dropped:
                warn(f"Dropped shot(s) {dropped} on the reviewer's final verdict")

    expanded, padded = pad_to_grid(expanded, settings.columns)
    if padded:
        info(f"Padded with {padded} hold shot(s) to keep a {settings.columns}-column grid")

    out_path = expanded_script_path(iteration)
    out_path.write_text(json.dumps(expanded, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "level": settings.level,
        "requested_shots": settings.shot_count,
        "final_shots": len(expanded["clips"]),
        "kinds": kind_counts(expanded),
        "dropped": dropped,
        "padded_holds": padded,
        "approved": bool(last_review.get("approved")) if last_review else None,
        "history": history,
    }
    (out_dir / "expansion_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    success(f"Saved {out_path} ({len(expanded['clips'])} shots)")
    return out_path


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Expand the faithful script of one iteration into a cinematic shot list.")
    ap.add_argument("--iter", type=int, default=int(os.environ.get("NUM_ITERATIONS", "2")) - 1)
    ap.add_argument("--creativity", default=None)
    a = ap.parse_args()
    main(a.iter, creative_config.load_settings(level=a.creativity, enabled=True, num_clips=NUM_CLIPS))
