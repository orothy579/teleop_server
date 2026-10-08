"""Thread-backed sensor source with status reporting."""
import logging
import threading
import time
import traceback

log = logging.getLogger(__name__)


class Source:
    kind = "source"

    def __init__(self, name, hub, stats):
        self.name = name
        self.hub = hub
        self.stats = stats.get(name)
        self.stop_event = threading.Event()
        self.thread = None
        self.state = "idle"
        self.error = None
        self.info = {}
        self.ready = threading.Event()  # set once running, or failed/stopped

    def status(self):
        return {"name": self.name, "kind": self.kind, "state": self.state, "error": self.error, "info": self.info}

    def _set_state(self, state, error=None):
        self.state, self.error = state, error
        if state in ("running", "error", "stopped"):
            self.ready.set()
        self.hub.publish(f"status:{self.name}", {"type": "status", **self.status()})

    def start(self):
        self.thread = threading.Thread(target=self._main, name=self.name, daemon=True)
        self.thread.start()

    def stop(self, timeout=10.0):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout)

    def _main(self):
        self._set_state("starting")
        try:
            self.run()
            self._set_state("stopped", self.error)
        except Exception as exc:  # report and keep server alive
            log.error("%s failed: %s", self.name, traceback.format_exc())
            self._set_state("error", repr(exc))

    def run(self):
        raise NotImplementedError

    def sleep_until(self, deadline):
        self.stop_event.wait(max(0.0, deadline - time.monotonic()))
