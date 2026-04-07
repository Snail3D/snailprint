# SnailPrint

> Text → 3D model → slice → print. One command.

Local 3D printing pipeline for Bambu Lab printers. Describe what you want, SnailPrint generates a 3D model, slices it with tree supports, checks the bed is clear, and sends it to your printer — with camera clips to Discord along the way.

---

## Install

```bash
git clone https://github.com/Snail3D/snailprint.git
cd snailprint
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Authenticate

```bash
# Bambu Cloud login (for MakerWorld + cloud features)
python3.12 bambu_cloud.py --login YOUR_EMAIL YOUR_PASSWORD

# Discord notifications (optional)
# Create ~/.snailprint/discord.json with {"token": "BOT_TOKEN", "channel": "CHANNEL_ID"}
```

### Printer Setup

1. Put your Bambu printer(s) in **LAN mode** (Settings → Network → LAN Only)
2. Note the IP and access code from the screen
3. Edit the `LAN_PRINTERS` dict in `bambu_cloud.py` with your printer details

### Start

```bash
python3.12 server.py  # API on port 7780
```

For text-to-3D generation, also run [SnailStudio](https://github.com/Snail3D/localclanka):
```bash
cd ~/localforge/tools/snailking-studio && python3 server.py &  # port 7777
```

---

## What It Does

Five input modes, one pipeline:

| Mode | You provide | SnailPrint does |
|------|------------|-----------------|
| **generate** | Text prompt | AI generates 3D model → flat base → slice → print |
| **image** | Single photo | Photo → 3D model → flat base → slice → print |
| **photos** | 3-8 photos from angles | Multi-view reconstruction → bust/figurine → print |
| **file** | STL or 3MF path | Flat base → slice → print |
| **makerworld** | Search query | Search → smart download → slice if needed → print |

Every print gets:
- **5% flat base cut** — solid bottom for bed adhesion
- **Tree supports** (build plate only) — clean removal
- **Print-friendly prompt enhancement** — no overhangs >45°, solid geometry
- **AMS filament matching** — checks what's loaded, picks the right slot
- **Pre-print safety check** — bed clear, nozzle size, filament type, HMS warnings
- **Camera monitoring** — 10s video clips to Discord every 30 minutes
- **First-layer vision check** — Gemma 4 analyzes the first layers for failures

---

## API

Base URL: `http://localhost:7780`

### Start a Print

```bash
# Generate from text
curl -X POST http://localhost:7780/api/print/start \
  -H "Content-Type: application/json" \
  -d '{"mode":"generate","prompt":"a dragon figurine","filament":"PLA","scale_mm":50}'

# From a single photo
curl -X POST http://localhost:7780/api/print/start \
  -d '{"mode":"image","image":"/path/to/photo.jpg","filament":"PLA"}'

# Bust from multiple photos
curl -X POST http://localhost:7780/api/print/start \
  -d '{"mode":"photos","images":["/path/front.jpg","/path/side.jpg","/path/back.jpg"],"filament":"PLA"}'

# Print existing file
curl -X POST http://localhost:7780/api/print/start \
  -d '{"mode":"file","file":"/path/to/model.stl","filament":"PETG"}'

# Search MakerWorld and print
curl -X POST http://localhost:7780/api/print/start \
  -d '{"mode":"makerworld","query":"phone stand","filament":"PLA"}'
```

**Parameters:**
- `mode` — generate, image, photos, file, makerworld
- `prompt` — text description (generate mode)
- `image` — path to photo (image mode)
- `images` — array of photo paths (photos mode, 3-8 angles)
- `file` — path to STL/3MF (file mode)
- `query` / `makerworld_id` — search term or model ID (makerworld mode)
- `filament` — PLA, PETG, ABS, ASA, TPU (default: PLA)
- `color` — matches against AMS inventory
- `scale_mm` — model size (default: 50)
- `printer` — auto or serial number (default: auto, picks idle printer)
- `engine` — spar3d (~1.5 min) or hunyuan (~5 min)

### Other Endpoints

```bash
# Check printers + AMS inventory
curl http://localhost:7780/api/print/printers

# Print job status
curl http://localhost:7780/api/print/status/{job_id}

# Search MakerWorld (without printing)
curl -X POST http://localhost:7780/api/print/makerworld/search \
  -d '{"query":"vase","limit":10}'

# Slice only (no print)
curl -X POST http://localhost:7780/api/print/slice \
  -d '{"stl":"/path/to/model.stl","filament":"PLA","supports":"tree"}'

# Cancel
curl -X POST http://localhost:7780/api/print/cancel/{job_id}
```

---

## Pipeline

```
Input (text / photo / photos / file / MakerWorld)
  ↓
3D Generation (SPAR3D or Hunyuan3D via SnailStudio)
  ↓
Flat Base Cut (5%) + Watertight Repair (trimesh)
  ↓
OrcaSlicer CLI (P2S hybrid profile, tree supports, AMS mapping)
  ↓
Safety Check (bed clear via MQTT + camera, nozzle, filament, HMS)
  ↓
FTPS Upload → MQTT Print Command (ftp:/// protocol)
  ↓
Discord Monitor (10s clips every 30min, first-layer AI vision check)
```

---

## Bambu P2S LAN Protocol

We reverse-engineered the P2S local network protocol. Key discoveries:

- **Print command URL must use `ftp:///`** (triple slash) — `file:///sdcard/` doesn't work on P2S
- **FTPS needs implicit TLS + session reuse** on port 990
- **Camera is RTSP** on port 322 (not JPEG stream on 6000)
- **OrcaSlicer CLI crashes** with P2S profiles — workaround: A1 hybrid profile with P2S settings

Full protocol documentation: [`docs/BAMBU_P2S_LAN_PROTOCOL.md`](docs/BAMBU_P2S_LAN_PROTOCOL.md)

---

## Hermes Integration

SnailPrint ships as a Hermes skill. Tell your agent:

- "print me a cat" → generates and prints
- "make a bust from these photos of me" → multi-view reconstruction
- "find a phone stand on MakerWorld and print it" → search + print
- "what filament do I have?" → shows AMS inventory
- "how's my print?" → status check

Skill file: `~/.hermes/skills/media/snailprint/SKILL.md`

---

## Configuration

All secrets live in `~/.snailprint/` (gitignored):

| File | Purpose |
|------|---------|
| `token.json` | Bambu Cloud auth token |
| `discord.json` | Discord bot token + channel ID |
| `printers.json` | Cached printer data |
| `profiles/` | Patched slicer profiles |

---

## Requirements

- macOS with Apple Silicon (M-series)
- Python 3.12
- OrcaSlicer (`brew install --cask orcaslicer`)
- ffmpeg (`brew install ffmpeg`)
- Bambu Lab printer(s) in LAN mode
- USB drive in printer (for file storage)

Optional:
- [SnailStudio](https://github.com/Snail3D/localclanka) — for 3D model generation
- [ClawHip](https://github.com/Yeachan-Heo/clawhip) — Discord event gateway
- Local vision model (Gemma 4) — for first-layer quality analysis

---

## License

MIT
