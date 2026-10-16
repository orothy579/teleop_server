"""Authenticated HTTPS-facing proxy for the existing DGX web app.

Run this on loopback only and expose this port through an HTTPS tunnel.  The Jetson and
policy WebSockets stay on the original private port, which this proxy never forwards.
"""

import argparse
import asyncio
import base64
import getpass
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web


COOKIE = "teleop_login"
CLIENT = web.AppKey("backend_client", ClientSession)
USER_RE = re.compile(r"[A-Za-z0-9_.-]{1,48}\Z")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailer", "transfer-encoding", "upgrade", "host", "cookie", "authorization"}
BLOCKED = ("/edge", "/policy", "/api/policy")


def _private_write(path: Path, data: bytes):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(8))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class Accounts:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.users_path = self.directory / "users.json"
        self.secret_path = self.directory / "session.key"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.secret_path.exists():
            _private_write(self.secret_path, secrets.token_bytes(32))
        self.secret = self.secret_path.read_bytes()

    def users(self):
        if not self.users_path.exists():
            return {}
        return json.loads(self.users_path.read_text())

    def add(self, username, password):
        if not USER_RE.fullmatch(username) or len(password) < 12:
            raise ValueError("username must be 1-48 letters/digits/._- and password at least 12 characters")
        users = self.users()
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        users[username] = {"salt": salt.hex(), "hash": digest.hex(), "session_version": secrets.token_hex(16)}
        _private_write(self.users_path, (json.dumps(users, indent=2) + "\n").encode())

    def remove(self, username):
        users = self.users()
        if username not in users:
            raise ValueError("unknown user")
        del users[username]
        _private_write(self.users_path, (json.dumps(users, indent=2) + "\n").encode())

    def verify(self, username, password):
        record = self.users().get(username)
        if not record:
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(record["salt"]), n=2**14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), record["hash"])

    def issue(self, username):
        record = self.users()[username]
        payload = json.dumps([username, int(time.time()) + 12 * 3600, record["session_version"]],
                             separators=(",", ":")).encode()
        signature = hmac.new(self.secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")

    def session_user(self, token):
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(signature, hmac.new(self.secret, payload, hashlib.sha256).digest()):
                return None
            username, expiry, version = json.loads(payload)
            current = self.users().get(username)
            if expiry < time.time() or not current or not hmac.compare_digest(version, current["session_version"]):
                return None
            return username
        except (ValueError, TypeError, IndexError, KeyError):
            return None


def login_page(error=""):
    notice = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return web.Response(text=f'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>로봇 웹앱 로그인</title><style>
body{{font:16px system-ui;max-width:380px;margin:12vh auto;padding:20px;background:#f7f8fa;color:#17212f}}
form{{background:white;padding:28px;border-radius:12px;box-shadow:0 6px 28px #0001}}
label{{display:block;margin:18px 0 5px}}input{{box-sizing:border-box;width:100%;padding:12px;font:inherit}}
button{{margin-top:24px;width:100%;padding:12px;font:inherit;background:#1765d1;color:white;border:0;border-radius:6px}}
.error{{color:#af2424}}</style><form method="post" action="/login">
<h1>로봇 웹앱</h1>{notice}<label for="user">사용자명</label><input id="user" name="username" required autofocus>
<label for="pass">비밀번호</label><input id="pass" name="password" type="password" required>
<button>로그인</button></form></html>''', content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def _headers(headers, response=False):
    excluded = (HOP - {"cookie", "authorization"}) if response else HOP
    if response:
        excluded = excluded | {"content-length", "set-cookie"}
    return {name: value for name, value in headers.items()
            if name.lower() not in excluded and not name.lower().startswith("sec-websocket-")}


def create_app(accounts, upstream="http://127.0.0.1:8081"):
    app = web.Application(client_max_size=10 * 1024**2)

    async def startup(app):
        app[CLIENT] = ClientSession(timeout=ClientTimeout(total=None, sock_connect=5), auto_decompress=False)

    async def shutdown(app):
        await app[CLIENT].close()

    app.on_startup.append(startup)
    app.on_cleanup.append(shutdown)

    async def handle(request):
        path = request.path
        if path == "/health":
            return web.json_response({"ok": True})
        if path == "/login" and request.method == "GET":
            return login_page()
        if path == "/login" and request.method == "POST":
            form = await request.post()
            username, password = str(form.get("username", "")), str(form.get("password", ""))
            if len(username) > 48 or len(password) > 1024 or not await asyncio.to_thread(accounts.verify, username, password):
                await asyncio.sleep(0.25)
                return login_page("사용자명 또는 비밀번호가 올바르지 않습니다.")
            response = web.HTTPSeeOther("/")
            response.set_cookie(COOKIE, accounts.issue(username), httponly=True, secure=True,
                                samesite="Strict", max_age=12 * 3600, path="/")
            raise response
        if path == "/logout" and request.method == "POST":
            response = web.HTTPSeeOther("/login")
            response.del_cookie(COOKIE, path="/")
            raise response
        # Never make the machine, upload, or policy channels publicly reachable.
        if any(path == prefix or path.startswith(prefix + "/") for prefix in BLOCKED):
            raise web.HTTPNotFound()
        user = accounts.session_user(request.cookies.get(COOKIE, ""))
        if not user:
            if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):
                raise web.HTTPSeeOther("/login")
            raise web.HTTPUnauthorized()
        origin = request.headers.get("Origin")
        if origin and request.method != "GET" and origin != f"https://{request.host}":
            raise web.HTTPForbidden(text="Invalid origin")
        if request.headers.get("Upgrade", "").lower() == "websocket":
            if origin and origin != f"https://{request.host}":
                raise web.HTTPForbidden(text="Invalid origin")
            return await proxy_websocket(request)
        return await proxy_http(request, user)

    async def proxy_http(request, user):
        target = upstream + request.raw_path
        body = request.content.iter_chunked(65536) if request.can_read_body else None
        async with app[CLIENT].request(request.method, target, headers=_headers(request.headers),
                                          data=body, allow_redirects=False) as response:
            outbound = web.StreamResponse(status=response.status, reason=response.reason,
                                          headers=_headers(response.headers, response=True))
            outbound.headers["Cache-Control"] = "private, no-store"
            await outbound.prepare(request)
            async for chunk in response.content.iter_chunked(65536):
                await outbound.write(chunk)
            await outbound.write_eof()
            print(f"web {user} {request.method} {request.path} {response.status}", flush=True)
            return outbound

    async def proxy_websocket(request):
        url = "ws" + upstream[4:] + request.raw_path
        async with app[CLIENT].ws_connect(url, headers=_headers(request.headers), max_msg_size=20 * 1024**2,
                                            heartbeat=30, compress=0) as backend:
            frontend = web.WebSocketResponse(heartbeat=30, max_msg_size=20 * 1024**2, compress=False)
            await frontend.prepare(request)

            async def forward(source, destination):
                async for message in source:
                    if message.type == WSMsgType.TEXT:
                        await destination.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await destination.send_bytes(message.data)
                    elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break

            tasks = [asyncio.create_task(forward(frontend, backend)),
                     asyncio.create_task(forward(backend, frontend))]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await frontend.close()
            return frontend

    app.router.add_route("*", "/{tail:.*}", handle)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("/home/midbar/ws/teleop_server/.public_gateway"))
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add-user")
    add.add_argument("username")
    add.add_argument("--password-stdin", action="store_true")
    delete = sub.add_parser("remove-user")
    delete.add_argument("username")
    sub.add_parser("list-users")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    accounts = Accounts(args.data_dir)
    if args.command == "add-user":
        password = __import__("sys").stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass()
        accounts.add(args.username, password)
        print(f"user {args.username} added")
    elif args.command == "remove-user":
        accounts.remove(args.username)
        print(f"user {args.username} removed")
    elif args.command == "list-users":
        print("\n".join(sorted(accounts.users())))
    else:
        if not accounts.users():
            parser.error("add at least one user before starting the public gateway")
        web.run_app(create_app(accounts), host="127.0.0.1", port=args.port, access_log=None)


if __name__ == "__main__":
    main()
