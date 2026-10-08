"""Feature 9: virtual base controller (browser -> Twist), with its safety behaviour.

Nothing here publishes to the real /cmd_vel: fake publishers, or a separate test topic for ROS.
"""
import asyncio
import json
import os
import time
import unittest

from teleop_server.base_control import BaseController, FakeTwistPublisher, RosTwistPublisher
from teleop_server.server import BASE
from teleop_server.sources.arms import ArmSource, FakeArm
from teleop_server.sources.odom import FakeOdom
from teleop_server.tests.browser import Chrome
from teleop_server.tests.test_8_buttons import TEXT, wait_page
from teleop_server.tests.util import SHOTS, AppHarness

KEYS = [("follower", "left"), ("follower", "right"), ("leader", "left"), ("leader", "right")]
ROS = os.environ.get("TELEOP_ROS") == "1"


def fake_app(holder):
    def sources(hub, stats):
        holder["odom"] = FakeOdom(hub, stats, driven=True)
        return [ArmSource(hub, stats, lambda: {k: FakeArm(*k) for k in KEYS}), holder["odom"]]

    def base(hub, srcs):
        holder["pub"] = FakeTwistPublisher(holder["odom"].command)
        return BaseController(hub, holder["pub"], max_linear=0.3, max_angular=0.6)
    return sources, None, base


