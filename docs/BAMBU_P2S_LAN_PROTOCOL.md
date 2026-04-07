# Bambu Lab P2S — LAN Mode Protocol Reference

Hard-won knowledge from reverse engineering the P2S local network protocol. This is what actually works, not what the docs say.

## Overview

The Bambu Lab P2S in LAN mode exposes these services:

| Port | Protocol | Purpose |
|------|----------|---------|
| 990 | FTPS (implicit TLS) | File upload (.3mf, .gcode) |
| 3000 | Proprietary | Slicer bind/pairing handshake (not usable) |
| 3002 | TLS | Secure slicer bind (not usable) |
| 6000 | TLS | Camera JPEG stream (P1S/A1 protocol, NOT used by P2S) |
| 322 | RTSPS | Camera live view (only available in LAN mode) |
| 8883 | MQTT over TLS | Main control channel — print commands, status, AMS |

## Authentication

All services use the same credentials:
- **Username:** `bblp`
- **Password:** Your printer's access code (shown on screen under Settings → Network → LAN Only)

## MQTT Connection

```python
import paho.mqtt.client as mqtt
import ssl

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311, clean_session=True)
client.username_pw_set("bblp", ACCESS_CODE)
client.tls_set(cert_reqs=ssl.CERT_NONE)
client.tls_insecure_set(True)
client.connect(PRINTER_IP, 8883, keepalive=5)
```

**Topics:**
- Subscribe: `device/{SERIAL}/report` — printer status, AMS data, print progress
- Publish: `device/{SERIAL}/request` — commands

## Getting Printer Status + AMS Data

```python
# Request full status dump
client.publish(f"device/{SERIAL}/request", json.dumps({
    "pushing": {"sequence_id": "0", "command": "pushall"}
}))
```

Response includes:
- `gcode_state` — IDLE, RUNNING, PREPARE, PAUSE, FINISH, FAILED
- `mc_percent` — print progress 0-100
- `mc_remaining_time` — minutes remaining
- `layer_num` / `total_layer_num`
- `nozzle_diameter`, `nozzle_temper`, `bed_temper`
- `ams.ams[N].tray[N]` — filament type, color (ARGB hex), remaining (0-1000 scale)
- `subtask_name` — current job name
- `hms` — health warnings
- `ipcam`, `xcam` — camera/detection settings

## Starting a Print — THE CRITICAL PART

### Step 1: Upload via FTPS (port 990, implicit TLS)

```python
from ftplib import FTP_TLS
import ssl, socket

class ImplicitFTPS(FTP_TLS):
    """Bambu requires implicit FTPS with TLS session reuse."""
    def __init__(self):
        super().__init__()
        self._ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def connect(self, host='', port=990, timeout=30, source_address=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = socket.create_connection((host, port), timeout)
        self.af = self.sock.family
        # Wrap in TLS immediately (implicit FTPS)
        self.sock = self._ssl_ctx.wrap_socket(self.sock, server_hostname=host)
        self.file = self.sock.makefile('r')
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        from ftplib import FTP
        conn, size = FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            # Reuse control channel TLS session (required by Bambu)
            session = self.sock.session
            conn = self._ssl_ctx.wrap_socket(conn, server_hostname=self.host, session=session)
        return conn, size

ftp = ImplicitFTPS()
ftp.connect(host=PRINTER_IP, port=990, timeout=30)
ftp.login(user="bblp", passwd=ACCESS_CODE)
ftp.prot_p()
with open("model.3mf", "rb") as f:
    ftp.storbinary("STOR model.3mf", f)
ftp.quit()
```

**Key details:**
- Must be implicit FTPS (TLS wraps the socket before any FTP commands)
- Must reuse the TLS session for data connections (the `ntransfercmd` override)
- Files upload to root directory
- Printer needs a USB drive or SD card inserted
- Only accepts `.3mf` and `.gcode` files

### Step 2: Send Print Command via MQTT

