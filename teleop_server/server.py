"""Teleoperation web app: cameras, arm proprioception, base odometry, latency, control and recording.

Run from /home/midbar/ws/teleop_server:
    /home/midbar/ws/lerobot_trossen/.venv/bin/python -m teleop_server.server --cameras fake --arms fake --odom fake
"""
import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from teleop_server.hub import Client, Hub
from teleop_server.latency import StatsRegistry

STATIC = Path(__file__).parent / "static"
DEFAULT_SERIALS = ["260322273312", "260522278645", "260322272677"]
log = logging.getLogger("teleop_server")
STATS, HUB = web.AppKey("stats", StatsRegistry), web.AppKey("hub", Hub)
SOURCES, STATS_TASK = web.AppKey("sources", list), web.AppKey("stats_task", asyncio.Task)
START_TASK = web.AppKey("start_task", asyncio.Task)
RECORDER = web.AppKey("recorder", object)
BASE = web.AppKey("base", object)
REPO = Path(__file__).resolve().parents[1]
DATASET_DIR = REPO / "dataset"  # recordings, one folder per session named by start time
SYNTHETIC_DIR = Path("/tmp/teleop_server_synthetic")  # fake-sensor recordings never land in DATASET_DIR
HF_CACHE = REPO / ".cache" / "huggingface"


def build_sources(args, hub, stats):
    sources = []
    if args.cameras == "realsense":
        from teleop_server.sources.camera import RealSenseCamera
        for i, serial in enumerate(args.serials):
            sources.append(RealSenseCamera(f"camera{i + 1}", hub, stats, serial, fps=args.fps,
                                           jpeg_quality=args.jpeg_quality, scale=args.scale, depth=args.depth))
    elif args.cameras == "fake":
        from teleop_server.sources.camera import FakeCamera
        for i in range(3):
            sources.append(FakeCamera(f"camera{i + 1}", hub, stats, fps=args.fps,
                                      jpeg_quality=args.jpeg_quality, scale=args.scale, depth=args.depth))
    if args.arms != "off":
        from teleop_server.sources.arms import build_arm_source
        sources.append(build_arm_source(args, hub, stats))
    if args.odom != "off":
        from teleop_server.sources.odom import build_odom_source
        sources.append(build_odom_source(args, hub, stats))
    return sources


def build_recorder(args, hub, sources):
    """Recorder in the mobile_capture.py format; needs the arm source and three cameras."""
    from teleop_server.recorder import FakeBase, Recorder, collector_modules
    arms = next((s for s in sources if s.kind == "arms"), None)
    cams = [s for s in sources if s.kind == "camera"]
    if arms is None or len(cams) != 3:
        return None
    if args.odom == "ros":
        def base_factory(path):
            return collector_modules()[0].BaseSubscriber(args.odom_topic, args.cmd_topic, path)
    elif args.odom == "fake":
        base_factory = FakeBase
    else:
        base_factory = None
    real = args.cameras == "realsense" and args.arms == "teleop" and args.odom == "ros"
    manifest = {"odom_topic": args.odom_topic, "cmd_topic": args.cmd_topic,
                "serials": args.serials if args.cameras == "realsense" else None,
                "ips": {"follower_left": args.follower_left, "follower_right": args.follower_right,
                        "leader_left": args.leader_left, "leader_right": args.leader_right}}
    output_dir = args.record_dir or (DATASET_DIR if real else SYNTHETIC_DIR)
    return Recorder(hub, arms, cams, base_factory, fps=args.fps, synthetic=not real, output_dir=output_dir,
                    default_task=args.task, manifest_extra=manifest, validate=not args.no_validate,
                    depth=args.depth, depth_min=args.depth_min, depth_max=args.depth_max,
                    depth_preset=args.depth_preset)


