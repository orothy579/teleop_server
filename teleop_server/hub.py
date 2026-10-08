"""Fan-out of sensor messages to websocket clients with latest-value-only mailboxes.

Sources run in threads and call ``publish`` thread-safely. Each client keeps at most one
pending message per key; an unsent message is replaced (counted as a drop) so slow clients
see fresh data instead of an ever-growing queue.
"""
import asyncio
import json
import struct
import time

from aiohttp import ClientConnectionResetError


MAX_INFLIGHT = 3  # unacknowledged sensor messages per key; bounds socket-buffer backlog


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.inflight = {}
        self.slots = {}
        self.event = asyncio.Event()
        self.dropped = {}
        self.report = None  # latest browser-side latency report

    def ack(self, key):
        """Client processed one message for ``key``; allow the next one through."""
        if self.inflight.get(key, 0) > 0:
            self.inflight[key] -= 1
            if key in self.slots:
                self.event.set()


class Hub:
    def __init__(self, stats):
        self.stats = stats
        self.clients = set()
        self.loop = None
        self.latest = {}  # key -> last message, for new clients and /api/snapshot

    def bind(self, loop):
        self.loop = loop

    def publish(self, key, header, payload=None):
        """Thread-safe. ``header`` is a JSON-able dict; ``payload`` optional bytes (camera JPEG)."""
        if self.loop is None or self.loop.is_closed():
            return
        try:
            self.loop.call_soon_threadsafe(self._dispatch, key, header, payload)
        except RuntimeError:
            pass  # loop shutting down

    def _dispatch(self, key, header, payload):
        header["t_hub_ns"] = time.time_ns()
        header["key"] = key
        self.latest[key] = (header, payload)
        for client in self.clients:
            if key in client.slots:
                client.dropped[key] = client.dropped.get(key, 0) + 1
                sensor = header.get("sensor")
                if sensor:
                    self.stats.get(sensor).dropped += 1
            client.slots[key] = (header, payload)
            client.event.set()

    async def serve(self, client):
        """Sender loop for one client. Stamps t_sent_ns immediately before transmission."""
        self.clients.add(client)
        try:
            now = time.time_ns()
            for key, msg in self.latest.items():
                # New clients get the last value of each stream, but never stale sensor data
                # (e.g. arm positions from a session that ended minutes ago).
                if "sensor" in msg[0] and now - msg[0]["t_hub_ns"] > 2_000_000_000:
                    continue
                client.slots[key] = msg
            client.event.set()
            while not client.ws.closed:
                await client.event.wait()
                client.event.clear()
                # Sensor messages are flow-controlled by client acks: a key with MAX_INFLIGHT
                # unacknowledged messages stays in its slot (and is replaced by newer data) instead
                # of queueing in socket buffers, which would add seconds of latency on slow links.
                ready = [k for k, (h, _) in client.slots.items()
                         if "sensor" not in h or client.inflight.get(k, 0) < MAX_INFLIGHT]
                pending = [client.slots.pop(k) for k in ready]
                for k, (h, _) in zip(ready, pending):
                    if "sensor" in h:
                        client.inflight[k] = client.inflight.get(k, 0) + 1
                for header, payload in pending:
                    header = dict(header, t_sent_ns=time.time_ns())
                    sensor = header.get("sensor")
                    if sensor and "t_recv_ns" in header:
                        self.stats.get(sensor).add_stage(
                            "server_ms", (header["t_sent_ns"] - header["t_recv_ns"]) / 1e6)
                    if payload is None:
                        await client.ws.send_str(json.dumps(header))
                    else:
                        raw = json.dumps(header).encode()
                        await client.ws.send_bytes(struct.pack("<I", len(raw)) + raw + payload)
        except (ConnectionResetError, ClientConnectionResetError):
            pass  # client went away mid-send
        finally:
            self.clients.discard(client)


def decode_binary(data):
    (n,) = struct.unpack_from("<I", data)
    return json.loads(data[4:4 + n]), data[4 + n:]
