"""Feature 7: depth recorded with every RGB frame, paired per frame and per pixel."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from teleop_server.recorder import FakeBase, Recorder
from teleop_server.sources.arms import ArmSource, FakeArm
from teleop_server.sources.camera import FakeCamera, RealSenseCamera
from teleop_server.sources.odom import FakeOdom
from teleop_server.hub import decode_binary
from teleop_server.server import DEFAULT_SERIALS
from teleop_server.tests.test_7_record import start_control, wait_saved
from teleop_server.tests.util import HW, AppHarness

KEYS = [("follower", "left"), ("follower", "right"), ("leader", "left"), ("leader", "right")]
CAMS = ["camera1", "camera2", "camera3"]


def bar_columns(rgb, depth):
    """Columns of the moving white bar in the RGB image and of the 0.4 m bar in depth."""
    white = np.where(rgb[:, 200:300].min(axis=0).mean(axis=0) > 0.9)[0]
    near = np.where(np.abs(depth[200:300] - 0.4).mean(axis=0) < 0.002)[0]
    return white, near


def depth_factories(out, mismatch_every=0):
    def sources(hub, stats):
        return ([FakeCamera(c, hub, stats, depth=True, mismatch_every=mismatch_every) for c in CAMS]
                + [ArmSource(hub, stats, lambda: {k: FakeArm(*k) for k in KEYS}), FakeOdom(hub, stats)])

    def recorder(hub, srcs):
        return Recorder(hub, next(s for s in srcs if s.kind == "arms"), [s for s in srcs if s.kind == "camera"],
                        FakeBase, fps=30, synthetic=True, output_dir=out)
    return sources, recorder


class DepthRecordTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="teleop_depth_"))

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    async def test_depth_recorded_and_paired(self):
        async with AppHarness(*depth_factories(self.out)) as h:
            await start_control(self, h)
            code, body = await h.post("/api/record", action="start")
            self.assertEqual(code, 200, body)
            await h.collect(2.5)
            await h.post("/api/record", action="stop")
            result, _ = await wait_saved(h)
        self.assertEqual(result["status"], "completed", result)
        v = result["validation"]
        self.assertTrue(v["passed"], v)
        root = Path(result["path"])
        n = result["frames"]
        for cam in CAMS:
            self.assertEqual(v["depth_frames"][f"observation.images.{cam}_depth"], n)
        self.assertEqual(v["max_rgb_depth_dt_ms"], [0.3, 0.3, 0.3])
        manifest = json.loads((root / "session.json").read_text())
        self.assertTrue(manifest["depth"]["enabled"])
        self.assertEqual(manifest["depth"]["aligned_to"], "color")

        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        ds = LeRobotDataset("local/mobile_paper_bag", root=root / "dataset", video_backend="pyav", depth_output_unit="m")
        checked = []
        for i in (0, n // 2, n - 1):
            item = ds[i]
            for cam in CAMS:
                rgb = item[f"observation.images.{cam}"].numpy()          # (3, H, W) in [0, 1]
                depth = item[f"observation.images.{cam}_depth"].numpy()[0]  # (H, W) metres
                # Pixel pairing: the red patch is the 0.25 m object in depth, at the same pixels.
                self.assertGreater(rgb[0, 390:450, 30:90].mean(), 0.7)
                self.assertAlmostEqual(float(np.median(depth[390:450, 30:90])), 0.25, delta=0.001)
                # Frame pairing: the white bar moves 7 px per frame; depth must show it at the same
                # columns in the same frame index (a one-frame offset would miss by 7 px).
                white, near = bar_columns(rgb, depth)
                self.assertTrue(len(white) >= 6 and len(near) >= 6, (i, cam, white, near))
                self.assertLessEqual(abs(float(np.median(white)) - float(np.median(near))), 1.5, (i, cam, white, near))
                self.assertAlmostEqual(float(depth[:16, :16].max()), 0.05, places=3)  # no measurement
                checked.append((i, cam, int(np.median(white))))
        print(f"{n} frames; validation {v}; bar columns checked {checked[:3]}...")

    async def test_mismatched_framesets_are_never_recorded(self):
        """Every 15th frameset carries the previous exposure's depth; none may be stored as a pair."""
        async with AppHarness(*depth_factories(self.out, mismatch_every=15)) as h:
            await start_control(self, h)
            code, _ = await h.post("/api/record", action="start")
            self.assertEqual(code, 200)
            await h.collect(2.0)
            await h.post("/api/record", action="stop")
            result, _ = await wait_saved(h)
        self.assertTrue(result["validation"]["passed"], result)
        self.assertEqual(result["validation"]["max_rgb_depth_dt_ms"], [0.3, 0.3, 0.3])
        manifest = json.loads((Path(result["path"]) / "session.json").read_text())
        self.assertGreater(manifest["cameras"]["camera1"]["unpaired_framesets"], 2)
        self.assertGreaterEqual(min(result["camera_quality"]["unpaired_framesets_skipped"]), 3)
        self.assertGreater(min(result["camera_quality"]["camera_repeated_frames"]), 0)  # previous pair reused

        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        root = Path(result["path"]) / "dataset"
        ds = LeRobotDataset("local/mobile_paper_bag", root=root, video_backend="pyav", depth_output_unit="m")
        for i in range(result["frames"]):  # every stored frame, not a sample
            item = ds[i]
            white, near = bar_columns(item["observation.images.camera1"].numpy(),
                                      item["observation.images.camera1_depth"].numpy()[0])
            self.assertLessEqual(abs(float(np.median(white)) - float(np.median(near))), 1.5, (i, white, near))
        print(f"{result['frames']} stored frames all paired; quality {result['camera_quality']}")

    async def test_recording_refused_when_pairing_is_poor(self):
        async with AppHarness(*depth_factories(self.out, mismatch_every=2)) as h:
            await start_control(self, h)
            await h.collect(1.5)  # fill the pairing window
            stats = await h.stats()
            self.assertGreater(next(s for s in stats["sources"] if s["name"] == "camera1")["info"]["unpaired_recent_pct"], 40)
            code, body = await h.post("/api/record", action="start")
        self.assertEqual(code, 409, body)
        self.assertIn("짝 불일치", body["error"])

    async def test_depth_preview_stream(self):
        async with AppHarness(*depth_factories(self.out)) as h:
            ws = await h.client.ws_connect("/ws")
            previews = {}
            for _ in range(400):
                msg = await ws.receive(timeout=5)
                if msg.type.name != "BINARY":
                    continue
                header, payload = decode_binary(msg.data)
                if header.get("type") == "depth_preview":
                    img = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_GRAYSCALE)
                    previews.setdefault(header["camera"], []).append(img.shape)
                elif "key" in header:
                    await ws.send_str(json.dumps({"type": "ack", "key": header["key"]}))
                if all(len(previews.get(c, [])) >= 3 for c in CAMS):
                    break
            await ws.close()
        for cam in CAMS:
            self.assertGreaterEqual(len(previews.get(cam, [])), 3, previews.keys())
            self.assertEqual(previews[cam][0], (240, 320))


