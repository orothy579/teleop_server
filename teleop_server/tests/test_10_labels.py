"""Feature 8: human labels (outcome, task name, environment, operator, notes) after recording."""
import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from teleop_server.tests.browser import Chrome
from teleop_server.tests.test_7_record import factories, start_control, wait_saved
from teleop_server.tests.test_8_buttons import TEXT, wait_page
from teleop_server.tests.util import SHOTS, AppHarness


async def record_once(test, h, seconds=1.0):
    await start_control(test, h)
    code, _ = await h.post("/api/record", action="start", task="pick the bag")
    test.assertEqual(code, 200)
    await h.collect(seconds)
    await h.post("/api/record", action="stop")
    result, _ = await wait_saved(h)
    return result


class LabelApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="teleop_lbl_"))

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    async def test_label_lifecycle(self):
        async with AppHarness(*factories(self.out)) as h:
            result = await record_once(self, h)
            session = result["session"]
            resp = await h.client.get("/api/sessions")
            listed = (await resp.json())["sessions"]
            self.assertEqual([x["session"] for x in listed], [session])
            self.assertIsNone(listed[0]["labels"])
            self.assertEqual(listed[0]["task"], "pick the bag")

            code, body = await h.post("/api/label", session=session, task_name="x")
            self.assertEqual(code, 400, body)  # outcome is required
            code, body = await h.post("/api/label", session="../../etc", outcome="success")
            self.assertEqual(code, 400, body)
            code, body = await h.post("/api/label", session="20990101_000000", outcome="success")
            self.assertEqual(code, 404, body)
            code, body = await h.post("/api/label", session=session, outcome="maybe")
            self.assertEqual(code, 400, body)

            ws = await h.client.ws_connect("/ws")
            code, body = await h.post("/api/label", session=session, outcome="failure", task_name="종이백 옮기기",
                                      environment="실험실 책상 A", operator="op1", notes="그리퍼가 미끄러짐")
            self.assertEqual(code, 200, body)
            self.assertEqual(body["labels"]["revision"], 1)
            got = None
            for _ in range(50):
                msg = await ws.receive(timeout=3)
                if msg.type.name == "TEXT" and json.loads(msg.data).get("type") == "labels":
                    got = json.loads(msg.data)
                    break
            await ws.close()
            self.assertEqual(got["session"], session)
            code, body = await h.post("/api/label", session=session, outcome="success", task_name="종이백 옮기기",
                                      environment="실험실 책상 A", operator="op1", notes="재검토 후 성공으로 수정")
            self.assertEqual(body["labels"]["revision"], 2)

            # A session that is still being recorded cannot be labelled (its session.json is rewritten).
            code, body = await h.post("/api/record", action="start")
            self.assertEqual(code, 200, body)
            code, body = await h.post("/api/label", session=body["record"]["session"], outcome="success")
            self.assertEqual(code, 409, body)
            await h.post("/api/record", action="stop")
            await wait_saved(h)

        root = Path(result["path"])
        labels = json.loads((root / "session.json").read_text())["labels"]
        self.assertEqual((labels["outcome"], labels["environment"], labels["notes"], labels["revision"]),
                         ("success", "실험실 책상 A", "재검토 후 성공으로 수정", 2))
        mirror = json.loads((root / "dataset/meta/episode_labels.json").read_text())["0"]
        self.assertEqual(mirror["outcome"], "success")
        self.assertEqual(mirror["recorded_task"], "pick the bag")
        self.assertEqual(json.loads((root / "session.json").read_text())["status"], "completed")


class LabelBrowserTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="teleop_lbl_ui_"))

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    async def test_label_popup_from_button(self):
        """No label panel on the page; the pop-up (also reachable from "라벨 입력") edits any session."""
        async with AppHarness(*factories(self.out)) as h:
            await h.wait_for(lambda s: all(x["state"] == "running" for x in s["sources"] if x["kind"] == "camera"), 5)
            async with Chrome(1440, 1500) as c:
                await c.open(str(h.client.make_url("/")))
                await wait_page(c, "document.querySelector('#conn').dataset.state === 'open'")
                self.assertIsNone(await c.eval("document.querySelector('#label-card')"))
                self.assertTrue(await c.eval("document.querySelector('#btn-labels').hidden"), "no sessions yet")
                result = await record_once(self, h)
                session = result["session"]
                self.assertTrue(await wait_page(c, "!document.querySelector('#btn-labels').hidden"))
                self.assertEqual(await c.eval(TEXT.format(sel="#btn-labels")), "라벨 입력")
                self.assertIn("라벨 미입력", await c.eval(TEXT.format(sel="#rec-last")))
                self.assertFalse(await c.eval("document.querySelector('#label-dialog').open"),
                                 "a recording stopped elsewhere does not pop up here")

                await c.eval("document.querySelector('#btn-labels').click()")
                self.assertTrue(await wait_page(c, "document.querySelector('#label-dialog').open"))
                self.assertFalse(await c.eval("document.querySelector('#ld-pick').hidden"))
                self.assertEqual(await c.eval("document.querySelector('#ld-session').value"), session)
                self.assertEqual(await c.eval("document.querySelector('#ld-task').value"), "pick the bag")
                self.assertEqual(await c.eval(TEXT.format(sel="#ld-summary")), "라벨 미입력")
                await c.eval("document.querySelector('#ld-save').click()")  # no outcome chosen yet
                self.assertIn("결과", await wait_page(c, TEXT.format(sel="#ld-msg")))
                await c.eval("""(() => {
                    document.querySelector('input[name=ld-outcome][value=failure]').click();
                    const set = (id, v) => { const e = document.querySelector(id); e.value = v;
                                             e.dispatchEvent(new Event('input', {bubbles: true})); };
                    set('#ld-env', '실험실 책상 A'); set('#ld-op', 'op1'); set('#ld-notes', '두 번째 시도에서 놓침');
                    document.querySelector('#ld-save').click(); })()""")
                self.assertTrue(await wait_page(c, "!document.querySelector('#label-dialog').open"),
                                await c.eval(TEXT.format(sel="#ld-msg")))
                self.assertTrue(await wait_page(c, TEXT.format(sel="#rec-last") + ".includes('라벨 저장됨')"))
                self.assertEqual(await c.eval(TEXT.format(sel="#btn-labels")), "라벨 보기·수정")

                # Reopen: the saved labels are shown and can be revised.
                await c.eval("document.querySelector('#btn-labels').click()")
                self.assertTrue(await wait_page(c, "document.querySelector('#label-dialog').open"))
                self.assertTrue(await c.eval("document.querySelector('input[name=ld-outcome][value=failure]').checked"))
                self.assertEqual(await c.eval("document.querySelector('#ld-env').value"), "실험실 책상 A")
                self.assertIn("수정 1회", await c.eval(TEXT.format(sel="#ld-summary")))
                self.assertIn("라벨: 실패", await c.eval("document.querySelector('#ld-session').selectedOptions[0].text"))
                await c.screenshot(os.path.join(SHOTS, "10_labels.png"), full=False)
                await c.eval("""(() => {
                    document.querySelector('input[name=ld-outcome][value=success]').click();
                    document.querySelector('#ld-save').click(); })()""")
                self.assertTrue(await wait_page(c, "!document.querySelector('#label-dialog').open"))
                self.assertEqual(c.exceptions, [])
        labels = json.loads((Path(result["path"]) / "session.json").read_text())["labels"]
        self.assertEqual((labels["outcome"], labels["task_name"], labels["environment"], labels["operator"],
                          labels["revision"]), ("success", "pick the bag", "실험실 책상 A", "op1", 2))


if __name__ == "__main__":
    unittest.main()
