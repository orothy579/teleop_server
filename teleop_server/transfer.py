"""Send recorded sessions to another computer over SSH (dataset browser "보내기").

The password is typed in the browser and used once, to open an SSH master connection (OpenSSH
ControlMaster) through ``ssh_askpass.sh``; it is never written anywhere and dropped after login.
Every session is streamed with tar into ``<remote_dir>/.incoming/<session>``, verified file by file
with SHA-256 against the local copy, then moved to ``<remote_dir>/<session>``. A session that already
exists there is skipped - nothing on the remote side is ever overwritten or deleted, except our own
unfinished ``.incoming`` copy of a session being (re)sent.

Saved targets (no passwords) live in ``~/.config/teleop_server/transfer_targets.json``.
"""
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from teleop_server.labels import SESSION_RE

log = logging.getLogger(__name__)
TARGETS_FILE = Path(os.environ.get("TELEOP_TRANSFER_TARGETS", "~/.config/teleop_server/transfer_targets.json")).expanduser()
KNOWN_HOSTS = TARGETS_FILE.with_name("known_hosts")
ASKPASS = Path(__file__).with_name("ssh_askpass.sh")
SSH = os.environ.get("TELEOP_SSH_BIN", "ssh")  # tests substitute a local stand-in
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
NAME_RE = re.compile(r"^[^\x00-\x1f/]{1,60}$")
STAGING = ".incoming"
BROWSE_IDLE_S = 300  # a folder-browsing connection closes after this long unused
FOLDER_RE = re.compile(r"^[^\x00-\x1f/]{1,100}$")


class TransferError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------- targets ----------
def clean_target(t):
    if not isinstance(t, dict):
        raise TransferError("대상 정보가 없습니다.")
    name = str(t.get("name") or "").strip() or str(t.get("host") or "").strip()
    host, user = str(t.get("host") or "").strip(), str(t.get("user") or "").strip()
    remote_dir = str(t.get("remote_dir") or "teleop_datasets").strip()
    raw_port = t.get("port")
    try:
        port = 22 if raw_port in (None, "") else int(raw_port)
    except (TypeError, ValueError):
        port = -1
    if not NAME_RE.match(name):
        raise TransferError("대상 이름이 올바르지 않습니다.")
    if not HOST_RE.match(host):
        raise TransferError("호스트 주소가 올바르지 않습니다.")
    if not 1 <= port <= 65535:
        raise TransferError("포트는 1~65535 사이여야 합니다.")
    if not USER_RE.match(user):
        raise TransferError("사용자 이름이 올바르지 않습니다.")
    if not remote_dir or len(remote_dir) > 300 or any(c in remote_dir for c in "\x00\n\r"):
        raise TransferError("받을 폴더 경로가 올바르지 않습니다.")
    return {"name": name, "host": host, "port": port, "user": user, "remote_dir": remote_dir,
            "check_host_key": bool(t.get("check_host_key", False))}


def parse_ssh_config(text):
    """First ``Host`` block of an ssh_config snippet -> target fields."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or parts[0].startswith("#"):
            continue
        key, value = parts[0].lower(), parts[1].strip()
        if key == "host":
            if "name" in out:
                break
            out["name"] = value.split()[0]
        elif key == "hostname":
            out["host"] = value
        elif key == "user":
            out["user"] = value
        elif key == "port":
            out["port"] = value
        elif key == "stricthostkeychecking":
            out["check_host_key"] = value.lower() not in ("no", "off", "false")
    out.setdefault("check_host_key", True)
    return out


def load_targets():
    try:
        data = json.loads(TARGETS_FILE.read_text())
        return [clean_target(t) for t in data.get("targets", [])]
    except (OSError, ValueError, TransferError):
        return []


def _write_targets(targets):
    TARGETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TARGETS_FILE.with_name(f".{TARGETS_FILE.name}.tmp")
    tmp.write_text(json.dumps({"targets": targets}, indent=2, ensure_ascii=False))
    os.replace(tmp, TARGETS_FILE)


def save_target(t):
    t = clean_target(t)
    targets = [x for x in load_targets() if x["name"] != t["name"]]
    _write_targets([t] + targets)  # most recently saved first: the "바로 보내기" default
    return t


def delete_target(name):
    _write_targets([x for x in load_targets() if x["name"] != name])


# ---------- local side ----------
def session_files(root):
    """{"./relative/path": (size, path)} for every file of a session."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out["./" + p.relative_to(root).as_posix()] = (p.stat().st_size, p)
    return out


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def _remote_dir(remote_dir):
    """Quoted for the remote shell; ``~/x`` and ``x`` are relative to the remote home."""
    d = remote_dir.rstrip("/") or "/"
    if d == "~":
        d = "."
    elif d.startswith("~/"):
        d = d[2:] or "."
    return shlex.quote(d)


