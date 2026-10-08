"""Feature 10: camera tiles can be moved and resized; the layout persists per browser."""
import asyncio
import os
import unittest

from teleop_server.sources.camera import FakeCamera
from teleop_server.tests.browser import Chrome
from teleop_server.tests.test_8_buttons import wait_page
from teleop_server.tests.util import SHOTS, AppHarness

CAMS = ["camera1", "camera2", "camera3"]
RECTS = """(() => Object.fromEntries(['camera1', 'camera2', 'camera3'].map(c => {
  const r = document.querySelector(`.cam[data-cam=${c}]`).getBoundingClientRect();
  return [c, [Math.round(r.left), Math.round(r.top + scrollY), Math.round(r.width), Math.round(r.height)]]; })))()"""


def factory(hub, stats):
    return [FakeCamera(c, hub, stats, depth=True) for c in CAMS]


class LayoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_move_resize_presets_persist(self):
        async with AppHarness(factory) as h:
            async with Chrome(1440, 1400) as c:
                url = str(h.client.make_url("/"))
                await c.open(url)
                await wait_page(c, "document.querySelector('#conn').dataset.state === 'open'")
                await asyncio.sleep(0.5)
                r0 = await c.eval(RECTS)
                # Default: one row, equal widths, increasing x.
                self.assertEqual(len({r[1] for r in r0.values()}), 1, r0)
                self.assertLess(r0["camera1"][0], r0["camera2"][0])
                self.assertLess(r0["camera2"][0], r0["camera3"][0])
                self.assertLessEqual(max(r[2] for r in r0.values()) - min(r[2] for r in r0.values()), 2)

                # Edit mode: drag camera3 down-left, resize camera1 from its corner handle.
                await c.eval("document.querySelector('#lay-edit').click()")
                x3, y3 = r0["camera3"][0] + 100, r0["camera3"][1] + 100 - await c.eval("scrollY")
                await c.send("Input.dispatchMouseEvent", type="mousePressed", x=x3, y=y3, button="left", clickCount=1)
                for k in range(1, 11):
                    await c.send("Input.dispatchMouseEvent", type="mouseMoved", x=x3 - 60 * k, y=y3 + 40 * k, button="left")
                await c.send("Input.dispatchMouseEvent", type="mouseReleased", x=x3 - 600, y=y3 + 400, button="left", clickCount=1)
                hx, hy = await c.eval("(() => { const r = document.querySelector('.cam[data-cam=camera1] .rs-handle')"
                                      ".getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()")
                await c.send("Input.dispatchMouseEvent", type="mousePressed", x=hx, y=hy, button="left", clickCount=1)
                for k in range(1, 6):
                    await c.send("Input.dispatchMouseEvent", type="mouseMoved", x=hx + 40 * k, y=hy, button="left")
                await c.send("Input.dispatchMouseEvent", type="mouseReleased", x=hx + 200, y=hy, button="left", clickCount=1)
                r1 = await c.eval(RECTS)
                self.assertAlmostEqual(r1["camera3"][0], r0["camera3"][0] - 600, delta=12)
                self.assertAlmostEqual(r1["camera3"][1], r0["camera3"][1] + 400, delta=12)
                self.assertAlmostEqual(r1["camera1"][2], r0["camera1"][2] + 200, delta=12)
                self.assertAlmostEqual(r1["camera1"][3] - r0["camera1"][3], 200 * 3 / 4, delta=12)  # 4:3 kept
                stage_h = await c.eval("document.querySelector('#cams').offsetHeight")
                self.assertGreaterEqual(stage_h, r1["camera3"][1] + r1["camera3"][3] - r0["camera1"][1] - 2)

                # Keyboard: focus camera2, move right twice (1 % each), shrink once.
                await c.eval("document.querySelector('.cam[data-cam=camera2]').focus()")
                for key, code in (("ArrowRight", "ArrowRight"), ("ArrowRight", "ArrowRight"), ("-", "Minus")):
                    await c.send("Input.dispatchKeyEvent", type="keyDown", key=key, code=code)
                    await c.send("Input.dispatchKeyEvent", type="keyUp", key=key, code=code)
                r2 = await c.eval(RECTS)
                W = await c.eval("document.querySelector('#cams').clientWidth")
                self.assertAlmostEqual(r2["camera2"][0] - r1["camera2"][0], 0.02 * W, delta=W * 0.006)
                self.assertAlmostEqual(r1["camera2"][2] - r2["camera2"][2], 0.02 * W, delta=W * 0.006)
                await c.screenshot(os.path.join(SHOTS, "12_layout_custom.png"), full=False)
                drawn = await c.eval("window.__teleop.S.camera3.drawn")

                # Persisted per browser: reload keeps the layout.
                await c.open(url)
                await wait_page(c, "document.querySelector('#conn').dataset.state === 'open'")
                await asyncio.sleep(0.5)
                self.assertEqual(await c.eval(RECTS), r2)
                self.assertGreater(await c.eval("window.__teleop.S.camera3.drawn"), 10, "video keeps drawing")

                # Preset: camera 2 large, others stacked on the right; reset restores the row.
                await c.eval("const s = document.querySelector('#lay-preset'); s.value = 'big2';"
                             "s.dispatchEvent(new Event('change'))")
                r3 = await c.eval(RECTS)
                self.assertGreater(r3["camera2"][2], 1.8 * r3["camera1"][2])
                self.assertEqual(r3["camera1"][0], r3["camera3"][0])
                self.assertGreaterEqual(r3["camera3"][1], r3["camera1"][1] + r3["camera1"][3])  # no overlap
                await c.screenshot(os.path.join(SHOTS, "12_layout_big2.png"), full=False)
                await c.eval("document.querySelector('#lay-reset').click()")
                r4 = await c.eval(RECTS)
                for cam in CAMS:
                    self.assertLessEqual(abs(r4[cam][0] - r0[cam][0]) + abs(r4[cam][2] - r0[cam][2]), 3, (r4, r0))
                self.assertEqual(c.exceptions, [])
                print("drawn while editing:", drawn, "| custom:", r2, "| big2:", r3)

    async def test_narrow_screen_stacks(self):
        async with AppHarness(factory) as h:
            async with Chrome(700, 1400) as c:
                await c.open(str(h.client.make_url("/")))
                await wait_page(c, "document.querySelector('#conn').dataset.state === 'open'")
                await asyncio.sleep(0.5)
                r = await c.eval(RECTS)
                self.assertTrue(await c.eval("document.querySelector('#lay-edit').disabled"))
                self.assertLess(r["camera1"][1], r["camera2"][1])
                self.assertLess(r["camera2"][1], r["camera3"][1])
                self.assertEqual(c.exceptions, [])


if __name__ == "__main__":
    unittest.main()
