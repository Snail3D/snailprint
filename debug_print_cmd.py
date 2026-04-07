#!/usr/bin/env python3
"""
Diagnostic: test P2S MQTT print commands and capture ALL responses.
Tests 4 variants to find which (if any) the firmware accepts.
"""
import json
import ssl
import time
import threading

import paho.mqtt.client as mqtt

PRINTER_IP = "192.168.1.154"
PRINTER_PORT = 8883
SERIAL = "22E8AJ5C2800915"
ACCESS_CODE = "cf972ede"
USERNAME = "bblp"

TOPIC_REPORT = f"device/{SERIAL}/report"
TOPIC_REQUEST = f"device/{SERIAL}/request"

PUSHALL = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}})


# ── helpers ────────────────────────────────────────────────────────────────

def make_client():
    """Build a fresh, connected MQTT client. Returns (client, connected_event)."""
    connected = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties=None):
        code = reason_code if isinstance(reason_code, int) else reason_code.value
        if code == 0:
            client.subscribe(TOPIC_REPORT)
            connected.set()
        else:
            print(f"  [MQTT] Connect failed, code={code}")
            connected.set()

    c = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        protocol=mqtt.MQTTv311,
        clean_session=True,
    )
    c.username_pw_set(USERNAME, ACCESS_CODE)
    c.tls_set(cert_reqs=ssl.CERT_NONE)
    c.tls_insecure_set(True)
    c.on_connect = on_connect
    c.connect(PRINTER_IP, PRINTER_PORT, keepalive=30)
    c.loop_start()
    connected.wait(timeout=10)
    return c


def collect_messages(client, duration_s):
    """Attach a raw collector, wait duration_s, return all payloads received."""
    messages = []
    lock = threading.Lock()
    new_msg = threading.Event()

    def on_message(client, userdata, msg):
        try:
            raw = msg.payload.decode("utf-8", errors="replace")
            with lock:
                messages.append(raw)
            new_msg.set()
        except Exception as e:
            with lock:
                messages.append(f"<decode error: {e}>")

    client.on_message = on_message
    deadline = time.time() + duration_s
    while time.time() < deadline:
        new_msg.wait(timeout=0.2)
        new_msg.clear()
    return messages


def dump_messages(label, msgs):
    print(f"\n{'='*60}")
    print(f"  {label}  ({len(msgs)} message(s))")
    print(f"{'='*60}")
    if not msgs:
        print("  (no messages received)")
        return
    for i, raw in enumerate(msgs, 1):
        print(f"\n--- message {i} ---")
        try:
            parsed = json.loads(raw)
            print(json.dumps(parsed, indent=2))
        except Exception:
            print(raw)


# ── main ───────────────────────────────────────────────────────────────────

def main():
    print(f"Connecting to {PRINTER_IP}:{PRINTER_PORT} as {USERNAME}…")
    client = make_client()
    print("Connected and subscribed to", TOPIC_REPORT)

    # ── Phase 1: pushall — capture full state ──────────────────────────────
    print("\n[1] Sending pushall…")
    client.publish(TOPIC_REQUEST, PUSHALL)
    state_msgs = collect_messages(client, 6)
    dump_messages("PUSHALL response", state_msgs)

    # ── Phase 2: project_file (original URL format) ────────────────────────
    cmd_project = {
        "print": {
            "sequence_id": "0",
            "command": "project_file",
            "param": "Metadata/plate_1.gcode",
            "subtask_name": "cat_orca",
            "url": "file:///sdcard/cat_orca.3mf",
            "bed_type": "auto",
            "timelapse": False,
            "bed_leveling": True,
            "flow_cali": True,
            "vibration_cali": True,
            "layer_inspect": True,
            "use_ams": False,
            "ams_mapping": [0],
            "project_id": "0",
            "profile_id": "0",
            "task_id": "0",
            "subtask_id": "0",
            "md5": "",
        }
    }
    print("\n[2] Sending project_file (url=file:///sdcard/cat_orca.3mf)…")
    print("    payload:", json.dumps(cmd_project, indent=4))
    client.publish(TOPIC_REQUEST, json.dumps(cmd_project))
    r2 = collect_messages(client, 10)
    dump_messages("project_file (file:///sdcard/) response", r2)

    # ── Phase 3: gcode_file command ────────────────────────────────────────
    cmd_gcode = {
        "print": {
            "sequence_id": "0",
            "command": "gcode_file",
            "param": "cat_orca.3mf",
        }
    }
    print("\n[3] Sending gcode_file…")
    print("    payload:", json.dumps(cmd_gcode, indent=4))
    client.publish(TOPIC_REQUEST, json.dumps(cmd_gcode))
    r3 = collect_messages(client, 10)
    dump_messages("gcode_file response", r3)

    # ── Phase 4: project_file with url = bare filename ────────────────────
    cmd_bare = {
        "print": {
            "sequence_id": "0",
            "command": "project_file",
            "param": "Metadata/plate_1.gcode",
            "subtask_name": "cat_orca",
            "url": "cat_orca.3mf",
            "bed_type": "auto",
            "timelapse": False,
            "bed_leveling": True,
            "flow_cali": True,
            "vibration_cali": True,
            "layer_inspect": True,
            "use_ams": False,
            "ams_mapping": [0],
            "project_id": "0",
            "profile_id": "0",
            "task_id": "0",
            "subtask_id": "0",
            "md5": "",
        }
    }
    print("\n[4] Sending project_file (url=cat_orca.3mf, bare filename)…")
    print("    payload:", json.dumps(cmd_bare, indent=4))
    client.publish(TOPIC_REQUEST, json.dumps(cmd_bare))
    r4 = collect_messages(client, 10)
    dump_messages("project_file (bare filename) response", r4)

    # ── Phase 5: project_file with ftp:/// triple-slash ───────────────────
    cmd_ftp = {
        "print": {
            "sequence_id": "0",
            "command": "project_file",
            "param": "Metadata/plate_1.gcode",
            "subtask_name": "cat_orca",
            "url": "ftp:///cat_orca.3mf",
            "bed_type": "auto",
            "timelapse": False,
            "bed_leveling": True,
            "flow_cali": True,
            "vibration_cali": True,
            "layer_inspect": True,
            "use_ams": False,
            "ams_mapping": [0],
            "project_id": "0",
            "profile_id": "0",
            "task_id": "0",
            "subtask_id": "0",
            "md5": "",
        }
    }
    print("\n[5] Sending project_file (url=ftp:///cat_orca.3mf)…")
    print("    payload:", json.dumps(cmd_ftp, indent=4))
    client.publish(TOPIC_REQUEST, json.dumps(cmd_ftp))
    r5 = collect_messages(client, 10)
    dump_messages("project_file (ftp:///) response", r5)

    print("\nDone. Disconnecting.")
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
