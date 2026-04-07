#!/usr/bin/env python3
"""
Bambu Camera — capture JPEG frames from Bambu Lab printers using libBambuSource.

Works with ALL Bambu printer models (P2S, H2S, X1C, P1S, A1, etc.) because it
uses the same native library that Bambu Studio uses internally. This bypasses
the RTSP/BRTC protocol differences entirely.

Requirements:
  - Bambu Studio installed (provides libBambuSource.dylib in its plugins dir)
  - Printer on the same LAN with access code known

Usage:
  python3 bambu_camera.py <printer_ip> <access_code> <serial> [output.jpg]

  # Or as a module:
  from bambu_camera import BambuCamera
  cam = BambuCamera("192.168.1.81", "ac555123", "22E8AJ612200029")
  jpeg_bytes = cam.capture_frame()
"""

import ctypes
import ctypes.util
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate libBambuSource.dylib
# ---------------------------------------------------------------------------
_LIB_SEARCH_PATHS = [
    Path.home() / "Library/Application Support/BambuStudio/plugins/libBambuSource.dylib",
    Path("/Applications/BambuStudio.app/Contents/Frameworks/libBambuSource.dylib"),
    Path("/Applications/BambuStudio.app/Contents/MacOS/libBambuSource.dylib"),
]


def _find_lib():
    for p in _LIB_SEARCH_PATHS:
        if p.exists():
            return str(p)
    raise FileNotFoundError(
        "libBambuSource.dylib not found. Is Bambu Studio installed?\n"
        f"Searched: {[str(p) for p in _LIB_SEARCH_PATHS]}"
    )


# ---------------------------------------------------------------------------
# C type definitions mirroring bambu_tunnel.h
# ---------------------------------------------------------------------------

# Bambu_StreamType
VIDE = 0
AUDI = 1

# Bambu_VideoSubType
AVC1 = 0
MJPG = 1

# Bambu_FormatType
VIDEO_AVC_PACKET = 0
VIDEO_AVC_BYTE_STREAM = 1
VIDEO_JPEG = 2
AUDIO_RAW = 3
AUDIO_ADTS = 4

# Bambu_Error
BAMBU_SUCCESS = 0
BAMBU_STREAM_END = 1
BAMBU_WOULD_BLOCK = 2
BAMBU_BUFFER_LIMIT = 3


class _VideoFormat(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("frame_rate", ctypes.c_int),
    ]


class _AudioFormat(ctypes.Structure):
    _fields_ = [
        ("sample_rate", ctypes.c_int),
        ("channel_count", ctypes.c_int),
        ("sample_size", ctypes.c_int),
    ]


class _FormatUnion(ctypes.Union):
    _fields_ = [
        ("video", _VideoFormat),
        ("audio", _AudioFormat),
    ]


class BambuStreamInfo(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("sub_type", ctypes.c_int),
        ("format", _FormatUnion),
        ("format_type", ctypes.c_int),
        ("format_size", ctypes.c_int),
        ("max_frame_size", ctypes.c_int),
        ("format_buffer", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class BambuSample(ctypes.Structure):
    _fields_ = [
        ("itrack", ctypes.c_int),
        ("size", ctypes.c_int),
        ("flags", ctypes.c_int),
        ("buffer", ctypes.POINTER(ctypes.c_ubyte)),
        ("decode_time", ctypes.c_ulonglong),
    ]


# Logger callback type: void (*Logger)(void* context, int level, tchar const* msg)
LOGGER_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p)


def _setup_lib(lib):
    """Set up function signatures for the Bambu library."""
    lib.Bambu_Init.restype = ctypes.c_int
    lib.Bambu_Init.argtypes = []

    lib.Bambu_Deinit.restype = None
    lib.Bambu_Deinit.argtypes = []

    lib.Bambu_Create.restype = ctypes.c_int
    lib.Bambu_Create.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p]

    lib.Bambu_Open.restype = ctypes.c_int
    lib.Bambu_Open.argtypes = [ctypes.c_void_p]

    lib.Bambu_StartStream.restype = ctypes.c_int
    lib.Bambu_StartStream.argtypes = [ctypes.c_void_p, ctypes.c_int]

    lib.Bambu_GetStreamCount.restype = ctypes.c_int
    lib.Bambu_GetStreamCount.argtypes = [ctypes.c_void_p]

    lib.Bambu_GetStreamInfo.restype = ctypes.c_int
    lib.Bambu_GetStreamInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(BambuStreamInfo),
    ]

    lib.Bambu_ReadSample.restype = ctypes.c_int
    lib.Bambu_ReadSample.argtypes = [ctypes.c_void_p, ctypes.POINTER(BambuSample)]

    lib.Bambu_Close.restype = None
    lib.Bambu_Close.argtypes = [ctypes.c_void_p]

    lib.Bambu_Destroy.restype = None
    lib.Bambu_Destroy.argtypes = [ctypes.c_void_p]

    lib.Bambu_GetLastErrorMsg.restype = ctypes.c_char_p
    lib.Bambu_GetLastErrorMsg.argtypes = []

    lib.Bambu_SetLogger.restype = None
    lib.Bambu_SetLogger.argtypes = [ctypes.c_void_p, LOGGER_FUNC, ctypes.c_void_p]

    lib.Bambu_FreeLogMsg.restype = None
    lib.Bambu_FreeLogMsg.argtypes = [ctypes.c_char_p]

    return lib


