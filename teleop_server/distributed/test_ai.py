"""Web "AI 제어": inference-folder discovery and a keep-loaded worker driving the real policy path.

The worker is the real inference/pi05/serve.py in its own process with a stand-in model (no GPU), the
DGX server and the Jetson edge are the real ones (simulated arms and sensors), as in test_policy.py.
"""
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from teleop_server import ai_control
from teleop_server.distributed.server import AI, BRIDGE
from teleop_server.distributed import test_distributed as td  # module import: do not re-run its tests here

PI05 = Path(__file__).resolve().parents[3] / "inference" / "pi05"
BOOT = """
import sys, types
import numpy as np
sys.path.insert(0, {pi05!r})
class Runner:
    device = "cpu"
    def __init__(self, *args, **kwargs):
        import os
        if os.environ.get("FAKE_LOAD_ERROR"):
            raise RuntimeError("CUDA out of memory (fake)")
    def predict(self, state, images, task):
        if task == "boom":
            raise RuntimeError("model exploded")
        actions = np.tile(np.array(state, dtype=np.float32), (50, 1))
        actions[:, 0] -= .005
        return actions
sys.modules["infer"] = types.SimpleNamespace(PI05Runner=Runner)
import serve
serve.main(sys.argv[1:])
"""


class ProcessLauncher:
    """Workers as child processes (the server uses transient systemd units)."""

    def __init__(self, config):
        self.config, self.procs, self.env, self.paths = config, {}, {}, {}

    def start(self, model, checkpoint, socket):
        argv = [sys.executable, "-c", BOOT.format(pi05=str(PI05)), "--config", str(self.config), "--socket", socket]
        if checkpoint:
            argv += ["--model", str(Path(model["path"]) / checkpoint)]
        self.paths[model["id"]] = Path(model["path"]) / "worker.log"
        with open(self.paths[model["id"]], "ab") as log:
            self.procs[model["id"]] = subprocess.Popen(argv, cwd=model["path"], stdout=log, stderr=subprocess.STDOUT,
                                                       env=dict(os.environ, **self.env))

    def stop(self, model_id):
        p = self.procs.pop(model_id, None)
        if p and p.poll() is None:
            p.terminate()
            p.wait(20)

    def state(self, model_id):  # like systemd: an abnormal exit stays "failed" until the next load/unload
        p = self.procs.get(model_id)
        return "inactive" if p is None else "active" if p.poll() is None else "failed" if p.returncode else "inactive"

    def unit_info(self, model_id):
        p = self.procs[model_id]
        return {"invocation": str(p.pid), "result": "signal", "exit": f"killed {-p.returncode}",
                "summary": f"워커가 비정상 종료했습니다 (signal, killed {-p.returncode})"}

    def logs(self, model_id, lines=40):
        path = self.paths.get(model_id)
        return path.read_text(errors="replace").splitlines()[-lines:] if path and path.exists() else []


