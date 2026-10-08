"""Feature 1: three camera streams over the websocket."""
import unittest

import cv2
import numpy as np

from teleop_server.hub import Hub
from teleop_server.latency import StatsRegistry
from teleop_server.sources.camera import FakeCamera, RealSenseCamera
from teleop_server.server import DEFAULT_SERIALS
from teleop_server.tests.util import HW, AppHarness, browse

NAMES = ["camera1", "camera2", "camera3"]
WARMUP = 15


def check_frames(test, frames, min_per_cam, clock):
    by_cam = {n: [h for h, _ in frames if h["sensor"] == n] for n in NAMES}
    for name, headers in by_cam.items():
        test.assertGreaterEqual(len(headers), min_per_cam, name)
        seqs = [h["seq"] for h in headers]
        test.assertEqual(seqs, sorted(seqs), f"{name} out of order")
        # The first frames after pipeline start are buffered backlog; check steady state only.
        for h in headers[WARMUP:]:
            test.assertEqual(h["capture_clock"], clock)
            chain = [h["t_recv_ns"], h["t_encoded_ns"], h["t_hub_ns"], h["t_sent_ns"], h["t_client_ns"]]
            test.assertLess(h["t_capture_ns"], h["t_recv_ns"], f"{name} capture after receipt")
            test.assertEqual(chain, sorted(chain), f"{name} timestamps not monotonic: {chain}")
            test.assertLess((h["t_recv_ns"] - h["t_capture_ns"]) / 1e6, 100, "implausible sensor latency")
    for header, jpg in frames[:: max(1, len(frames) // 10)]:
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        test.assertEqual(img.shape, (header["height"], header["width"], 3))
    return by_cam


class HubTest(unittest.TestCase):
    def test_slow_client_keeps_only_latest(self):
        import asyncio
        from teleop_server.hub import Client
        hub = Hub(StatsRegistry())
        loop = asyncio.new_event_loop()
        hub.bind(loop)
        client = Client(ws=None)
        hub.clients.add(client)
        for i in range(5):
            hub._dispatch("cam:camera1", {"sensor": "camera1", "seq": i}, b"x")
        self.assertEqual(client.slots["cam:camera1"][0]["seq"], 4)
        self.assertEqual(client.dropped["cam:camera1"], 4)
        loop.close()


class FakeCameraTest(unittest.IsolatedAsyncioTestCase):
    async def test_three_streams(self):
        factory = lambda hub, stats: [FakeCamera(n, hub, stats) for n in NAMES]
        async with AppHarness(factory) as h:
            frames, texts = await h.collect(3.0)
            stats = await h.stats()
        by_cam = check_frames(self, frames, 70, "simulated")
        self.assertTrue(any(t["type"] == "hello" for t in texts))
        for n in NAMES:
            s = stats["sensors"][n]
            self.assertAlmostEqual(s["rate_hz"], 30, delta=3, msg=n)
            self.assertIn("sensor_ms", s["stages"])
            self.assertIn("server_ms", s["stages"])
        print("fake frames per camera:", {k: len(v) for k, v in by_cam.items()})


def check_page(test, page, min_drawn):
    test.assertEqual(page["exceptions"], [])
    test.assertEqual(page["conn"], "open")
    test.assertIsNotNone(page["offset"])
    for n in NAMES:
        s = page["sensors"][n]
        test.assertGreaterEqual(s["drawn"], min_drawn, n)
        test.assertIsNotNone(s["e2e"], n)
        test.assertLess(s["e2e"]["p95"], 200, n)
        test.assertEqual(s["status"], "정상", n)
    return {n: {"drawn": page["sensors"][n]["drawn"], "e2e_p50": round(page["sensors"][n]["e2e"]["p50"], 1),
                "e2e_p95": round(page["sensors"][n]["e2e"]["p95"], 1)} for n in NAMES}


class FakeCameraBrowserTest(unittest.IsolatedAsyncioTestCase):
    async def test_page_draws_three_streams(self):
        factory = lambda hub, stats: [FakeCamera(n, hub, stats) for n in NAMES]
        async with AppHarness(factory) as h:
            page = await browse(h, 5.0, "1_cameras_fake.png")
        print("browser fake:", check_page(self, page, 90))


@unittest.skipUnless(HW, "set TELEOP_HW=1 to test real RealSense cameras")
class RealSenseTest(unittest.IsolatedAsyncioTestCase):
    async def test_three_realsense_streams(self):
        factory = lambda hub, stats: [RealSenseCamera(n, hub, stats, s) for n, s in zip(NAMES, DEFAULT_SERIALS)]
        async with AppHarness(factory) as h:
            frames, _ = await h.collect(6.0)
            stats = await h.stats()
        for src in stats["sources"]:
            self.assertEqual(src["state"], "running", src)
        # First ~1-2 s is pipeline start-up; demand >= 3 s worth at 30 fps per camera.
        by_cam = check_frames(self, frames, 90, "global")
        report = {n: {"frames": len(by_cam[n]), "rate_hz": round(stats["sensors"][n]["rate_hz"], 2),
                      "sensor_ms_p50": round(stats["sensors"][n]["stages"]["sensor_ms"]["p50"], 2),
                      "sensor_ms_p95": round(stats["sensors"][n]["stages"]["sensor_ms"]["p95"], 2),
                      "exposure_ms_p50": round(stats["sensors"][n]["stages"]["exposure_ms"]["p50"], 2),
                      "transport_ms_p95": round(stats["sensors"][n]["stages"]["transport_ms"]["p95"], 2),
                      "encode_ms_p50": round(stats["sensors"][n]["stages"]["encode_ms"]["p50"], 2),
                      "server_ms_p50": round(stats["sensors"][n]["stages"]["server_ms"]["p50"], 2)} for n in NAMES}
        print("realsense:", report)
        for n in NAMES:
            self.assertGreater(stats["sensors"][n]["rate_hz"], 25, n)

    async def test_page_draws_realsense(self):
        factory = lambda hub, stats: [RealSenseCamera(n, hub, stats, s) for n, s in zip(NAMES, DEFAULT_SERIALS)]
        async with AppHarness(factory) as h:
            page = await browse(h, 7.0, "1_cameras_realsense.png")
        print("browser realsense:", check_page(self, page, 90))


if __name__ == "__main__":
    unittest.main()