```python
print_command = {
    "print": {
        "sequence_id": "0",
        "command": "project_file",
        "param": "Metadata/plate_1.gcode",
        "subtask_name": "my_print",
        "url": "ftp:///model.3mf",           # <-- THIS IS THE KEY
        "bed_type": "auto",
        "timelapse": False,
        "bed_leveling": True,                 # single L, not "levelling"
        "flow_cali": True,
        "vibration_cali": True,
        "layer_inspect": True,
        "use_ams": True,
        "ams_mapping": [0],                   # AMS slot 0
        "project_id": "0",
        "profile_id": "0",
        "task_id": "0",
        "subtask_id": "0",
        "md5": "",
    }
}
client.publish(f"device/{SERIAL}/request", json.dumps(print_command))
```

### THE CRITICAL DISCOVERY: URL Format

**The P2S requires `ftp:///` (triple slash, no host) as the URL scheme.**

| URL Format | Result |
|------------|--------|
| `ftp:///model.3mf` | **SUCCESS** ✅ |
| `file:///sdcard/model.3mf` | FAIL - ERROR STATE ❌ |
| `ftp://model.3mf` | FAIL - ERROR STATE ❌ |
| `model.3mf` | FAIL - ERROR STATE ❌ |
| `/sdcard/model.3mf` | FAIL - ERROR STATE ❌ |

This is different from older Bambu printers (P1S, X1C, A1) which use `file:///sdcard/filename`. The P2S and H2-series printers use the `ftp:///` scheme.

The `ftp:///` command also works when the printer is in FAILED state — it clears the error and starts the print.

### Clearing Error States

If the printer is stuck in FAILED state:

```python
# Method 1: Just send ftp:/// print command — it auto-clears FAILED
# Method 2: Explicit clear sequence
client.publish(f"device/{SERIAL}/request", json.dumps({
    "print": {"sequence_id": "0", "command": "clean_print_error",
              "subtask_id": "", "print_error": 0}
}))
client.publish(f"device/{SERIAL}/request", json.dumps({
    "system": {"sequence_id": "0", "command": "uiop",
               "name": "print_error", "action": "close",
               "source": 1, "type": "dialog", "err": "00000000"}
}))
```

## Camera Access (RTSP)

Requires LAN mode with "LAN Only Liveview" enabled on the printer.

```bash
# Single frame snapshot
ffmpeg -y -rtsp_transport tcp \
  -i "rtsps://bblp:ACCESS_CODE@PRINTER_IP:322/streaming/live/1" \
  -frames:v 1 -update 1 snapshot.jpg

# 10-second video clip
ffmpeg -y -rtsp_transport tcp \
  -i "rtsps://bblp:ACCESS_CODE@PRINTER_IP:322/streaming/live/1" \
  -t 10 -c:v libx264 -crf 23 -movflags +faststart clip.mp4
```

**Note:** Port 6000 (used by P1S/A1 for JPEG streaming) is open on P2S but does NOT accept the standard auth packet — the P2S camera is RTSP-only on port 322.

## OrcaSlicer CLI on macOS

OrcaSlicer 2.3.2 and BambuStudio 2.5.0 both crash (SIGSEGV) when using P2S profiles in CLI mode. The crash is in `update_values_to_printer_extruders_for_multiple_filaments` — a NULL pointer dereference triggered by the P2S dual-extruder profile arrays.

**Workaround:** Create a hybrid machine profile using A1 as the base (single-extruder, avoids the crash) with P2S physical settings overlaid (bed size, start/end gcode, retraction, speeds). See `slicer.py:_ensure_p2s_hybrid_profile()` for the implementation.

## Common Gotchas

1. **`bed_leveling` not `bed_levelling`** — American spelling, single L
2. **`ams_mapping` must be an array** — `[0]` for slot 0, not `""` or `0`
3. **USB drive required** — FTPS upload writes to removable storage, not internal
4. **FAILED state is sticky** — survives MQTT stop/clean commands, but `ftp:///` print command auto-clears it
5. **Post-cancel delay** — wait 3-5 seconds after stopping a print before sending a new one
6. **Sequence IDs** — must be strings (`"0"` not `0`)

## Sources

- [Doridian/OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI) — MQTT protocol docs
- [synman/bambu-printer-manager](https://github.com/synman/bambu-printer-manager) — confirmed `ftp:///` for P2-series
- [greghesp/ha-bambulab](https://github.com/greghesp/ha-bambulab) — Home Assistant integration, battle-tested
- Our own testing on P2S firmware 01.01.03.00 / 01.01.50.50