async def next_of(ws, kind, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        msg = await ws.receive(timeout=end - time.monotonic())
        if msg.type.name == "TEXT":
            data = json.loads(msg.data)
            if data.get("type") == kind:
                return data
    return None


async def drive(ws, vx, wz, seconds, rate=20):
    for _ in range(int(seconds * rate)):
        await ws.send_str(json.dumps({"type": "base_cmd", "vx": vx, "wz": wz}))
        await asyncio.sleep(1 / rate)


class BaseControlTest(unittest.IsolatedAsyncioTestCase):
    async def test_enable_clamp_lease_watchdog_release(self):
        holder = {}
        async with AppHarness(*fake_app(holder)) as h:
            pub = holder["pub"]
            a = await h.client.ws_connect("/ws")
            b = await h.client.ws_connect("/ws")
            await a.send_str(json.dumps({"type": "base_cmd", "vx": 0.1, "wz": 0}))
            self.assertIn("꺼져", (await next_of(a, "base_reject"))["reason"])
            self.assertEqual(pub.sent, [], "nothing may be published while disabled")

            code, body = await h.post("/api/base", action="enable")
            self.assertEqual(code, 200, body)
            pos0 = holder["odom"]._cmd
            await drive(a, 5.0, -9.0, 1.0)                       # over the limits
            sent = [(vx, wz) for _, vx, wz in pub.sent]
            self.assertGreater(len(sent), 12)
            self.assertEqual(max(v for v, _ in sent), 0.3)       # clamped
            self.assertEqual(min(w for _, w in sent), -0.6)
            await b.send_str(json.dumps({"type": "base_cmd", "vx": -0.2, "wz": 0}))
            self.assertIn("다른 사용자", (await next_of(b, "base_reject"))["reason"])

            # Watchdog: A stops sending without releasing -> zeros, then silence.
            await asyncio.sleep(0.6)
            t_last_nonzero = max(t for t, vx, wz in pub.sent if vx or wz)
            zeros_after = [(t, vx, wz) for t, vx, wz in pub.sent if t > t_last_nonzero]
            self.assertTrue(zeros_after and all(vx == wz == 0 for _, vx, wz in zeros_after), zeros_after)
            self.assertLess(zeros_after[0][0] - t_last_nonzero, 0.35, "watchdog too slow")
            n = len(pub.sent)
            await asyncio.sleep(0.5)
            self.assertEqual(len(pub.sent), n, "must not publish while idle")

            # Explicit release (dead-man) -> immediate zero; then B may take over.
            await drive(a, 0.2, 0.0, 0.3)
            t0 = time.monotonic()
            await a.send_str(json.dumps({"type": "base_release"}))
            await asyncio.sleep(0.1)
            self.assertTrue(any(t >= t0 and vx == wz == 0 for t, vx, wz in pub.sent), "no zero on release")
            await drive(b, -0.2, 0.0, 0.3)
            self.assertEqual(pub.sent[-1][1], -0.2)

            # Owner disconnects while driving -> zero at once.
            t0 = time.monotonic()
            await b.close()
            await asyncio.sleep(0.1)
            self.assertTrue(any(t >= t0 and vx == wz == 0 for t, vx, wz in pub.sent), "no zero on disconnect")

            # Arm control stop disables the base too.
            await h.wait_for(lambda s: True, 0)
            await h.post("/api/control", action="start")
            await h.wait_for(lambda s: next(x for x in s["sources"] if x["name"] == "arms")["state"] == "running", 5)
            await h.post("/api/control", action="stop")
            await asyncio.sleep(0.2)
            self.assertFalse(h.app[BASE].status()["enabled"])
            await a.send_str(json.dumps({"type": "base_cmd", "vx": 0.1, "wz": 0}))
            self.assertIn("꺼져", (await next_of(a, "base_reject"))["reason"])
            await a.close()

    async def test_driven_fake_odometry_moves(self):
        holder = {}
        async with AppHarness(*fake_app(holder)) as h:
            ws = await h.client.ws_connect("/ws")
            await h.post("/api/base", action="enable")
            _, (_, texts) = await asyncio.gather(drive(ws, 0.3, 0.0, 2.0), h.collect(2.4))
            odom = [t for t in texts if t.get("type") == "odom"]
            await ws.close()
        moved = odom[-1]["p"][0] - odom[0]["p"][0]
        self.assertAlmostEqual(moved, 0.3 * 2.0, delta=0.15)


@unittest.skipUnless(ROS, "set TELEOP_ROS=1 (ROS sourced) to test the ROS publisher on a test topic")
class RosPublisherTest(unittest.TestCase):
    def test_twist_on_test_topic(self):
        import rclpy
        from geometry_msgs.msg import Twist
        topic = "/teleop_server_test/cmd_vel"  # never the real /cmd_vel: the driver would move the base
        ctx = rclpy.Context()
        rclpy.init(context=ctx)
        node = rclpy.create_node("teleop_server_test_sub", context=ctx)
        got = []
        node.create_subscription(Twist, topic, lambda m: got.append((time.monotonic(), m.linear.x, m.angular.z)), 10)
        from rclpy.executors import SingleThreadedExecutor
        executor = SingleThreadedExecutor(context=ctx)
        executor.add_node(node)

        class Hub:
            def publish(self, *a, **k):
                pass

        ctl = BaseController(Hub(), RosTwistPublisher(topic), max_linear=0.3, max_angular=0.6)
        ctl.start()
        try:
            end = time.monotonic() + 1.0
            while time.monotonic() < end:  # discovery
                executor.spin_once(timeout_sec=0.05)
            ctl.enable()
            t0 = time.monotonic()
            while time.monotonic() - t0 < 1.0:
                ctl.command("t", 0.25, 0.5)
                executor.spin_once(timeout_sec=0.02)
            ctl.release("t")
            end = time.monotonic() + 0.6
            while time.monotonic() < end:
                executor.spin_once(timeout_sec=0.05)
        finally:
            ctl.stop()
            executor.shutdown()
            node.destroy_node()
            rclpy.shutdown(context=ctx)
        moving = [g for g in got if g[1] or g[2]]
        rate = (len(moving) - 1) / (moving[-1][0] - moving[0][0])
        print(f"received {len(moving)} commands at {rate:.1f} Hz, last {got[-1]}")
        self.assertTrue(all(abs(vx - 0.25) < 1e-6 and abs(wz - 0.5) < 1e-6 for _, vx, wz in moving))
        self.assertAlmostEqual(rate, 20, delta=3)
        self.assertEqual(got[-1][1:], (0.0, 0.0), "must end with zero velocity")


class BaseBrowserTest(unittest.IsolatedAsyncioTestCase):
    async def test_robot_control_drives_floating_joystick(self):
        """One "로봇 제어" button runs arms and base; the joystick is a floating, see-through window."""
        holder = {}
        async with AppHarness(*fake_app(holder)) as h:
            pub = holder["pub"]
            async with Chrome(1440, 1500) as c:
                await c.open(str(h.client.make_url("/")))
                await wait_page(c, "document.querySelector('#conn').dataset.state === 'open'")
                self.assertIsNone(await c.eval("document.querySelector('#btn-base')"), "no separate base button")
                self.assertEqual(await c.eval(TEXT.format(sel="#base-state span")), "꺼짐")
                self.assertTrue(await c.eval("document.querySelector('#joy-pad').classList.contains('disabled')"))
                # Floating: fixed position, inside the viewport (bottom-right by default), above the page.
                box = await c.eval("(() => { const b = document.querySelector('#joy-float'), r = b.getBoundingClientRect();"
                                   " return [getComputedStyle(b).position, r.right, r.bottom, innerWidth, innerHeight]; })()")
                self.assertEqual(box[0], "fixed")
                self.assertTrue(box[3] - 40 < box[1] <= box[3] and box[4] - 40 < box[2] <= box[4], box)

                c.accept_dialogs = False
                await c.eval("document.querySelector('#btn-control').click()")
                await asyncio.sleep(0.5)
                self.assertFalse(h.app[BASE].status()["enabled"], "cancelled confirm must not enable")
                self.assertIn("모바일 베이스", c.dialogs[-1])
                c.accept_dialogs = True
                await c.eval("document.querySelector('#btn-control').click()")
                self.assertTrue(await wait_page(c, TEXT.format(sel="#base-state span") + " === '켜짐'"), "base on with control")
                self.assertTrue(h.app[BASE].status()["enabled"])

                # Drag the joystick knob straight up (forward) and hold for 1 s.
                box = await c.eval("(() => { const r = document.querySelector('#joy-pad').getBoundingClientRect();"
                                   " return [r.left + r.width / 2, r.top + r.height / 2, r.width / 2]; })()")
                cx, cy, rad = box
                await c.send("Input.dispatchMouseEvent", type="mousePressed", x=cx, y=cy, button="left", clickCount=1)
                await c.send("Input.dispatchMouseEvent", type="mouseMoved", x=cx, y=cy - rad, button="left")
                await asyncio.sleep(1.0)
                self.assertEqual(await c.eval(TEXT.format(sel="#base-state span")), "조작 중")
                await c.screenshot(os.path.join(SHOTS, "11_base_joystick.png"), full=False)
                await c.send("Input.dispatchMouseEvent", type="mouseReleased", x=cx, y=cy - rad, button="left", clickCount=1)
                await asyncio.sleep(0.3)
                drag = [(vx, wz) for _, vx, wz in pub.sent if vx or wz]
                self.assertGreater(len(drag), 12)
                self.assertAlmostEqual(drag[-1][0], 0.15, delta=0.01)  # forward at 50 % of 0.3 m/s
                self.assertAlmostEqual(drag[-1][1], 0.0, delta=0.01)
                self.assertEqual(pub.sent[-1][1:], (0.0, 0.0), "release must stop the base")

                # Keyboard: hold 'a' (turn left), release.
                n = len(pub.sent)
                await c.send("Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA", windowsVirtualKeyCode=65)
                await asyncio.sleep(0.6)
                await c.send("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA", windowsVirtualKeyCode=65)
                await asyncio.sleep(0.3)
                keys = [(vx, wz) for _, vx, wz in pub.sent[n:] if vx or wz]
                self.assertGreater(len(keys), 6)
                self.assertAlmostEqual(keys[-1][1], 0.3, delta=0.01)  # +wz = turn left at 50 % of 0.6
                self.assertEqual(pub.sent[-1][1:], (0.0, 0.0))

                # Typing in a text field must not drive the base.
                n = len(pub.sent)
                await c.eval("(() => { const i = document.createElement('input'); i.id = 'typing-probe';"
                             " document.body.appendChild(i); i.focus(); })()")
                self.assertEqual(await c.eval("document.activeElement.id"), "typing-probe")
                await c.send("Input.dispatchKeyEvent", type="keyDown", key="w", code="KeyW", windowsVirtualKeyCode=87)
                await asyncio.sleep(0.4)
                await c.send("Input.dispatchKeyEvent", type="keyUp", key="w", code="KeyW", windowsVirtualKeyCode=87)
                self.assertFalse([1 for _, vx, wz in pub.sent[n:] if vx or wz], "typing moved the base")
                await c.eval("document.querySelector('#typing-probe').blur()")

                # Transparency, moving the window by its header, folding; all kept after a reload.
                await c.eval("(() => { const a = document.querySelector('#joy-alpha'); a.value = '60';"
                             " a.dispatchEvent(new Event('input', {bubbles: true})); })()")
                self.assertAlmostEqual(float(await c.eval("getComputedStyle(document.querySelector('#joy-float')).opacity")),
                                       0.4, delta=0.01)
                hx, hy = await c.eval("(() => { const r = document.querySelector('#joy-drag .ctl-label').getBoundingClientRect();"
                                      " return [r.left + 5, r.top + r.height / 2]; })()")
                await c.send("Input.dispatchMouseEvent", type="mousePressed", x=hx, y=hy, button="left", clickCount=1)
                for step in range(1, 11):
                    await c.send("Input.dispatchMouseEvent", type="mouseMoved", x=hx - 100*step, y=hy - 60*step,
                                 button="left")
                await c.send("Input.dispatchMouseEvent", type="mouseReleased", x=hx - 1000, y=hy - 600, button="left",
                             clickCount=1)
                moved = await c.eval("(() => { const r = document.querySelector('#joy-float').getBoundingClientRect();"
                                     " return [r.left, r.top]; })()")
                self.assertLess(moved[0], 400)
                self.assertLess(moved[1], 700)
                await c.eval("document.querySelector('#joy-min').click()")
                self.assertTrue(await c.eval("document.querySelector('#joy-body').hidden"))
                n = len(pub.sent)
                await c.send("Input.dispatchKeyEvent", type="keyDown", key="w", code="KeyW", windowsVirtualKeyCode=87)
                await asyncio.sleep(0.3)
                await c.send("Input.dispatchKeyEvent", type="keyUp", key="w", code="KeyW", windowsVirtualKeyCode=87)
                self.assertFalse([1 for _, vx, wz in pub.sent[n:] if vx or wz], "a folded controller must not drive")
                await c.open(str(h.client.make_url("/")))
                await wait_page(c, "document.querySelector('#conn').dataset.state === 'open'")
                kept = await c.eval("(() => { const b = document.querySelector('#joy-float'), r = b.getBoundingClientRect();"
                                    " return [r.left, r.top, getComputedStyle(b).opacity,"
                                    " document.querySelector('#joy-body').hidden]; })()")
                self.assertAlmostEqual(kept[0], moved[0], delta=2)
                self.assertAlmostEqual(kept[1], moved[1], delta=2)
                self.assertEqual((round(float(kept[2]), 2), kept[3]), (0.4, True))
                await c.eval("document.querySelector('#joy-min').click()")

                # Stopping robot control turns the base off as well.
                await c.eval("document.querySelector('#btn-control').click()")
                self.assertTrue(await wait_page(c, TEXT.format(sel="#base-state span") + " === '꺼짐'", 30))
                self.assertFalse(h.app[BASE].status()["enabled"])
                self.assertEqual(pub.sent[-1][1:], (0.0, 0.0))
                self.assertEqual(c.exceptions, [])
        print(f"joystick commands {len(drag)}, keyboard commands {len(keys)}, last {pub.sent[-1]}")


if __name__ == "__main__":
    unittest.main()