def build_base_control(args, hub, sources):
    """Virtual base controller publishing Twist on --cmd-topic (the topic the recorder subscribes)."""
    from teleop_server.base_control import BaseController, FakeTwistPublisher, RosTwistPublisher
    mode = args.base_control
    if mode == "auto":
        mode = {"ros": "ros", "fake": "fake"}.get(args.odom, "off")
    if mode == "off":
        return None
    if mode == "ros":
        publisher = RosTwistPublisher(args.cmd_topic)
    else:
        odom = next((s for s in sources if s.kind == "odom" and getattr(s, "driven", False)), None)
        publisher = FakeTwistPublisher(odom.command if odom else None)
    return BaseController(hub, publisher, max_linear=args.base_max_linear, max_angular=args.base_max_angular)


def _error(status, message):
    return web.json_response({"ok": False, "error": message}, status=status)


async def _body(request):
    try:
        return await request.json()
    except Exception:
        return {}


def _arms(app):
    return next((s for s in app[SOURCES] if s.kind == "arms"), None)


async def api_control(request):
    """Start/stop robot control: arm teleoperation and the virtual base controller together.
    Start MOVES the arms (staged pose) and lets a held joystick move the base; stop runs staged -> sleep."""
    app, action = request.app, (await _body(request)).get("action")
    arms = _arms(app)
    if arms is None:
        return _error(400, "팔 소스가 꺼져 있습니다. 서버를 --arms teleop으로 실행하세요.")
    if action == "start":
        starting = [s.name for s in app[SOURCES] if s.kind == "camera" and s.state in ("idle", "starting")]
        if starting:
            return _error(409, f"카메라가 아직 시작 중입니다: {', '.join(starting)}")
        try:
            arms.start_control()
        except RuntimeError as exc:
            return _error(409, str(exc))
        if app[BASE] is not None:
            app[BASE].enable()
    elif action == "stop":
        if app[BASE] is not None:
            app[BASE].disable("arm control stopped")  # one button stops everything
        if arms.state not in ("starting", "running"):
            return _error(409, "팔 제어가 실행 중이 아닙니다.")
        if app[RECORDER] is not None:
            app[RECORDER].request_stop("arm control stopped")  # before the disconnect motion
        arms.stop_control()
    else:
        return _error(400, "action은 start 또는 stop이어야 합니다.")
    return web.json_response({"ok": True, "arms": arms.status()})


async def api_record(request):
    from teleop_server.recorder import RecorderError
    app, body = request.app, await _body(request)
    rec = app[RECORDER]
    if rec is None:
        return _error(400, "녹화를 사용할 수 없습니다. 카메라 3대와 팔 소스가 필요합니다.")
    if body.get("action") == "start":
        try:
            status = await asyncio.get_running_loop().run_in_executor(None, rec.start, body.get("task"))
        except RecorderError as exc:
            return _error(409, str(exc))
        return web.json_response({"ok": True, "record": status})
    if body.get("action") == "stop":
        if not rec.request_stop("user"):
            return _error(409, "녹화 중이 아닙니다.")
        return web.json_response({"ok": True, "record": rec.status()})
    return _error(400, "action은 start 또는 stop이어야 합니다.")


async def api_sessions(request):
    rec = request.app[RECORDER]
    if rec is None:
        return web.json_response({"sessions": []})
    from teleop_server.labels import list_sessions
    try:
        limit = max(1, min(100, int(request.query.get("limit", 20))))
    except ValueError:
        limit = 20
    sessions = await asyncio.get_running_loop().run_in_executor(None, list_sessions, rec.output_dir, limit)
    return web.json_response({"sessions": sessions})


async def api_label(request):
    """Save human labels (outcome, task name, environment, operator, notes) for a finished session."""
    from teleop_server.labels import LabelError, save_labels
    app, body = request.app, await _body(request)
    rec = app[RECORDER]
    if rec is None:
        return _error(400, "녹화를 사용할 수 없습니다.")
    current = rec.session
    if current is not None and current.root.name == body.get("session"):
        return _error(409, "저장이 끝난 뒤에 라벨을 입력하세요.")  # the recorder rewrites session.json
    try:
        labels = await asyncio.get_running_loop().run_in_executor(None, save_labels, rec.output_dir,
                                                                  body.get("session"), body)
    except LabelError as exc:
        return _error(exc.status, str(exc))
    app[HUB].publish("labels", {"type": "labels", "session": body["session"], "labels": labels})
    return web.json_response({"ok": True, "labels": labels})


