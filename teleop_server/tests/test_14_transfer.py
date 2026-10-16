"""Dataset transfer over SSH ("보내기"): password once, tar stream, SHA-256 check, skip existing, cancel.

A stand-in ``ssh`` (fake_ssh.py) runs the remote side in a local folder, so the whole pipeline is
tested without a network; it checks the password through the same askpass path as OpenSSH.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from teleop_server import transfer as tx
from teleop_server.tests.browser import Chrome
from teleop_server.tests.test_13_datasets import factories
from teleop_server.tests.test_8_buttons import TEXT, wait_page
from teleop_server.tests.util import SHOTS, AppHarness

FAKE_SSH = Path(__file__).with_name("fake_ssh.py")
PASSWORD = "correct horse battery"
TARGET = {"name": "lab-gpu", "host": "gpu.example.com", "port": 2222, "user": "work", "remote_dir": "teleop_datasets",
          "check_host_key": False}


def make_session(root, name, size=300_000):
    s = root / name
    (s / "dataset" / "data").mkdir(parents=True)
    (s / "depth_raw" / "camera1").mkdir(parents=True)
    (s / "session.json").write_text(json.dumps({"status": "completed", "frames": 10, "fps": 30, "task": "t",
                                                "start_unix_ns": time.time_ns()}))
    (s / "dataset" / "data" / "x.parquet").write_bytes(os.urandom(size))
    for i in range(5):
        (s / "depth_raw" / "camera1" / f"{i:06d}.png").write_bytes(os.urandom(size // 10))
    return s


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class FakeRemote:
    """Patches the module: fake ssh, temp targets file, temp remote home."""

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="teleop_tx_"))
        self.home = self.tmp / "remote_home"
        self.home.mkdir()
        self.patches = [patch.object(tx, "SSH", str(FAKE_SSH)),
                        patch.object(tx, "TARGETS_FILE", self.tmp / "cfg" / "targets.json"),
                        patch.object(tx, "KNOWN_HOSTS", self.tmp / "cfg" / "known_hosts"),
                        patch.dict(os.environ, {"FAKE_SSH_HOME": str(self.home), "FAKE_SSH_PASSWORD": PASSWORD})]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


def wait_job(mgr, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = mgr.status()
        if st["state"] not in ("connecting", "sending"):
            return st
        time.sleep(0.05)
    raise AssertionError(f"transfer did not finish: {mgr.status()}")


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="teleop_tx_src_"))
        self.a = make_session(self.out, "20261012_100000")
        self.b = make_session(self.out, "20261012_100100", size=200_000)

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def test_send_verify_skip_and_errors(self):
        with FakeRemote() as remote:
            published = []
            mgr = tx.Transfer(publish=published.append)
            # Wrong password: clear message, nothing created remotely.
            with self.assertRaises(tx.TransferError) as cm:
                mgr.test(self.out, TARGET, "wrong", ["20261012_100000"])
            self.assertEqual(cm.exception.status, 401)
            self.assertIn("로그인 실패", str(cm.exception))
            # Connection check.
            info = mgr.test(self.out, TARGET, PASSWORD, ["20261012_100000", "20261012_100100"])
            self.assertEqual(info["path"], str(remote.home / "teleop_datasets"))
            self.assertEqual(info["existing"], [])
            self.assertGreater(info["free_bytes"], info["total_bytes"])
            # Send both.
            mgr.start(self.out, TARGET, PASSWORD, ["20261012_100000", "20261012_100100"], rate_mbps=0)
            st = wait_job(mgr)
            self.assertEqual(st["state"], "done", st)
            self.assertEqual([s["state"] for s in st["sessions"]], ["done", "done"])
            for s in (self.a, self.b):
                self.assertEqual(tree(remote.home / "teleop_datasets" / s.name), tree(s), "byte-identical copy")
            self.assertEqual(list((remote.home / "teleop_datasets" / ".incoming").iterdir()), [])
            self.assertTrue(any(p.get("state") == "sending" for p in published))
            self.assertNotIn(PASSWORD, json.dumps(published), "the password is never in a status")
            # Second run: both already there -> skipped, nothing overwritten.
            marker = remote.home / "teleop_datasets" / self.a.name / "session.json"
            marker.write_text("remote edit")
            mgr.start(self.out, TARGET, PASSWORD, [self.a.name], rate_mbps=0)
            st = wait_job(mgr)
            self.assertEqual((st["state"], st["sessions"][0]["state"]), ("done", "skipped"))
            self.assertEqual(marker.read_text(), "remote edit")

    def test_rate_limit_verification_failure_and_cancel(self):
        with FakeRemote() as remote:
            mgr = tx.Transfer()
            # Rate limit: ~1.3 MB at 4 Mbit/s takes ~2.6 s.
            big = make_session(self.out, "20261012_100200", size=1_000_000)
            t0 = time.monotonic()
            mgr.start(self.out, TARGET, PASSWORD, [big.name], rate_mbps=4)
            st = wait_job(mgr)
            self.assertEqual(st["state"], "done", st)
            self.assertGreater(time.monotonic() - t0, 2.0)
            # A corrupted copy is detected and never appears under the final name.
            real = tx.sha256_file
            with patch.object(tx, "sha256_file", lambda p: "0" * 64 if p.name == "000003.png" else real(p)):
                mgr.start(self.out, TARGET, PASSWORD, [self.a.name], rate_mbps=0)
                st = wait_job(mgr)
            self.assertEqual(st["state"], "failed")
            self.assertIn("검증 실패", st["sessions"][0]["error"])
            self.assertFalse((remote.home / "teleop_datasets" / self.a.name).exists())
            # Cancel during a slow send.
            mgr.start(self.out, TARGET, PASSWORD, [self.b.name], rate_mbps=0.5)
            time.sleep(1.0)
            self.assertTrue(mgr.cancel())
            st = wait_job(mgr)
            self.assertEqual(st["state"], "cancelled", st)
            self.assertFalse((remote.home / "teleop_datasets" / self.b.name).exists())

    def test_browse_mkdir_and_send_without_retyping(self):
        with FakeRemote() as remote:
            (remote.home / "projects" / "robot").mkdir(parents=True)
            (remote.home / ".cache").mkdir()
            (remote.home / "20261012_090000").mkdir()  # an earlier session sent to the home folder
            mgr = tx.Transfer()
            with self.assertRaises(tx.TransferError) as cm:
                mgr.browse(TARGET, "wrong", None, "~")
            self.assertEqual(cm.exception.status, 401)
            home = mgr.browse(TARGET, PASSWORD, None, "~")
            token = home["token"]
            self.assertEqual((home["path"], home["dirs"], home["sessions"], home["writable"]),
                             (str(remote.home), ["projects"], 1, True))
            sub = mgr.browse(TARGET, None, token, "projects")  # same login, no password
            self.assertEqual((sub["path"], sub["dirs"], sub["token"]), (str(remote.home / "projects"), ["robot"], token))
            for bad in ("", ".", "..", "a/b", "-rf", "x\ny"):
                with self.assertRaises(tx.TransferError):
                    mgr.mkdir(TARGET, None, token, sub["path"], bad)
            made = mgr.mkdir(TARGET, None, token, sub["path"], "datasets 1")
            self.assertEqual(made["path"], str(remote.home / "projects" / "datasets 1"))
            with self.assertRaises(tx.TransferError) as cm:
                mgr.browse(TARGET, None, token, "no/such/folder")
            self.assertEqual(cm.exception.status, 404)
            # Send into the chosen folder over the browsing connection (no password this time).
            mgr.start(self.out, dict(TARGET, remote_dir=made["path"]), None, [self.a.name], rate_mbps=0, token=token)
            st = wait_job(mgr)
            self.assertEqual(st["state"], "done", st)
            self.assertEqual(tree(remote.home / "projects" / "datasets 1" / self.a.name), tree(self.a))
            self.assertEqual(mgr.browsers, {}, "the transfer took over and closed the connection")
            # The token is gone: without a password the login fails.
            with self.assertRaises(tx.TransferError) as cm:
                mgr.browse(TARGET, None, token, "~")
            self.assertEqual(cm.exception.status, 401)
            self.assertFalse(mgr.close_browser("nope"))

    def test_targets_and_ssh_config(self):
        with FakeRemote():
            cfg = tx.parse_ssh_config("Host bai-vscode\n User work\n Hostname proxy1.example.com\n Port 10773\n"
                                      " StrictHostKeyChecking no\n UserKnownHostsFile /dev/null\n")
            self.assertEqual(cfg, {"name": "bai-vscode", "user": "work", "host": "proxy1.example.com", "port": "10773",
                                   "check_host_key": False})
            tx.save_target(dict(cfg, remote_dir="~/data"))
            tx.save_target(TARGET)
            self.assertEqual([t["name"] for t in tx.load_targets()], ["lab-gpu", "bai-vscode"])  # newest first
            self.assertNotIn("password", tx.TARGETS_FILE.read_text())
            tx.delete_target("lab-gpu")
            self.assertEqual([t["name"] for t in tx.load_targets()], ["bai-vscode"])
            for bad in ({**TARGET, "host": "a;rm -rf /"}, {**TARGET, "port": 0}, {**TARGET, "user": "$(id)"},
                        {**TARGET, "remote_dir": "a\nb"}):
                with self.assertRaises(tx.TransferError):
                    tx.clean_target(bad)
            with self.assertRaises(tx.TransferError):
                tx.Transfer.plan(self.out, ["../etc"])


class TransferApiBrowserTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="teleop_tx_ui_"))
        for i, size in enumerate((400_000, 300_000, 200_000)):
            make_session(self.out, f"20261012_10{i}000", size=size)

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    async def test_api_and_send_dialog(self):
        with FakeRemote() as remote:
            (remote.home / "projects").mkdir()
            async with AppHarness(*factories(self.out)) as h:
                code, body = await h.post("/api/transfer", action="parse_config",
                                          text="Host bai\n User work\n Hostname h.example.com\n Port 10773")
                self.assertEqual((code, body["target"]["port"]), (200, "10773"))
                code, body = await h.post("/api/transfer", action="save_target", target=TARGET)
                self.assertEqual(code, 200, body)
                r = await h.client.get("/api/transfer")
                d = await r.json()
                self.assertEqual((d["transfer"]["state"], d["targets"][0]["name"]), ("idle", "lab-gpu"))
                code, body = await h.post("/api/transfer", action="test", target=TARGET, password="nope",
                                          sessions=["20261012_100000"])
                self.assertEqual(code, 401, body)
                # A session being sent cannot be deleted.
                code, body = await h.post("/api/transfer", action="start", target=TARGET, password=PASSWORD,
                                          sessions=["20261012_100000"], rate_mbps=0.8)
                self.assertEqual(code, 200, body)
                r = await h.client.delete("/api/datasets/20261012_100000")
                self.assertEqual(r.status, 409)
                code, body = await h.post("/api/transfer", action="start", target=TARGET, password=PASSWORD,
                                          sessions=["20261012_101000"])
                self.assertEqual(code, 409, body)  # one transfer at a time
                await h.post("/api/transfer", action="cancel")
                for _ in range(100):
                    r = await h.client.get("/api/transfer")
                    if (await r.json())["transfer"]["state"] == "cancelled":
                        break
                    await asyncio.sleep(0.05)

                # Browser: select two sessions, quick-send to the saved target with the password.
                async with Chrome(1440, 1000) as c:
                    await c.open(str(h.client.make_url("/")))
                    await wait_page(c, "document.querySelector('#conn').dataset.state === 'open'")
                    await c.eval("document.querySelector('#btn-datasets').click()")
                    self.assertTrue(await wait_page(c, "document.querySelectorAll('#ds-rows tr').length === 3"))
                    self.assertTrue(await c.eval("document.querySelector('#ds-send').disabled"), "nothing selected")
                    self.assertEqual(await c.eval(TEXT.format(sel="#ds-send-quick")), "lab-gpu(으)로 바로 보내기")
                    await c.eval("[...document.querySelectorAll('#ds-rows td.sel input')].slice(0, 2).forEach(i => i.click())")
                    self.assertEqual(await c.eval(TEXT.format(sel="#ds-selected")), "선택 2개")
                    self.assertTrue(await c.eval("document.querySelector('#ds-all').indeterminate"))
                    await c.eval("document.querySelector('#ds-send-quick').click()")
                    self.assertTrue(await wait_page(c, "document.querySelector('#tx-dialog').open"))
                    self.assertEqual(await c.eval("document.querySelector('#tx-host').value"), "gpu.example.com")
                    self.assertEqual(await c.eval("document.activeElement.id"), "tx-password")
                    await c.eval(f"document.querySelector('#tx-password').value = {json.dumps(PASSWORD)}")
                    await c.eval("document.querySelector('#tx-test').click()")
                    check = await wait_page(c, "(() => { const t = " + TEXT.format(sel="#tx-check") +
                                               "; return t.startsWith('연결됨') ? t : ''; })()", 20)
                    self.assertIn("teleop_datasets", check)
                    self.assertEqual(await c.eval("document.querySelector('#tx-password').value"), "", "connected now")
                    # Choose where the datasets go by browsing the receiving computer.
                    await c.eval("document.querySelector('#tx-dir').value = ''")
                    await c.eval("document.querySelector('#tx-browse').click()")
                    self.assertTrue(await wait_page(c, "[...document.querySelectorAll('#txb-list button')]"
                                                       ".some(b => b.textContent === 'projects')", 15))
                    self.assertEqual(await c.eval("document.querySelector('#txb-path').value"), str(remote.home))
                    await c.eval("[...document.querySelectorAll('#txb-list button')].find(b => b.textContent === 'projects').click()")
                    self.assertTrue(await wait_page(c, f"document.querySelector('#txb-path').value === {json.dumps(str(remote.home / 'projects'))}"))
                    await c.eval("document.querySelector('#txb-new').value = 'robot data'")
                    await c.eval("document.querySelector('#txb-mkdir').click()")
                    target_dir = str(remote.home / "projects" / "robot data")
                    self.assertTrue(await wait_page(c, f"document.querySelector('#txb-path').value === {json.dumps(target_dir)}"))
                    await c.screenshot(os.path.join(SHOTS, "14_transfer_browse.png"), full=False)
                    await c.eval("document.querySelector('#txb-pick').click()")
                    self.assertEqual(await c.eval("document.querySelector('#tx-dir').value"), target_dir)
                    self.assertTrue(await c.eval("document.querySelector('#tx-browser').hidden"))
                    await c.eval("document.querySelector('#tx-rate').value = '0'")
                    await c.eval("document.querySelector('#tx-start').click()")  # no password typed again
                    done = await wait_page(c, "(() => { const t = " + TEXT.format(sel="#tx-numbers") +
                                              "; return t.startsWith('끝났습니다') ? t : ''; })()", 60)
                    self.assertIn("세션 2/2", done)
                    self.assertEqual(await c.eval("document.querySelector('#tx-password').value"), "", "cleared")
                    self.assertEqual(await c.eval("document.querySelectorAll('#tx-list li .status[data-s=good]').length"), 2)
                    self.assertEqual(await c.eval(TEXT.format(sel="#tx-badge")), "전송 완료 → lab-gpu (보냄 2)")
                    await c.screenshot(os.path.join(SHOTS, "14_transfer.png"), full=False)
                    self.assertEqual(c.exceptions, [])
                dest = remote.home / "projects" / "robot data"
                names = sorted(p.name for p in dest.iterdir() if not p.name.startswith("."))
                self.assertEqual(names, ["20261012_101000", "20261012_102000"])
                for n in names:
                    self.assertEqual(tree(dest / n), tree(self.out / n))


if __name__ == "__main__":
    unittest.main()
