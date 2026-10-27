"""One command from manga pages to video.

    python src/full_pipeline.py -t <case> --creative -m seedance

runs, in order, the three scripts the README documents separately:

    1. src/croppanels.py        pages -> target/<case>/manga_panels/panel*.png
    2. src/run_all.py           transcript, character descriptions (+ model sheets),
                                the faithful refinement rounds, and with --creative
                                the shot expansion + creative storyboard
    3. src/create_video.py      storyboard -> video (faithful or --creative)

Each step is a subprocess with the same interpreter, so the heavy MAGI stack is
only loaded by the steps that need it, exactly as before. The driver keeps a
``pipeline_status.json`` next to the run's ``log.txt`` so ``--resume`` continues
from the first unfinished step, and every sub-command inherits ``--dry-run``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from src import creative_config
from src.log import error, info, init_log_file, rule, section, success, warn
from src.runs import latest_run_dir, new_run_id

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def parse_args():
    ap = argparse.ArgumentParser(
        description="Manga pages -> animation, end to end. Wraps croppanels.py, run_all.py and create_video.py.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python src/full_pipeline.py -t goblin                      # faithful, Seedance\n"
            "  python src/full_pipeline.py -t goblin --creative           # + balanced creative layer\n"
            "  python src/full_pipeline.py -t goblin --creative --creativity sakuga -m seedance --route i2v\n"
            "  python src/full_pipeline.py -t goblin --creative --dry-run # prompts only, no credentials\n"
            "  python src/full_pipeline.py -t goblin --creative --resume  # continue the newest run\n"
        ),
    )
    ap.add_argument("-t", "--target", required=True, help="Case folder under target/")
    ap.add_argument("--target-root", default="target")
    ap.add_argument("--results-root", default="results")
    ap.add_argument("-m", "--model", default=None,
                    help="Video backend for create_video.py (default: VIDEO_GENERATION_MODEL)")
    ap.add_argument("--creative", action="store_true", help="Run the Creative Expansion layer")
    ap.add_argument("--creativity", default=None,
                    help="conservative | balanced | aggressive (aliases: faithful, sakuga)")
    ap.add_argument("--route", choices=["auto", "ref", "i2v"], default=None)
    ap.add_argument("--crop-mode", choices=["magi", "grid"], default=None,
                    help="How create_video.py cuts the storyboard into panels (default: magi)")
    ap.add_argument("--character-sheets", choices=["auto", "true", "false"], default=None)
    ap.add_argument("-n", "--num-iterations", type=int, default=None, help="Faithful refinement rounds")
    ap.add_argument("--storyboard-backend", choices=["gemini", "flux2"], default=None)
    ap.add_argument("--time", default=None, help="Run folder timestamp to write into / resume")
    ap.add_argument("--resume", action="store_true",
                    help="Continue the newest run: skip finished steps and pass --auto-resume down")
    ap.add_argument("--skip-panels", action="store_true", help="Reuse existing manga_panels/")
    ap.add_argument("--skip-video", action="store_true", help="Stop after the storyboard stages")
    ap.add_argument("--dry-run", action="store_true",
                    help="No model calls: list the plan, write the creative prompts, show the video requests")
    return ap.parse_args()


def run(cmd: list[str], label: str, dry_run: bool) -> int:
    info(f"$ {' '.join(cmd)}")
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=PROJECT_ROOT)
    except FileNotFoundError as e:
        error(f"{label}: {e}")
        return 1
    dt = time.time() - t0
    (success if proc.returncode == 0 else error)(f"{label} finished in {dt:.0f}s (exit {proc.returncode})")
    return proc.returncode


def main():
    args = parse_args()
    target_dir = PROJECT_ROOT / args.target_root / args.target
    if not target_dir.is_dir():
        raise SystemExit(f"target folder not found: {target_dir}")

    results_target = PROJECT_ROOT / args.results_root / args.target
    run_id = args.time
    if run_id is None and args.resume:
        latest = latest_run_dir(results_target)
        run_id = latest.name if latest else None
    if run_id is None:
        run_id = new_run_id()
    run_dir = results_target / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    init_log_file(run_dir / "full_pipeline.log")

    status_path = run_dir / "pipeline_status.json"
    status = {}
    if status_path.is_file():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            status = {}

    def mark(step: str, ok: bool, extra: dict | None = None):
        status[step] = {"ok": ok, "at": datetime.now().astimezone().isoformat(timespec="seconds"), **(extra or {})}
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")

    creative = creative_config.load_settings(level=args.creativity, enabled=True if args.creative else None)
    rule(
        f"[bold cyan]Full pipeline  ·  target=[white]{args.target}[/]  ·  run=[white]{run_id}[/]  ·  "
        f"creative=[white]{creative.level if creative.enabled else 'off'}[/]  ·  "
        f"model=[white]{args.model or os.environ.get('VIDEO_GENERATION_MODEL', '?')}[/]"
        + ("  ·  [yellow]DRY RUN[/]" if args.dry_run else "")
    )
    info(f"Run directory: {run_dir}")

    # 1. Panels ---------------------------------------------------------------
    panels_dir = target_dir / "manga_panels"
    have_panels = panels_dir.is_dir() and any(panels_dir.glob("panel*.png"))
    section("STEP 1  ·  crop manga panels (MAGI)")
    if args.skip_panels or (have_panels and (args.resume or status.get("panels", {}).get("ok"))):
        success(f"manga_panels/ already present ({len(list(panels_dir.glob('panel*.png')))} panels) - skipping")
        mark("panels", True, {"skipped": True})
    elif args.dry_run:
        warn("--dry-run: would run croppanels.py" + (" (panels already exist)" if have_panels else ""))
    else:
        rc = run([PY, "src/croppanels.py", "-t", args.target], "croppanels", args.dry_run)
        mark("panels", rc == 0)
        if rc != 0:
            raise SystemExit(rc)

    # 2. Script / storyboard / creative ---------------------------------------
    section("STEP 2  ·  script, storyboard" + (", creative expansion" if creative.enabled else ""))
    cmd = [PY, "src/run_all.py", "-t", args.target, "--target-root", args.target_root,
           "--results-root", args.results_root, "--time", run_id]
    if args.resume:
        cmd.append("--auto-resume")
    if args.num_iterations is not None:
        cmd += ["-n", str(args.num_iterations)]
    if args.storyboard_backend:
        cmd += ["--storyboard-backend", args.storyboard_backend]
    if creative.enabled:
        cmd += ["--creative", "--creativity", creative.level]
    if args.character_sheets:
        cmd += ["--character-sheets", args.character_sheets]
    if args.dry_run:
        cmd.append("--dry-run")
    rc = run(cmd, "run_all", args.dry_run)
    mark("storyboard", rc == 0, {"creative": creative.enabled, "level": creative.level})
    if rc != 0:
        raise SystemExit(rc)

    if args.skip_video:
        success("--skip-video: stopping after the storyboard stages")
        return

    # 3. Video ---------------------------------------------------------------
    section("STEP 3  ·  video")
    cmd = [PY, "src/create_video.py", "-t", args.target, "--target-root", args.target_root,
           "--results-root", args.results_root, "--time", run_id]
    if args.model:
        cmd += ["-m", args.model]
    if creative.enabled:
        cmd.append("--creative")
    if args.route:
        cmd += ["--route", args.route]
    if args.crop_mode:
        cmd += ["--crop-mode", args.crop_mode]
    if args.resume:
        cmd.append("--auto-resume")
    if args.dry_run:
        cmd.append("--dry-run")
    rc = run(cmd, "create_video", args.dry_run)
    mark("video", rc == 0, {"model": args.model, "creative": creative.enabled})
    if rc != 0:
        raise SystemExit(rc)

    section("Done")
    success(f"Results: {run_dir}  ·  videos under {results_target / 'video'}")


if __name__ == "__main__":
    main()
