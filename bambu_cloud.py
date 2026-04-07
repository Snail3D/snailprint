#!/usr/bin/env python3
"""
Bambu Cloud API client — OAuth login, printer management, AMS inventory,
print job submission, camera snapshots.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

TOKEN_PATH = Path(os.path.expanduser("~/.snailprint/token.json"))
PRINTERS_PATH = Path(os.path.expanduser("~/.snailprint/printers.json"))


MQTT_BROKER = "us.mqtt.bambulab.com"
MQTT_PORT = 8883


class BambuCloud:
    def __init__(self):
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._api = None
        self._user_id = None
        self._token = None
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
        from bambulab import BambuClient
        self._token = token
        self._api = BambuClient(token=token)

    def login(self, username=None, password=None):
        """Interactive login with email + password."""
        from bambulab import BambuAuthenticator
        auth = BambuAuthenticator()

        if not username:
            username = input("Bambu Cloud email: ").strip()
        if not password:
            import getpass
            password = getpass.getpass("Bambu Cloud password: ")

        print("Logging in to Bambu Cloud...")
        token = auth.login(username, password)

        # Save token + region
        token_data = {"token": token, "region": auth.region}
        TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
        print(f"Token saved to {TOKEN_PATH}")

        self._user_id = None  # reset cached user id on fresh login
        self._init_api(token, auth.region)
        return True

    @property
    def is_authenticated(self):
        return self._api is not None

    def _get_user_id(self):
        """Return cached user ID, fetching from API if needed."""
        if not self._user_id:
            info = self._api.get_user_info()
            self._user_id = info.get("uid") or info.get("user_id") or info.get("id", "")
        return self._user_id

    def _get_ams_mqtt(self, dev_id):
        """
        Connect via MQTT, send a pushall command, and return a dict with:
          - ams: list of tray slot dicts
          - gcode_state: printer state string (IDLE, RUNNING, FINISH, …)
          - nozzle_diameter: float
          - mc_percent: int
          - mc_remaining_time: int (minutes)
        Returns None on failure.
        """
        import paho.mqtt.client as mqtt

        user_id = self._get_user_id()
        topic_report = f"device/{dev_id}/report"
        topic_request = f"device/{dev_id}/request"
        pushall_payload = json.dumps({
            "pushing": {"sequence_id": "0", "command": "pushall"}
        })

        result = {}
        received = threading.Event()

        def on_connect(client, userdata, flags, reason_code, properties=None):
            # paho 2.x passes reason_code as int or ReasonCode object
            code = reason_code if isinstance(reason_code, int) else reason_code.value
            if code == 0:
                client.subscribe(topic_report)
                client.publish(topic_request, pushall_payload)
            else:
                received.set()  # unblock on connection failure

        def on_message(client, userdata, msg):
            try:
                data = json.loads(msg.payload.decode())
                print_data = data.get("print", {})
                if not print_data:
                    return

                # AMS tray data
                ams_slots = []
                ams_info = print_data.get("ams", {})
                if isinstance(ams_info, dict):
                    for unit_idx, unit in enumerate(ams_info.get("ams", [])):
                        for tray in unit.get("tray", []):
                            tray_color = tray.get("tray_color", "000000FF")
                            # ARGB hex — last 6 chars are RGB
                            color_hex = "#" + tray_color[-6:] if len(tray_color) >= 6 else "#000000"
                            remain_raw = int(tray.get("remain", 0))
                            # remain is 0-1000; convert to percentage. -1 means empty/unknown.
                            if remain_raw < 0:
                                remaining_pct = 0
                            elif remain_raw > 100:
                                remaining_pct = remain_raw // 10
                            else:
                                remaining_pct = remain_raw
                            slot = {
                                "slot": int(tray.get("id", 0)),
                                "ams_unit": unit_idx,
                                "type": tray.get("tray_type", ""),
                                "color": tray_color,
                                "color_hex": color_hex,
                                "remaining": remaining_pct,
                                "name": tray.get("tray_sub_brands", ""),
                                "nozzle_temp_min": tray.get("nozzle_temp_min", 0),
                                "nozzle_temp_max": tray.get("nozzle_temp_max", 0),
                            }
                            ams_slots.append(slot)

                result["ams"] = ams_slots
                result["gcode_state"] = print_data.get("gcode_state", "")
                result["nozzle_diameter"] = print_data.get("nozzle_diameter", "")
                result["mc_percent"] = print_data.get("mc_percent", 0)
                result["mc_remaining_time"] = print_data.get("mc_remaining_time", 0)
                received.set()
            except Exception:
                pass  # keep waiting for a valid message

        def on_disconnect(client, userdata, disconnect_flags, reason_code=None, properties=None):
            received.set()

        try:
            mqttc = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                protocol=mqtt.MQTTv311,
                clean_session=True,
            )
            mqttc.username_pw_set(f"u_{user_id}", self._token)
            mqttc.tls_set()
            mqttc.reconnect_delay_set(min_delay=1, max_delay=5)
            mqttc.on_connect = on_connect
            mqttc.on_message = on_message
            mqttc.on_disconnect = on_disconnect

            mqttc.connect(MQTT_BROKER, MQTT_PORT, keepalive=5)
            mqttc.loop_start()
            received.wait(timeout=15)
            mqttc.loop_stop()
            mqttc.disconnect()
        except Exception as e:
            print(f"  MQTT error for {dev_id}: {e}")
            return None

        return result if result else None

    def list_printers(self):
        """Return list of printers with status and AMS info."""
        if not self.is_authenticated:
            raise RuntimeError("Not authenticated. Run: python3 bambu_cloud.py --login")

        devices = self._api.get_devices()
        printers = []
        for dev in devices:
            dev_id = dev.get("dev_id", "")
            printer = {
                "serial": dev_id,
                "name": dev.get("name", "Unknown"),
                "model": dev.get("dev_model_name", ""),
                "status": "unknown",
                "ams": [],
            }

            # Get AMS + live state via MQTT (single round-trip for everything)
            print(f"  Fetching MQTT data for {printer['name']} ({dev_id})...")
            mqtt_data = self._get_ams_mqtt(dev_id)
            if mqtt_data:
                printer["ams"] = mqtt_data.get("ams", [])
                printer["nozzle_diameter"] = mqtt_data.get("nozzle_diameter", "")

                gcode_state = mqtt_data.get("gcode_state", "").upper()
                progress = mqtt_data.get("mc_percent", 0)
                if gcode_state in ("RUNNING", "PAUSE"):
                    printer["status"] = "printing"
                    printer["progress"] = progress
                    printer["time_remaining"] = self._format_time(mqtt_data.get("mc_remaining_time", 0))
                elif gcode_state in ("IDLE", "FINISH", ""):
                    printer["status"] = "idle"
                else:
                    printer["status"] = gcode_state.lower()
                printer["gcode_state"] = gcode_state
            else:
                printer["status"] = "unreachable"

            printers.append(printer)

        # Cache printer list
        PRINTERS_PATH.write_text(json.dumps(printers, indent=2))
        return printers

    def _get_ams(self, dev_id):
        """Get AMS slot inventory for a printer (delegates to MQTT)."""
        mqtt_data = self._get_ams_mqtt(dev_id)
        if mqtt_data:
            return mqtt_data.get("ams", [])
        return []

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
        # Accept email and password as args: --login email password
        args = sys.argv[1:]
        idx = args.index("--login")
        email = args[idx + 1] if len(args) > idx + 1 else None
        pw = args[idx + 2] if len(args) > idx + 2 else None
        cloud.login(username=email, password=pw)
    elif "--printers" in sys.argv or cloud.is_authenticated:
        if not cloud.is_authenticated:
            print("Not logged in. Run: python3 bambu_cloud.py --login EMAIL PASSWORD")
            sys.exit(1)
        printers = cloud.list_printers()
        for p in printers:
            print(f"{p['name']} ({p['serial']}) — {p['status']}")
            for slot in p.get("ams", []):
                print(f"  Slot {slot['slot']}: {slot['type']} {slot.get('name', '')} ({slot['remaining']}%)")
    else:
        print("Not logged in. Run: python3 bambu_cloud.py --login EMAIL PASSWORD")
