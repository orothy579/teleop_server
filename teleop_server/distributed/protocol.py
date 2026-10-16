"""Versioned bounded packets; RGB PNG/JPEG, bit-exact depth (lz4 or zlib).

No pickle and no host-native object deserialization. RGB and depth share one frame
descriptor and are never sent separately. The default recording transport is PNG.
"""
import json
import struct
import zlib

import lz4.frame
import time

import cv2
import numpy as np

VERSION = 1
# Silence on the control link after which both hosts end the session and the Jetson stops arm
# control. Arm teleoperation runs on the Jetson (leader -> follower), so a short Wi-Fi stall does not
# need to stop it; the base has its own 0.25 s command dead-man and stops as soon as commands stop.
LINK_TIMEOUT_S = 3.0
GAP_LOG_S = 0.5  # control gaps longer than this are logged and counted (diagnosis)
MAX_PACKET = 32 * 1024 * 1024
MAX_HEADER = 4 * 1024 * 1024
MAX_PIXELS = 640 * 480
PREVIEW_JPEG_QUALITY = 85  # Live view only; the Jetson recorder uses the original RGB frames.


def encode_camera(name, frame, info, rgb_codec="png", depth_compressor="lz4"):
    """Compress one camera's RGB/depth pair. Thread-safe; cameras are encoded in parallel.

    Depth stays bit-exact: integer sensor units, a reversible row predictor and byte planes, then
    lz4 (default; ~7x faster than zlib on the Jetson, ~30 % larger) or zlib.
    """
    started = time.perf_counter()
    bgr = cv2.cvtColor(frame.rgb, cv2.COLOR_RGB2BGR)
    options = ([cv2.IMWRITE_PNG_COMPRESSION, 1] if rgb_codec == "png" else
               [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_JPEG_QUALITY])
    ok, rgb = cv2.imencode(".png" if rgb_codec == "png" else ".jpg", bgr, options)
    if not ok:
        raise ValueError("RGB encoding failed")
    rgb = rgb.tobytes()
    depth, depth_codec = b"", "f32-zlib"
    if frame.depth is not None:
        depth_codec, depth = encode_depth(frame.depth, info.get("depth_scale_m"), depth_compressor,
                                          getattr(frame, "depth_units", None))
    descriptor = dict(name=name, seq=frame.seq, frame_number=frame.frame_number,
                      t_recv_ns=frame.t_recv_ns, depth_dt_ms=frame.depth_dt_ms,
                      shape=list(frame.rgb.shape[:2]), rgb_codec=rgb_codec, depth_codec=depth_codec,
                      depth_shape=None if frame.depth is None else list(frame.depth.shape[:2]),
                      rgb_bytes=len(rgb), depth_bytes=len(depth), info=dict(info),
                      encode_ms=(time.perf_counter()-started)*1000)
    return descriptor, rgb, depth


def _units_blob(units, compressor):
    """uint16 units -> row predictor (mod 2**16), zigzag, low/high byte planes, then lz4/zlib.
    Few numpy passes: the Jetson runs this for 3 cameras at 30 Hz (~2.6 ms per frame)."""
    units = np.ascontiguousarray(units, dtype="<u2")
    delta = np.empty_like(units)
    delta[:, 0] = units[:, 0]
    np.subtract(units[:, 1:], units[:, :-1], out=delta[:, 1:])
    zigzag = (delta << 1) ^ (delta.view("<i2") >> 15).view("<u2")
    planes = np.empty((2,) + units.shape, np.uint8)
    np.bitwise_and(zigzag, 255, out=planes[0], casting="unsafe")
    np.right_shift(zigzag, 8, out=planes[1], casting="unsafe")
    if compressor == "lz4":
        return "u16-delta-lz4", lz4.frame.compress(planes, compression_level=0, store_size=True)
    return "u16-delta-zlib", zlib.compress(planes, 1)


def encode_depth(depth_m, scale, compressor="lz4", units=None):
    """Lossless depth blob. On the camera's unit grid: uint16 units, reversible row predictor and
    byte planes, then lz4/zlib. Otherwise the float32 values in zlib. Returns (codec, bytes).
    ``units`` (the camera's raw uint16 image of the same depth) skips the float round trip."""
    if units is not None and scale and scale > 0:
        return _units_blob(units, compressor)
    array = np.asarray(depth_m, dtype="<f4")
    if scale and scale > 0 and np.max(array) <= 65535 * scale:
        units = np.rint(array / np.float32(scale)).astype("<u2")
        if np.array_equal(units.astype(np.float32) * np.float32(scale), array):
            return _units_blob(units, compressor)
    return "f32-zlib", zlib.compress(array.tobytes(), 1)


