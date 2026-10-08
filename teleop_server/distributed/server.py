"""DGX receiver/browser server. Opens no local robot, camera or ROS device."""
import argparse
import asyncio
import hmac
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import cv2
import numpy as np
from aiohttp import WSMsgType, web

from teleop_server import server as original
from teleop_server.hub import Client, Hub
from teleop_server.latency import StatsRegistry
from teleop_server.recorder import RecorderError
from teleop_server.sources.arms import Cycle
from teleop_server.sources.camera import CameraFrame, depth_preview
from .protocol import GAP_LOG_S, LINK_TIMEOUT_S, MAX_PACKET, decode_packet
from .recordings import JetsonRecordings

log = logging.getLogger(__name__)
BRIDGE = web.AppKey("edge_bridge", object)


class RemoteSource:
    def __init__(self, name, kind, rate=30):
        self.name, self.kind, self.rate = name, kind, rate
        self.state, self.error, self.info = "idle", "Jetson 연결 대기", {}
        self.depth_enabled = True
        self.unpaired = 0
        self.frame = None
        self.teleop = True
        self.cycle_sink = None
        self.exit_hooks = []

    def status(self):
        return dict(name=self.name, kind=self.kind, state=self.state, error=self.error, info=self.info)

    def latest_frame(self, paired=False):
        return self.frame if self.frame is not None and (not paired or self.frame.depth is not None) else None

    def unpaired_recent_pct(self):
        return self.info.get("unpaired_recent_pct")


