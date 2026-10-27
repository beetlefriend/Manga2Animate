"""Character model sheets: one turnaround + expression sheet per character.

The paper's pipeline conditions every stage on whatever the target ships:
``ref.png`` (one composite sheet) or ``characters/char<id>.png``. A single
composite drawn from one angle is a weak reference once the creative layer
starts asking for low angles, reverse angles and extreme close-ups. This stage
turns the manga panels + the shipped reference into a proper model sheet per
character (four-view turnaround, six expressions, three detail call-outs),
reviewed against the source so a bad sheet never becomes the reference.

    prompts/character_sheet_creator.txt    image model, N candidates per character
    prompts/character_sheet_reviewer.txt   text model, pairwise A/B pick

Output under ``<run>/character/sheets/``:

    char<id>.png                 the winning sheet (what the other stages use)
    candidates/char<id>_<k>.png  every candidate
    review_char<id>.json         the reviewer's verdict (last comparison)
    manifest.json                {"sheets": {id: path}, "unusable": [ids]}

run_all.py exports CHARACTER_SHEETS_DIR after this stage, and
src.utils.character_ref_images() appends the usable sheets to the shipped
references for the storyboard, creative and video stages. The script
reviewers keep looking at the shipped references only: those are ground truth,
the sheets are derived.
"""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path

from dotenv import load_dotenv
from google.genai import types

