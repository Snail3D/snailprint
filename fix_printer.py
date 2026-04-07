#!/usr/bin/env python3
"""Bambu Lab P2S MQTT diagnostic and recovery script."""

import json
import ssl
import sys
import time
import paho.mqtt.client as mqtt

PRINTER_IP = "192.168.1.154"
SERIAL = "22E8AJ5C2800915"
ACCESS_CODE = "cf972ede"
MQTT_PORT = 8883

TOPIC_REPORT = f"device/{SERIAL}/report"
TOPIC_REQUEST = f"device/{SERIAL}/request"

collected_reports = []
connected = False

def on_connect(client, userdata, flags, rc, properties=None):
    global connected
    if rc == 0:
        print(f"[OK] Connected to MQTT broker at {PRINTER_IP}:{MQTT_PORT}")
        connected = True
        client.subscribe(TOPIC_REPORT)
        print(f"[OK] Subscribed to {TOPIC_REPORT}")
    else:
        print(f"[FAIL] Connection failed with rc={rc}")
        sys.exit(1)

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
    except:
        print(f"[RAW] {msg.payload[:500]}")
        return
    collected_reports.append(data)
    print(f"\n{'='*80}")
    print(f"[REPORT] Full payload ({len(msg.payload)} bytes):")
    print(json.dumps(data, indent=2))
    print(f"{'='*80}")

def send_command(client, cmd_dict, label="command"):
    payload = json.dumps(cmd_dict)
    print(f"\n>>> Sending {label}: {payload}")
    client.publish(TOPIC_REQUEST, payload)

def wait_for_reports(client, seconds=5):
    """Loop the MQTT client for `seconds`, collecting reports."""
    start = time.time()
    while time.time() - start < seconds:
        client.loop(timeout=0.5)