class AIControlTests(unittest.IsolatedAsyncioTestCase):
    FPS = 30
    until = td.IntegrationTests.until
    _control_running = td.IntegrationTests._control_running

    async def asyncSetUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="teleop_ai_"))
        model = self.root / "pi05test"
        for ckpt in ("001", "002"):
            (model / "models" / ckpt).mkdir(parents=True)
            (model / "models" / ckpt / "config.json").write_text(json.dumps({"chunk_size": 50}))
        (model / "inference.yaml").write_text('name: "Test PI05"\ndescription: "stand-in model"\n'
                                              'serve: [python, serve.py]\nconfig: config.yaml\ncheckpoints: "models/*"\n')
        (self.root / "broken").mkdir()
        (self.root / "broken" / "inference.yaml").write_text("name: broken\n")
        (self.root / "notes").mkdir()  # no manifest: not a model
        self.config = model / "config.yaml"
        self.config.write_text("model:\n  path: models/002\n  device: cpu\nexecution:\n  task: pick test\n  seconds: 3\n")
        self.patches = [patch.object(ai_control, "INFERENCE_ROOT", self.root),
                        patch.object(ai_control, "SOCKET_DIR", self.root / "sock"),
                        patch.object(ai_control, "SystemdLauncher", lambda: ProcessLauncher(self.config))]
        for p in self.patches:
            p.start()
        await td.IntegrationTests.asyncSetUp(self)
        (self.root / "edge.env").write_text("TELEOP_EDGE_TOKEN=test-secret\n")
        self.config.write_text(  # now the test server's address is known
            f"model:\n  path: models/002\n  device: cpu\nexecution:\n  task: pick test\n  seconds: 3\n"
            f"control:\n  max_joint_gap_rad: 1.2\nruntime:\n  server_url: {self.url}\n"
            f"  token_file: {self.root / 'edge.env'}\n  log_dir: {model / 'outputs'}\n")
        self.ai = self.app[AI]

    async def asyncTearDown(self):
        for model_id in list(self.ai.launcher.procs):
            self.ai.launcher.stop(model_id)
        await td.IntegrationTests.asyncTearDown(self)
        for p in reversed(self.patches):
            p.stop()
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    async def api(self, **body):
        async with self.client.post(self.url + "/api/ai", json=body) as r:
            return r.status, await r.json()

    async def model(self, model_id="pi05test"):
        async with self.client.get(self.url + "/api/ai") as r:
            data = await r.json()
        return next(m for m in data["models"] if m["id"] == model_id), data

    async def wait_state(self, state, timeout=40):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            m, _ = await self.model()
            if m["state"] == state:
                return m
            await asyncio.sleep(0.2)
        self.fail(f"model never became {state}: {m}")

    def test_discovery(self):
        models = {m["id"]: m for m in ai_control.discover(self.root)}
        self.assertEqual(set(models), {"pi05test", "broken"})
        m = models["pi05test"]
        self.assertEqual((m["name"], m["checkpoints"], m["default_checkpoint"]),
                         ("Test PI05", ["models/001", "models/002"], "models/002"))
        self.assertEqual(m["defaults"], {"task": "pick test", "seconds": 3})
        # Checkpoints are named by training run (train_config.json), step and when they were saved.
        (self.root / "pi05test" / "models" / "002" / "train_config.json").write_text(json.dumps({"job_name": "pi05_bag_20261013"}))
        weights = self.root / "pi05test" / "models" / "002" / "model.safetensors"
        weights.write_bytes(b"0")
        os.utime(weights, (1791810000, 1791810000))  # the weights' save time, not later file copies
        labels = [c["label"] for c in ai_control.discover(self.root)[1]["checkpoint_info"]]
        self.assertEqual(labels[1], "pi05_bag_20261013 · 002 · " + time.strftime("%m-%d %H:%M", time.localtime(1791810000)))
        self.assertTrue(labels[0].startswith("001 · "))
        self.assertIn("serve", models["broken"]["error"])

    async def test_load_once_then_start_and_stop_without_reloading(self):
        m, data = await self.model()
        self.assertEqual(m["state"], "off")
        self.assertIn("broken", [x["id"] for x in data["models"]])
        code, body = await self.api(action="run", model="pi05test", task="x", seconds=1)
        self.assertEqual(code, 409)
        self.assertIn("제어 시작", body["error"])  # arms first
        bridge = await self._control_running()

        code, body = await self.api(action="run", model="pi05test", task="x", seconds=1)
        self.assertEqual(code, 409)
        self.assertIn("로드", body["error"])  # model first
        code, body = await self.api(action="load", model="pi05test", checkpoint="models/001")
        self.assertEqual(code, 200, body)
        m = await self.wait_state("ready")
        self.assertEqual(m["checkpoint"], "models/001")
        pid = m["worker"]["pid"]

        # A run with motion that returns when it ends: the policy owns the followers, then all arms go
        # back to the staged pose and teleop resumes by itself.
        code, body = await self.api(action="run", model="pi05test", task="pick test", seconds=1.2, execute=True,
                                    return_after=True)
        self.assertEqual(code, 200, body)
        await self.until(lambda: self.edge.arms.policy.mode == "policy", timeout=10)
        await self.until(lambda: (self.ai.snapshot.get("active") or {}).get("chunks", 0) >= 1, timeout=10)
        self.assertEqual(self.ai.snapshot["active"]["name"], "Test PI05")
        await self.until(lambda: ((bridge.sources["arms"].status().get("info") or {}).get("policy") or {})
                         .get("mode") == "returning", timeout=15)  # as the DGX (and the browser) see it
        code, body = await self.api(action="run", model="pi05test", task="pick test", seconds=1, execute=True)
        self.assertEqual(code, 409)
        self.assertIn("돌아가는 중", body["error"])
        await self.until(lambda: ((bridge.sources["arms"].status().get("info") or {}).get("policy") or {})
                         .get("mode") == "teleop", timeout=10)
        self.assertTrue(self.ai.snapshot["auto_return"]["ok"])
        self.assertTrue(self.edge.base.enabled)
        m = await self.wait_state("ready")
        last = m["worker"]["last_run"]
        self.assertIsNone(last["error"], last)
        self.assertGreaterEqual(last["chunks"], 1)
        self.assertTrue(Path(last["log"]).is_file())

        # Again, with the model still loaded (same process): starts at once; 'AI 정지' holds the pose
        # even with return_after (a stop is often a reaction to a problem).
        t0 = time.monotonic()
        code, body = await self.api(action="run", model="pi05test", task="pick test", seconds=60, execute=True,
                                    return_after=True)
        self.assertEqual(code, 200, body)
        await self.until(lambda: self.edge.arms.policy.mode == "policy", timeout=10)
        started_in = time.monotonic() - t0
        await asyncio.sleep(0.5)
        code, body = await self.api(action="stop")
        self.assertEqual(code, 200, body)
        await self.until(lambda: self.edge.arms.policy.mode == "hold", timeout=5)
        m = await self.wait_state("ready")
        self.assertEqual(m["worker"]["pid"], pid, "the model was not reloaded")
        self.assertTrue(m["worker"]["last_run"]["stopped_by_user"])
        self.assertLess(started_in, 5.0)
        await asyncio.sleep(1.0)
        self.assertEqual(self.edge.arms.policy.mode, "hold")

        # Prediction only: no ownership change.
        sequence = self.edge.arms.policy.sequence
        code, body = await self.api(action="run", model="pi05test", task="pick test", seconds=0.5, execute=False)
        self.assertEqual(code, 200, body)
        m = await self.wait_state("ready")
        await asyncio.sleep(0.3)
        self.assertEqual((self.edge.arms.policy.mode, self.edge.arms.policy.sequence), ("hold", sequence))
        self.assertFalse(m["worker"]["last_run"]["execute"])

        # Run history and log download (the browser's "실행 기록" / "로그 받기").
        code, body = await self.api(action="runs", model="pi05test")
        self.assertEqual(code, 200, body)
        self.assertEqual([r["status"] for r in body["runs"]], ["done", "stopped", "done"])  # newest first
        self.assertFalse(body["runs"][0]["execute"])
        async with self.client.get(f"{self.url}/api/ai/runs/pi05test/{body['runs'][1]['file']}") as r:
            self.assertEqual(r.status, 200)
            self.assertIn("attachment", r.headers["Content-Disposition"])
            lines = (await r.text()).splitlines()
        self.assertEqual(json.loads(lines[-1])["event"], "end")
        async with self.client.get(f"{self.url}/api/ai/runs/pi05test/..%2Fconfig.yaml") as r:
            self.assertEqual(r.status, 400)

        # Chosen logs as one zip ("선택한 로그 받기"): the files plus an index of their results.
        chosen = [body["runs"][0]["file"], body["runs"][2]["file"]]
        async with self.client.get(f"{self.url}/api/ai/bundle/pi05test", params=[("run", f) for f in chosen]) as r:
            data = await r.read()
            self.assertEqual(r.status, 200, data[:300])
            self.assertEqual(r.headers["Content-Type"], "application/zip")
            self.assertRegex(r.headers["Content-Disposition"], r'filename="pi05test_ai_logs_\d{8}_\d{6}\.zip"')
            z = zipfile.ZipFile(io.BytesIO(data))
        names = sorted(n.split("/", 1)[1] for n in z.namelist())
        self.assertEqual(names, ["index.json", *sorted(f"runs/{f}" for f in chosen)])
        folder = z.namelist()[0].split("/")[0]
        index = json.loads(z.read(f"{folder}/index.json"))
        self.assertEqual([x["file"] for x in index["runs"]], chosen)
        self.assertEqual(z.read(f"{folder}/runs/{chosen[0]}"), (Path(m["log_dir"]) / chosen[0]).read_bytes())
        for params, code in (([], 400), ([("run", "../config.yaml")], 400), ([("run", "robot_run_20990101_000000.jsonl")], 404),
                             ([("error", "notes.txt")], 400)):
            async with self.client.get(f"{self.url}/api/ai/bundle/pi05test", params=params) as r:
                self.assertEqual(r.status, code, (params, await r.text()))

        code, body = await self.api(action="unload", model="pi05test")
        self.assertEqual(code, 200, body)
        await self.wait_state("off")
        self.assertTrue(bridge.connected)

    async def report(self, model, kind):
        """The saved report of the model's current error of this kind (as the browser links it)."""
        error = next(e for e in model["current_errors"] if e["kind"] == kind)
        self.assertTrue(error["file"], "report saved when the error was first seen")
        async with self.client.get(f"{self.url}/api/ai/errors/pi05test/{error['file']}") as r:
            self.assertEqual(r.status, 200)
            self.assertIn("attachment", r.headers["Content-Disposition"])
            return error, json.loads(await r.text())

    async def test_load_failure_and_worker_crash_are_saved(self):
        await self._control_running()
        self.ai.launcher.env = {"FAKE_LOAD_ERROR": "1"}
        await self.api(action="load", model="pi05test")
        m = await self.wait_state("error")
        error, report = await self.report(m, "load")
        self.assertEqual((report["kind"], report["key"]), ("load", error["key"]))
        self.assertIn("CUDA out of memory (fake)", report["message"])
        self.assertTrue(any("RuntimeError: CUDA out of memory (fake)" in line for line in report["traceback"]))
        self.assertTrue(any("모델 로딩 실패" in line for line in report["worker_log"]))
        self.assertTrue(report["robot_when_detected"]["connected"])
        self.assertIn("policy", report["robot_when_detected"]["arms"])

        # Seen again (every poll, a page reload, a web-server restart): still one report.
        await self.model()
        fresh = ai_control.AIControl(launcher=self.ai.launcher)
        await fresh.refresh()
        errors = self.root / "pi05test" / "outputs" / "errors"
        self.assertEqual(len(list(errors.glob("ai_error_*_load.json"))), 1)

        # The worker dies (here: killed): reported as a crash with its log, until the next unload/load.
        self.ai.launcher.env = {}
        await self.api(action="unload", model="pi05test")
        await self.wait_state("off")
        await self.api(action="load", model="pi05test")
        m = await self.wait_state("ready")
        os.kill(m["worker"]["pid"], 9)
        m = await self.wait_state("failed")
        error, report = await self.report(m, "crash")
        self.assertIn("killed 9", report["message"])
        self.assertTrue(report["worker_log"])
        self.assertEqual([e["kind"] for e in m["errors"]], ["crash", "load"])  # newest first
        async with self.client.get(f"{self.url}/api/ai/errors/pi05test/..%2Fconfig.yaml") as r:
            self.assertEqual(r.status, 400)
        params = [("error", e["file"]) for e in m["errors"]]
        async with self.client.get(f"{self.url}/api/ai/bundle/pi05test", params=params) as r:
            data = await r.read()
            self.assertEqual(r.status, 200, data[:300])
            z = zipfile.ZipFile(io.BytesIO(data))
        index = json.loads(z.read(next(n for n in z.namelist() if n.endswith("index.json"))))
        self.assertEqual([e["kind"] for e in index["errors"]], ["crash", "load"])
        self.assertEqual(sum("/errors/" in n for n in z.namelist()), 2)
        await self.api(action="unload", model="pi05test")
        await self.wait_state("off")

    async def test_control_stop_ends_the_ai_run_first(self):
        await self._control_running()
        await self.api(action="load", model="pi05test")
        await self.wait_state("ready")
        code, body = await self.api(action="run", model="pi05test", task="pick test", seconds=60, execute=True)
        self.assertEqual(code, 200, body)
        await self.until(lambda: self.edge.arms.policy.mode == "policy", timeout=10)
        await self.until(lambda: self.ai.snapshot.get("active") is not None, timeout=10)
        async with self.client.post(self.url + "/api/control", json={"action": "stop"}) as r:
            self.assertEqual(r.status, 200, await r.text())
        m = await self.wait_state("ready")
        self.assertTrue(m["worker"]["last_run"]["stopped_by_user"])
        self.assertIsNone(self.ai.snapshot.get("active"))
        await self.until(lambda: self.edge.arms.thread is None or not self.edge.arms.thread.is_alive(), timeout=20)

    async def test_browser_ai_button(self):
        from teleop_server.tests.browser import Chrome
        from teleop_server.tests.test_8_buttons import TEXT, wait_page
        await self._control_running()
        async with Chrome(1440, 1000) as c:
            await c.open(self.url + "/")
            await wait_page(c, "document.querySelector('#conn').dataset.state === 'open'")
            self.assertTrue(await wait_page(c, "!document.querySelector('#ai-group').hidden"))
            await c.eval("document.querySelector('#btn-ai').click()")
            self.assertTrue(await wait_page(c, "document.querySelectorAll('#ai-models .ai-model').length === 2"))
            self.assertIn("serve", await c.eval(TEXT.format(sel="#ai-models .ai-model[data-model=broken] .err")))
            await c.eval("document.querySelector('#ai-models .ai-model[data-model=pi05test] .name').click()")
            self.assertEqual(await c.eval("document.querySelector('#ai-task').value"), "pick test")
            self.assertTrue(await c.eval("document.querySelector('#ai-start').disabled"), "load first")
            await c.eval("[...document.querySelectorAll('#ai-models .ai-model[data-model=pi05test] button')]"
                         ".find(b => b.textContent === '모델 로드').click()")
            self.assertTrue(await wait_page(c, "!document.querySelector('#ai-start').disabled", 40))
            await c.eval("document.querySelector('#ai-seconds').value = '60'")
            c.accept_dialogs = True
            await c.eval("document.querySelector('#ai-start').click()")
            self.assertTrue(await wait_page(c, "!document.querySelector('#ai-banner').hidden && "
                                               + TEXT.format(sel="#ai-banner-title") + " === 'AI 제어 중'", 15))
            self.assertIn("양팔을 60초 동안 직접 움직입니다", c.dialogs[-1])
            self.assertTrue(await wait_page(c, TEXT.format(sel="#arm-state span") + " === 'AI가 제어 중'"))
            self.assertTrue(await wait_page(c, "document.querySelector('#arms').classList.contains('ai-mode')"))
            self.assertTrue(await c.eval("getComputedStyle(document.querySelector('.arm .col-leader')).display === 'none'"))
            await c.eval("document.querySelector('#ai-close').click()")
            await c.screenshot(os.path.join(os.environ.get("TELEOP_SHOTS", "/tmp/teleop_server_shots"), "15_ai_running.png"), full=False)
            await c.eval("document.querySelector('#ai-banner-stop').click()")
            self.assertTrue(await wait_page(c, TEXT.format(sel="#ai-banner-title") + " === 'AI 정지함'", 15))
            self.assertIn("자세 유지", await c.eval(TEXT.format(sel="#arm-state span")))
            banner = await c.eval(TEXT.format(sel="#ai-banner"))
            self.assertNotIn("제어 중지", banner, "no how-to text in the banner")
            self.assertTrue(await c.eval("document.querySelector('#ai-banner-stop').hidden"))

            # Holding after 'AI 정지', the operator chooses: AI 재개 (same model and task, from this pose) ...
            self.assertFalse(await c.eval("document.querySelector('#btn-return').hidden"))
            self.assertTrue(await wait_page(c, "!document.querySelector('#ai-banner-return').hidden && "
                                               "!document.querySelector('#ai-banner-resume').hidden"))
            self.assertFalse(await c.eval("document.querySelector('#btn-ai-resume').hidden"))
            self.assertFalse(await c.eval("document.querySelector('#ai-return').checked"), "default: hold, then choose")
            await c.screenshot(os.path.join(os.environ.get("TELEOP_SHOTS", "/tmp/teleop_server_shots"), "15_ai_choose.png"), full=False)
            await c.eval("document.querySelector('#ai-banner-resume').click()")
            self.assertTrue(await wait_page(c, TEXT.format(sel="#ai-banner-title") + " === 'AI 제어 중'", 15))
            self.assertIn('지금 자세에서 같은 작업을 60초 동안 다시 실행합니다', c.dialogs[-1])
            self.assertIn('"pick test"', c.dialogs[-1])
            await self.until(lambda: self.edge.arms.policy.mode == "policy", timeout=10)
            await c.eval("document.querySelector('#ai-banner-stop').click()")
            self.assertTrue(await wait_page(c, TEXT.format(sel="#ai-banner-title") + " === 'AI 정지함'", 15))
            # ... or 원격조작 전환: every arm back to the staged pose, then teleop.
            self.assertTrue(await wait_page(c, "!document.querySelector('#ai-banner-return').hidden"))
            await c.screenshot(os.path.join(os.environ.get("TELEOP_SHOTS", "/tmp/teleop_server_shots"), "15_ai_hold_return.png"), full=False)
            await c.eval("document.querySelector('#ai-banner-return').click()")
            self.assertTrue(await wait_page(c, TEXT.format(sel="#ai-banner-title") + " === '준비 자세로 돌아가는 중'", 5))
            self.assertIn("팔 4대(리더 포함)가 준비 자세로", c.dialogs[-1])
            self.assertIn("돌아가는 중", await c.eval(TEXT.format(sel="#arm-state span")))
            await c.screenshot(os.path.join(os.environ.get("TELEOP_SHOTS", "/tmp/teleop_server_shots"), "15_ai_returning.png"), full=False)
            self.assertTrue(await wait_page(c, TEXT.format(sel="#arm-state span") + " === '제어 중'", 10))
            self.assertTrue(await wait_page(c, TEXT.format(sel="#ai-banner-text") + ".includes('원격조작 재개됨')"))
            self.assertTrue(await c.eval("document.querySelector('#btn-return').hidden"))
            self.assertTrue(await c.eval("document.querySelector('#ai-banner-return').hidden"))
            self.assertFalse(await c.eval("document.querySelector('#arms').classList.contains('ai-mode')"))
            await c.eval("document.querySelector('#btn-ai').click()")
            await c.eval("document.querySelector('#ai-runs-box').open = true")
            self.assertTrue(await wait_page(c, "document.querySelectorAll('#ai-runs li a').length >= 1", 10))
            self.assertIn("정지함", await c.eval(TEXT.format(sel="#ai-runs li")))

            # An error is impossible to miss: red ⚠ bar until "확인", ⚠ in the tab title and the status.
            await c.eval("(() => { const t = document.querySelector('#ai-task'); t.value = 'boom'; })()")
            await c.eval("document.querySelector('#ai-start').click()")
            self.assertTrue(await wait_page(c, TEXT.format(sel="#ai-banner-title") + " === '⚠ AI 제어가 오류로 끝났습니다'", 15))
            self.assertIn("model exploded", await c.eval(TEXT.format(sel="#ai-banner-text")))
            self.assertTrue(await c.eval("document.querySelector('#ai-banner').classList.contains('error')"))
            self.assertTrue(await c.eval("document.title.startsWith('⚠ AI 오류')"))
            self.assertFalse(await c.eval("document.querySelector('#ai-banner-log').hidden"), "log download offered")
            self.assertEqual(await c.eval(TEXT.format(sel="#ai-banner-log")), "오류 기록 받기")
            href = await c.eval("document.querySelector('#ai-banner-log').getAttribute('href')")
            self.assertTrue(href.startswith("/api/ai/errors/pi05test/ai_error_"), href)
            async with self.client.get(self.url + href) as r:
                report = json.loads(await r.text())
            self.assertEqual((report["kind"], report["run"]["task"]), ("run", "boom"))
            self.assertTrue(any("RuntimeError: model exploded" in line for line in report["traceback"]))
            self.assertEqual(report["run_log"]["last_events"][-1]["event"], "end")
            self.assertTrue(any("model exploded" in line for line in report["worker_log"]))
            self.assertEqual({s["name"] for s in report["robot_when_detected"]["sources"]} & {"arms", "camera1"}, {"arms", "camera1"})
            await c.eval("document.querySelector('#ai-errors-box').open = true")
            self.assertTrue(await wait_page(c, "document.querySelectorAll('#ai-errors li a').length === 1"))
            self.assertIn("model exploded", await c.eval(TEXT.format(sel="#ai-errors li")))

            # Tick logs, download only those as one zip.
            await c.eval("document.querySelector('#ai-runs-box').open = true")
            self.assertTrue(await wait_page(c, "document.querySelectorAll('#ai-runs li input[type=checkbox]').length >= 3"))
            self.assertTrue(await c.eval("document.querySelector('#ai-pick-download').disabled"))
            await c.eval("document.querySelector('#ai-runs li input[type=checkbox]').click()")
            await c.eval("document.querySelector('#ai-errors li input[type=checkbox]').click()")
            self.assertEqual(await c.eval(TEXT.format(sel="#ai-pick-count")), "2개 선택 (실행 1 · 오류 1)")
            self.assertTrue(await c.eval("document.querySelector('#ai-runs-all').indeterminate"))
            await c.eval("window.aiSave = (blob, name) => { window.__saved = {size: blob.size, type: blob.type, name}; }")
            await c.eval("document.querySelector('#ai-pick-download').click()")
            self.assertTrue(await wait_page(c, "!!window.__saved", 10))
            saved = await c.eval("window.__saved")
            self.assertRegex(saved["name"], r"^pi05test_ai_logs_\d{8}_\d{6}\.zip$")
            self.assertGreater(saved["size"], 1000)
            self.assertEqual(saved["type"], "application/zip")
            await c.eval("document.querySelector('#ai-runs-all').click()")  # 전체 선택
            runs = await c.eval("document.querySelectorAll('#ai-runs li input[type=checkbox]').length")
            self.assertEqual(await c.eval(TEXT.format(sel="#ai-pick-count")), f"{runs + 1}개 선택 (실행 {runs} · 오류 1)")
            await c.eval("document.querySelector('#ai-errors-box').scrollIntoView()")
            await c.screenshot(os.path.join(os.environ.get("TELEOP_SHOTS", "/tmp/teleop_server_shots"), "15_ai_pick.png"), full=False)
            await c.eval("document.querySelector('#ai-pick-clear').click()")
            self.assertTrue(await wait_page(c, "document.querySelector('#ai-pick-download').disabled"))
            self.assertFalse(await c.eval("document.querySelector('#ai-runs-all').checked"))
            await c.eval("document.querySelector('#ai-errors-box').scrollIntoView()")
            await c.screenshot(os.path.join(os.environ.get("TELEOP_SHOTS", "/tmp/teleop_server_shots"), "15_ai_errors.png"), full=False)
            self.assertEqual(await c.eval("document.querySelector('#ai-state').dataset.s"), "critical")
            await c.eval("document.querySelector('#ai-close').click()")
            await asyncio.sleep(3)  # still shown: errors do not time out
            self.assertTrue(await c.eval("document.querySelector('#ai-banner').classList.contains('error')"))
            await c.screenshot(os.path.join(os.environ.get("TELEOP_SHOTS", "/tmp/teleop_server_shots"), "15_ai_error.png"), full=False)
            await c.eval("document.querySelector('#ai-banner-ok').click()")
            self.assertTrue(await wait_page(c, "!document.querySelector('#ai-banner').classList.contains('error')"))
            self.assertFalse(await c.eval("document.title.startsWith('⚠')"))
            await c.screenshot(os.path.join(os.environ.get("TELEOP_SHOTS", "/tmp/teleop_server_shots"), "15_ai_hold.png"), full=False)
            self.assertEqual(c.exceptions, [])


if __name__ == "__main__":
    unittest.main()
