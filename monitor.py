#!/usr/bin/env python3
"""
Print monitor — periodic camera snapshots + Discord notifications via ClawHip.
Schedule: first snapshot at T+10min, then every 30min, plus completion/failure.
"""
import os
import subprocess
import tempfile
import threading
import time

CLAWHIP = os.path.expanduser("~/.cargo/bin/clawhip")


class PrintMonitor:
    """Monitors a print job with camera snapshots and Discord updates."""

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
        """Monitor loop: first pic at 10min, then every 30min."""
        # Wait 10 minutes for first snapshot
        if self._wait(600):  # 10 minutes
            return  # stopped

        self._take_snapshot_and_notify("just started")

        # Then every 30 minutes
        while not self._stop.is_set():
            if self._wait(1800):  # 30 minutes
                return  # stopped

            # Check if print is still going
            status = self.cloud.get_job_status(self.serial)
            if status is None:
                self._notify("Lost connection to printer")
                return

            progress = status.get("progress", 0)
            time_left = status.get("time_remaining", "unknown")

            if progress >= 100:
                self._take_snapshot_and_notify("complete! 🎉")
                return
            elif status.get("status") == "idle" and progress == 0:
                self._notify("Print appears to have stopped or failed")
                return
            else:
                self._take_snapshot_and_notify(
                    f"{progress}% done, {time_left} remaining"
                )

    def _wait(self, seconds):
        """Wait for N seconds, checking stop flag every 10s. Returns True if stopped."""
        elapsed = 0
        while elapsed < seconds:
            if self._stop.is_set():
                return True
            time.sleep(min(10, seconds - elapsed))
            elapsed += 10
        return False

    def _take_snapshot_and_notify(self, message):
        """Take camera snapshot and send to Discord via ClawHip."""
        snap_path = tempfile.mktemp(suffix=".jpg", prefix="print_snap_")

        has_image = self.cloud.get_camera_snapshot(self.serial, snap_path)

        full_msg = f"🖨️ **{self.job_name}** — {message}"

        if has_image and os.path.exists(snap_path):
            self._notify_with_image(full_msg, snap_path)
            os.unlink(snap_path)
        else:
            self._notify(full_msg)

    def _notify(self, message):
        """Send a text message to Discord via ClawHip."""
        try:
            subprocess.run(
                [CLAWHIP, "send", "--message", message],
                capture_output=True,
                timeout=30,
            )
        except Exception as e:
            print(f"[MONITOR] ClawHip notify failed: {e}")

    def _notify_with_image(self, message, image_path):
        """Send a message with image to Discord via ClawHip."""
        try:
            subprocess.run(
                [CLAWHIP, "emit", "print.progress",
                 "--message", message, "--image", image_path],
                capture_output=True,
                timeout=30,
            )
        except Exception as e:
            print(f"[MONITOR] ClawHip image notify failed: {e}")
            # Fallback: text only
            self._notify(message)