def prepare_packet(data, with_depth_preview=True):
    meta, decoded = decode_packet(data)
    result = {}
    for name, (rgb, depth, descriptor) in decoded.items():
        ok, jpg = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            raise ValueError("preview encoding failed")
        preview = None
        if depth is not None and with_depth_preview:
            ok, dj = cv2.imencode(".jpg", depth_preview(depth, 1.0), [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                preview = dj.tobytes()
        result[name] = (rgb, depth, descriptor, jpg.tobytes(), preview)
    return meta, result


class Bridge:
    def __init__(self, token, fps=30, depth_preview_hz=10.):
        self.token = token
        # RGB goes to browsers at the full capture rate; the depth preview is only a visual check.
        self.depth_preview_period = 1/depth_preview_hz
        self.last_depth_preview = 0.
        self.stats = StatsRegistry()
        self.hub = Hub(self.stats)
        self.sources = {f"camera{i}": RemoteSource(f"camera{i}", "camera") for i in range(1, 4)}
        self.sources.update(arms=RemoteSource("arms", "arms", fps), odom=RemoteSource("odom", "odom"))
        self.ws = self.data_ws = None
        self.boot = self.session = None
        self.synthetic = False
        self.rgb_codec = None
        self.offset_ns = 0
        self.rtt_ns = 10**18
        self.clock_samples = deque(maxlen=7)
        self.clock_updated = 0.0
        # Trossen's native connect/disconnect calls can hold the Jetson GIL for several seconds.
        # Keep the two WebSockets alive only while that deliberate arm transition is in progress;
        # normal operation continues to use the tight control-link deadline below.
        self.control_grace_until = 0.0
        self.control_transition_edge_ns = 0
        self.control_transition_action = None
        self.last_data = self.last_control = 0.
        self.sequence = None
        self.received = self.wire_bytes = 0
        self.pending = {}
        self.base_sample = {}
        self.base_status = {"type": "base_ctl", "enabled": False, "active": False, "owner": None,
                            "max_linear": 0.3, "max_angular": 0.6, "cmd": [0, 0], "topic": "/cmd_vel"}
        self.recorder = None
        self.transport = {}
        self.status_ns = 0
        self.link = {"timeout_s": LINK_TIMEOUT_S, "gaps": 0, "max_gap_s": 0.0, "last_gap": None, "closed": None}
        self.edge_link = None    # the Jetson's view (DGX -> Jetson gaps), from its pongs
        self.last_pong_edge_ns = 0

    @property
    def connected(self):
        timeout = 60.0 if self.in_control_transition else LINK_TIMEOUT_S
        return self.ws is not None and not self.ws.closed and time.monotonic()-self.last_control < timeout

    @property
    def in_control_transition(self):
        return time.monotonic() < self.control_grace_until

    def authorize(self, request):
        if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer "+self.token):
            raise web.HTTPUnauthorized()

    def update_sources(self, values, stamp_ns):
        # Pongs and pipelined data packets arrive interleaved; a packet queued before e.g. control
        # start must not roll the state back. Both stamps are the Jetson clock at status time.
        if stamp_ns < self.status_ns:
            return
        self.status_ns = stamp_ns
        for item in values:
            source = self.sources.get(item.get("name"))
            if source is not None:
                source.state, source.error, source.info = item["state"], item.get("error"), dict(item.get("info", {}))
                source.unpaired = source.info.get("unpaired_framesets", 0)
                self.hub.publish("status:"+source.name, dict(type="status", **source.status()))
        # Do not let a packet queued before the command end the grace period. The first stable arm
        # status sampled after the command proves the Jetson event loop is responsive again.
        arms = self.sources["arms"]
        finished_states = (("running", "error", "stopped") if self.control_transition_action == "control_start"
                           else ("error", "stopped"))
        if (self.in_control_transition and stamp_ns >= self.control_transition_edge_ns and
                arms.state in finished_states):
            self.control_grace_until = 0.0
            self.control_transition_edge_ns = 0
            self.control_transition_action = None

    async def call(self, action, **kwargs):
        if not self.connected:
            raise RuntimeError("Jetson 연결이 끊겼습니다.")
        # A user can click immediately after reconnect. Wait briefly for fresh clock samples so a
        # host clock step during NTP convergence cannot make a current command look expired.
        deadline = time.monotonic()+1.5
        while (len(self.clock_samples) < 3 or time.monotonic()-self.clock_updated > 0.6) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if len(self.clock_samples) < 3 or time.monotonic()-self.clock_updated > 0.6:
            raise RuntimeError("Jetson 시계 동기화 중입니다. 잠시 후 다시 시도하세요.")
        ident = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        transition = action in ("control_start", "control_stop")
        issued_ns = time.time_ns()-self.offset_ns
        if transition:
            self.control_grace_until = time.monotonic()+60.0
            self.control_transition_edge_ns = issued_ns
            self.control_transition_action = action
        try:
            # issued_ns is in Jetson clock, estimated using minimum RTT control pings.
            await self.ws.send_json(dict(type="command", id=ident, action=action,
                                         issued_ns=issued_ns, **kwargs))
            result = await asyncio.wait_for(future, 60.0 if transition else 2.0)
            if not result["ok"]:
                if transition:
                    self.control_grace_until = 0.0
                    self.control_transition_edge_ns = 0
                    self.control_transition_action = None
                raise RuntimeError(result.get("error", "command rejected"))
            return result
        finally:
            self.pending.pop(ident, None)

    def disconnected(self, reason):
        self.control_grace_until = 0.0
        self.control_transition_edge_ns = 0
        self.control_transition_action = None
        for source in self.sources.values():
            source.state, source.error = "error", reason
            source.frame = None
            self.hub.publish("status:"+source.name, dict(type="status", **source.status()))
        self.base_status.update(enabled=False, active=False, owner=None)
        self.hub.publish("base_ctl", dict(self.base_status))
        self.base_sample = {}
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(RuntimeError(reason))

    async def control(self, request):
        self.authorize(request)
        if self.ws is not None and not self.ws.closed:
            raise web.HTTPConflict(text="An edge is already connected")
        ws = web.WebSocketResponse(heartbeat=120, max_msg_size=1<<20)
        await ws.prepare(request)
        self.ws = ws
        task = None
        try:
            hello = await asyncio.wait_for(ws.receive_json(), 3)
            if hello.get("type") != "hello":
                raise ValueError("expected hello")
            self.boot, self.session = hello["boot"], hello["session"]
            self.synthetic, self.rgb_codec = bool(hello["synthetic"]), hello["rgb_codec"]
            if hello["fps"] != self.sources["arms"].rate:
                raise ValueError("edge/DGX fps mismatch")
            self.sequence = None
            self.last_pong_edge_ns = 0
            self.rtt_ns = 10**18
            self.clock_samples.clear()
            self.clock_updated = 0.0
            self.control_grace_until = 0.0
            self.control_transition_edge_ns = 0
            self.control_transition_action = None
            self.last_control = time.monotonic()
            self.status_ns = 0
            self.update_sources(hello["sources"], 0)
            async def ping():
                while True:
                    await ws.send_json(dict(type="ping", t0=time.time_ns()))
                    await asyncio.sleep(0.2)
            task = asyncio.create_task(ping())
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                data = json.loads(message.data)
                now_mono = time.monotonic()
                if now_mono-self.last_control > GAP_LOG_S:
                    self.note_gap(now_mono-self.last_control, data)
                self.last_control = now_mono
                if data.get("type") == "pong":
                    self.last_pong_edge_ns = data["edge_ns"]
                    self.edge_link = data.get("link")
                    now = time.time_ns()
                    self.observe_clock(data["t0"], data["edge_ns"], now)
                    self.update_sources(data["sources"], data["edge_ns"])
                    self.base_status = data["base"]
                    self.hub.publish("base_ctl", dict(self.base_status))
                    if self.recorder is not None:
                        self.recorder.on_jetson(data.get("recorder"))
                elif data.get("type") == "result":
                    future = self.pending.get(data.get("id"))
                    if future is not None and not future.done():
                        future.set_result(data)
        except (ValueError, KeyError, asyncio.TimeoutError):
            log.exception("Invalid edge control session")
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.ws = None
            self.disconnected("Jetson control link disconnected")
            if self.data_ws:
                await self.data_ws.close()
        return ws

    def note_gap(self, gap, data):
        """A Jetson -> DGX control gap. The late pong tells which side stalled: a normal Jetson send
        interval means the network held it back; a long one means the ping (DGX -> Jetson) or the
        Jetson itself was late."""
        detail = {"gap_s": round(gap, 3), "unix_ns": time.time_ns(), "arm_transition": self.in_control_transition}
        text = ""
        if data.get("type") == "pong" and self.last_pong_edge_ns:
            detail["jetson_send_interval_s"] = round((data["edge_ns"]-self.last_pong_edge_ns)/1e9, 3)
            detail["round_trip_ms"] = round((time.time_ns()-data["t0"])/1e6, 1)
            text = (f"; Jetson sent this pong {detail['jetson_send_interval_s']:.2f} s after the previous one,"
                    f" ping->pong round trip {detail['round_trip_ms']:.0f} ms")
        self.link["gaps"] += 1
        self.link["max_gap_s"] = round(max(self.link["max_gap_s"], gap), 3)
        self.link["last_gap"] = detail
        log.warning("Jetson -> DGX control gap %.2f s (limit %.1f s)%s%s", gap, LINK_TIMEOUT_S, text,
                    " during an arm transition" if self.in_control_transition else "")

    def control_expired(self):
        """The control link is open but silent past the limit: the Jetson's own watchdog stops the
        arms at the same limit; end the session here too."""
        return (self.ws is not None and not self.ws.closed and not self.in_control_transition
                and time.monotonic()-self.last_control >= LINK_TIMEOUT_S)

    async def close_expired(self, where):
        silent = time.monotonic()-self.last_control
        self.link["closed"] = {"silent_s": round(silent, 3), "unix_ns": time.time_ns(), "noticed_by": where}
        log.error("Jetson control link silent for %.2f s (limit %.1f s): closing the session (Jetson stops "
                  "arm control and base)", silent, LINK_TIMEOUT_S)
        await self.ws.close()

    def observe_clock(self, t0_ns, edge_ns, now_ns=None):
        """Track recent clock offset continuously; host NTP can slew or step after boot."""
        now_ns = now_ns or time.time_ns()
        rtt = now_ns-t0_ns
        if not 0 <= rtt <= 1_000_000_000:
            return
        offset = (now_ns+t0_ns)//2-edge_ns
        self.clock_samples.append((rtt, offset))
        offsets = sorted(sample[1] for sample in self.clock_samples)
        self.offset_ns = offsets[len(offsets)//2]
        self.rtt_ns = min(sample[0] for sample in self.clock_samples)
        self.clock_updated = time.monotonic()

    async def data(self, request):
        self.authorize(request)
        if self.data_ws is not None and not self.data_ws.closed:
            raise web.HTTPConflict(text="An edge data stream is already connected")
        ws = web.WebSocketResponse(heartbeat=120, max_msg_size=MAX_PACKET, compress=False)
        await ws.prepare(request)
        self.data_ws = ws
        try:
            hello = await asyncio.wait_for(ws.receive_json(), 3)
            if not self.connected or hello.get("boot") != self.boot or hello.get("session") != self.session:
                raise ValueError("control session mismatch")
            async for message in ws:
                if message.type != WSMsgType.BINARY:
                    continue
                preview = time.monotonic()-self.last_depth_preview >= self.depth_preview_period
                if preview:
                    self.last_depth_preview = time.monotonic()
                meta, prepared = await asyncio.to_thread(prepare_packet, message.data, preview)
                if meta["boot"] != self.boot or not self.connected:
                    raise ValueError("expired edge session (control link "
                                     f"silent {time.monotonic()-self.last_control:.2f} s)")
                self.receive(meta, prepared)
                self.wire_bytes += len(message.data)
                await ws.send_json(dict(sequence=meta["sequence"]))
        except (ValueError, KeyError, asyncio.TimeoutError) as exc:
            log.warning("Jetson data link closed: %s", exc)
        finally:
            self.data_ws = None
            # A data-only failure leaves robot control alone (the Jetson reconnects the stream);
            # an expired control link ends the session.
            if self.control_expired():
                await self.close_expired("data link")
        return ws

    def receive(self, meta, prepared):
        now = time.time_ns()
        self.last_data = time.monotonic()
        self.sequence = meta["sequence"]
        self.received += 1
        self.transport = dict(encode_ms=meta.get("encode_ms"), dropped=meta.get("dropped"),
                              queue=meta.get("queue"), inflight=meta.get("inflight"),
                              cameras=[{k: c.get(k) for k in ("name", "rgb_bytes", "depth_bytes", "depth_codec", "encode_ms")}
                                       for c in meta["cameras"]])
        self.update_sources(meta["sources"], meta["sample_unix_ns"])
        self.base_sample = meta["base"]  # recordings are made on the Jetson; this is only the live view
        frames = {}
        for name, (rgb, depth, descriptor, jpeg, preview) in prepared.items():
            source = self.sources[name]
            frame = CameraFrame(rgb, depth, descriptor["t_recv_ns"]+self.offset_ns,
                                descriptor["seq"], descriptor["frame_number"], descriptor["depth_dt_ms"])
            source.frame = frames[name] = frame
            source.info.update(descriptor["info"])
            capture = descriptor["info"].get("capture", {})
            header = dict(capture)
            header.update(type="camera", sensor=name, seq=frame.seq,
                          t_capture_ns=capture.get("t_capture_ns", descriptor["t_recv_ns"])+self.offset_ns,
                          t_recv_ns=now, capture_clock="Jetson receive (clock corrected)",
                          width=rgb.shape[1], height=rgb.shape[0], bytes=len(jpeg),
                          depth_dt_ms=frame.depth_dt_ms, frame_number=frame.frame_number,
                          t_edge_recv_ns=descriptor["t_recv_ns"])
            self.stats.get(name).record({"edge_to_dgx_ms": (now-frame.t_recv_ns)/1e6}, now)
            self.hub.publish("cam:"+name, header, jpeg)
            if preview:
                self.hub.publish("depth:"+name, dict(type="depth_preview", camera=name, t_recv_ns=now,
                                                      valid_fraction=float((depth>0).mean()), range_m=[0.05, 1.]), preview)
        for message in meta["messages"]:
            kind = message.get("type")
            if kind not in ("arm", "odom"):
                continue
            header = dict(message)
            sensor = header["sensor"]
            edge_recv = header["t_recv_ns"]
            if now-(edge_recv+self.offset_ns) > 500_000_000:
                continue
            header["t_edge_recv_ns"] = edge_recv
            header["t_recv_ns"] = now
            if header.get("t_source_ns"):
                header["t_source_ns"] += self.offset_ns
            self.stats.get(sensor).record({"edge_to_dgx_ms": (now-edge_recv-self.offset_ns)/1e6}, now)
            self.hub.publish(("arm:"+sensor) if kind == "arm" else "odom", header)

    def status(self):
        return dict(connected=self.connected, boot=self.boot, received=self.received, wire_bytes=self.wire_bytes,
                    capture_sequence=self.sequence, rgb_codec=self.rgb_codec, synthetic=self.synthetic,
                    last_data_age_ms=(time.monotonic()-self.last_data)*1000 if self.last_data else None,
                    clock_offset_ms=self.offset_ns/1e6, clock_rtt_ms=self.rtt_ns/1e6,
                    sources=[s.status() for s in self.sources.values()], transport=self.transport,
                    link=dict(self.link, jetson=self.edge_link))


async def control_api(request):
    bridge = request.app[BRIDGE]
    action = (await original._body(request)).get("action")
    if action not in ("start", "stop"):
        return original._error(400, "action must be start or stop")
    try:
        if action == "stop" and bridge.recorder.state == "recording":
            await bridge.recorder.stop("arm control stopped")  # save the recording before the arms move
        await bridge.call("control_"+action)
        return web.json_response(dict(ok=True, arms=bridge.sources["arms"].status()))
    except (RuntimeError, asyncio.TimeoutError) as exc:
        return original._error(409, str(exc) or "Jetson command timeout")


async def record_api(request):
    """Recording runs on the Jetson; the session reaches the DGX after stop."""
    rec, body = request.app[BRIDGE].recorder, await original._body(request)
    try:
        if body.get("action") == "start":
            return web.json_response({"ok": True, "record": await rec.start(body.get("task"))})
        if body.get("action") == "stop":
            if not await rec.stop("user"):
                return original._error(409, "녹화 중이 아닙니다.")
            return web.json_response({"ok": True, "record": rec.status()})
    except RecorderError as exc:
        return original._error(409, str(exc))
    return original._error(400, "action은 start 또는 stop이어야 합니다.")


async def base_api(request):
    bridge = request.app[BRIDGE]
    action = (await original._body(request)).get("action")
    if action not in ("enable", "disable"):
        return original._error(400, "action must be enable or disable")
    try:
        await bridge.call("base_"+action)
        return web.json_response(dict(ok=True, base=bridge.base_status))
    except (RuntimeError, asyncio.TimeoutError) as exc:
        return original._error(409, str(exc) or "Jetson command timeout")


async def browser(request):
    bridge = request.app[BRIDGE]
    ws = web.WebSocketResponse(heartbeat=10, max_msg_size=1<<20, compress=False)
    await ws.prepare(request)
    client = Client(ws)
    owner = uuid.uuid4().hex
    sender = asyncio.create_task(bridge.hub.serve(client))
    try:
        await ws.send_json(dict(type="hello", server_time_ns=time.time_ns(),
                                sources=[s.status() for s in bridge.sources.values()],
                                record=bridge.recorder.status(), base=bridge.base_status, client_id=owner))
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            data = json.loads(message.data)
            kind = data.get("type")
            if kind == "ping":
                await ws.send_json(dict(type="pong", t0=data["t0"], server_ns=time.time_ns()))
            elif kind == "ack":
                client.ack(data["key"])
            elif kind == "client_report":
                client.report = data
            elif kind in ("base_cmd", "base_release"):
                try:
                    await bridge.call(kind, owner=owner, vx=data.get("vx"), wz=data.get("wz"))
                except (RuntimeError, asyncio.TimeoutError) as exc:
                    await ws.send_json(dict(type="base_reject", reason=str(exc)))
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        bridge.hub.clients.discard(client)
        if bridge.connected:
            try:
                await bridge.call("base_release", owner=owner)
            except Exception:
                pass
    return ws


def create_app(token, output_dir, fps=30, validate=True):
    app = web.Application(client_max_size=MAX_PACKET)
    bridge = Bridge(token, fps)
    bridge.recorder = JetsonRecordings(bridge, output_dir, validate)
    app[BRIDGE] = bridge
    app[original.STATS], app[original.HUB] = bridge.stats, bridge.hub
    app[original.SOURCES] = list(bridge.sources.values())
    app[original.RECORDER], app[original.BASE] = bridge.recorder, None
    async def background():
        data_stalled, tick = False, 0
        while True:
            await asyncio.sleep(0.5)
            tick += 1
            if bridge.control_expired():
                await bridge.close_expired("monitor")
            # No camera/telemetry data: the browser shows it per sensor; robot control is unaffected.
            stalled = bridge.connected and not bridge.in_control_transition and time.monotonic()-bridge.last_data > 1
            if stalled and not data_stalled:
                log.warning("no Jetson sensor data for %.1f s", time.monotonic()-bridge.last_data)
            data_stalled = stalled
            if tick % 2:
                continue  # stats once per second
            bridge.hub.publish("stats", dict(type="stats", server_time_ns=time.time_ns(),
                                             sensors=bridge.stats.summary(), sources=[s.status() for s in bridge.sources.values()]))
    async def startup(app):
        bridge.hub.bind(asyncio.get_running_loop())
        app[original.STATS_TASK] = asyncio.create_task(background())
        app[original.START_TASK] = asyncio.create_task(asyncio.to_thread(original._preload_recording))
    async def shutdown(app):
        # A Jetson recording keeps running and its upload resumes after restart; an interrupted
        # conversion is redone from the verified files in .incoming/.
        if bridge.ws:
            await bridge.ws.close()
        if bridge.data_ws:
            await bridge.data_ws.close()
        for client in list(bridge.hub.clients):
            await client.ws.close()
        app[original.STATS_TASK].cancel()
        await asyncio.gather(app[original.STATS_TASK], return_exceptions=True)
        await asyncio.to_thread(bridge.recorder.wait, 120)
    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    app.router.add_get("/", original.index)
    app.router.add_static("/static", original.STATIC)
    app.router.add_get("/ws", browser)
    app.router.add_get("/api/stats", original.api_stats)
    async def edge_status(request):
        return web.json_response(bridge.status())
    app.router.add_get("/api/edge", edge_status)
    app.router.add_post("/api/control", control_api)
    app.router.add_post("/api/base", base_api)
    app.router.add_post("/api/record", record_api)
    app.router.add_get("/api/sessions", original.api_sessions)
    app.router.add_post("/api/label", original.api_label)
    app.router.add_get("/edge/control", bridge.control)
    app.router.add_get("/edge/data", bridge.data)
    app.router.add_put("/edge/upload/{session}/{file}", bridge.recorder.upload_file)
    app.router.add_post("/edge/upload/{session}/commit", bridge.recorder.upload_commit)
    return app


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--record-dir", type=Path, default=Path(__file__).resolve().parents[2]/"dataset")
    p.add_argument("--no-validate", action="store_true")
    args = p.parse_args()
    token = os.environ.get("TELEOP_EDGE_TOKEN")
    if not token:
        p.error("TELEOP_EDGE_TOKEN is required")
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(token, args.record_dir, args.fps, not args.no_validate),
                host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