@unittest.skipUnless(HW, "set TELEOP_HW=1 to test real RealSense depth")
class RealDepthTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_depth_pairs(self):
        cams = {}

        def factory(hub, stats):
            for c, serial in zip(CAMS, DEFAULT_SERIALS):
                cams[c] = RealSenseCamera(c, hub, stats, serial, depth=True)
            return list(cams.values())

        async with AppHarness(factory) as h:
            await h.wait_for(lambda s: all(x["state"] == "running" for x in s["sources"]), 20)
            frames, _ = await h.collect(3.0)
            stats = await h.stats()
            latest = {c: cams[c].latest_frame() for c in CAMS}
        report = {}
        for c in CAMS:
            dts = [hdr["depth_dt_ms"] for hdr, _ in frames if hdr["sensor"] == c][15:]
            f = latest[c]
            self.assertEqual(f.depth.shape, f.rgb.shape[:2])
            self.assertEqual(f.depth.dtype, np.float32)
            valid = f.depth[f.depth > 0]
            report[c] = {"rate_hz": round(stats["sensors"][c]["rate_hz"], 1), "max_abs_dt_ms": round(max(abs(x) for x in dts), 3),
                         "valid_fraction": round(float((f.depth > 0).mean()), 3),
                         "median_depth_m": round(float(np.median(valid)), 3),
                         "depth_scale_m": next(s for s in stats["sources"] if s["name"] == c)["info"]["depth_scale_m"]}
            self.assertGreater(stats["sensors"][c]["rate_hz"], 25, c)
            self.assertLess(report[c]["max_abs_dt_ms"], 1.0, report[c])
            self.assertGreater(report[c]["valid_fraction"], 0.2, report[c])
            self.assertTrue(0.07 < report[c]["median_depth_m"] < 3.0, report[c])
        print("real depth:", report)


if __name__ == "__main__":
    unittest.main()