from src import creative_config
from src.log import error, info, step, subsection, success, warn
from src.runs import results_dir_from_env
from src.utils import (
    find_character_ref_images,
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

CREATE_PROMPT = PROMPTS_DIR / "character_sheet_creator.txt"
REVIEW_PROMPT = PROMPTS_DIR / "character_sheet_reviewer.txt"

IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gemini-3-pro-image")
TEXT_MODEL = os.environ.get("TEXT_MODEL", "gemini-3.1-pro-preview")
NUM_CLIPS = int(os.environ.get("NUM_CLIPS", "6"))
SHEET_ASPECT = os.environ.get("CHARACTER_SHEET_ASPECT", "16:9")
SHEET_SIZE = os.environ.get("CHARACTER_SHEET_SIZE", "2K")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

_client = None


def client():
    global _client
    if _client is None:
        _client = make_genai_client()
    return _client


# --- Paths -------------------------------------------------------------------

def sheets_dir() -> Path:
    return RESULTS_DIR / "character" / "sheets"


def manifest_path() -> Path:
    return sheets_dir() / "manifest.json"


def panel_paths() -> list[Path]:
    return [p for p in (PANELS_DIR / f"panel{i}.png" for i in range(1, NUM_CLIPS + 1)) if p.is_file()]


def should_generate(mode: str | None = None) -> bool:
    """CHARACTER_SHEETS=true always, false never, auto only when the target has
    a single composite reference (no characters/ folder)."""
    mode = mode or creative_config.character_sheets_mode()
    if mode == "true":
        return True
    if mode == "false":
        return False
    return not (TARGET_DIR / "characters").is_dir()


def done() -> bool:
    p = manifest_path()
    if not p.is_file():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    sheets = data.get("sheets") or {}
    return bool(sheets) and all(Path(v).is_file() for v in sheets.values())


def load_characters() -> list[dict]:
    path = RESULTS_DIR / "character" / "character_description.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    chars = data.get("characters", []) if isinstance(data, dict) else []
    return [c for c in chars if isinstance(c, dict) and "id" in c]


# --- Prompts -----------------------------------------------------------------

def _fill(template: str, char: dict) -> str:
    n = len(panel_paths())
    return (
        template.replace("{Panel Count Plus One}", str(n + 1))
        .replace("{Panel Count}", str(n))
        .replace("{Character Id}", str(char["id"]))
        .replace("{Character Description}", str(char.get("description", "")))
    )


def context_images() -> list[Path]:
    return [*panel_paths(), *(p for _, p in find_character_ref_images(TARGET_DIR))]


# --- Generation + review -----------------------------------------------------

def generate_sheet(char: dict, index: int, out_dir: Path) -> Path | None:
    existing = next((p for p in out_dir.glob(f"char{char['id']}_{index}.*")
                     if p.suffix.lower() in IMAGE_EXTS and p.stat().st_size > 0), None)
    if existing is not None:
        info(f"candidate {index} already exists ({existing.name}) - skipping")
        return existing

    prompt = _fill(CREATE_PROMPT.read_text(encoding="utf-8"), char)
    images = context_images()
    if is_dry_run():
        p = out_dir / f"dryrun_sheet_prompt_char{char['id']}_{index}.txt"
        p.write_text(prompt, encoding="utf-8")
        warn(f"--dry-run: wrote sheet prompt ({len(prompt)} chars, {len(images)} images) to {p.name}")
        return None

    parts = [image_part(p) for p in images]
    parts.append(types.Part.from_text(text=prompt))
    config = types.GenerateContentConfig(
        image_config=types.ImageConfig(aspect_ratio=SHEET_ASPECT, image_size=SHEET_SIZE),
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
                out = out_dir / f"char{char['id']}_{index}{ext}"
                out.write_bytes(part.inline_data.data)
                saved.append(out)

    with_retry(_stream, label=f"Character sheet char{char['id']} #{index} ({IMAGE_MODEL})")
    return saved[-1] if saved else None


def review_pair(char: dict, sheet_a: Path, sheet_b: Path, out_path: Path) -> dict:
    prompt = _fill(REVIEW_PROMPT.read_text(encoding="utf-8"), char)
    images = [*context_images(), sheet_a, sheet_b]
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
        label=f"Character sheet reviewer char{char['id']} ({TEXT_MODEL})",
    )
    result = parse_json_object(raw) if raw else None
    if result is None:
        result = {"_raw": raw or ""}
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def pick_winner(char: dict, candidates: list[Path], out_dir: Path) -> tuple[Path, bool]:
    """King-of-the-hill over the candidates; returns (winner, usable)."""
    if len(candidates) == 1:
        return candidates[0], True
    champion = candidates[0]
    usable = True
    for k, challenger in enumerate(candidates[1:], start=1):
        step(f"char{char['id']}: sheet reviewer, {champion.name} vs {challenger.name}")
        res = review_pair(char, champion, challenger, out_dir / f"review_char{char['id']}_{k}.json")
        label = (res.get("overall_winner") or {}).get("sheet")
        if label not in ("A", "B"):
            scores = res.get("scores") or {}
            a, b = scores.get("A"), scores.get("B")
            label = "B" if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b > a else "A"
        winner = champion if label == "A" else challenger
        usable_map = res.get("usable") or {}
        usable = bool(usable_map.get(label, True))
        info(f"char{char['id']}: {winner.name} wins ({label}), usable={usable}")
        champion = winner
    return champion, usable


# --- Main --------------------------------------------------------------------

def main() -> Path | None:
    chars = load_characters()
    if not chars:
        error("character_description.json has no characters; cannot build sheets.")
        return None
    out_dir = sheets_dir()
    cand_dir = out_dir / "candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)
    n_cand = creative_config.character_sheet_candidates()

    subsection(f"character_sheets ({len(chars)} character(s), {n_cand} candidate(s) each)")

    manifest = {"sheets": {}, "unusable": [], "candidates": {}}
    if manifest_path().is_file():
        try:
            manifest.update(json.loads(manifest_path().read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    for char in chars:
        cid = str(char["id"])
        final = next((p for p in out_dir.glob(f"char{cid}.*") if p.suffix.lower() in IMAGE_EXTS), None)
        if final is not None and cid in manifest.get("sheets", {}):
            success(f"char{cid}: sheet already exists ({final.name}) - skipping")
            continue
        step(f"Character[{cid}] model sheet")
        candidates = []
        for k in range(1, n_cand + 1):
            p = generate_sheet(char, k, cand_dir)
            if p is not None:
                candidates.append(p)
        if not candidates:
            if not is_dry_run():
                warn(f"char{cid}: no candidate produced")
            continue
        winner, usable = pick_winner(char, candidates, out_dir)
        final = out_dir / f"char{cid}{winner.suffix}"
        final.write_bytes(winner.read_bytes())
        manifest["sheets"][cid] = str(final)
        manifest["candidates"][cid] = [str(p) for p in candidates]
        if not usable:
            manifest["unusable"] = sorted(set(manifest.get("unusable", [])) | {cid})
            warn(f"char{cid}: reviewer marked the winning sheet unusable; it will NOT be used as a reference")
        else:
            manifest["unusable"] = [u for u in manifest.get("unusable", []) if u != cid]
            success(f"char{cid}: saved {final.name}")

    if is_dry_run() and not manifest["sheets"]:
        return None
    manifest_path().write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    success(f"Saved {manifest_path()} ({len(manifest['sheets'])} sheet(s), {len(manifest['unusable'])} unusable)")
    return manifest_path()


if __name__ == "__main__":
    main()
