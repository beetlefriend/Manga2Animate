"""Creative Expansion settings, read once from .env / the environment.

Everything the creative layer tunes lives here so that run_all.py, create_video.py
and full_pipeline.py agree on names and defaults. Nothing in this module talks to
a model; it only resolves configuration.

Creativity levels
-----------------
    conservative  same shot count as manga panels; a few re-angles, no inserts
    balanced      +50% shots: every clip kept, plus reaction / impact inserts
    aggressive    x2 shots: the full sakuga treatment

Shot counts are kept at a multiple of STORYBOARD_COLUMNS (3) so the creative
storyboard is always a full, regular grid that crop_storyboard_panels.py can
reconcile. ``shots_for_level`` rounds up to the next multiple when NUM_CLIPS is
not 6.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

LEVELS = ("conservative", "balanced", "aggressive")

# Friendly aliases accepted on the command line and in .env. The right-hand side
# is the canonical level name used everywhere else.
LEVEL_ALIASES = {
    "faithful": "conservative",
    "safe": "conservative",
    "low": "conservative",
    "medium": "balanced",
    "default": "balanced",
    "high": "aggressive",
    "sakuga": "aggressive",
    "full": "aggressive",
    "full-sakuga": "aggressive",
}

STORYBOARD_COLUMNS = 3

# Gemini image models take an aspect ratio, not a pixel size. Pick the one whose
# shape is closest to a grid of 16:9-ish cells with the given row count.
GRID_ASPECT_BY_ROWS = {1: "16:9", 2: "16:9", 3: "4:3", 4: "3:4", 5: "3:4", 6: "9:16"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def normalize_level(value: str | None, default: str = "balanced") -> str:
    """'faithful' -> 'conservative', 'sakuga' -> 'aggressive', etc."""
    if not value:
        return default
    v = value.strip().lower()
    if v in LEVELS:
        return v
    if v in LEVEL_ALIASES:
        return LEVEL_ALIASES[v]
    raise ValueError(
        f"Unknown creativity level '{value}'. Use one of {', '.join(LEVELS)} "
        f"(aliases: {', '.join(sorted(LEVEL_ALIASES))})."
    )


def round_up_to_columns(n: int, columns: int = STORYBOARD_COLUMNS) -> int:
    return int(math.ceil(n / columns)) * columns


def shots_for_level(level: str, num_clips: int) -> int:
    """Target shot count for a level, honouring CREATIVE_SHOTS_<LEVEL> overrides.

    Defaults scale with the number of manga panels (6 -> 6 / 9 / 12) and are
    rounded up to a full storyboard row.
    """
    level = normalize_level(level)
    multiplier = {"conservative": 1.0, "balanced": 1.5, "aggressive": 2.0}[level]
    default = round_up_to_columns(int(round(num_clips * multiplier)))
    override = _env_int(f"CREATIVE_SHOTS_{level.upper()}", 0)
    target = override if override > 0 else default
    return max(num_clips, round_up_to_columns(target))


@dataclass(frozen=True)
class CreativeSettings:
    enabled: bool
    level: str
    num_clips: int
    shot_count: int
    max_inserts: int
    max_reangles: int
    num_storyboards: int
    review_rounds: int
    image_size: str
    columns: int
    notes: str

    @property
    def rows(self) -> int:
        return int(math.ceil(self.shot_count / self.columns))

    @property
    def grid_aspect(self) -> str:
        return GRID_ASPECT_BY_ROWS.get(self.rows, "3:4")

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "level": self.level,
            "num_clips": self.num_clips,
            "shot_count": self.shot_count,
            "max_inserts": self.max_inserts,
            "max_reangles": self.max_reangles,
            "num_storyboards": self.num_storyboards,
            "review_rounds": self.review_rounds,
            "image_size": self.image_size,
            "grid": f"{self.columns}x{self.rows}",
            "grid_aspect": self.grid_aspect,
            "notes": self.notes,
        }


LEVEL_NOTES = {
    "conservative": (
        "Stay close to the manga. Re-angle only where the panel composition is weak "
        "for animation (e.g. a flat medium shot of an impact becomes a low angle)."
    ),
    "balanced": (
        "Add reaction close-ups and one impact frame on the strongest action beat. "
        "At most one establishing shot. Keep dialogue shots calm and readable."
    ),
    "aggressive": (
        "Full sakuga: crash zooms, whip pans, orbital moves, Dutch angles, first-person "
        "flashes, speed-line impact frames. Every action beat gets an insert. Still no "
        "new events, cast, wardrobe or dialogue."
    ),
}


def load_settings(
    level: str | None = None,
    enabled: bool | None = None,
    num_clips: int | None = None,
) -> CreativeSettings:
    """Resolve the creative settings from arguments first, then the environment."""
    level = normalize_level(level or os.environ.get("CREATIVITY_LEVEL"), "balanced")
    if enabled is None:
        enabled = _env_bool("CREATIVE_MODE", False)
    if num_clips is None:
        num_clips = _env_int("NUM_CLIPS", 6)

    shot_count = shots_for_level(level, num_clips)
    inserts_default = {"conservative": 0, "balanced": shot_count - num_clips,
                       "aggressive": shot_count - num_clips}[level]
    reangles_default = {"conservative": 2, "balanced": 3, "aggressive": num_clips}[level]

    return CreativeSettings(
        enabled=enabled,
        level=level,
        num_clips=num_clips,
        shot_count=shot_count,
        max_inserts=max(0, _env_int("CREATIVE_MAX_INSERTS", inserts_default)),
        max_reangles=max(0, _env_int("CREATIVE_MAX_REANGLES", reangles_default)),
        num_storyboards=max(1, _env_int("CREATIVE_NUM_STORYBOARDS", 2)),
        review_rounds=max(0, _env_int("CREATIVE_REVIEW_ROUNDS", 1)),
        image_size=os.environ.get("CREATIVE_IMAGE_SIZE", "2K").strip() or "2K",
        columns=STORYBOARD_COLUMNS,
        notes=os.environ.get("CREATIVE_NOTES", "").strip() or LEVEL_NOTES[level],
    )


# --- Video-side knobs (read by create_video.py) -------------------------------

def seconds_per_shot() -> float:
    """Target seconds of video per shot when sizing the ref-route request."""
    return _env_float("VIDEO_SECONDS_PER_SHOT", 2.5)


def max_shots_per_segment() -> int:
    """Ref route: split the shot list into segments of at most this many shots."""
    return max(1, _env_int("VIDEO_MAX_SHOTS_PER_SEGMENT", 6))


def video_route() -> str:
    v = (os.environ.get("VIDEO_ROUTE", "auto") or "auto").strip().lower()
    return v if v in {"auto", "ref", "i2v"} else "auto"


def video_character_refs(default: bool) -> bool:
    return _env_bool("VIDEO_CHARACTER_REFS", default)


def character_sheets_mode() -> str:
    """auto | true | false. 'auto' generates sheets only when the target has a
    single composite ref image (no characters/ folder)."""
    v = (os.environ.get("CHARACTER_SHEETS", "auto") or "auto").strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return "true"
    if v in {"0", "false", "no", "off"}:
        return "false"
    return "auto"


def character_sheet_candidates() -> int:
    return max(1, _env_int("CHARACTER_SHEET_CANDIDATES", 2))
