"""Build a synthetic case (target/_dryrun) and a completed faithful run so every
entry point can be exercised with --dry-run, without MAGI, Gemini or a video
provider:

    python tests/make_fixture.py            # repo root is the default
    python src/full_pipeline.py -t _dryrun --creative -m seedance --crop-mode grid --dry-run
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

repo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
case = "_dryrun"
run_id = "20260101_000000"

target = repo / "target" / case
panels = target / "manga_panels"
panels.mkdir(parents=True, exist_ok=True)
page = Image.new("RGB", (900, 1200), "white")
d = ImageDraw.Draw(page)
for i in range(6):
    x, y = (i % 2) * 450, (i // 2) * 400
    d.rectangle([x + 10, y + 10, x + 440, y + 390], outline="black", width=4)
    d.text((x + 30, y + 30), f"panel {i + 1}", fill="black")
    page.crop((x, y, x + 450, y + 400)).save(panels / f"panel{i + 1}.png")
page.save(target / "manga_page.jpg")
ref = Image.new("RGB", (600, 400), "white")
ImageDraw.Draw(ref).text((20, 20), "ref sheet", fill="black")
ref.save(target / "ref.png")

run = repo / "results" / case / run_id
char = run / "character"
char.mkdir(parents=True, exist_ok=True)
(char / "transcript.txt").write_text(" ### Transcript ###\nline 1\nline 2\n", encoding="utf-8")
(char / "character_description.json").write_text(json.dumps({
    "characters": [
        {"id": 1, "description": "Teen boy, lean; short black hair; sharp eyes; school uniform"},
        {"id": 2, "description": "Teen girl; long red hair; green eyes; track jacket"},
    ]
}, indent=2), encoding="utf-8")

script = {
    "characters": [
        {"id": 1, "description": "Teen boy, lean; short black hair; sharp dark eyes; navy school blazer; grey trousers; black shoes"},
        {"id": 2, "description": "Teen girl, athletic; long red hair in a ponytail; green eyes; red track jacket; black shorts; white sneakers"},
    ],
    "clips": [
        {"id": 1, "visual_analysis": "Two characters face off; one speech bubble.", "environment": "Afternoon. School courtyard, one bench on the left side of the screen, a single tree to its right.",
         "description": "Medium shot. Character[1] stands on the left side of the screen facing screen right, fists clenched. Character[2] stands on the right side facing screen left. Dialogue: 'You came.'", "characters_in_panel": [1, 2]},
        {"id": 2, "visual_analysis": "Close-up, silent.", "environment": "Afternoon. Same courtyard, tree behind.",
         "description": "Close-up. Character[2] on the right side of the screen, eyes narrowed, facing screen left. Silent.", "characters_in_panel": [2]},
        {"id": 3, "visual_analysis": "Punch thrown; speed lines.", "environment": "Afternoon. Courtyard, bench on the left side of the screen.",
         "description": "Wide shot. Character[1] lunges from the left side of the screen toward screen right, right fist extended. Character[2] leans back on the right side.", "characters_in_panel": [1, 2]},
        {"id": 4, "visual_analysis": "Block.", "environment": "Afternoon. Courtyard.",
         "description": "Medium shot. Character[2] on the right side of the screen catches the fist with her left hand, facing screen left. Dialogue: 'Too slow.'", "characters_in_panel": [1, 2]},
        {"id": 5, "visual_analysis": "Reaction.", "environment": "Afternoon. Courtyard, tree on the right side of the screen.",
         "description": "Close-up. Character[1] on the left side of the screen, teeth gritted, facing screen right. Thoughts: 'She is faster.'", "characters_in_panel": [1]},
        {"id": 6, "visual_analysis": "Both step back.", "environment": "Late afternoon. Courtyard, bench on the left, tree on the right, long shadows.",
         "description": "Wide shot. Character[1] on the left side of the screen and Character[2] on the right side both step back, facing each other. Dialogue: 'Again.'", "characters_in_panel": [1, 2]},
    ],
}

sb = Image.new("RGB", (1536, 1024), "white")
d = ImageDraw.Draw(sb)
for i in range(6):
    x, y = (i % 3) * 512, (i // 3) * 512
    d.rectangle([x + 8, y + 8, x + 504, y + 480], outline="black", width=5)
    d.text((x + 240, y + 486), str(i + 1), fill="black")

for it in (0, 1):
    idir = run / f"iter{it}"
    (idir / "scripts").mkdir(parents=True, exist_ok=True)
    (idir / "intermediate").mkdir(parents=True, exist_ok=True)
    (idir / "storyboard").mkdir(parents=True, exist_ok=True)
    for k in (1, 2, 3):
        (idir / "scripts" / f"script_{k}.json").write_text(json.dumps(script), encoding="utf-8")
    (idir / "bestScript.json").write_text(json.dumps(script, indent=2), encoding="utf-8")
    (idir / "intermediate" / "feedback.json").write_text("{}", encoding="utf-8")
    (idir / "intermediate" / "character.json").write_text('{"overall_winner": {"script": "Script 1"}}', encoding="utf-8")
    for c in range(1, 7):
        (idir / "intermediate" / f"clip_{c}.json").write_text('{"Highest Script": "Script 1"}', encoding="utf-8")
    for k in range(3):
        sb.save(idir / "storyboard" / f"storyboard_{k}.png")
    sb.save(idir / "bestStoryboard.png")
    (idir / "intermediate" / "bestStoryboard_meta.json").write_text(json.dumps({"branch": "iter0_clean", "winner_idx": 0, "mode_for_next_iter": "one"}), encoding="utf-8")
    (idir / "intermediate" / "storyboard_feedback.json").write_text('{"feedbacks": [{}]}', encoding="utf-8")

# A finished creative stage for the video dry runs (9 balanced shots).
cdir = run / "iter1" / "creative"
cdir.mkdir(parents=True, exist_ok=True)
shots = []
kinds = [("faithful", 1), ("insert", 1), ("reangle", 2), ("reangle", 3), ("insert", 3), ("faithful", 4), ("insert", 4), ("reangle", 5), ("faithful", 6)]
for i, (kind, src) in enumerate(kinds, start=1):
    base = script["clips"][src - 1]
    shots.append({
        "id": i, "source_clip": src, "shot_kind": kind, "placement": "replaces" if kind != "insert" else "after",
        "camera": {"angle": "low angle" if kind == "reangle" else "eye level", "framing": "close-up" if kind == "insert" else "medium",
                   "movement": "crash zoom in" if kind == "reangle" else "static hold", "lens": "wide"},
        "motion": "speed lines from the fist, hair follow-through" if src == 3 else "subtle",
        "sakuga_tier": 3 if src == 3 else 1, "duration_hint": 1.5 if kind == "insert" else 3.0,
        "environment": base["environment"],
        "description": base["description"] if kind != "insert" else "Close-up. Character[2] on the right side of the screen, eyes widening. Silent.",
        "characters_in_panel": base["characters_in_panel"], "rationale": "fixture",
    })
expanded = {"creativity": "balanced", "source_clip_count": 6, "characters": script["characters"], "clips": shots}
(cdir / "expandedScript.json").write_text(json.dumps(expanded, indent=2), encoding="utf-8")
(cdir / "creative_config.json").write_text(json.dumps({"level": "balanced", "shot_count": 9, "grid": "3x3"}), encoding="utf-8")
csb = Image.new("RGB", (1536, 1536), "white")
d = ImageDraw.Draw(csb)
for i in range(9):
    x, y = (i % 3) * 512, (i // 3) * 512
    d.rectangle([x + 8, y + 8, x + 504, y + 480], outline="black", width=5)
    d.text((x + 240, y + 486), str(i + 1), fill="black")
csb.save(cdir / "bestStoryboard.png")
print("fixture ok:", target, run)