def _datasets_dir(request):
    from teleop_server.labels import LabelError
    rec = request.app[RECORDER]
    if rec is None:
        raise LabelError("녹화를 사용할 수 없습니다.")
    return rec.output_dir


def _run(fn, *args):
    return asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _dataset_handler(fn):
    """Dataset browser handlers: LabelError (bad name, missing session/file) -> JSON error."""
    async def handler(request):
        from teleop_server.labels import LabelError
        try:
            return await fn(request)
        except LabelError as exc:
            return _error(exc.status, str(exc))
    return handler


@_dataset_handler
async def api_datasets(request):
    """Dataset browser: every recorded session, newest first."""
    from teleop_server.datasets import list_all
    return web.json_response({"ok": True, "sessions": await _run(list_all, _datasets_dir(request))})


@_dataset_handler
async def api_dataset(request):
    from teleop_server.datasets import detail
    data = await _run(detail, _datasets_dir(request), request.match_info["session"])
    return web.json_response({"ok": True, "session": data})


@_dataset_handler
async def api_dataset_delete(request):
    """Move a session to <dataset>/.trash (recoverable). Never the one being recorded or saved."""
    from teleop_server.datasets import trash
    name, rec = request.match_info["session"], request.app[RECORDER]
    current = getattr(rec, "session", None)
    if current is not None and current.root.name == name:
        return _error(409, "녹화·저장 중인 세션은 삭제할 수 없습니다.")
    if name in request.app[TRANSFER].busy_sessions():
        return _error(409, "보내는 중인 세션은 삭제할 수 없습니다.")
    target = await _run(trash, _datasets_dir(request), name)
    log.info("session %s moved to %s", name, target)
    request.app[HUB].publish("labels", {"type": "labels", "session": name, "deleted": True})
    return web.json_response({"ok": True, "trash": str(target)})


@_dataset_handler
async def api_dataset_video(request):
    from teleop_server.datasets import video_path
    path = await _run(video_path, _datasets_dir(request), request.match_info["session"],
                      request.match_info["camera"])
    return web.FileResponse(path, headers={"Content-Type": "video/mp4", "Cache-Control": "no-cache"})


@_dataset_handler
async def api_dataset_depth(request):
    from teleop_server.datasets import depth_jpeg
    try:
        index = int(request.match_info["index"])
    except ValueError:
        return _error(400, "잘못된 프레임 번호입니다.")
    jpg, info = await _run(depth_jpeg, _datasets_dir(request), request.match_info["session"],
                           request.match_info["camera"], index)
    return web.Response(body=jpg, content_type="image/jpeg",
                        headers={"Cache-Control": "max-age=3600", "X-Depth-Valid": str(info["valid_fraction"]),
                                 "X-Depth-Median-M": "" if info["median_m"] is None else str(info["median_m"])})


TRANSFER = web.AppKey("transfer", object)
PREVIEWS = web.AppKey("depth_previews", object)


@_dataset_handler
async def api_depth_preview(request):
    """GET: status of the session's depth preview videos; POST: also start making missing ones."""
    previews, name = request.app[PREVIEWS], request.match_info["session"]
    fn = previews.ensure if request.method == "POST" else previews.status
    return web.json_response({"ok": True, "cameras": await _run(fn, _datasets_dir(request), name)})


@_dataset_handler
async def api_depth_video(request):
    path = await _run(request.app[PREVIEWS].video, _datasets_dir(request), request.match_info["session"],
                      request.match_info["camera"])
    return web.FileResponse(path, headers={"Content-Type": "video/mp4", "Cache-Control": "no-cache"})


def _transfer_error(fn):
    async def handler(request):
        from teleop_server.labels import LabelError
        from teleop_server.transfer import TransferError
        try:
            return await fn(request)
        except (TransferError, LabelError) as exc:
            return _error(exc.status, str(exc))
    return handler


@_transfer_error
async def api_transfer(request):
    """Current/last transfer and the saved targets (never a password)."""
    from teleop_server.transfer import load_targets
    return web.json_response({"ok": True, "transfer": request.app[TRANSFER].status(), "targets": load_targets()})


