#!/usr/bin/env python3
"""
Print monitor — periodic 10s video clips + Discord notifications.
Schedule: first clip at T+10min, then every 30min, plus completion/failure.
Sends clips directly to Discord with camera footage from the printer.
Includes first-layer vision check via local Gemma 4 model.
"""
import base64
import json
import os
import subprocess
import tempfile
import threading
import time

import requests

VISION_API_URL = "http://localhost:8080/v1/chat/completions"
FIRST_LAYER_PROMPT = (
    "This is a photo of a 3D printer bed after the first few layers of printing. "
    "Analyze the print quality. Look for: poor bed adhesion, warping or lifting at "
    "corners/edges, gaps in extrusion lines, stringing/spaghetti, uneven first layer, "
    "layer shifting. Rate the print as GOOD, WARNING, or FAIL. Explain what you see."
)

CLAWHIP = os.path.expanduser("~/.cargo/bin/clawhip")

# Discord config loaded from ~/.snailprint/discord.json (NOT in repo)
_discord_config_path = os.path.expanduser("~/.snailprint/discord.json")

# Printer LAN configs
PRINTER_LAN = {
    "22E8AJ612200029": {"ip": "192.168.1.81", "access_code": "ac555123", "name": "P3Pio"},
    "22E8AJ5C2800915": {"ip": "192.168.1.154", "access_code": "cf972ede", "name": "P2D2"},
}


def _load_discord_config():
    if os.path.exists(_discord_config_path):
        return json.loads(open(_discord_config_path).read())
    return None


def _capture_clip(serial, output_path, duration=10):
    """Capture a video clip from the printer via RTSP."""
    printer = PRINTER_LAN.get(serial)
    if not printer:
        return False
    url = f"rtsps://bblp:{printer['access_code']}@{printer['ip']}:322/streaming/live/1"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-rtsp_transport", "tcp", "-i", url,
             "-t", str(duration), "-c:v", "libx264", "-crf", "23",
             "-c:a", "aac", "-movflags", "+faststart",
             str(output_path)],
            capture_output=True, timeout=duration + 15,
        )
        return result.returncode == 0 and os.path.exists(output_path)
    except Exception as e:
        print(f"[MONITOR] Clip capture failed: {e}")
        return False


def _capture_snapshot(serial, output_path):
    """Capture a single JPEG frame from the printer via RTSP."""
    printer = PRINTER_LAN.get(serial)
    if not printer:
        return False
    url = f"rtsps://bblp:{printer['access_code']}@{printer['ip']}:322/streaming/live/1"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-rtsp_transport", "tcp", "-i", url,
             "-frames:v", "1", "-update", "1", str(output_path)],
            capture_output=True, timeout=15,
        )
        return result.returncode == 0 and os.path.exists(output_path)
    except Exception as e:
        print(f"[MONITOR] Snapshot failed: {e}")
        return False


