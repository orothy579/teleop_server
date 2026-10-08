"""Feature 3: mobile base odometry streaming."""
import math
import os
import unittest

from teleop_server.sources.odom import FakeOdom, RosOdom
from teleop_server.tests.util import AppHarness, browse

ROS = os.environ.get("TELEOP_ROS") == "1"


def check_odom(test, texts, stats, min_msgs, rate):
    msgs = [t for t in texts if t.get("type") == "odom"]
    test.assertGreaterEqual(len(msgs), min_msgs)
    for m in msgs:
        test.assertTrue(all(math.isfinite(x) for x in m["p"] + m["q"] + m["v"] + m["w"]))
        test.assertAlmostEqual(sum(x * x for x in m["q"]), 1.0, delta=0.1)
        chain = [m["t_recv_ns"], m["t_hub_ns"], m["t_sent_ns"], m["t_client_ns"]]
        test.assertEqual(chain, sorted(chain))
        test.assertGreaterEqual(m["t_recv_ns"], m["t_source_ns"], "odom stamp is in the future")
        test.assertLess((m["t_recv_ns"] - m["t_source_ns"]) / 1e6, 250, "odom stamp too old")
    test.assertEqual(len({(m["frame_id"], m["child_frame_id"]) for m in msgs}), 1, "frame ids changed")
    s = stats["sensors"]["odom"]
    test.assertAlmostEqual(s["rate_hz"], rate, delta=rate * 0.15)
    return msgs, s


class FakeOdomTest(unittest.IsolatedAsyncioTestCase):
    async def test_stream(self):
        async with AppHarness(lambda hub, stats: [FakeOdom(hub, stats)]) as h:
            _, texts = await h.collect(3.0)
            stats = await h.stats()
        msgs, s = check_odom(self, texts, stats, 50, 20)
        self.assertGreater(abs(msgs[-1]["p"][1] - msgs[0]["p"][1]), 0.1, "fake base should move")

    async def test_browser(self):
        async with AppHarness(lambda hub, stats: [FakeOdom(hub, stats)]) as h:
            page = await browse(h, 4.0, "3_odom_fake.png")
        self.assertEqual(page["exceptions"], [])
        s = page["sensors"]["odom"]
        self.assertEqual(s["status"], "정상")
        self.assertGreater(s["drawn"], 50)
        print("browser odom fake:", round(s["hz"], 1), round(s["e2e"]["p50"], 1))


@unittest.skipUnless(ROS, "set TELEOP_ROS=1 (ROS sourced, SLATE driver running) to test the real /odom")
class RosOdomTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_odom(self):
        async with AppHarness(lambda hub, stats: [RosOdom(hub, stats, "/odom")]) as h:
            page = await browse(h, 6.0, "3_odom_ros.png")
            stats = await h.stats()
            _, texts = await h.collect(3.0)
        self.assertEqual(stats["sources"][0]["state"], "running", stats["sources"])
        msgs, s = check_odom(self, texts, stats, 40, 20)
        self.assertEqual(page["sensors"]["odom"]["status"], "정상")
        m = msgs[-1]
        print("ros odom:", {"rate_hz": round(s["rate_hz"], 2), "sensor_ms": {k: round(v, 2) for k, v in s["stages"]["sensor_ms"].items()},
                            "frames": (m["frame_id"], m["child_frame_id"]), "p": [round(x, 3) for x in m["p"]],
                            "browser_e2e_p50": round(page["sensors"]["odom"]["e2e"]["p50"], 2)})


if __name__ == "__main__":
    unittest.main()
