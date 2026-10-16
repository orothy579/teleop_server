"""Features 5+6 in a real browser: control and record buttons drive the server."""
import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from teleop_server.tests.browser import Chrome
from teleop_server.tests.test_7_record import arms_state, factories
from teleop_server.tests.util import SHOTS, AppHarness

TEXT = "document.querySelector({sel!r}).textContent"


async def wait_page(chrome, expr, timeout=10.0):
    """Poll a JS expression until truthy; returns its value."""
    end = time.monotonic() + timeout
    while True:
        value = await chrome.eval(expr)
        if value or time.monotonic() > end:
            return value
        await asyncio.sleep(0.1)


class ButtonsBrowserTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="teleop_btn_"))

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    async def test_control_and_record_buttons(self):
        async with AppHarness(*factories(self.out)) as h:
            await h.wait_for(lambda s: all(x["state"] == "running" for x in s["sources"] if x["kind"] == "camera"), 5)
            async with Chrome(1440, 1800) as c:
                await c.open(str(h.client.make_url("/")))
                await wait_page(c, "document.querySelector('#conn').dataset.state === 'open'")
                self.assertTrue(await wait_page(c, "!document.querySelector('#btn-control').disabled"))
                self.assertEqual(await c.eval(TEXT.format(sel=".ctl-group .ctl-label")), "로봇 제어")
                self.assertIsNone(await c.eval("document.querySelector('#label-card')"), "labels only in the pop-up")
                self.assertTrue(await c.eval("document.querySelector('#btn-record').disabled"),
                                "record must be disabled until arm control runs")

                c.accept_dialogs = False  # operator cancels the confirmation: nothing may move
                await c.eval("document.querySelector('#btn-control').click()")
                await asyncio.sleep(0.8)
                self.assertEqual(len(c.dialogs), 1)
                self.assertIn("준비 자세", c.dialogs[0])
                self.assertEqual(arms_state(await h.stats()), "idle")

                c.accept_dialogs = True
                await c.eval("document.querySelector('#btn-control').click()")
                self.assertTrue(await wait_page(c, TEXT.format(sel="#arm-state span") + " === '제어 중'"))
                self.assertEqual(await c.eval(TEXT.format(sel="#btn-control")), "제어 중지")
                self.assertTrue(await wait_page(c, "!document.querySelector('#btn-record').disabled"))

                self.assertIsNone(await c.eval("document.querySelector('#task')"), "no task input before recording")
                await c.eval("document.querySelector('#btn-record').click()")
                self.assertTrue(await wait_page(c, TEXT.format(sel="#rec-state span") + " === '녹화 중'"))
                self.assertEqual(await c.eval(TEXT.format(sel="#btn-record .lbl")), "녹화 중지")
                await asyncio.sleep(2.0)
                await c.screenshot(os.path.join(SHOTS, "8_recording.png"), full=False)
                meta = await c.eval(TEXT.format(sel="#rec-meta"))
                self.assertRegex(meta, r"00:0[12] · \d+ 프레임")

                await c.eval("document.querySelector('#btn-record').click()")
                # Stopping opens the label pop-up immediately, while the session is still saving.
                self.assertTrue(await wait_page(c, "document.querySelector('#label-dialog').open"))
                await c.eval("""(() => {
                    document.querySelector('input[name=ld-outcome][value=success]').click();
                    const set = (id, v) => { const e = document.querySelector(id); e.value = v; };
                    set('#ld-task', 'browser task'); set('#ld-env', 'lab A'); set('#ld-op', 'op1');
                    document.querySelector('#ld-save').click(); })()""")
                closed = await wait_page(c, "!document.querySelector('#label-dialog').open", 60)
                self.assertTrue(closed, await c.eval(TEXT.format(sel="#ld-msg")))
                last = await wait_page(c, f"(() => {{ const t = {TEXT.format(sel='#rec-last')};"
                                          " return t.includes('검증 통과') && t.includes('라벨 저장됨') ? t : ''; })()", 60)
                self.assertIn("검증 통과", last)
                await c.screenshot(os.path.join(SHOTS, "8_saved.png"), full=False)

                await c.eval("document.querySelector('#btn-control').click()")
                self.assertTrue(await wait_page(c, TEXT.format(sel="#arm-state span") + " === '정지'"))
                self.assertTrue(await c.eval("document.querySelector('#btn-record').disabled"))
                self.assertEqual(c.exceptions, [])
                print("dialogs:", [d.splitlines()[0] for d in c.dialogs], "| last:", last)

        root = next(self.out.iterdir())
        manifest = json.loads((root / "session.json").read_text())
        self.assertEqual((manifest["status"], manifest["task"], manifest["stop_reason"]),
                         ("completed", "browser task", "user"))
        self.assertEqual((manifest["labels"]["outcome"], manifest["labels"]["environment"]), ("success", "lab A"))
        # The label's task name is the dataset's task text.
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        ds = LeRobotDataset("local/mobile_paper_bag", root=root / "dataset", video_backend="pyav")
        self.assertEqual(ds[0]["task"], "browser task")


if __name__ == "__main__":
    unittest.main()
