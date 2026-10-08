"""Rolling latency/rate statistics. All timestamps are host unix nanoseconds."""
import threading
import time
from collections import deque

import numpy as np


class StageStats:
    """Rolling window of samples (ms) for one latency stage."""

    def __init__(self, window_s=10.0, maxlen=4000):
        self.window_ns = int(window_s * 1e9)
        self.samples = deque(maxlen=maxlen)  # (t_ns, value_ms)

    def add(self, value_ms, t_ns=None):
        self.samples.append((t_ns or time.time_ns(), float(value_ms)))

    def summary(self, now_ns=None):
        now_ns = now_ns or time.time_ns()
        values = [v for t, v in self.samples if now_ns - t <= self.window_ns]
        if not values:
            return None
        arr = np.asarray(values)
        return {"n": len(arr), "mean": float(arr.mean()), "p50": float(np.percentile(arr, 50)),
                "p95": float(np.percentile(arr, 95)), "max": float(arr.max()), "last": values[-1]}


class SensorStats:
    """Per-sensor stats: message rate plus named latency stages."""

    def __init__(self, name, window_s=10.0):
        self.name = name
        self.window_s = window_s
        self.lock = threading.Lock()
        self.stages = {}
        self.arrivals = deque(maxlen=5000)
        self.last_ns = None
        self.count = 0
        self.dropped = 0

    def record(self, stages_ms, t_ns=None):
        t_ns = t_ns or time.time_ns()
        with self.lock:
            self.count += 1
            self.last_ns = t_ns
            self.arrivals.append(t_ns)
            for stage, value in stages_ms.items():
                if value is None:
                    continue
                self.stages.setdefault(stage, StageStats(self.window_s)).add(value, t_ns)

    def add_stage(self, stage, value_ms, t_ns=None):
        """Add a stage sample without counting a new sensor message."""
        with self.lock:
            self.stages.setdefault(stage, StageStats(self.window_s)).add(value_ms, t_ns)

    def summary(self, now_ns=None):
        now_ns = now_ns or time.time_ns()
        with self.lock:
            recent = [t for t in self.arrivals if now_ns - t <= self.window_s * 1e9]
            rate = (len(recent) - 1) * 1e9 / (recent[-1] - recent[0]) if len(recent) > 1 and recent[-1] > recent[0] else 0.0
            return {"name": self.name, "count": self.count, "dropped": self.dropped, "rate_hz": rate,
                    "stale_ms": (now_ns - self.last_ns) / 1e6 if self.last_ns else None,
                    "stages": {k: s.summary(now_ns) for k, s in self.stages.items()}}


class StatsRegistry:
    def __init__(self, window_s=10.0):
        self.window_s = window_s
        self.sensors = {}
        self.lock = threading.Lock()

    def get(self, name):
        with self.lock:
            if name not in self.sensors:
                self.sensors[name] = SensorStats(name, self.window_s)
            return self.sensors[name]

    def summary(self):
        with self.lock:
            sensors = list(self.sensors.values())
        now = time.time_ns()
        return {s.name: s.summary(now) for s in sensors}


class MinOffsetClock:
    """Map a device clock to host time with the minimum observed (host - device) offset.

    The minimum offset corresponds to the fastest observed delivery, so ages computed with it
    are relative to the best-case transport delay (a lower bound on true latency, not absolute).
    """

    def __init__(self, window=2000):
        self.offsets = deque(maxlen=window)

    def to_host_ns(self, device_ns, host_ns):
        self.offsets.append(host_ns - device_ns)
        return device_ns + min(self.offsets)
