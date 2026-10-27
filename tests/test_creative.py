"""Unit tests for the Creative Expansion layer that need no model and no network.

    .venv/Scripts/python.exe -m unittest tests/test_creative.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The stage modules resolve their run directory at import time from these.
_TMP = tempfile.mkdtemp(prefix="sakuga-test-")
os.environ["TARGET"] = "_unit"
os.environ["TARGET_ROOT"] = str(Path(_TMP) / "target")
os.environ["RESULTS_ROOT"] = str(Path(_TMP) / "results")
os.environ["RUN_ID"] = "20260101_000000"
os.environ["GEMINI_API_KEY"] = "unit-test-placeholder"
os.environ["DRY_RUN"] = "1"

from src import creative_config, creative_expansion, video_models  # noqa: E402
import src.create_video as cv  # noqa: E402

FAITHFUL = {
    "characters": [{"id": 1, "description": "boy; black hair"}, {"id": 2, "description": "girl; red hair"}],
    "clips": [
        {"id": i, "environment": f"env {i}", "description": f"Medium shot. Character[1] faces screen right. Dialogue: 'line {i}'",
         "characters_in_panel": [1]} for i in range(1, 7)
    ],
}


def expanded_from_creator(n, with_bad=False):
    clips = []
    for i in range(1, n + 1):
        src = min(6, (i + 1) // 2 if n > 6 else i)
        clips.append({
            "id": 99 if with_bad else i,
            "source_clip": src,
            "shot_kind": "insert" if (n > 6 and i % 2 == 0) else "reangle",
            "placement": "after" if (n > 6 and i % 2 == 0) else "replaces",
            "camera": {"angle": "low angle", "framing": "close-up", "movement": "crash zoom in", "lens": "wide"},
            "motion": "speed lines",
            "sakuga_tier": 2,
            "duration_hint": 2.0,
            "environment": "env",
            "description": f"Low angle. Character[1] on the left side of the screen. Dialogue: 'line {src}'",
            "characters_in_panel": [1, 7] if with_bad else [1],
        })
    return {"characters": [{"id": 1, "description": "WRONG"}], "clips": clips}


class NormalizeTests(unittest.TestCase):
    def test_characters_are_forced_to_faithful_and_ids_renumbered(self):
        settings = creative_config.load_settings(level="balanced", enabled=True, num_clips=6)
        out, problems = creative_expansion.normalize_expanded(expanded_from_creator(9, with_bad=True), FAITHFUL, settings)
        self.assertEqual(out["characters"], FAITHFUL["characters"])
        self.assertEqual([c["id"] for c in out["clips"]], list(range(1, 10)))
        self.assertTrue(all(7 not in c["characters_in_panel"] for c in out["clips"]))
        self.assertEqual(len(out["clips"]), settings.shot_count)

    def test_coverage_gap_is_reported(self):
        settings = creative_config.load_settings(level="conservative", enabled=True, num_clips=6)
        bad = expanded_from_creator(6)
        bad["clips"][2]["shot_kind"] = "insert"  # clip 3 loses its only faithful/reangle shot
        _, problems = creative_expansion.normalize_expanded(bad, FAITHFUL, settings)
        self.assertTrue(any("coverage gap" in p for p in problems))

    def test_drop_then_pad_keeps_grid_regular(self):
        settings = creative_config.load_settings(level="balanced", enabled=True, num_clips=6)
        out, _ = creative_expansion.normalize_expanded(expanded_from_creator(9), FAITHFUL, settings)
        review = {"shots": [{"id": 2, "verdict": "drop"}, {"id": 4, "verdict": "keep"}]}
        out, dropped = creative_expansion.apply_drops(out, review)
        self.assertEqual(dropped, [2])
        self.assertEqual(len(out["clips"]), 8)
        out, padded = creative_expansion.pad_to_grid(out, 3)
        self.assertEqual(padded, 1)
        self.assertEqual(len(out["clips"]), 9)
        self.assertEqual(out["clips"][-1]["shot_kind"], "hold")
        self.assertNotIn("Dialogue", out["clips"][-1]["description"])
        self.assertEqual([c["id"] for c in out["clips"]], list(range(1, 10)))


class ConfigTests(unittest.TestCase):
    def test_levels_and_aliases(self):
        self.assertEqual(creative_config.normalize_level("sakuga"), "aggressive")
        self.assertEqual(creative_config.normalize_level("faithful"), "conservative")
        with self.assertRaises(ValueError):
            creative_config.normalize_level("wild")
        for level, shots in (("conservative", 6), ("balanced", 9), ("aggressive", 12)):
            self.assertEqual(creative_config.shots_for_level(level, 6), shots)
        self.assertEqual(creative_config.shots_for_level("balanced", 8), 12)

    def test_grid_shape(self):
        s = creative_config.load_settings(level="aggressive", enabled=True, num_clips=6)
        self.assertEqual((s.rows, s.grid_aspect), (4, "3:4"))


class RoutingTests(unittest.TestCase):
    def _args(self, **kw):
        base = dict(route=None, creative=True, no_character_refs=True, target="_unit", target_root="t")
        base.update(kw)
        return Namespace(**base)

    def test_auto_route_by_level(self):
        seedance = video_models.get_backend("seedance")
        self.assertEqual(cv.choose_route(self._args(), seedance, 12, "aggressive"), "i2v")
        self.assertEqual(cv.choose_route(self._args(), seedance, 9, "balanced"), "ref")
        wan = video_models.get_backend("wan")
        self.assertEqual(cv.choose_route(self._args(), wan, 12, "aggressive"), "i2v")
        minimax = video_models.get_backend("minimax-api")
        self.assertEqual(cv.choose_route(self._args(), minimax, 12, "aggressive"), "ref")
        self.assertEqual(cv.choose_route(self._args(route="i2v"), minimax, 6, "balanced"), "ref")  # unsupported -> fallback
        self.assertEqual(cv.choose_route(self._args(creative=False), seedance, 6, None), "ref")

    def test_ref_duration_scales_and_clamps(self):
        seedance = video_models.get_backend("seedance")
        self.assertEqual(cv.ref_duration_for(6, seedance), 15)
        self.assertEqual(cv.ref_duration_for(9, seedance), 15)
        self.assertEqual(cv.ref_duration_for(2, seedance), 5)


class BackendTests(unittest.TestCase):
    def test_higgsfield_body_template(self):
        os.environ["HIGGSFIELD_BODY_TEMPLATE"] = '{"prompt": {prompt}, "image_url": {image_url}, "duration": {duration}, "refs": {extra_refs}}'
        try:
            b = video_models.get_backend("higgsfield")
            body = b.build_body('say "hi"', "https://x/y.png", 7, ["https://x/a.png", "https://x/b.png"])
            self.assertEqual(body["prompt"], 'say "hi"')
            self.assertEqual(body["duration"], 7)
            self.assertEqual(body["refs"], ["https://x/a.png", "https://x/b.png"])
        finally:
            del os.environ["HIGGSFIELD_BODY_TEMPLATE"]

    def test_manual_backend_queues_jobs(self):
        cv.MANUAL_JOBS.clear()
        b = video_models.get_backend("manual")
        task = b.submit("p", "img.png", 3, seed=1, extra_refs=["s.png"])
        with tempfile.TemporaryDirectory() as d:
            out = cv.collect_candidate(b, Path(d) / "candidate_1.mp4", task, Path(d) / "tasks_info.json")
        self.assertIsNone(out)
        self.assertEqual(len(cv.MANUAL_JOBS), 1)
        self.assertEqual(cv.MANUAL_JOBS[0]["extra_refs"], ["s.png"])

    def test_seedance_roles_follow_route(self):
        b = video_models.get_backend("seedance")
        self.assertTrue(b.supports("i2v") and b.supports("ref"))
        self.assertEqual(video_models.detect_backend("manual"), "manual")
        self.assertEqual(video_models.detect_backend("higgsfield"), "higgsfield")


class GridSliceTests(unittest.TestCase):
    def test_grid_slice_counts(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            sb = Path(d) / "sb.png"
            Image.new("RGB", (900, 900), "white").save(sb)
            out = cv.grid_slice(sb, Path(d) / "panels", 9)
            self.assertEqual(len(list(out.glob("panel*.png"))), 9)
            out = cv.grid_slice(sb, Path(d) / "panels12", 12)
            self.assertEqual(len(list(out.glob("panel*.png"))), 12)


if __name__ == "__main__":
    unittest.main()
