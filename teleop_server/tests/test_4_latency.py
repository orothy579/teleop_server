"""Feature 4: per-sensor latency measurement and monitoring."""
import asyncio
import json
import time
import unittest

from teleop_server.hub import decode_binary
from teleop_server.latency import MinOffsetClock, SensorStats
from teleop_server.sources.arms import ArmSource, FakeArm
from teleop_server.sources.camera import FakeCamera
from teleop_server.sources.odom import FakeOdom
from teleop_server.tests.util import AppHarness, browse

ALL = ["camera1", "camera2", "camera3", "leader_left", "follower_left", "leader_right", "follower_right", "odom"]
KEYS = [("follower", "left"), ("follower", "right"), ("leader", "left"), ("leader", "right")]


def all_fake(hub, stats):
    return ([FakeCamera(f"camera{i}", hub, stats) for i in (1, 2, 3)]
            + [ArmSource(hub, stats, {k: FakeArm(*k) for k in KEYS}, autostart=True), FakeOdom(hub, stats)])


class StatsUnitTest(unittest.TestCase):
    def test_percentiles_rate_and_window(self):
        s = SensorStats("x", window_s=10)
        now = time.time_ns()
        for i in range(100):
            s.record({"a_ms": float(i + 1)}, now - (99 - i) * 10_000_000)  # 100 Hz for 1 s
        sm = s.summary(now)
        self.assertAlmostEqual(sm["rate_hz"], 100, delta=0.5)
        self.assertAlmostEqual(sm["stages"]["a_ms"]["p50"], 50.5, delta=0.6)
        self.assertAlmostEqual(sm["stages"]["a_ms"]["p95"], 95.05, delta=0.6)
        self.assertEqual(sm["stages"]["a_ms"]["max"], 100)
        later = s.summary(now + 20 * 10**9)  # all samples outside the window
        self.assertIsNone(later["stages"]["a_ms"])
        self.assertAlmostEqual(later["stale_ms"], 20000, delta=1)

    def test_min_offset_clock(self):
        c = MinOffsetClock()
        offset = 5_000_000_000
        delays = [3e6, 1e6, 7e6, 2e6]
        mapped = [c.to_host_ns(i * 10**7, i * 10**7 + offset + int(d)) for i, d in enumerate(delays)]
        # After the 1 ms sample, mapping is relative to the fastest observed delivery.
        self.assertEqual(mapped[-1], 3 * 10**7 + offset + 1_000_000)


class EndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_sensors_monitored_with_skewed_browser_clock(self):
        """A browser clock 500 ms off must not distort end-to-end latency (offset is estimated)."""
        async with AppHarness(all_fake) as h:
            straight = await browse(h, 5.0, "4_latency_all_fake.png")
            skewed = await browse(h, 5.0, query="?skew_ms=500")
            stats = await h.stats()
        for page in (straight, skewed):
            self.assertEqual(page["exceptions"], [])
            for n in ALL:
                self.assertEqual(page["sensors"][n]["status"], "정상", n)
        self.assertAlmostEqual(skewed["offset"], -500, delta=3)
        for n in ALL:
            # The network stage (send -> receive) is where a wrong clock offset would show directly.
            na, nb = straight["sensors"][n]["network"]["p50"], skewed["sensors"][n]["network"]["p50"]
            self.assertLess(abs(na - nb), 3, f"{n}: network {na:.1f} vs skewed {nb:.1f}")
            # End-to-end also includes waiting for the next animation frame (0-16.7 ms at 60 Hz),
            # whose phase against 20-30 Hz streams differs between runs: allow one frame.
            a, b = straight["sensors"][n]["e2e"]["p50"], skewed["sensors"][n]["e2e"]["p50"]
            self.assertLess(abs(a - b), 17, f"{n}: e2e {a:.1f} vs skewed {b:.1f}")
            self.assertGreater(b, 0, n)
            server = stats["sensors"][n]["stages"]
            self.assertIn("sensor_ms", server, n)
            self.assertIn("server_ms", server, n)
        print("e2e p50 straight/skewed:", {n: (round(straight["sensors"][n]["e2e"]["p50"], 1),
                                              round(skewed["sensors"][n]["e2e"]["p50"], 1)) for n in ALL})

    async def test_client_report_reaches_api(self):
        from teleop_server.tests.browser import Chrome
        async with AppHarness(all_fake) as h:
            async with Chrome() as chrome:
                await chrome.open(str(h.client.make_url("/")))
                await asyncio.sleep(4.0)
                stats = await h.stats()
        reports = [c["report"] for c in stats["clients"] if c["report"]]
        self.assertEqual(len(reports), 1)
        for n in ALL:
            self.assertIsNotNone(reports[0]["sensors"][n]["e2e_ms"], n)
            self.assertGreater(reports[0]["sensors"][n]["rate_hz"], 15, n)

    async def test_stalled_sensor_is_flagged(self):
        class StoppingCamera(FakeCamera):
            def run(self):
                self._set_state("running")
                end = time.monotonic() + 1.5
                while time.monotonic() < end and not self.stop_event.is_set():
                    time.sleep(1 / 30)
                    t = time.time_ns()
                    import numpy as np
                    self.emit(np.zeros((480, 640, 3), np.uint8), t - 10**7, t, "simulated")
                self.stop_event.wait()  # thread alive but silent: a hung driver

        factory = lambda hub, stats: [StoppingCamera("camera1", hub, stats), FakeCamera("camera2", hub, stats)]
        async with AppHarness(factory) as h:
            page = await browse(h, 4.5, "4_latency_stalled.png")
        self.assertEqual(page["sensors"]["camera1"]["status"], "수신 없음")
        self.assertEqual(page["sensors"]["camera2"]["status"], "정상")
        self.assertEqual(page["sensors"]["camera3"]["status"], "꺼짐")

    async def test_slow_client_latency_stays_bounded(self):
        """A client reading slower than the stream gets fresh frames, not a growing backlog."""
        async with AppHarness(lambda hub, stats: [FakeCamera(f"camera{i}", hub, stats, scale=1.0) for i in (1, 2, 3)]) as h:
            ws = await h.client.ws_connect("/ws")
            ages = []
            end = time.monotonic() + 8.0
            while time.monotonic() < end:
                msg = await ws.receive(timeout=5)
                if msg.type.name == "BINARY":
                    header, _ = decode_binary(msg.data)
                    ages.append((time.time_ns() - header["t_capture_ns"]) / 1e6)
                    await asyncio.sleep(0.1)  # ~10 msg/s consumer vs 90 msg/s producer
                    await ws.send_str(json.dumps({"type": "ack", "key": header["key"]}))
            await ws.close()
            stats = await h.stats()
        late = ages[len(ages) // 2:]
        print(f"slow client: {len(ages)} frames, age p50 {sorted(late)[len(late)//2]:.0f} ms, max {max(late):.0f} ms, "
              f"server drops {sum(stats['sensors'][f'camera{i}']['dropped'] for i in (1, 2, 3))}")
        # Bound = in-flight window x streams x consumer period (3 x 3 x 100 ms) plus slack.
        from teleop_server.hub import MAX_INFLIGHT
        self.assertLess(max(late), MAX_INFLIGHT * 3 * 100 + 300, "frame age exceeds flow-control bound")
        q = len(ages) // 4
        early, final = sum(ages[q:2 * q]) / q, sum(ages[-q:]) / q
        self.assertLess(final - early, 150, f"frame age keeps growing: {early:.0f} -> {final:.0f} ms")
        self.assertGreater(len(ages), 50)


if __name__ == "__main__":
    unittest.main()