# Singleton library handle
_lib = None
_initialized = False


def _get_lib():
    global _lib, _initialized
    if _lib is None:
        path = _find_lib()
        _lib = ctypes.cdll.LoadLibrary(path)
        _setup_lib(_lib)
    if not _initialized:
        res = _lib.Bambu_Init()
        if res != BAMBU_SUCCESS:
            raise RuntimeError(f"Bambu_Init failed with code {res}")
        _initialized = True
    return _lib


# ---------------------------------------------------------------------------
# BambuCamera class
# ---------------------------------------------------------------------------

class BambuCamera:
    """Capture JPEG frames from a Bambu Lab printer camera."""

    # URL format from BambuStudio source (MediaPlayCtrl.cpp)
    URL_FORMAT = "bambu:///local/{ip}.?port=6000&user=bblp&passwd={code}&device={serial}&version=00.00.00.00"

    def __init__(self, ip: str, access_code: str, serial: str, verbose: bool = False):
        self.ip = ip
        self.access_code = access_code
        self.serial = serial
        self.verbose = verbose
        self._lib = _get_lib()
        self._tunnel = None
        self._stream_started = False

    def _log(self, msg):
        if self.verbose:
            print(f"[BambuCamera] {msg}", file=sys.stderr)

    def connect(self, timeout: float = 30.0):
        """Open connection and start video stream."""
        if self._tunnel is not None:
            return

        url = self.URL_FORMAT.format(
            ip=self.ip, code=self.access_code, serial=self.serial
        )
        self._log(f"Connecting: {url.replace(self.access_code, '***')}")

        # Create tunnel
        tunnel = ctypes.c_void_p()
        res = self._lib.Bambu_Create(ctypes.byref(tunnel), url.encode("utf-8"))
        if res != BAMBU_SUCCESS:
            err = self._lib.Bambu_GetLastErrorMsg()
            raise RuntimeError(f"Bambu_Create failed: {res} — {err}")
        self._tunnel = tunnel

        # Set up logger in verbose mode
        if self.verbose:
            @LOGGER_FUNC
            def _logger_cb(ctx, level, msg):
                try:
                    if msg:
                        text = msg.decode("utf-8", errors="replace")
                        print(f"  Bambu<{level}>: {text}", file=sys.stderr, flush=True)
                except Exception:
                    pass

            self._logger_ref = _logger_cb  # prevent GC
            self._lib.Bambu_SetLogger(self._tunnel, _logger_cb, None)

        # Open tunnel
        res = self._lib.Bambu_Open(self._tunnel)
        if res != BAMBU_SUCCESS:
            err = self._lib.Bambu_GetLastErrorMsg()
            raise RuntimeError(f"Bambu_Open failed: {res} — {err}")
        self._log("Tunnel opened")

        # Start video stream (1 = video)
        deadline = time.time() + timeout
        while True:
            res = self._lib.Bambu_StartStream(self._tunnel, 1)
            if res == BAMBU_SUCCESS:
                break
            elif res == BAMBU_WOULD_BLOCK:
                if time.time() > deadline:
                    raise TimeoutError("Timed out waiting for stream to start")
                time.sleep(0.1)
            else:
                err = self._lib.Bambu_GetLastErrorMsg()
                raise RuntimeError(f"Bambu_StartStream failed: {res} — {err}")

        self._stream_started = True
        self._log("Stream started")

        # Verify stream info
        count = self._lib.Bambu_GetStreamCount(self._tunnel)
        self._log(f"Stream count: {count}")

        if count >= 1:
            info = BambuStreamInfo()
            res = self._lib.Bambu_GetStreamInfo(self._tunnel, 1, ctypes.byref(info))
            if res == BAMBU_SUCCESS:
                self._log(
                    f"Stream: {info.format.video.width}x{info.format.video.height} "
                    f"@ {info.format.video.frame_rate}fps, "
                    f"sub_type={'MJPG' if info.sub_type == MJPG else 'AVC1'}, "
                    f"format_type={info.format_type}"
                )

    def read_frame(self, timeout: float = 10.0) -> bytes:
        """Read one raw frame from the stream. Returns bytes (JPEG or H.264 NAL)."""
        if self._tunnel is None:
            raise RuntimeError("Not connected. Call connect() first.")

        sample = BambuSample()
        deadline = time.time() + timeout

        while True:
            res = self._lib.Bambu_ReadSample(self._tunnel, ctypes.byref(sample))
            if res == BAMBU_SUCCESS:
                # Copy buffer contents before it gets reused
                size = sample.size
                buf = bytes(ctypes.cast(sample.buffer, ctypes.POINTER(ctypes.c_ubyte * size)).contents)
                self._log(f"Frame: {size} bytes, flags={sample.flags}")
                return buf
            elif res == BAMBU_WOULD_BLOCK:
                if time.time() > deadline:
                    raise TimeoutError("Timed out waiting for frame")
                time.sleep(0.05)
            elif res == BAMBU_STREAM_END:
                raise RuntimeError("Stream ended unexpectedly")
            else:
                err = self._lib.Bambu_GetLastErrorMsg()
                raise RuntimeError(f"Bambu_ReadSample failed: {res} — {err}")

    def capture_frame(self, timeout: float = 30.0) -> bytes:
        """
        High-level: connect, grab one frame, return as JPEG bytes.
        If the stream is H.264 (AVC1), decodes one keyframe to JPEG.
        If the stream is MJPEG, returns the raw JPEG directly.
        """
        self.connect(timeout=timeout)

        # Get stream info to determine codec
        info = BambuStreamInfo()
        self._lib.Bambu_GetStreamInfo(self._tunnel, 1, ctypes.byref(info))

        # Read frames until we get a good one
        # For AVC1, we need a keyframe (sync flag), then decode to JPEG
        # For MJPEG, any frame is a complete JPEG
        is_mjpeg = (info.sub_type == MJPG) or (info.format_type == VIDEO_JPEG)

        for attempt in range(30):  # try up to 30 frames to get a keyframe
            frame = self.read_frame(timeout=timeout)

            if is_mjpeg:
                # MJPEG: frame is already a JPEG
                if frame[:2] == b'\xff\xd8':
                    return frame
                self._log(f"Non-JPEG MJPEG frame (attempt {attempt}), retrying...")
                continue

            # AVC1/H.264: need to decode with ffmpeg
            # Look for a keyframe (sync flag = 1)
            sample = BambuSample()
            # We already read the frame, check if it starts with a NAL sync
            # For simplicity, decode with ffmpeg subprocess
            return self._decode_h264_frame(frame, info)

        raise RuntimeError("Could not capture a valid frame after 30 attempts")

    def _decode_h264_frame(self, nal_data: bytes, info: BambuStreamInfo) -> bytes:
        """Decode an H.264 NAL unit to JPEG using ffmpeg."""
        import subprocess
        import tempfile

        # Write H.264 data to temp file
        with tempfile.NamedTemporaryFile(suffix=".h264", delete=False) as f:
            # If we have SPS/PPS in format_buffer, prepend it
            if info.format_buffer and info.format_size > 0:
                header = bytes(
                    ctypes.cast(
                        info.format_buffer,
                        ctypes.POINTER(ctypes.c_ubyte * info.format_size),
                    ).contents
                )
                f.write(header)
            f.write(nal_data)
            h264_path = f.name

        jpg_path = h264_path.replace(".h264", ".jpg")

        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", h264_path,
                    "-frames:v", "1",
                    "-q:v", "2",
                    jpg_path,
                ],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0 and os.path.exists(jpg_path):
                return Path(jpg_path).read_bytes()
            else:
                raise RuntimeError(
                    f"ffmpeg decode failed: {result.stderr.decode(errors='replace')[-200:]}"
                )
        finally:
            for p in [h264_path, jpg_path]:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def close(self):
        """Disconnect from the printer."""
        if self._tunnel is not None:
            try:
                self._lib.Bambu_Close(self._tunnel)
            except Exception:
                pass
            try:
                self._lib.Bambu_Destroy(self._tunnel)
            except Exception:
                pass
            self._tunnel = None
            self._stream_started = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        self.close()


