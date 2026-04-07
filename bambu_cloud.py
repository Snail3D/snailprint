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
                result["subtask_name"] = print_data.get("subtask_name", "")
                result["layer_num"] = print_data.get("layer_num", 0)
                result["total_layer_num"] = print_data.get("total_layer_num", 0)
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

    def is_bed_clear(self, serial):
        """
        Check if the printer bed is likely clear and ready for a new print.

        Returns (is_clear: bool, reason: str, last_print: str)

        Logic:
        - IDLE with no subtask → probably clear
        - FINISH with subtask → previous print likely still on bed
        - RUNNING/PAUSE → currently printing
        """
        mqtt_data = self._get_ams_mqtt(serial)
        if mqtt_data is None:
            return False, "unreachable", ""

        state = mqtt_data.get("gcode_state", "").upper()
        subtask = mqtt_data.get("subtask_name", "")
        progress = mqtt_data.get("mc_percent", 0)

        if state in ("RUNNING", "PREPARE"):
            return False, "currently printing", subtask
        elif state == "PAUSE":
            return False, "print paused", subtask
        elif state == "FINISH" and subtask:
            return False, f"finished print may still be on bed: '{subtask}'", subtask
        elif state == "IDLE" and not subtask:
            return True, "idle, bed appears clear", ""
        elif state == "IDLE" and subtask:
            # IDLE but has a subtask name — might have been cleared manually
            return True, f"idle (last print: '{subtask}' — confirm bed is clear)", subtask
        else:
            return True, f"state={state}, likely clear", subtask

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
        """
        Upload 3MF and start print on specified printer.

        Strategy:
        1. Try cloud API first (works when printers are cloud-connected).
        2. If cloud fails, fall back to local LAN mode:
           a. Upload .3mf via FTPS (port 990) to the printer.
           b. Send print command via local MQTT (port 8883).
        """
        # --- Attempt 1: Cloud API ---
        if self.is_authenticated:
            try:
                result = self._api.send_print(
                    dev_id=serial,
                    file_path=threemf_path,
                )
                print(f"  Cloud submit succeeded for {serial}")
                return {"method": "cloud", "result": result}
            except Exception as cloud_err:
                print(f"  Cloud submit failed ({cloud_err}), trying local LAN mode...")

        # --- Attempt 2: Local LAN (FTP upload + MQTT print command) ---
        printer_cfg = self._get_printer_config(serial)
        if not printer_cfg:
            raise RuntimeError(
                f"No local config for printer {serial}. "
                "Cloud API also failed. Cannot submit print."
            )

        ip = printer_cfg["ip"]
        access_code = printer_cfg["access_code"]

        return self._submit_print_local(serial, ip, access_code, threemf_path)

    def _submit_print_local(self, serial, ip, access_code, threemf_path):
        """
        Submit a print job via local LAN: FTPS upload + MQTT print command.

        The Bambu P2S in LAN mode exposes:
          - FTPS on port 990 (implicit TLS)
          - MQTT on port 8883 (TLS, user=bblp, pass=access_code)
        """
        import ssl
        from ftplib import FTP_TLS

        threemf_path = str(threemf_path)
        filename = Path(threemf_path).name

        # ---- Step 1: Upload via FTPS (port 990, implicit TLS) ----
        print(f"  Uploading {filename} to {ip} via FTPS...")
        try:
            ftp = FTP_TLS()
            # Implicit FTPS: connect with TLS from the start on port 990
            ftp.connect(host=ip, port=990, timeout=30)
            ftp.login(user="bblp", passwd=access_code)
            ftp.prot_p()  # enable data channel encryption

            with open(threemf_path, "rb") as f:
                ftp.storbinary(f"STOR {filename}", f)
            print(f"  Upload complete: {filename}")
            ftp.quit()
        except Exception as e:
            # Try plain FTP on port 21 as a second fallback
            print(f"  FTPS port 990 failed ({e}), trying plain FTP port 21...")
            try:
                from bambulab import LocalFTPClient
                with LocalFTPClient(ip, access_code, use_tls=False) as ftp_client:
                    upload_result = ftp_client.upload_file(threemf_path)
                    filename = upload_result["filename"]
                    print(f"  Plain FTP upload complete: {filename}")
            except Exception as e2:
                raise RuntimeError(f"FTP upload failed (FTPS: {e}, FTP: {e2})")

        # ---- Step 2: Send print command via local MQTT (port 8883) ----
        print(f"  Sending print command to {ip} via local MQTT...")
        import paho.mqtt.client as mqtt

        print_payload = {
            "print": {
                "sequence_id": "0",
                "command": "project_file",
                "param": "Metadata/plate_1.gcode",
                "subtask_name": filename.replace(".3mf", ""),
                "url": f"ftp://{filename}",
                "bed_type": "auto",
                "timelapse": False,
                "bed_levelling": True,
                "flow_cali": True,
                "vibration_cali": True,
                "layer_inspect": True,
                "use_ams": True,
                "project_id": "0",
                "profile_id": "0",
                "task_id": "0",
                "subtask_id": "0",
                "ams_mapping": "",
                "md5": "",
            }
        }

        topic_request = f"device/{serial}/request"
        publish_ok = threading.Event()
        connect_err = [None]

        def on_connect(client, userdata, flags, reason_code, properties=None):
            code = reason_code if isinstance(reason_code, int) else reason_code.value
            if code == 0:
                client.publish(topic_request, json.dumps(print_payload))
                print(f"  Print command published to {topic_request}")
                publish_ok.set()
            else:
                connect_err[0] = f"MQTT connect failed (code {code})"
                publish_ok.set()

        try:
            mqttc = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                protocol=mqtt.MQTTv311,
                clean_session=True,
            )
            mqttc.username_pw_set("bblp", access_code)
            mqttc.tls_set(cert_reqs=ssl.CERT_NONE)
            mqttc.tls_insecure_set(True)
            mqttc.on_connect = on_connect

            mqttc.connect(ip, 8883, keepalive=10)
            mqttc.loop_start()
            publish_ok.wait(timeout=10)
            mqttc.loop_stop()
            mqttc.disconnect()
        except Exception as e:
            raise RuntimeError(f"Local MQTT print command failed: {e}")

        if connect_err[0]:
            raise RuntimeError(connect_err[0])

        return {
            "method": "local_lan",
            "ip": ip,
            "filename": filename,
            "serial": serial,
        }

    def get_job_status(self, serial):
        """Get current print job status via MQTT."""
        mqtt_data = self._get_ams_mqtt(serial)
        if mqtt_data is None:
            return None

        state = mqtt_data.get("gcode_state", "").upper()
        progress = mqtt_data.get("mc_percent", 0)

        if state in ("RUNNING", "PREPARE"):
            status = "printing"
        elif state == "PAUSE":
            status = "paused"
        elif state == "FINISH":
            status = "complete"
        else:
            status = "idle"

        return {
            "progress": progress,
            "time_remaining": self._format_time(mqtt_data.get("mc_remaining_time", 0)),
            "status": status,
            "subtask_name": mqtt_data.get("subtask_name", ""),
            "gcode_state": state,
            "layer_num": mqtt_data.get("layer_num", 0),
            "total_layer_num": mqtt_data.get("total_layer_num", 0),
        }

    def get_camera_snapshot(self, serial, output_path):
        """Save a camera snapshot from the printer via RTSP (LAN mode required)."""
        import subprocess

        printer = self._get_printer_config(serial)
        if not printer:
            print(f"Camera: no config for {serial}")
            return False

        ip = printer.get("ip", "")
        code = printer.get("access_code", "")
        if not ip or not code:
            print(f"Camera: missing IP or access code for {serial}")
            return False

        url = f"rtsps://bblp:{code}@{ip}:322/streaming/live/1"
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-rtsp_transport", "tcp", "-i", url,
                 "-frames:v", "1", "-update", "1", str(output_path)],
                capture_output=True, timeout=15,
            )
            if result.returncode == 0 and Path(output_path).exists():
                print(f"Camera: snapshot saved ({Path(output_path).stat().st_size // 1024}KB)")
                return True
            else:
                print(f"Camera: ffmpeg failed (rc={result.returncode})")
        except Exception as e:
            print(f"Camera: {e}")
        return False

    def _get_printer_config(self, serial):
        """Get printer IP and access code. Uses cached data or PRINTERS_PATH."""
        # Known printer configs (LAN mode)
        configs = {
            "22E8AJ612200029": {"ip": "192.168.1.81", "access_code": "ac555123", "name": "P3Pio"},
            "22E8AJ5C2800915": {"ip": "192.168.1.154", "access_code": "cf972ede", "name": "P2D2"},
        }
        return configs.get(serial)

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
