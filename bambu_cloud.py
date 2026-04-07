#!/usr/bin/env python3
"""
Bambu Cloud API client — OAuth login, printer management, AMS inventory,
print job submission, camera snapshots.
"""
import json
import os
import sys
import time
from pathlib import Path

TOKEN_PATH = Path(os.path.expanduser("~/.snailprint/token.json"))
PRINTERS_PATH = Path(os.path.expanduser("~/.snailprint/printers.json"))


class BambuCloud:
    def __init__(self):
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._api = None
        self._load_token()

    def _load_token(self):
        """Load saved auth token if available."""
        if TOKEN_PATH.exists():
            data = json.loads(TOKEN_PATH.read_text())
            self._init_api(data.get("token"), data.get("region", ""))
            return True
        return False

    def _init_api(self, token, region=""):
        """Initialize the bambulab cloud API client."""
        try:
            from bambulab import BambuClient
            self._api = BambuClient(token=token, region=region)
        except ImportError:
            from bambu_lab_cloud_api import BambuClient
            self._api = BambuClient(token=token, region=region)

    def login(self):
        """Interactive OAuth login — opens browser."""
        try:
            from bambulab import BambuAuthenticator
            auth = BambuAuthenticator()
        except ImportError:
            from bambu_lab_cloud_api import BambuAuthenticator
            auth = BambuAuthenticator()

        print("Logging in to Bambu Cloud...")
        token_data = auth.login()

        # Save token
        TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
        print(f"Token saved to {TOKEN_PATH}")

        self._init_api(token_data.get("token"), token_data.get("region", ""))
        return True

    @property
    def is_authenticated(self):
        return self._api is not None

    def list_printers(self):
        """Return list of printers with status and AMS info."""
        if not self.is_authenticated:
            raise RuntimeError("Not authenticated. Run: python3 bambu_cloud.py --login")

        devices = self._api.get_devices()
        printers = []
        for dev in devices:
            printer = {
                "serial": dev.get("dev_id", ""),
                "name": dev.get("name", "Unknown"),
                "model": dev.get("dev_model_name", ""),
                "status": "idle",
                "ams": [],
            }

            # Get print status
            try:
                status = self._api.get_print_status(dev["dev_id"])
                if status:
                    progress = status.get("mc_percent", 0)
                    if progress > 0 and progress < 100:
                        printer["status"] = "printing"
                        printer["progress"] = progress
                    elif status.get("stg_cur", 0) == 0:
                        printer["status"] = "idle"
                    printer["print_status"] = status
            except Exception:
                printer["status"] = "unknown"

            # Get AMS inventory
            try:
                ams_data = self._get_ams(dev["dev_id"])
                printer["ams"] = ams_data
            except Exception:
                pass

            printers.append(printer)

        # Cache printer list
        PRINTERS_PATH.write_text(json.dumps(printers, indent=2))
        return printers

    def _get_ams(self, dev_id):
        """Get AMS slot inventory for a printer."""
        status = self._api.get_print_status(dev_id)
        ams_slots = []

        ams_info = status.get("ams", {})
        if isinstance(ams_info, dict):
            for unit in ams_info.get("ams", []):
                for tray in unit.get("tray", []):
                    slot = {
                        "slot": int(tray.get("id", 0)),
                        "type": tray.get("tray_type", ""),
                        "color": tray.get("tray_color", ""),
                        "color_hex": "#" + tray.get("tray_color", "000000")[-6:],
                        "remaining": int(tray.get("remain", 0)),
                        "name": tray.get("tray_sub_brands", ""),
                    }
                    ams_slots.append(slot)

        return ams_slots

    def find_filament(self, filament_type="PLA", color=None):
        """Find a printer with matching filament in AMS. Returns (serial, slot)."""
        printers = self.list_printers()
        best = None

        for printer in printers:
            if printer["status"] == "printing":
                continue  # skip busy printers

            for slot in printer.get("ams", []):
                type_match = filament_type.upper() in slot.get("type", "").upper()
                if not type_match:
                    continue

                if color:
                    color_match = color.lower() in slot.get("name", "").lower()
                    if not color_match:
                        continue

                # Prefer idle printer with most remaining filament
                if best is None or slot.get("remaining", 0) > best[2]:
                    best = (printer["serial"], slot["slot"], slot.get("remaining", 0))

        return (best[0], best[1]) if best else (None, None)

    def submit_print(self, serial, threemf_path):
        """Upload 3MF and start print on specified printer."""
        if not self.is_authenticated:
            raise RuntimeError("Not authenticated")

        result = self._api.send_print(
            dev_id=serial,
            file_path=threemf_path,
        )
        return result

    def get_job_status(self, serial):
        """Get current print job status for a printer."""
        status = self._api.get_print_status(serial)
        if not status:
            return None

        return {
            "progress": status.get("mc_percent", 0),
            "layer": f"{status.get('layer_num', 0)}/{status.get('total_layer_num', 0)}",
            "time_remaining": self._format_time(status.get("mc_remaining_time", 0)),
            "status": "printing" if status.get("mc_percent", 0) > 0 else "idle",
            "subtask_name": status.get("subtask_name", ""),
        }

    def get_camera_snapshot(self, serial, output_path):
        """Save a camera snapshot from the printer."""
        try:
            frame = self._api.get_camera_frame(serial)
            if frame:
                Path(output_path).write_bytes(frame)
                return True
        except Exception as e:
            print(f"Camera snapshot failed: {e}")
        return False

    @staticmethod
    def _format_time(minutes):
        if minutes <= 0:
            return "done"
        h = minutes // 60
        m = minutes % 60
        if h > 0:
            return f"{h}h {m}m"
        return f"{m}m"


if __name__ == "__main__":
    cloud = BambuCloud()
    if "--login" in sys.argv:
        cloud.login()
    elif cloud.is_authenticated:
        printers = cloud.list_printers()
        for p in printers:
            print(f"{p['name']} ({p['serial']}) — {p['status']}")
            for slot in p.get("ams", []):
                print(f"  Slot {slot['slot']}: {slot['type']} {slot.get('name', '')} ({slot['remaining']}%)")
    else:
        print("Not logged in. Run: python3 bambu_cloud.py --login")