@_transfer_error
async def api_transfer_post(request):
    """Actions: browse | mkdir | close_browse | test | start | cancel | save_target | delete_target | parse_config."""
    from teleop_server import transfer as tx
    body, mgr = await _body(request), request.app[TRANSFER]
    action = body.get("action")
    if action == "parse_config":
        return web.json_response({"ok": True, "target": tx.parse_ssh_config(body.get("text"))})
    if action == "save_target":
        return web.json_response({"ok": True, "target": tx.save_target(body.get("target")), "targets": tx.load_targets()})
    if action == "delete_target":
        tx.delete_target(str(body.get("name") or ""))
        return web.json_response({"ok": True, "targets": tx.load_targets()})
    if action == "cancel":
        return web.json_response({"ok": True, "cancelled": mgr.cancel()})
    if action == "close_browse":
        return web.json_response({"ok": True, "closed": await _run(mgr.close_browser, body.get("token"))})
    if action == "browse":
        listing = await _run(mgr.browse, body.get("target"), body.get("password"), body.get("token"), body.get("path"))
        return web.json_response({"ok": True, "listing": listing})
    if action == "mkdir":
        listing = await _run(mgr.mkdir, body.get("target"), body.get("password"), body.get("token"), body.get("path"),
                             body.get("name"))
        return web.json_response({"ok": True, "listing": listing})
    record_dir = _datasets_dir(request)
    if action == "test":
        info = await _run(mgr.test, record_dir, body.get("target"), body.get("password"), body.get("sessions") or [],
                          body.get("token"))
        return web.json_response({"ok": True, "check": info})
    if action == "start":
        status = await _run(mgr.start, record_dir, body.get("target"), body.get("password"), body.get("sessions"),
                            body.get("rate_mbps", 100), body.get("token"))
        return web.json_response({"ok": True, "transfer": status})
    return _error(400, "알 수 없는 동작입니다.")


def add_dataset_routes(app):
    from teleop_server.datasets import DepthPreviews
    from teleop_server.transfer import Transfer
    app[TRANSFER] = Transfer(publish=lambda status: app[HUB].publish("transfer", status))
    app[PREVIEWS] = DepthPreviews()
    app.router.add_get("/api/datasets/{session}/depth_preview", api_depth_preview)
    app.router.add_post("/api/datasets/{session}/depth_preview", api_depth_preview)
    app.router.add_get("/api/datasets/{session}/depth_video/{camera}", api_depth_video)
    app.router.add_get("/api/transfer", api_transfer)
    app.router.add_post("/api/transfer", api_transfer_post)
    app.router.add_get("/api/datasets", api_datasets)
    app.router.add_get("/api/datasets/{session}", api_dataset)
    app.router.add_delete("/api/datasets/{session}", api_dataset_delete)
    app.router.add_get("/api/datasets/{session}/video/{camera}", api_dataset_video)
    app.router.add_get("/api/datasets/{session}/depth/{camera}/{index}", api_dataset_depth)


async def api_base(request):
    """Enable/disable the virtual base controller. Enabled + a held joystick MOVES the base."""
    base, action = request.app[BASE], (await _body(request)).get("action")
    if base is None:
        return _error(400, "베이스 조작을 사용할 수 없습니다 (--base-control).")
    if action == "enable":
        base.enable()
    elif action == "disable":
        base.disable("user")
    else:
        return _error(400, "action은 enable 또는 disable이어야 합니다.")
    return web.json_response({"ok": True, "base": base.status()})


async def index(request):
    return web.FileResponse(STATIC / "index.html")


async def api_stats(request):
    app = request.app
    clients = [{"report": c.report, "dropped": c.dropped} for c in app[HUB].clients]
    rec = app[RECORDER]
    return web.json_response({"server_time_ns": time.time_ns(), "sensors": app[STATS].summary(),
                              "sources": [s.status() for s in app[SOURCES]], "clients": clients,
                              "record": rec.status() if rec is not None else None})