# ---------------------------------------------------------------------------
# Convenience function for snailprint integration
# ---------------------------------------------------------------------------

# Known printers (name -> (serial, access_code))
KNOWN_PRINTERS = {
    "P2D2": ("22E8AJ5C2800915", "cf972ede"),
    "P3Pio": ("22E8AJ612200029", "ac555123"),
}


def discover_printer_ips():
    """
    Discover Bambu printer IPs via cloud MQTT (reads net.info.ip as int).
    Returns dict of serial -> ip.
    """
    import json
    import struct
    import socket
    import threading

    token_path = Path.home() / ".snailprint/token.json"
    if not token_path.exists():
        return {}

    token_data = json.loads(token_path.read_text())
    token = token_data.get("token")
    if not token:
        return {}

    try:
        import paho.mqtt.client as mqtt
        from bambulab import BambuClient
    except ImportError:
        return {}

    api = BambuClient(token=token)
    info = api.get_user_info()
    user_id = info.get("uid") or info.get("user_id") or info.get("id", "")

    results = {}
    received = threading.Event()

    serials = [v[0] for v in KNOWN_PRINTERS.values()]

    def on_connect(client, userdata, flags, reason_code, properties=None):
        code = reason_code if isinstance(reason_code, int) else reason_code.value
        if code == 0:
            for serial in serials:
                client.subscribe(f"device/{serial}/report")
                client.publish(
                    f"device/{serial}/request",
                    json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}),
                )

    def on_message(client, userdata, msg):
        data = json.loads(msg.payload.decode())
        serial = msg.topic.split("/")[1]
        print_data = data.get("print", {})
        if not print_data:
            return
        net = print_data.get("net", {})
        if net:
            net_info = net.get("info", [])
            if net_info and net_info[0].get("ip", 0) != 0:
                ip_int = net_info[0]["ip"]
                ip_str = socket.inet_ntoa(struct.pack("<I", ip_int))
                results[serial] = ip_str
                if len(results) >= len(serials):
                    received.set()

    try:
        mqttc = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
        mqttc.username_pw_set(f"u_{user_id}", token)
        mqttc.tls_set()
        mqttc.on_connect = on_connect
        mqttc.on_message = on_message
        mqttc.connect("us.mqtt.bambulab.com", 8883, keepalive=10)
        mqttc.loop_start()
        received.wait(timeout=15)
        mqttc.loop_stop()
        mqttc.disconnect()
    except Exception as e:
        print(f"MQTT discovery error: {e}", file=sys.stderr)

    return results