def _send_discord(message, file_path=None):
    """Send a message (with optional file) to Discord."""
    config = _load_discord_config()
    if not config:
        # Fallback to ClawHip text-only
        subprocess.run([CLAWHIP, "send", "--message", message],
                       capture_output=True, timeout=30)
        return

    data = {"content": message}
    headers = {"Authorization": f"Bot {config['token']}"}
    url = f"https://discord.com/api/v10/channels/{config['channel']}/messages"

    try:
        if file_path and os.path.exists(file_path):
            ext = os.path.splitext(file_path)[1]
            mime = "video/mp4" if ext == ".mp4" else "image/jpeg"
            fname = f"print_update{ext}"
            with open(file_path, "rb") as f:
                resp = requests.post(url, headers=headers, data=data,
                                     files={"file": (fname, f, mime)}, timeout=60)
        else:
            resp = requests.post(url, headers=headers, json=data, timeout=15)

        if resp.status_code != 200:
            print(f"[MONITOR] Discord API error: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        print(f"[MONITOR] Discord send failed: {e}")
        # Fallback
        subprocess.run([CLAWHIP, "send", "--message", message],
                       capture_output=True, timeout=30)


class PrintMonitor:
    """Monitors a print job with camera clips and Discord updates."""

    def __init__(self, bambu_cloud, serial, job_name="print"):
        self.cloud = bambu_cloud
        self.serial = serial
        self.job_name = job_name
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """Start monitoring in a background thread."""
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop monitoring."""
        self._stop.set()

    def _run(self):
        """Monitor loop: first-layer vision check at 10min, then clips every 30min."""
        # Wait 10 minutes for first check
        if self._wait(600):
            return

        self._first_layer_check()

        # Then every 30 minutes
        while not self._stop.is_set():
            if self._wait(1800):
                return

            status = self.cloud.get_job_status(self.serial)
            if status is None:
                _send_discord(f"🖨️ **{self.job_name}** — Lost connection to printer")
                return

            progress = status.get("progress", 0)
            time_left = status.get("time_remaining", "unknown")
            state = status.get("gcode_state", "")

            if progress >= 100 or state == "FINISH":
                self._send_update("complete! 🎉")
                return
            elif state == "IDLE" and progress == 0:
                _send_discord(f"🖨️ **{self.job_name}** — Print stopped or failed")
                return
            else:
                self._send_update(f"{progress}% done, {time_left} remaining")

    def _first_layer_check(self):
        """Grab a snapshot, run vision analysis, and send results to Discord."""
        # Also send the usual clip update
        self._send_update("just started — first layer check")

        # Grab a still frame for vision analysis
        snap_path = tempfile.mktemp(suffix=".jpg", prefix="first_layer_")
        if not _capture_snapshot(self.serial, snap_path):
            print("[MONITOR] First layer check: failed to capture snapshot")
            return

        # Send to local vision model
        try:
            with open(snap_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()

            resp = requests.post(VISION_API_URL, json={
                "model": "default",
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": FIRST_LAYER_PROMPT},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{b64}"
                    }},
                ]}],
                "max_tokens": 300,
            }, timeout=60)

            if resp.status_code != 200:
                print(f"[MONITOR] Vision API error: {resp.status_code}")
                return

            analysis = resp.json()["choices"][0]["message"]["content"]
            print(f"[MONITOR] Vision analysis: {analysis[:120]}...")

            # Determine rating from the response
            analysis_upper = analysis.upper()
            if "FAIL" in analysis_upper:
                rating = "FAIL"
            elif "WARNING" in analysis_upper:
                rating = "WARNING"
            else:
                rating = "GOOD"

            if rating == "GOOD":
                msg = (f"\U0001f5a8\ufe0f **{self.job_name}** — "
                       f"First layer check passed \u2705\n\n{analysis}")
            else:
                emoji = "\U0001f6a8" if rating == "FAIL" else "\u26a0\ufe0f"
                msg = (f"{emoji} **{self.job_name}** — "
                       f"First layer **{rating}**\n\n{analysis}")

            _send_discord(msg, snap_path)

        except Exception as e:
            print(f"[MONITOR] Vision check failed: {e}")
            # Still send the snapshot without analysis
            _send_discord(
                f"\U0001f5a8\ufe0f **{self.job_name}** — First layer snapshot "
                f"(vision check unavailable)",
                snap_path,
            )
        finally:
            try:
                os.unlink(snap_path)
            except OSError:
                pass

    def _wait(self, seconds):
        """Wait N seconds, checking stop flag. Returns True if stopped."""
        elapsed = 0
        while elapsed < seconds:
            if self._stop.is_set():
                return True
            time.sleep(min(10, seconds - elapsed))
            elapsed += 10
        return False

    def _send_update(self, message):
        """Capture a 10s video clip and send to Discord."""
        clip_path = tempfile.mktemp(suffix=".mp4", prefix="print_clip_")
        msg = f"🖨️ **{self.job_name}** — {message}"

        got_clip = _capture_clip(self.serial, clip_path, duration=10)

        if got_clip:
            _send_discord(msg, clip_path)
            try:
                os.unlink(clip_path)
            except OSError:
                pass
        else:
            # Fallback to snapshot
            snap_path = tempfile.mktemp(suffix=".jpg", prefix="print_snap_")
            got_snap = _capture_snapshot(self.serial, snap_path)
            if got_snap:
                _send_discord(msg, snap_path)
                try:
                    os.unlink(snap_path)
                except OSError:
                    pass
            else:
                _send_discord(msg)