async def websocket(request):
    ws = web.WebSocketResponse(heartbeat=10, max_msg_size=1 << 20, compress=False)  # JPEG: deflate only adds latency
    await ws.prepare(request)
    hub, base = request.app[HUB], request.app[BASE]
    client = Client(ws)
    client_id = f"c{id(client):x}"
    last_reject = 0.0
    sender = asyncio.create_task(hub.serve(client))
    try:
        rec = request.app[RECORDER]
        await ws.send_str(json.dumps({"type": "hello", "server_time_ns": time.time_ns(),
                                      "sources": [s.status() for s in request.app[SOURCES]],
                                      "record": rec.status() if rec is not None else None,
                                      "base": base.status() if base is not None else None,
                                      "client_id": client_id}))
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if data.get("type") == "ping":
                # NTP-style: client keeps the min-RTT sample to estimate its clock offset.
                await ws.send_str(json.dumps({"type": "pong", "t0": data["t0"], "server_ns": time.time_ns()}))
            elif data.get("type") == "ack":
                client.ack(data["key"])
            elif data.get("type") == "client_report":
                client.report = data
            elif data.get("type") == "base_cmd" and base is not None:
                reason = base.command(client_id, data.get("vx"), data.get("wz"))
                if reason and time.monotonic() - last_reject > 0.5:
                    last_reject = time.monotonic()
                    await ws.send_str(json.dumps({"type": "base_reject", "reason": reason}))
            elif data.get("type") == "base_release" and base is not None:
                base.release(client_id)
    finally:
        if base is not None:
            base.release(client_id)  # owner disconnected: stop the base now
        sender.cancel()
        hub.clients.discard(client)
    return ws


def _preload_recording():
    try:
        from teleop_server.recorder import collector_modules
        collector_modules()
        import lerobot.datasets.lerobot_dataset  # noqa: F401
    except Exception:
        log.exception("recording preload failed (recording will import on first use)")