def capture_snapshot(printer_name: str, output_path: str = None, verbose: bool = False) -> bytes:
    """
    Capture a single JPEG frame from a named printer.

    Args:
        printer_name: "P2D2" or "P3Pio"
        output_path: Optional path to save JPEG file
        verbose: Print debug info

    Returns:
        JPEG bytes
    """
    if printer_name not in KNOWN_PRINTERS:
        raise ValueError(f"Unknown printer: {printer_name}. Known: {list(KNOWN_PRINTERS.keys())}")

    serial, access_code = KNOWN_PRINTERS[printer_name]

    # Discover IP
    print(f"Discovering IP for {printer_name}...", file=sys.stderr)
    ips = discover_printer_ips()
    ip = ips.get(serial)
    if not ip:
        raise RuntimeError(f"Could not discover IP for {printer_name} ({serial})")

    print(f"Found {printer_name} at {ip}", file=sys.stderr)

    with BambuCamera(ip, access_code, serial, verbose=verbose) as cam:
        jpeg = cam.capture_frame()

    if output_path:
        Path(output_path).write_bytes(jpeg)
        print(f"Saved {len(jpeg)} bytes to {output_path}", file=sys.stderr)

    return jpeg


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage:")
        print(f"  {sys.argv[0]} <printer_name> [output.jpg]       # e.g. P2D2, P3Pio")
        print(f"  {sys.argv[0]} <ip> <access_code> <serial> [output.jpg]")
        print()
        print(f"Known printers: {list(KNOWN_PRINTERS.keys())}")
        sys.exit(1)

    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    args = [a for a in sys.argv[1:] if a not in ("--verbose", "-v")]

    if len(args) == 1 or (len(args) == 2 and args[1].endswith(".jpg")):
        # Named printer mode
        name = args[0]
        output = args[1] if len(args) > 1 else f"{name}_snapshot.jpg"
        jpeg = capture_snapshot(name, output, verbose=verbose)
        print(f"Captured {len(jpeg)} byte JPEG from {name}")

    elif len(args) >= 3:
        # Direct IP mode
        ip, code, serial = args[0], args[1], args[2]
        output = args[3] if len(args) > 3 else "snapshot.jpg"

        with BambuCamera(ip, code, serial, verbose=verbose) as cam:
            jpeg = cam.capture_frame()
            Path(output).write_bytes(jpeg)
            print(f"Captured {len(jpeg)} byte JPEG -> {output}")
    else:
        print("Invalid arguments. Run without args for usage.", file=sys.stderr)
        sys.exit(1)
