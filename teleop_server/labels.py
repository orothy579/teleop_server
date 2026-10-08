"""Human labels for recorded sessions (outcome, task name, environment, operator, notes).

Canonical copy: ``session.json["labels"]``. Mirrored to ``dataset/meta/episode_labels.json``
(keyed by episode index) so dataset consumers find them next to the LeRobot metadata.
Files are replaced atomically; LeRobot ignores the extra meta file.
"""
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SESSION_RE = re.compile(r"^\d{8}_\d{6}(_\d{1,2})?(_SYNTHETIC)?$")  # YYYYMMDD_HHMMSS[_n][_SYNTHETIC]
OUTCOMES = ("success", "failure")
TEXT_FIELDS = {"task_name": 300, "environment": 200, "operator": 100, "notes": 4000}
_lock = threading.Lock()


class LabelError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _write_json(path, data):
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def session_dir(record_dir, session):
    if not isinstance(session, str) or not SESSION_RE.match(session):
        raise LabelError("잘못된 세션 이름입니다.")
    root = Path(record_dir) / session
    if not (root / "session.json").is_file():
        raise LabelError("세션을 찾을 수 없습니다.", 404)
    return root


def list_sessions(record_dir, limit=20):
    record_dir = Path(record_dir)
    if not record_dir.is_dir():
        return []
    def order(name):  # start time (to the second), then _n
        parts = name.removesuffix("_SYNTHETIC").split("_")
        return parts[0], parts[1], int(parts[2]) if len(parts) > 2 else 1
    names = sorted((p.name for p in record_dir.iterdir() if p.is_dir() and SESSION_RE.match(p.name)),
                   key=order, reverse=True)[:limit + 5]
    out = []
    for name in names:
        try:
            m = json.loads((record_dir / name / "session.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"session": name, "status": m.get("status"), "frames": m.get("frames"), "task": m.get("task"),
                    "synthetic": m.get("synthetic", name.endswith("_SYNTHETIC")),
                    "start_unix_ns": m.get("start_unix_ns"), "labels": m.get("labels")})
    out.sort(key=lambda x: (x["start_unix_ns"] or 0, order(x["session"])), reverse=True)  # newest first
    out = out[:limit]
    return out


def rename_dataset_task(dataset_root, old, new):
    """Replace the episode's task text in a LeRobot v3 dataset (meta/tasks.parquet and the
    episodes metadata). Frames only reference task_index, so data files are untouched."""
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq
    tasks_path = Path(dataset_root) / "meta" / "tasks.parquet"
    if not tasks_path.is_file():
        return False  # e.g. a failed session without a saved episode
    tasks = pd.read_parquet(tasks_path)
    if old not in tasks.index or new in tasks.index:
        return False
    tmp = tasks_path.with_name(".tasks.parquet.tmp")
    tasks.rename(index={old: new}).to_parquet(tmp)
    episode_files = sorted((Path(dataset_root) / "meta" / "episodes").rglob("*.parquet"))
    staged = []
    for path in episode_files:
        table = pq.read_table(path)
        i = table.schema.get_field_index("tasks")
        values = [[new if t == old else t for t in row] for row in table.column(i).to_pylist()]
        table = table.set_column(i, table.schema.field(i), pa.array(values, type=table.schema.field(i).type))
        etmp = path.with_name(f".{path.name}.tmp")
        pq.write_table(table, etmp)
        staged.append((etmp, path))
    os.replace(tmp, tasks_path)
    for etmp, path in staged:
        os.replace(etmp, path)
    return True


def save_labels(record_dir, session, data):
    root = session_dir(record_dir, session)
    outcome = data.get("outcome")
    if outcome not in OUTCOMES:
        raise LabelError("결과(성공/실패)를 선택하세요.")
    labels = {"outcome": outcome}
    for key, limit in TEXT_FIELDS.items():
        value = data.get(key) or ""
        if not isinstance(value, str):
            raise LabelError(f"{key}는 문자열이어야 합니다.")
        value = value.strip()
        if len(value) > limit:
            raise LabelError(f"{key}가 너무 깁니다 (최대 {limit}자).")
        labels[key] = value
    with _lock:
        manifest = json.loads((root / "session.json").read_text())
        previous = manifest.get("labels") or {}
        # The label's task name is the recording's task text (there is no task input before recording).
        if labels["task_name"] and labels["task_name"] != manifest.get("task"):
            try:
                rename_dataset_task(root / "dataset", manifest.get("task"), labels["task_name"])
            except Exception as exc:
                raise LabelError(f"데이터셋 작업 문장을 바꾸지 못했습니다: {exc}", 500) from exc
            manifest.setdefault("recorded_task", manifest.get("task"))
            manifest["task"] = labels["task_name"]
        labels["revision"] = previous.get("revision", 0) + 1
        labels["labeled_at"] = datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
        manifest["labels"] = labels
        _write_json(root / "session.json", manifest)
        meta = root / "dataset" / "meta"
        if meta.is_dir():
            _write_json(meta / "episode_labels.json",
                        {"0": {**labels, "session": session, "task": manifest.get("task"),
                               "recorded_task": manifest.get("recorded_task", manifest.get("task"))}})
    return labels