def decode_depth(blob, codec, shape, scale):
    """Inverse of ``encode_depth``: float32 metres (bit-exact). Raises ValueError on bad input."""
    dh, dw = shape
    if codec not in ("u16-zlib", "u16-delta-zlib", "u16-delta-lz4", "f32-zlib"):
        raise ValueError("unknown depth codec")
    size = dh*dw*(4 if codec == "f32-zlib" else 2)
    blob = bytes(blob)
    if codec == "u16-delta-lz4":
        # Check the declared size before inflating, so a bad blob cannot allocate freely.
        try:
            declared = lz4.frame.get_frame_info(blob).get("content_size")
        except RuntimeError as exc:
            raise ValueError("invalid depth payload") from exc
        if declared != size:
            raise ValueError("invalid depth payload")
        try:
            raw, used = lz4.frame.decompress(blob, return_bytes_read=True)
        except RuntimeError as exc:
            raise ValueError("invalid depth payload") from exc
        if len(raw) != size or used != len(blob):
            raise ValueError("invalid depth payload")
    else:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(blob, size+1)
        if len(raw) != size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("invalid depth payload")
    if codec in ("u16-delta-zlib", "u16-delta-lz4"):
        planes = np.frombuffer(raw, dtype=np.uint8).reshape(2, dh, dw).astype(np.int32)
        zigzag = planes[0] | (planes[1] << 8)
        delta = (zigzag >> 1) ^ -(zigzag & 1)
        depth = np.cumsum(delta, axis=1, dtype=np.int32).astype(np.uint16).astype(np.float32)
    else:
        depth = np.frombuffer(raw, dtype="<u2" if codec == "u16-zlib" else "<f4").reshape(dh, dw).astype(np.float32)
    if codec != "f32-zlib":
        if not isinstance(scale, (int, float)) or not 0 < scale < 1:
            raise ValueError("invalid depth scale")
        depth *= np.float32(scale)
    if not np.isfinite(depth).all() or (depth < 0).any():
        raise ValueError("invalid depth values")
    return depth


def pack_packet(meta, encoded):
    """Assemble already encoded cameras (from ``encode_camera``) into one bounded packet."""
    meta = dict(meta, version=VERSION, cameras=[])
    blobs = []
    offset = 0
    for descriptor, rgb, depth in encoded:
        meta["cameras"].append(dict(descriptor, offset=offset))
        blobs.extend((rgb, depth))
        offset += len(rgb) + len(depth)
    raw = json.dumps(meta, allow_nan=False, separators=(",", ":")).encode()
    packet = struct.pack("<I", len(raw)) + raw + b"".join(blobs)
    if len(raw) > MAX_HEADER or len(packet) > MAX_PACKET:
        raise ValueError("packet exceeds transport limit")
    return packet


def encode_packet(meta, frames, rgb_codec="png", depth_compressor="lz4"):
    started = time.perf_counter()
    encoded = [encode_camera(name, frame, info, rgb_codec, depth_compressor) for name, frame, info in frames]
    return pack_packet(dict(meta, encode_ms=(time.perf_counter()-started)*1000), encoded)


def decode_packet(data, with_encoded_rgb=False):
    if not 4 <= len(data) <= MAX_PACKET:
        raise ValueError("invalid packet size")
    n, = struct.unpack_from("<I", data)
    if not 0 < n <= MAX_HEADER or n + 4 > len(data):
        raise ValueError("invalid header size")
    meta = json.loads(data[4:4+n])
    if meta.get("version") != VERSION:
        raise ValueError("unsupported protocol")
    payload = memoryview(data)[4+n:]
    frames, end = {}, 0
    descriptors = meta.get("cameras", [])
    if len(descriptors) > 3:
        raise ValueError("too many cameras")
    for cam in descriptors:
        name = cam["name"]
        if name not in ("camera1", "camera2", "camera3") or name in frames:
            raise ValueError("invalid camera identity")
        h, w = cam["shape"]
        if not (isinstance(h, int) and isinstance(w, int) and 0 < h <= 480 and 0 < w <= 640):
            raise ValueError("invalid image dimensions")
        offset, nr, nd = cam["offset"], cam["rgb_bytes"], cam["depth_bytes"]
        if not all(isinstance(x, int) for x in (offset, nr, nd)) or offset != end or nr <= 0 or nd < 0:
            raise ValueError("invalid blob offsets")
        end = offset + nr + nd
        if end > len(payload):
            raise ValueError("truncated image payload")
        rgb_data = payload[offset:offset+nr]
        # Verify encoded image dimensions before OpenCV allocates the decoded image.
        from PIL import Image
        from io import BytesIO
        with Image.open(BytesIO(rgb_data)) as im:
            if (im.size != (w, h) or im.format not in ("PNG", "JPEG") or
                    im.format != cam.get("rgb_codec", "png").upper()):
                raise ValueError("encoded dimensions mismatch")
        bgr = cv2.imdecode(np.frombuffer(rgb_data, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None or bgr.shape != (h, w, 3):
            raise ValueError("invalid RGB image")
        depth = None
        if nd:
            # Raw (unaligned) depth may have its own resolution; older packets share the RGB shape.
            dh, dw = cam.get("depth_shape") or (h, w)
            if not (isinstance(dh, int) and isinstance(dw, int) and 0 < dh <= 480 and 0 < dw <= 640):
                raise ValueError("invalid depth dimensions")
            depth = decode_depth(payload[offset+nr:end], cam.get("depth_codec"), (dh, dw),
                                 cam["info"].get("depth_scale_m"))
        decoded = (cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), depth, cam)
        frames[name] = (*decoded, bytes(rgb_data)) if with_encoded_rgb else decoded
    if end != len(payload):
        raise ValueError("unexpected trailing bytes")
    return meta, frames
