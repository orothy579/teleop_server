"""Minimal headless Chrome driver over the DevTools protocol (no extra dependencies)."""
import asyncio
import base64
import itertools
import json
import os
import shutil
import socket
import subprocess
import tempfile

import aiohttp

CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Chrome:
    def __init__(self, width=1440, height=1600):
        self.size = (width, height)
        self.ids = itertools.count(1)
        self.pending = {}
        self.exceptions = []
        self.console = []
        self.dialogs = []
        self.accept_dialogs = True

    async def __aenter__(self):
        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="teleop-chrome-")
        self.proc = subprocess.Popen(
            [CHROME, "--headless=new", f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile}",
             "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--disable-extensions",
             f"--window-size={self.size[0]},{self.size[1]}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.http = aiohttp.ClientSession()
        for _ in range(100):
            try:
                async with self.http.get(f"http://127.0.0.1:{self.port}/json/list") as r:
                    pages = [t for t in await r.json() if t["type"] == "page"]
                if pages:
                    break
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.1)
        self.ws = await self.http.ws_connect(pages[0]["webSocketDebuggerUrl"], max_msg_size=64 << 20)
        self.reader = asyncio.create_task(self._read())
        await self.send("Runtime.enable")
        await self.send("Page.enable")
        return self

    async def __aexit__(self, *exc):
        self.reader.cancel()
        await self.ws.close()
        await self.http.close()
        self.proc.terminate()
        self.proc.wait(10)
        shutil.rmtree(self.profile, ignore_errors=True)

    async def _read(self):
        async for msg in self.ws:
            data = json.loads(msg.data)
            if "id" in data and data["id"] in self.pending:
                self.pending.pop(data["id"]).set_result(data)
            elif data.get("method") == "Runtime.exceptionThrown":
                self.exceptions.append(data["params"]["exceptionDetails"])
            elif data.get("method") == "Runtime.consoleAPICalled":
                self.console.append(data["params"])
            elif data.get("method") == "Page.javascriptDialogOpening":
                self.dialogs.append(data["params"]["message"])
                asyncio.create_task(self.send("Page.handleJavaScriptDialog", accept=self.accept_dialogs))

    async def send(self, method, **params):
        i = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[i] = fut
        await self.ws.send_str(json.dumps({"id": i, "method": method, "params": params}))
        data = await asyncio.wait_for(fut, 30)
        if "error" in data:
            raise RuntimeError(data["error"])
        return data["result"]

    async def open(self, url):
        await self.send("Page.navigate", url=url)

    async def eval(self, expr):
        res = await self.send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in res:
            raise RuntimeError(res["exceptionDetails"])
        return res["result"].get("value")

    async def screenshot(self, path, full=True):
        res = await self.send("Page.captureScreenshot", format="png", captureBeyondViewport=full)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(base64.b64decode(res["data"]))
        return path
