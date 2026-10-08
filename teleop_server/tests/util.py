import asyncio
import json
import os
import time

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from teleop_server.hub import decode_binary
from teleop_server.server import create_app

HW = os.environ.get("TELEOP_HW") == "1"


class AppHarness:
    def __init__(self, factory, recorder_factory=None, base_factory=None):
        self.app = create_app(factory, recorder_factory, base_factory)

    async def __aenter__(self):
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        return self

    async def __aexit__(self, *exc):
        await self.client.close()

    async def collect(self, seconds, on_text=None, ack=True):
        """Collect websocket messages for ``seconds``; returns (camera frames, text messages).
        Depth previews go to ``self.previews``."""
        frames, texts = [], []
        self.previews = []
        ws = await self.client.ws_connect("/ws")
        end = time.monotonic() + seconds
        try:
            while (left := end - time.monotonic()) > 0:
                try:
                    msg = await ws.receive(timeout=left)
                except asyncio.TimeoutError:
                    break
                now = time.time_ns()
                if msg.type in (WSMsgType.BINARY, WSMsgType.TEXT) and ack:
                    key = (decode_binary(msg.data)[0] if msg.type == WSMsgType.BINARY else json.loads(msg.data)).get("key")
                    if key:
                        await ws.send_str(json.dumps({"type": "ack", "key": key}))
                if msg.type == WSMsgType.BINARY:
                    header, jpg = decode_binary(msg.data)
                    header["t_client_ns"] = now
                    if header.get("type") == "depth_preview":
                        self.previews.append((header, jpg))
                    else:
                        frames.append((header, jpg))
                elif msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    data["t_client_ns"] = now
                    texts.append(data)
                    if on_text:
                        await on_text(ws, data)
                else:
                    break
        finally:
            await ws.close()
        return frames, texts

    async def stats(self):
        resp = await self.client.get("/api/stats")
        return await resp.json()

    async def post(self, path, **body):
        resp = await self.client.post(path, json=body)
        return resp.status, await resp.json()

    async def wait_for(self, predicate, timeout, interval=0.1):
        """Poll /api/stats until predicate(stats) is truthy; returns the last stats."""
        end = time.monotonic() + timeout
        while True:
            stats = await self.stats()
            if predicate(stats) or time.monotonic() > end:
                return stats
            await asyncio.sleep(interval)

SHOTS = os.environ.get("TELEOP_SHOTS", "/tmp/teleop_server_shots")

# Browser-side summary of every sensor, read from the page's debug handle.
PAGE_SUMMARY = """(() => { const o = {}; for (const [k, s] of Object.entries(window.__teleop.S)) {
  o[k] = {drawn: s.drawn, hz: s.arrivals.rate(), e2e: s.e2e.stats(), network: s.network.stats(), render: s.render.stats(),
          status: s.row.querySelector('.status span').textContent}; }
  return {sensors: o, offset: window.__teleop.clock.offset, rtt: window.__teleop.clock.rtt,
          conn: document.querySelector('#conn').dataset.state}; })()"""


async def browse(harness, seconds, shot=None, width=1440, height=1700, query=""):
    from teleop_server.tests.browser import Chrome
    async with Chrome(width, height) as chrome:
        await chrome.open(str(harness.client.make_url("/")) + query)
        await asyncio.sleep(seconds)
        summary = await chrome.eval(PAGE_SUMMARY)
        if shot:
            await chrome.screenshot(os.path.join(SHOTS, shot))
        summary["exceptions"] = chrome.exceptions
        summary["console_errors"] = [c for c in chrome.console if c.get("type") in ("error", "warning")]
        return summary
