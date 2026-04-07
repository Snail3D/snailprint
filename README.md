# SnailPrint 🖨️

Text-to-3D-print pipeline for Bambu Lab printers. Generate 3D models from text, search MakerWorld, auto-slice with tree supports, and send to your printers — all from a single command.

## Features

- **Text to Print** — describe what you want, get a physical object
- **MakerWorld Integration** — search and download models with smart profile matching
- **Bambu Cloud + LAN** — MQTT printer control, AMS filament inventory
- **Camera Monitoring** — RTSP camera snapshots and 10s video clips
- **Discord Updates** — print progress with video clips every 30 minutes
- **Safety Checks** — bed clear detection, nozzle verification, filament matching
- **Auto Tree Supports** — BambuStudio CLI slicing with optimal settings

## Quick Start

```bash
# Clone and install
git clone https://github.com/Snail3D/snailprint.git
cd snailprint
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Login to Bambu Cloud
python3.12 bambu_cloud.py --login YOUR_EMAIL YOUR_PASSWORD

# Start the server
python3.12 server.py
```

## Usage

```bash
# Generate and print a model
curl -X POST http://localhost:7780/api/print/start \
  -H "Content-Type: application/json" \
  -d '{"mode":"generate","prompt":"a cat figurine","filament":"PLA"}'

# Search MakerWorld and print
curl -X POST http://localhost:7780/api/print/start \
  -d '{"mode":"makerworld","query":"phone stand","filament":"PLA"}'

# Print an existing STL
curl -X POST http://localhost:7780/api/print/start \
  -d '{"mode":"file","file":"/path/to/model.stl","filament":"PETG"}'

# Check printers and AMS
curl http://localhost:7780/api/print/printers
```

## Pipeline

```
Text Prompt / STL File / MakerWorld Search
  → 3D Model Generation (SPAR3D / Hunyuan3D)
  → Flat Base Cut + Watertight Repair
  → BambuStudio CLI Slice (tree supports, printer profile)
  → Pre-Print Safety Check (bed clear, nozzle, filament, HMS)
  → Bambu Cloud Upload → Print
  → Discord Monitoring (10s video clips every 30min)
```

## Requirements

- macOS with Apple Silicon
- Python 3.12
- BambuStudio (`/Applications/BambuStudio.app`)
- ffmpeg (`brew install ffmpeg`)
- Bambu Lab printer(s) in LAN mode
- [SnailStudio](https://github.com/Snail3D/localclanka) for 3D model generation (optional)

## Configuration

All secrets stored in `~/.snailprint/` (not in repo):
- `token.json` — Bambu Cloud auth token
- `discord.json` — Discord bot token and channel ID
- `printers.json` — cached printer data

## License

MIT