def create_app(sources_factory, recorder_factory=None, base_factory=None):
    app = web.Application()
    stats = StatsRegistry()
    hub = Hub(stats)
    app[STATS], app[HUB] = stats, hub
    app[SOURCES] = sources_factory(hub, stats)
    app[RECORDER] = recorder_factory(hub, app[SOURCES]) if recorder_factory else None
    app[BASE] = base_factory(hub, app[SOURCES]) if base_factory else None
    arms = _arms(app)
    if app[BASE] is not None and arms is not None:
        # Robot control is one unit: whenever arm control ends (button, error), the base stops too.
        arms.exit_hooks.append(lambda error, base=app[BASE]: base.disable("robot control stopped"))

    async def push_stats():
        while True:
            await asyncio.sleep(1.0)
            hub.publish("stats", {"type": "stats", "server_time_ns": time.time_ns(), "sensors": stats.summary(),
                                  "sources": [s.status() for s in app[SOURCES]]})

    async def start_sources():
        # Same order as teleop_server/mobile_capture.py: cameras must deliver a first frame before any
        # arm connects (arm connect moves the robots). Concurrent start-up coincided with a
        # controller dropping its TCP connection during configure(); the root cause is not known.
        loop = asyncio.get_running_loop()
        first = [s for s in app[SOURCES] if s.kind != "arms"]
        for source in first:
            source.start()
        await asyncio.gather(*(loop.run_in_executor(None, s.ready.wait, 20.0) for s in first))
        for source in app[SOURCES]:
            if source.kind == "arms":
                source.start()  # idle until the browser's control button (unless autostart)
        if app[RECORDER] is not None:
            app[RECORDER].publish()
            # Import LeRobot (torch) now so the first record press does not wait for it.
            await loop.run_in_executor(None, _preload_recording)

    async def startup(app):
        hub.bind(asyncio.get_running_loop())
        if app[BASE] is not None:
            app[BASE].start()
        app[START_TASK] = asyncio.create_task(start_sources())
        app[STATS_TASK] = asyncio.create_task(push_stats())

    async def cleanup(app):
        app[STATS_TASK].cancel()
        app[START_TASK].cancel()
        loop = asyncio.get_running_loop()
        if app[BASE] is not None:
            await loop.run_in_executor(None, app[BASE].stop)  # zero velocity first
        rec = app[RECORDER]
        if rec is not None:  # an interrupted recording is kept but marked failed (as mobile_capture.py)
            rec.request_stop("server shutdown", error="server shutdown during recording")
        await asyncio.gather(*(loop.run_in_executor(None, s.stop) for s in app[SOURCES]))
        if rec is not None:
            await loop.run_in_executor(None, rec.wait, 600)
        for client in list(hub.clients):
            await client.ws.close()

    app.on_startup.append(startup)
    app.on_shutdown.append(cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/ws", websocket)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_post("/api/control", api_control)
    app.router.add_post("/api/record", api_record)
    app.router.add_get("/api/sessions", api_sessions)
    app.router.add_post("/api/label", api_label)
    add_dataset_routes(app)
    app.router.add_post("/api/base", api_base)
    app.router.add_static("/static", STATIC)
    return app


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--cameras", choices=["realsense", "fake", "off"], default="realsense")
    p.add_argument("--serials", nargs=3, default=DEFAULT_SERIALS)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--jpeg-quality", type=int, default=80)
    p.add_argument("--scale", type=float, default=1.0, help="Resize factor before JPEG encoding")
    p.add_argument("--no-depth", dest="depth", action="store_false",
                   help="Do not capture/record depth (default: colour-aligned depth paired with every RGB frame)")
    p.add_argument("--depth-min", type=float, default=0.05, help="Recorded depth range (m); <= min means no data")
    p.add_argument("--depth-max", type=float, default=6.5535, help="Default: full D405 range (no clipping)")
    p.add_argument("--depth-preset", default="ultrafast", help="x265 preset for lossless depth (speed vs size)")
    p.add_argument("--arms", choices=["teleop", "fake", "off"], default="off",
                   help="teleop: real arms, connected from the browser's control button (they MOVE to the "
                        "staged pose, then followers mirror leaders)")
    p.add_argument("--arm-rate", type=float, default=30.0,
                   help="Arm read/teleop loop rate (Hz); 30 matches the existing collectors (follower goal time 0.1 s)")
    p.add_argument("--telemetry-hz", type=float, default=50.0, help="Max arm telemetry rate sent to browsers")
    p.add_argument("--follower-left", default="192.168.1.5")
    p.add_argument("--follower-right", default="192.168.1.4")
    p.add_argument("--leader-left", default="192.168.1.3")
    p.add_argument("--leader-right", default="192.168.1.2")
    p.add_argument("--odom", choices=["ros", "fake", "off"], default="ros")
    p.add_argument("--odom-topic", default="/odom")
    p.add_argument("--cmd-topic", default="/cmd_vel", help="geometry_msgs/Twist, recorded like mobile_capture.py")
    p.add_argument("--task", default="Pick up the paper bag and place it in the marked target area.",
                   help="Default task text for recordings (editable in the browser)")
    p.add_argument("--record-dir", type=Path, default=None,
                   help="Default: <repo>/dataset (real sensors) or /tmp/teleop_server_synthetic (any fake sensor)")
    p.add_argument("--base-control", choices=["auto", "ros", "fake", "off"], default="auto",
                   help="Virtual base controller publishing Twist on --cmd-topic (auto: follows --odom)")
    p.add_argument("--base-max-linear", type=float, default=0.3, help="m/s, clamp for browser commands")
    p.add_argument("--base-max-angular", type=float, default=0.6, help="rad/s, clamp for browser commands")
    p.add_argument("--no-validate", action="store_true", help="Skip check_mobile validation after each recording")
    args = p.parse_args(argv)
    if len(set(args.serials)) != 3:
        p.error("three distinct camera serials required")
    return args


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.environ.setdefault("ROS_LOG_DIR", "/tmp/lerobot-ros-logs")
    args = parse_args(argv)
    os.environ.setdefault("HF_HOME", str(HF_CACHE))
    app = create_app(lambda hub, stats: build_sources(args, hub, stats),
                     lambda hub, sources: build_recorder(args, hub, sources),
                     lambda hub, sources: build_base_control(args, hub, sources))
    web.run_app(app, host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