def _remote_path(path):
    """A path typed or browsed in the UI, quoted; empty and ``~`` mean the remote home."""
    p = str(path or "").strip()
    if len(p) > 1000 or any(c in p for c in "\x00\n\r"):
        raise TransferError("경로가 올바르지 않습니다.")
    if p in ("", "~"):
        return "."
    if p.startswith("~/"):
        p = p[2:] or "."
    if p.startswith("-"):
        p = "./" + p
    return shlex.quote(p)


# ---------- SSH ----------
class Connection:
    """One authenticated SSH master connection; commands reuse it without the password."""

    def __init__(self, target, password):
        self.t = target
        self.tmp = tempfile.mkdtemp(prefix="teleop-ssh-")
        os.chmod(self.tmp, 0o700)
        self.ctl = os.path.join(self.tmp, "ctl")
        self.base = [SSH, "-p", str(target["port"]), "-o", f"ControlPath={self.ctl}", "-o", "LogLevel=ERROR",
                     "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
                     "-o", "Compression=no"]
        if target["check_host_key"]:
            KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
            self.base += ["-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}"]
        else:  # as in the user's ssh_config for proxies whose host key changes
            self.base += ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
        self.dest = f"{target['user']}@{target['host']}"
        env = dict(os.environ, SSH_ASKPASS=str(ASKPASS), SSH_ASKPASS_REQUIRE="force",
                   TELEOP_SSH_PASSWORD=password or "")
        env.pop("DISPLAY", None)
        r = subprocess.run(self.base + ["-o", "ControlMaster=yes", "-o", f"ControlPersist={BROWSE_IDLE_S}",
                                        "-o", "NumberOfPasswordPrompts=1",
                                        "-fN", self.dest], env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            shutil.rmtree(self.tmp, ignore_errors=True)
            err = (r.stderr or "").strip().splitlines()
            msg = err[-1] if err else f"ssh exit {r.returncode}"
            if "Permission denied" in msg or "Authentication failed" in msg:
                raise TransferError("로그인 실패: 비밀번호나 사용자 이름을 확인하세요.", 401)
            raise TransferError(f"연결 실패: {msg}", 502)

    def _cmd(self, command):
        # BatchMode: commands only ever use the open master connection, never ask for a password.
        return self.base + ["-o", "BatchMode=yes", self.dest, command]

    def run(self, command, timeout=120, check=True):
        r = subprocess.run(self._cmd(command), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 255 and not os.path.exists(self.ctl):
            raise TransferError("SSH 연결이 끊겼거나 만료되었습니다. 비밀번호를 다시 입력하세요.", 401)
        if check and r.returncode != 0:
            raise TransferError(f"원격 명령 실패: {(r.stderr or r.stdout).strip()[-300:]}", 502)
        return r

    def popen(self, command, **kw):
        return subprocess.Popen(self._cmd(command), **kw)

    def close(self):
        subprocess.run(self.base + ["-O", "exit", self.dest], capture_output=True, timeout=15)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def list_dir(self, path):
        """Folders under ``path`` (hidden ones and recorded-session folders summarized, not listed)."""
        r = self.run(f"cd {_remote_path(path)} && pwd && (test -w . && echo W || echo R)"
                     " && df -Pk . | awk 'NR==2{print $4}'"
                     " && find . -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%f\\n' | LC_ALL=C sort | head -n 2000",
                     check=False)
        lines = r.stdout.splitlines()
        if r.returncode != 0 or len(lines) < 3:
            raise TransferError(f"폴더를 열 수 없습니다: {path or '~'}", 404)
        names = lines[3:]
        return {"path": lines[0], "writable": lines[1] == "W", "free_bytes": int(lines[2]) * 1024,
                "dirs": [n for n in names if not SESSION_RE.match(n)],
                "sessions": sum(1 for n in names if SESSION_RE.match(n))}

    def probe(self, sessions=()):
        """Remote user/host, absolute folder, free space, tools, which of ``sessions`` exist."""
        d = _remote_dir(self.t["remote_dir"])
        r = self.run(f"mkdir -p {d} && cd {d} && echo \"$(whoami)@$(hostname)\" && pwd"
                     " && df -Pk . | awk 'NR==2{print $4}'"
                     " && for c in tar sha256sum find xargs; do command -v $c >/dev/null || echo missing:$c; done"
                     " && ls -1A")
        lines = r.stdout.splitlines()
        if len(lines) < 3:
            raise TransferError(f"원격 응답을 이해할 수 없습니다: {r.stdout[-200:]}", 502)
        missing = [x.split(":", 1)[1] for x in lines[3:] if x.startswith("missing:")]
        if missing:
            raise TransferError(f"받는 컴퓨터에 필요한 명령이 없습니다: {', '.join(missing)}", 502)
        names = {x for x in lines[3:] if not x.startswith("missing:")}
        return {"who": lines[0], "path": lines[1], "free_bytes": int(lines[2]) * 1024,
                "existing": sorted(s for s in sessions if s in names)}


# ---------- job ----------
class Transfer:
    """At most one transfer at a time, in a background thread; status is pushed via ``publish``."""

    def __init__(self, publish=lambda status: None):
        self.publish_cb = publish
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.thread = None
        self.procs = []
        self.job = None
        self._last_pub = 0.0
        self.browsers = {}  # token -> {"conn", "key", "used"}: folder browsing, reusable for sending

    def busy_sessions(self):
        j = self.job
        return {s["session"] for s in j["sessions"]} if j and j["state"] in ("connecting", "sending") else set()

    def status(self):
        with self.lock:
            return json.loads(json.dumps(self.job)) if self.job else {"state": "idle"}

    def _publish(self, force=False):
        now = time.monotonic()
        if force or now - self._last_pub > 0.5:
            self._last_pub = now
            self.publish_cb(dict(self.status(), type="transfer"))

    # -- folder browsing (one SSH login, reused for "연결 확인" and the transfer) --
    @staticmethod
    def _key(t):
        return t["host"], t["port"], t["user"], t["check_host_key"]

    def _expire(self):
        now = time.monotonic()
        with self.lock:
            old = [k for k, b in self.browsers.items() if now - b["used"] > BROWSE_IDLE_S]
            gone = [self.browsers.pop(k) for k in old]
        for b in gone:
            b["conn"].close()

    def _browser(self, t, password, token):
        """(token, connection) for target ``t``: the open one for ``token`` or a new login."""
        self._expire()
        with self.lock:
            b = self.browsers.get(token) if token else None
            if b and b["key"] == self._key(t):
                b["used"] = time.monotonic()
                b["conn"].t = t
                return token, b["conn"]
        if b:
            self.close_browser(token)  # the target was changed in the form
        conn = Connection(t, password)
        token = secrets.token_urlsafe(18)
        with self.lock:
            self.browsers[token] = {"conn": conn, "key": self._key(t), "used": time.monotonic()}
        return token, conn

    def _take(self, token, t):
        with self.lock:
            b = self.browsers.get(token) if token else None
            if not b or b["key"] != self._key(t):
                return None
            del self.browsers[token]
        b["conn"].t = t
        return b["conn"]

    def close_browser(self, token):
        with self.lock:
            b = self.browsers.pop(token, None) if token else None
        if b:
            b["conn"].close()
        return b is not None

    def browse(self, target, password, token, path):
        t = clean_target(dict(target or {}, remote_dir=(target or {}).get("remote_dir") or "."))
        token, conn = self._browser(t, password, token)
        try:
            out = conn.list_dir(path)
        except TransferError as exc:
            if exc.status == 401:
                self.close_browser(token)
            raise
        return dict(out, token=token)

    def mkdir(self, target, password, token, path, name):
        name = str(name or "").strip()
        if not FOLDER_RE.match(name) or name in (".", "..") or name.startswith("-"):
            raise TransferError("새 폴더 이름이 올바르지 않습니다 (/, 줄바꿈, '.', '..', '-'로 시작 불가).")
        t = clean_target(dict(target or {}, remote_dir=(target or {}).get("remote_dir") or "."))
        token, conn = self._browser(t, password, token)
        r = conn.run(f"cd {_remote_path(path)} && mkdir {shlex.quote(name)}", check=False)
        if r.returncode != 0:
            raise TransferError(f"폴더를 만들 수 없습니다: {(r.stderr or '').strip()[-200:]}", 409)
        here = conn.list_dir(path)["path"]
        return dict(conn.list_dir(here.rstrip("/") + "/" + name), token=token)

    @staticmethod
    def plan(record_dir, sessions):
        if not isinstance(sessions, list) or not sessions:
            raise TransferError("보낼 세션을 고르세요.")
        out = []
        for s in sessions:
            if not isinstance(s, str) or not SESSION_RE.match(s):
                raise TransferError(f"잘못된 세션 이름입니다: {s!r}")
            root = Path(record_dir) / s
            if not (root / "session.json").is_file():
                raise TransferError(f"세션을 찾을 수 없습니다: {s}", 404)
            out.append({"session": s, "bytes": sum(sz for sz, _ in session_files(root).values())})
        return out

    def test(self, record_dir, target, password, sessions, token=None):
        t = clean_target(target)
        plan = self.plan(record_dir, sessions) if sessions else []
        token, conn = self._browser(t, password, token)  # stays open: browsing and sending reuse it
        try:
            info = conn.probe([p["session"] for p in plan])
        except TransferError as exc:
            if exc.status == 401:
                self.close_browser(token)
            raise
        info["token"] = token
        info["total_bytes"] = sum(p["bytes"] for p in plan)
        info["send_bytes"] = sum(p["bytes"] for p in plan if p["session"] not in info["existing"])
        return info

    def start(self, record_dir, target, password, sessions, rate_mbps=100, token=None):
        t = clean_target(target)
        plan = self.plan(record_dir, sessions)
        try:
            rate = max(0.0, float(rate_mbps or 0))
        except (TypeError, ValueError):
            raise TransferError("속도 제한 값이 올바르지 않습니다.")
        if self.thread is not None and self.thread.is_alive():
            if self.job and self.job["state"] in ("connecting", "sending"):
                raise TransferError("이미 전송 중입니다.", 409)
            self.thread.join(20)  # finished; only closing its connection
        conn = self._take(token, t)  # already logged in while browsing / checking (takes the lock itself)
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                if conn is not None:
                    conn.close()
                raise TransferError("이미 전송 중입니다.", 409)
            self.cancel_event.clear()
            self.job = {"state": "connecting", "target": {k: t[k] for k in ("name", "host", "port", "user", "remote_dir")},
                        "started_unix": time.time(), "finished_unix": None, "error": None, "remote": None,
                        "rate_limit_mbps": rate, "total_bytes": sum(p["bytes"] for p in plan), "sent_bytes": 0,
                        "rate_bps": 0.0, "eta_s": None, "current": None,
                        "sessions": [dict(p, state="queued", sent=0, error=None) for p in plan]}
            self.thread = threading.Thread(target=self._run, args=(Path(record_dir), t, password, rate, conn),
                                           name="transfer", daemon=True)
            self.thread.start()
        self._publish(force=True)
        return self.status()

    def cancel(self):
        if not (self.thread and self.thread.is_alive()):
            return False
        self.cancel_event.set()
        for p in list(self.procs):
            try:
                p.kill()
            except OSError:
                pass
        return True

    # -- worker --
    def _set(self, **kw):
        with self.lock:
            self.job.update(kw)
        self._publish(force=True)

    def _run(self, record_dir, t, password, rate_mbps, conn=None):
        try:
            conn = conn or Connection(t, password)
            password = None  # only the master connection needed it
            names = [s["session"] for s in self.job["sessions"]]
            info = conn.probe(names)
            need = sum(s["bytes"] for s in self.job["sessions"] if s["session"] not in info["existing"])
            self._set(state="sending", remote={"who": info["who"], "path": info["path"], "free_bytes": info["free_bytes"]})
            if need > info["free_bytes"] * 0.98:
                raise TransferError(f"받는 쪽 여유 공간 부족: 필요 {need / 1e9:.1f} GB, 여유 {info['free_bytes'] / 1e9:.1f} GB")
            started, done_bytes = time.monotonic(), 0
            for item in self.job["sessions"]:
                if self.cancel_event.is_set():
                    break
                name = item["session"]
                if name in info["existing"]:
                    with self.lock:
                        item.update(state="skipped", error="받는 쪽에 이미 있음")
                        self.job["total_bytes"] -= item["bytes"]
                    self._publish(force=True)
                    continue
                with self.lock:
                    item["state"], self.job["current"] = "sending", name
                self._publish(force=True)
                try:
                    self._send(conn, record_dir, t, item, rate_mbps, started, done_bytes)
                    with self.lock:
                        item["state"] = "done"
                except Exception as exc:
                    if self.cancel_event.is_set():
                        with self.lock:
                            item.update(state="cancelled", error="취소됨")
                        break
                    log.exception("sending %s failed", name)
                    with self.lock:
                        item.update(state="failed", error=str(exc))
                done_bytes += item["bytes"]
                self._publish(force=True)
            failed = [s for s in self.job["sessions"] if s["state"] == "failed"]
            final = "cancelled" if self.cancel_event.is_set() else ("failed" if failed else "done")
            if final == "cancelled":
                with self.lock:
                    for s in self.job["sessions"]:
                        if s["state"] == "queued":
                            s.update(state="cancelled", error="취소됨")
            self._set(state=final, current=None, finished_unix=time.time(), rate_bps=0.0, eta_s=None,
                      error=f"{len(failed)}개 세션 전송 실패" if failed else None)
        except Exception as exc:
            if not isinstance(exc, TransferError):
                log.exception("transfer failed")
            with self.lock:
                for s in self.job["sessions"]:
                    if s["state"] in ("queued", "sending", "verifying"):
                        s.update(state="cancelled" if self.cancel_event.is_set() else "failed", error=str(exc))
            self._set(state="cancelled" if self.cancel_event.is_set() else "failed", error=str(exc),
                      current=None, finished_unix=time.time(), rate_bps=0.0, eta_s=None)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            log.info("transfer %s: %s", self.job["state"],
                     {s["session"]: s["state"] for s in self.job["sessions"]})

    def _send(self, conn, record_dir, t, item, rate_mbps, started, done_before):
        name, d = item["session"], _remote_dir(t["remote_dir"])
        stage_parent, stage, final = f"{d}/{STAGING}", f"{d}/{STAGING}/{shlex.quote(name)}", f"{d}/{shlex.quote(name)}"
        files = session_files(record_dir / name)
        tar = subprocess.Popen(["tar", "-C", str(record_dir), "-cf", "-", name], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        ssh = conn.popen(f"mkdir -p {stage_parent} && rm -rf {stage} && tar -C {stage_parent} -xf -",
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.procs = [tar, ssh]
        limit = rate_mbps * 1e6 / 8 if rate_mbps > 0 else 0
        t0, sent = time.monotonic(), 0
        try:
            while block := tar.stdout.read1(1 << 18):  # what is there, at most 256 KB: smooth throttling
                if self.cancel_event.is_set():
                    raise TransferError("취소됨")
                ssh.stdin.write(block)
                sent += len(block)
                if limit:
                    ahead = sent / limit - (time.monotonic() - t0)
                    if ahead > 0 and self.cancel_event.wait(ahead):  # the wait ends at once on cancel
                        raise TransferError("취소됨")
                now = time.monotonic()
                with self.lock:
                    item["sent"] = min(sent, item["bytes"])
                    total_sent = done_before + item["sent"]
                    self.job["sent_bytes"] = total_sent
                    rate = total_sent / max(now - started, 1e-3)
                    self.job["rate_bps"] = rate
                    self.job["eta_s"] = (self.job["total_bytes"] - total_sent) / rate if rate > 0 else None
                self._publish()
            ssh.stdin.close()
            tar_err = tar.stderr.read().decode(errors="replace").strip()
            if tar.wait() != 0:
                raise TransferError(f"tar 실패: {tar_err[-200:]}")
            ssh_err = ssh.stderr.read().decode(errors="replace").strip()
            if ssh.wait() != 0:
                raise TransferError(f"원격 저장 실패: {ssh_err[-200:]}")
        finally:
            for p in (tar, ssh):
                if p.poll() is None:
                    p.kill()
                    p.wait()
            self.procs = []
        if self.cancel_event.is_set():
            raise TransferError("취소됨")
        # Verify every file before the session appears under its final name.
        with self.lock:
            item["state"] = "verifying"
        self._publish(force=True)
        local = {rel: sha256_file(path) for rel, (_, path) in files.items()}
        r = conn.run(f"cd {stage}/ && find . -type f -print0 | xargs -0 -r sha256sum", timeout=900)
        remote = {}
        for line in r.stdout.splitlines():
            digest, _, rel = line.partition("  ")
            remote[rel] = digest
        if remote != local:
            bad = sorted(k for k in set(local) | set(remote) if local.get(k) != remote.get(k))
            raise TransferError(f"검증 실패: {len(bad)}개 파일 불일치 (예: {bad[0]})")
        if self.cancel_event.is_set():
            raise TransferError("취소됨")
        r = conn.run(f"if [ -e {final} ]; then echo exists; else mv {stage} {final} && echo moved; fi")
        if r.stdout.strip() != "moved":
            raise TransferError("받는 쪽에 같은 이름의 세션이 생겨서 옮기지 않았습니다 (.incoming에 남김).")
        with self.lock:
            item["sent"] = item["bytes"]
            item["files"] = len(files)
