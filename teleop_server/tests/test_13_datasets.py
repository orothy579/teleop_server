"""Dataset browser: list, detail (labels, series, media), video/depth endpoints, delete to .trash, browser UI."""
import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from teleop_server import datasets

from teleop_server.recorder import Recorder
from teleop_server.sources.arms import ArmSource, FakeArm
from teleop_server.sources.odom import FakeOdom
from teleop_server.distributed.edge import RawDepthFakeCamera
from teleop_server.tests.browser import Chrome
from teleop_server.tests.test_7_record import FakeBase, KEYS, start_control, wait_saved
from teleop_server.tests.test_8_buttons import TEXT, wait_page
from teleop_server.tests.util import SHOTS, AppHarness


def factories(out):
    """Synthetic cameras with raw depth on the D405 0.1 mm grid, stored as uint16 PNGs."""
    def sources(hub, stats):
        return ([RawDepthFakeCamera(f"camera{i}", hub, stats, depth=True) for i in (1, 2, 3)]
                + [ArmSource(hub, stats, lambda: {k: FakeArm(*k) for k in KEYS}), FakeOdom(hub, stats)])

    def recorder(hub, srcs):
        return Recorder(hub, next(s for s in srcs if s.kind == "arms"), [s for s in srcs if s.kind == "camera"],
                        FakeBase, fps=30, synthetic=True, output_dir=out, depth_storage="png16")
    return sources, recorder


async def record(test, h, seconds=1.5, task="pick the bag", start=True):
    if start:
        await start_control(test, h)
    code, body = await h.post("/api/record", action="start", task=task)
    test.assertEqual(code, 200, body)
    await h.collect(seconds)
    await h.post("/api/record", action="stop")
    result, _ = await wait_saved(h)
    test.assertEqual(result["status"], "completed", result)
    return result


class DatasetApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="teleop_ds_"))
        self.cache = Path(tempfile.mkdtemp(prefix="teleop_ds_cache_"))
        self.p = patch.object(datasets, "PREVIEW_CACHE", self.cache)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.out, ignore_errors=True)
        shutil.rmtree(self.cache, ignore_errors=True)

    async def test_list_detail_media_and_delete(self):
        async with AppHarness(*factories(self.out)) as h:
            result = await record(self, h)
            name = result["session"]
            code, body = await h.post("/api/label", session=name, outcome="success", task_name="pick the bag",
                                      environment="lab A", operator="op1", notes="ok")
            self.assertEqual(code, 200, body)

            r = await h.client.get("/api/datasets")
            listed = (await r.json())["sessions"]
            self.assertEqual([x["session"] for x in listed], [name])
            x = listed[0]
            self.assertEqual((x["status"], x["frames"], x["labels"]["outcome"]), ("completed", result["frames"], "success"))
            self.assertGreater(x["elapsed_s"], 1.0)
            self.assertTrue(x["validation_passed"])

            r = await h.client.get(f"/api/datasets/{name}")
            d = (await r.json())["session"]
            n = min(result["frames"], 600)
            S = d["series"]
            self.assertEqual((len(S["t"]), len(S["follower"]), len(S["follower"][0])), (n, n, 14))
            self.assertEqual(len(S["base"]["x"]), n)
            self.assertEqual(S["frame"][0], 0)
            self.assertEqual(S["frame"][-1], result["frames"] - 1)
            self.assertEqual([c["name"] for c in d["cameras"]], ["camera1", "camera2", "camera3"])
            for c in d["cameras"]:
                self.assertTrue(c["video"].endswith(".mp4"), c)  # LeRobot video (no Jetson rgb_mp4 here)
                self.assertEqual(c["depth_frames"], result["frames"])
            self.assertGreater(d["sizes"]["depth_raw"], 0)
            self.assertEqual(d["labels"]["environment"], "lab A")

            # Video: byte ranges, so the browser can seek.
            r = await h.client.get(f"/api/datasets/{name}/video/camera1", headers={"Range": "bytes=0-99"})
            self.assertEqual((r.status, r.headers["Content-Type"], len(await r.read())), (206, "video/mp4", 100))
            # Depth: the stored raw frame as an image, with its valid fraction.
            r = await h.client.get(f"/api/datasets/{name}/depth/camera2/{result['frames'] - 1}")
            self.assertEqual((r.status, r.headers["Content-Type"]), (200, "image/jpeg"))
            self.assertGreater(float(r.headers["X-Depth-Valid"]), 0.9)
            self.assertTrue((await r.read()).startswith(b"\xff\xd8"))

            # Depth as 30 fps videos (made once, cached outside the dataset folder): frame i = PNG i.
            r = await h.client.get(f"/api/datasets/{name}/depth_video/camera1")
            self.assertEqual(r.status, 409, "not made yet")
            r = await h.client.post(f"/api/datasets/{name}/depth_preview")
            self.assertEqual(sorted((await r.json())["cameras"]), ["camera1", "camera2", "camera3"])
            for _ in range(300):
                r = await h.client.get(f"/api/datasets/{name}/depth_preview")
                cams = (await r.json())["cameras"]
                if all(c["ready"] for c in cams.values()):
                    break
                await asyncio.sleep(0.1)
            self.assertTrue(all(c["ready"] for c in cams.values()), cams)
            r = await h.client.get(f"/api/datasets/{name}/depth_video/camera2", headers={"Range": "bytes=0-99"})
            self.assertEqual((r.status, r.headers["Content-Type"]), (206, "video/mp4"))
            import av
            with av.open(str(self.cache / name / "camera2.mp4")) as c:
                st = c.streams.video[0]
                self.assertEqual((int(st.average_rate), sum(1 for _ in c.decode(video=0))), (30, result["frames"]))
            self.assertFalse(any((self.out / name).rglob("*.tmp.mp4")))
            self.assertFalse((self.out / name / "camera2.mp4").exists(), "never inside the dataset folder")

            for url, status in ((f"/api/datasets/{name}/video/camera9", 400),
                                (f"/api/datasets/{name}/depth/camera1/99999", 404),
                                (f"/api/datasets/{name}/depth/camera1/abc", 400),
                                ("/api/datasets/20990101_000000", 404),
                                ("/api/datasets/..%2F..%2Fetc", 400)):
                r = await h.client.get(url)
                self.assertEqual(r.status, status, url)

            # A session being recorded cannot be deleted.
            code, body = await h.post("/api/record", action="start", task="second")
            self.assertEqual(code, 200, body)
            current = body["record"]["session"]
            r = await h.client.delete(f"/api/datasets/{current}")
            self.assertEqual(r.status, 409)
            await h.post("/api/record", action="stop")
            await wait_saved(h)

            ws = await h.client.ws_connect("/ws")
            r = await h.client.delete(f"/api/datasets/{name}")
            body = await r.json()
            self.assertEqual(r.status, 200, body)
            self.assertFalse((self.out / name).exists())
            self.assertTrue((self.out / ".trash" / name / "session.json").is_file(), "recoverable")
            self.assertFalse((self.cache / name).exists(), "its depth videos are dropped")
            for _ in range(50):  # other browsers are told to refresh (after the hub's replay of the last one)
                msg = await ws.receive(timeout=3)
                data = json.loads(msg.data) if msg.type.name == "TEXT" else {}
                if data.get("type") == "labels" and data.get("deleted"):
                    self.assertEqual(data["session"], name)
                    break
            else:
                self.fail("no refresh message")
            await ws.close()
            r = await h.client.get("/api/datasets")
            self.assertNotIn(name, [x["session"] for x in (await r.json())["sessions"]])
            r = await h.client.delete(f"/api/datasets/{name}")
            self.assertEqual(r.status, 404)


class DatasetBrowserTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="teleop_ds_ui_"))
        self.cache = Path(tempfile.mkdtemp(prefix="teleop_ds_cache_"))
        self.p = patch.object(datasets, "PREVIEW_CACHE", self.cache)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.out, ignore_errors=True)
        shutil.rmtree(self.cache, ignore_errors=True)

    async def test_browse_play_label_and_delete(self):
        async with AppHarness(*factories(self.out)) as h:
            first = await record(self, h, 1.5, task="first task")
            second = await record(self, h, 1.0, task="second task", start=False)
            await h.post("/api/label", session=first["session"], outcome="failure", task_name="first task",
                         environment="lab B", operator="kim", notes="missed")
            async with Chrome(1440, 1100) as c:
                await c.open(str(h.client.make_url("/")))
                await wait_page(c, "document.querySelector('#conn').dataset.state === 'open'")
                await c.eval("document.querySelector('#btn-datasets').click()")
                self.assertTrue(await wait_page(c, "document.querySelector('#ds-dialog').open && "
                                                   "document.querySelectorAll('#ds-rows tr').length === 2"))
                self.assertIn("2 / 2개", await c.eval(TEXT.format(sel="#ds-count")))
                # Search and filter.
                await c.eval("(() => { const s = document.querySelector('#ds-search'); s.value = 'kim';"
                             " s.dispatchEvent(new Event('input')); })()")
                self.assertEqual(await c.eval("document.querySelectorAll('#ds-rows tr').length"), 1)
                await c.eval("(() => { const s = document.querySelector('#ds-search'); s.value = '';"
                             " s.dispatchEvent(new Event('input')); const f = document.querySelector('#ds-filter');"
                             " f.value = 'unlabeled'; f.dispatchEvent(new Event('change')); })()")
                rows = await c.eval("[...document.querySelectorAll('#ds-rows tr')].map(r => r.dataset.session)")
                self.assertEqual(rows, [second["session"]])
                await c.eval("(() => { const f = document.querySelector('#ds-filter'); f.value = 'all';"
                             " f.dispatchEvent(new Event('change')); })()")
                await c.screenshot(os.path.join(SHOTS, "13_dataset_list.png"), full=False)

                # Detail of the labelled session: labels, three synchronized videos, depth, charts.
                await c.eval(f"document.querySelector('#ds-rows tr[data-session=\"{first['session']}\"] td:nth-child(2)').click()")
                self.assertTrue(await wait_page(c, "!document.querySelector('#ds-detail-view').hidden && "
                                                   "document.querySelectorAll('#dd-videos video').length === 3 && "
                                                   "[...document.querySelectorAll('#dd-videos video')].every(v => v.readyState >= 2)", 15))
                labels = await c.eval(TEXT.format(sel="#dd-labels"))
                self.assertIn("lab B", labels)
                self.assertIn("missed", labels)
                self.assertEqual(await c.eval(TEXT.format(sel="#dd-outcome span")), "실패")
                # Depth videos (made on first view) replace the single frames and play with the RGB.
                self.assertTrue(await wait_page(c, "document.querySelectorAll('#dd-videos video.ds-depthvid').length === 3 && "
                                                   "[...document.querySelectorAll('#dd-videos video.ds-depthvid')].every(v => v.readyState >= 2)", 30))
                self.assertTrue(await c.eval("[...document.querySelectorAll('#dd-videos img.ds-depth')].every(i => i.hidden)"))
                self.assertTrue(await wait_page(c, "[...document.querySelectorAll('#dd-videos .dinfo')].every(e => e.textContent.includes('depth 측정'))", 10))
                self.assertEqual(await c.eval("document.querySelectorAll('#dd-l-left .key').length"), 6)
                drawn = await c.eval("(() => { const cv = document.querySelector('#dd-c-left');"
                                     " const px = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;"
                                     " let n = 0; for (let i = 3; i < px.length; i += 4) n += px[i] > 0; return n; })()")
                self.assertGreater(drawn, 1000, "joint chart drawn")

                # Seek to the middle: every video follows; depth reloads for that frame.
                mid = first["frames"] // 2
                await c.eval(f"(() => {{ const s = document.querySelector('#dd-seek'); s.value = '{mid}';"
                             " s.dispatchEvent(new Event('input')); })()")
                times = await wait_page(c, "(() => { const t = [...document.querySelectorAll('#dd-videos video')]"
                                           ".map(v => v.currentTime); return t.every(x => x > 0) ? t : null; })()")
                for t in times:
                    self.assertAlmostEqual(t, (mid + 0.5) / 30, delta=0.02)
                self.assertIn(f"프레임 {mid + 1} /", await c.eval(TEXT.format(sel="#dd-time")))
                # Play: frames advance and the videos stay together.
                await c.eval("document.querySelector('#dd-play').click()")
                await asyncio.sleep(0.4)  # the test session is only 1.5 s long
                state = await c.eval("(() => { const v = [...document.querySelectorAll('#dd-videos video')];"
                                     " return {frame: window.__teleop.datasets.frame, paused: v.map(x => x.paused),"
                                     " spread: Math.max(...v.map(x => x.currentTime)) - Math.min(...v.map(x => x.currentTime))}; })()")
                await c.eval("document.querySelector('#dd-play').click()")
                self.assertGreater(state["frame"], mid + 5, state)
                self.assertEqual(len(state["paused"]), 6)
                self.assertFalse(any(state["paused"]), state)
                self.assertLess(state["spread"], 0.1, state)
                # Clicking a chart moves to that time.
                await c.eval("document.querySelector('#dd-c-left').scrollIntoView({block: 'center'})")
                await asyncio.sleep(0.2)
                box = await c.eval("(() => { const r = document.querySelector('#dd-c-left').getBoundingClientRect();"
                                   " return [r.left, r.top, r.width, r.height]; })()")
                await c.send("Input.dispatchMouseEvent", type="mouseMoved", x=box[0] + 50, y=box[1] + box[3] / 2)
                self.assertFalse(await c.eval("document.querySelector('#ds-tip').hidden"), "hover tooltip")
                await c.send("Input.dispatchMouseEvent", type="mousePressed", x=box[0] + 50, y=box[1] + box[3] / 2,
                             button="left", clickCount=1)
                await c.send("Input.dispatchMouseEvent", type="mouseReleased", x=box[0] + 50, y=box[1] + box[3] / 2,
                             button="left", clickCount=1)
                self.assertLess(await c.eval("window.__teleop.datasets.frame"), mid)
                await c.screenshot(os.path.join(SHOTS, "13_dataset_detail.png"), full=False)

                # Edit labels from the detail view (label pop-up on top), the detail refreshes.
                await c.eval("document.querySelector('#dd-edit').click()")
                self.assertTrue(await wait_page(c, "document.querySelector('#label-dialog').open"))
                self.assertEqual(await c.eval("document.querySelector('#ld-session').value"), first["session"])
                await c.eval("""(() => { document.querySelector('input[name=ld-outcome][value=success]').click();
                    document.querySelector('#ld-save').click(); })()""")
                self.assertTrue(await wait_page(c, "!document.querySelector('#label-dialog').open"))
                self.assertTrue(await wait_page(c, TEXT.format(sel="#dd-outcome span") + " === '성공'"))
                self.assertTrue(await c.eval("document.querySelector('#ds-dialog').open"))

                # Delete (confirm accepted): back to the list with one session; the folder is in .trash.
                c.accept_dialogs = True
                await c.eval("document.querySelector('#dd-delete').click()")
                self.assertTrue(await wait_page(c, "!document.querySelector('#ds-list-view').hidden && "
                                                   "document.querySelectorAll('#ds-rows tr').length === 1"))
                self.assertIn("dataset/.trash", c.dialogs[-1])
                self.assertTrue((self.out / ".trash" / first["session"]).is_dir())
                # Cancelled delete changes nothing.
                c.accept_dialogs = False
                await c.eval("document.querySelector('#ds-rows tr .btn.danger').click()")
                await asyncio.sleep(0.5)
                self.assertTrue((self.out / second["session"]).is_dir())
                await c.eval("document.querySelector('#ds-close').click()")
                self.assertFalse(await c.eval("document.querySelector('#ds-dialog').open"))
                self.assertEqual(c.exceptions, [])


if __name__ == "__main__":
    unittest.main()