def main():
    # Step 1: Connect
    print(f"[*] Connecting to {PRINTER_IP}:{MQTT_PORT} ...")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="snailprint_fix")
    client.username_pw_set("bblp", ACCESS_CODE)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    client.tls_set_context(ctx)
    client.tls_insecure_set(True)

    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(PRINTER_IP, MQTT_PORT, keepalive=60)

    # Wait for connection
    for _ in range(10):
        client.loop(timeout=1.0)
        if connected:
            break

    if not connected:
        print("[FAIL] Could not connect after 10 seconds")
        sys.exit(1)

    # Step 2-3: Send pushall and capture full state
    print("\n" + "="*80)
    print("STEP 3: Sending pushall to get full printer state")
    print("="*80)
    send_command(client, {"pushing": {"sequence_id": "0", "command": "pushall"}}, "pushall")
    wait_for_reports(client, 8)

    if not collected_reports:
        print("[WARN] No reports received after pushall, waiting longer...")
        wait_for_reports(client, 10)

    # Analyze state
    gcode_state = None
    hms_errors = []
    print_error = None

    for report in collected_reports:
        if "print" in report:
            p = report["print"]
            if "gcode_state" in p:
                gcode_state = p["gcode_state"]
            if "print_error" in p:
                print_error = p["print_error"]
            if "hms" in p:
                hms_errors = p["hms"]

    print(f"\n{'='*80}")
    print(f"STATE SUMMARY:")
    print(f"  gcode_state: {gcode_state}")
    print(f"  print_error: {print_error}")
    print(f"  hms_errors: {hms_errors}")
    print(f"  total reports: {len(collected_reports)}")
    print(f"{'='*80}")

    # Step 4: Clean HMS errors if any
    if hms_errors:
        print("\n[*] STEP 4: Cleaning HMS errors...")
        send_command(client, {"system": {"sequence_id": "0", "command": "clean_hms"}}, "clean_hms")
        wait_for_reports(client, 5)

    # Step 5: If FAILED, try recovery sequence
    if gcode_state == "FAILED":
        print("\n[*] STEP 5a: Sending STOP command...")
        send_command(client, {"print": {"sequence_id": "0", "command": "stop"}}, "stop")
        wait_for_reports(client, 4)

        print("\n[*] STEP 5b: Sending system clean_print_error...")
        send_command(client, {"system": {"sequence_id": "0", "command": "clean_print_error"}}, "system clean_print_error")
        wait_for_reports(client, 4)

        print("\n[*] STEP 5c: Sending print clean_print_error...")
        send_command(client, {"print": {"sequence_id": "0", "command": "clean_print_error"}}, "print clean_print_error")
        wait_for_reports(client, 4)

        print("\n[*] STEP 5d: Sending HOME (G28)...")
        send_command(client, {"print": {"sequence_id": "0", "command": "gcode_line", "param": "G28\n"}}, "G28 home")
        wait_for_reports(client, 5)

        # Re-check state
        print("\n[*] Re-checking state after recovery...")
        collected_reports.clear()
        send_command(client, {"pushing": {"sequence_id": "0", "command": "pushall"}}, "pushall recheck")
        wait_for_reports(client, 8)

        for report in collected_reports:
            if "print" in report:
                p = report["print"]
                if "gcode_state" in p:
                    gcode_state = p["gcode_state"]
                    print(f"  [UPDATE] gcode_state is now: {gcode_state}")

    # Step 6: Try every URL format for print command
    print(f"\n{'='*80}")
    print(f"STEP 6: Trying print with various URL formats (current state: {gcode_state})")
    print(f"{'='*80}")

    url_formats = [
        "file:///sdcard/cat_orca.3mf",
        "ftp:///cat_orca.3mf",
        "/sdcard/cat_orca.3mf",
        "cat_orca.3mf",
        "/cat_orca.3mf",
    ]

    for url in url_formats:
        print(f"\n[*] STEP 6: Trying url={url}")
        cmd = {
            "print": {
                "sequence_id": "0",
                "command": "project_file",
                "param": f"Metadata/plate_1.gcode",
                "subtask_name": "cat_orca",
                "url": url,
                "bed_type": "auto",
                "timelapse": False,
                "bed_leveling": True,
                "flow_cali": True,
                "vibration_cali": True,
                "layer_inspect": False,
                "use_ams": False,
            }
        }
        collected_reports.clear()
        send_command(client, cmd, f"project_file url={url}")
        wait_for_reports(client, 5)

        # Check if it started
        for report in collected_reports:
            if "print" in report:
                p = report["print"]
                gs = p.get("gcode_state", "")
                if gs in ("PREPARE", "RUNNING", "IDLE"):
                    print(f"\n[SUCCESS] Print appears to have started! gcode_state={gs}")
                    # Don't try more URLs
                    client.disconnect()
                    return

    # Step 7: Try gcode_file command
    print(f"\n{'='*80}")
    print("STEP 7: Trying gcode_file command")
    print(f"{'='*80}")

    gcode_file_params = [
        "cat_orca.3mf",
        "/sdcard/cat_orca.3mf",
        "file:///sdcard/cat_orca.3mf",
    ]

    for param in gcode_file_params:
        print(f"\n[*] Trying gcode_file param={param}")
        collected_reports.clear()
        send_command(client, {"print": {"sequence_id": "0", "command": "gcode_file", "param": param}}, f"gcode_file {param}")
        wait_for_reports(client, 5)

        for report in collected_reports:
            if "print" in report:
                p = report["print"]
                gs = p.get("gcode_state", "")
                if gs in ("PREPARE", "RUNNING"):
                    print(f"\n[SUCCESS] gcode_file worked! gcode_state={gs}")
                    client.disconnect()
                    return

    # Step 8: Try raw gcode movement to verify MQTT works
    print(f"\n{'='*80}")
    print("STEP 8: Sending raw gcode movement test")
    print(f"{'='*80}")

    collected_reports.clear()
    send_command(client, {
        "print": {
            "sequence_id": "0",
            "command": "gcode_line",
            "param": "G28\nG1 X100 Y100 F3000\n"
        }
    }, "raw gcode movement")
    wait_for_reports(client, 8)

    print(f"\n{'='*80}")
    print("FINAL: Getting last state...")
    print(f"{'='*80}")
    collected_reports.clear()
    send_command(client, {"pushing": {"sequence_id": "0", "command": "pushall"}}, "final pushall")
    wait_for_reports(client, 8)

    for report in collected_reports:
        if "print" in report:
            p = report["print"]
            if "gcode_state" in p:
                print(f"  Final gcode_state: {p['gcode_state']}")

    client.disconnect()
    print("\n[DONE] Script complete.")

if __name__ == "__main__":
    main()
