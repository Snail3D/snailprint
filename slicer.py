#!/usr/bin/env python3
"""
BambuStudio CLI slicer wrapper.
Slices STL files to 3MF with tree supports, printer profiles, filament settings.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

BAMBU_CLI = "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"
PROFILES_DIR = Path("/Applications/BambuStudio.app/Contents/Resources/profiles/BBL")

# Machine profile mapping
MACHINE_PROFILES = {
    "P2S": "Bambu Lab P2S 0.4 nozzle.json",
    "P1S": "Bambu Lab P1S 0.4 nozzle.json",
    "X1C": "Bambu Lab X1 Carbon 0.4 nozzle.json",
    "A1": "Bambu Lab A1 0.4 nozzle.json",
}

# Process profile mapping
PROCESS_PROFILES = {
    "P2S": "0.20mm Standard @BBL P2S.json",
    "P1S": "0.20mm Standard @BBL P1P.json",
    "X1C": "0.20mm Standard @BBL X1C.json",
    "A1": "0.20mm Standard @BBL A1.json",
}

# Filament type to profile mapping
FILAMENT_PROFILES = {
    "PLA": "Generic PLA @base.json",
    "PETG": "Generic PETG @base.json",
    "TPU": "Generic TPU @base.json",
    "ABS": "Generic ABS @base.json",
    "ASA": "Generic ASA @base.json",
}


def slice_stl(stl_path, output_path=None, printer_model="P2S",
              filament_type="PLA", tree_supports=True):
    """
    Slice an STL file using BambuStudio CLI.

    Args:
        stl_path: Path to input STL file
        output_path: Path for output 3MF file (auto-generated if None)
        printer_model: Printer model for profile selection
        filament_type: Filament type (PLA, PETG, etc.)
        tree_supports: Enable tree supports (default True)

    Returns:
        Path to output 3MF file
    """
    stl_path = str(Path(stl_path).resolve())

    if output_path is None:
        stem = Path(stl_path).stem
        output_path = str(Path(stl_path).parent / f"{stem}_sliced.3mf")

    # Resolve profiles
    machine_file = PROFILES_DIR / "machine" / MACHINE_PROFILES.get(printer_model, MACHINE_PROFILES["P2S"])
    process_file = PROFILES_DIR / "process" / PROCESS_PROFILES.get(printer_model, PROCESS_PROFILES["P2S"])
    filament_file = PROFILES_DIR / "filament" / FILAMENT_PROFILES.get(filament_type.upper(), FILAMENT_PROFILES["PLA"])

    if not machine_file.exists():
        raise FileNotFoundError(f"Machine profile not found: {machine_file}")
    if not process_file.exists():
        raise FileNotFoundError(f"Process profile not found: {process_file}")
    if not filament_file.exists():
        raise FileNotFoundError(f"Filament profile not found: {filament_file}")

    # Build command
    cmd = [
        BAMBU_CLI,
        "--orient",
        "--arrange", "1",
        "--load-settings", f"{machine_file};{process_file}",
        "--load-filaments", str(filament_file),
        "--slice", "0",
        "--export-3mf", str(output_path),
    ]

    # Tree supports via process settings override
    if tree_supports:
        # Create a temporary process override with tree supports
        override = _create_support_override(process_file, tree_supports=True)
        cmd[cmd.index("--load-settings") + 1] = f"{machine_file};{override}"

    cmd.append(stl_path)

    print(f"[SLICER] Slicing {Path(stl_path).name} for {printer_model} with {filament_type}")
    print(f"[SLICER] CMD: {' '.join(cmd[:6])}...")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "DISPLAY": ""},  # headless
    )

    if result.returncode != 0:
        err = result.stderr[:500] if result.stderr else result.stdout[:500]
        raise RuntimeError(f"BambuStudio slice failed (rc={result.returncode}): {err}")

    if not Path(output_path).exists():
        raise RuntimeError(f"Sliced 3MF not created at {output_path}")

    size = Path(output_path).stat().st_size
    print(f"[SLICER] Done: {output_path} ({size / 1024:.0f} KB)")
    return str(output_path)


def _create_support_override(base_process_file, tree_supports=True):
    """Create a temporary process file with tree support settings."""
    base = json.loads(Path(base_process_file).read_text())

    if tree_supports:
        base["support_type"] = "tree(auto)"
        base["enable_support"] = "1"
        base["support_on_build_plate_only"] = "0"

    tmp = Path(tempfile.mktemp(suffix=".json", prefix="snailprint_process_"))
    tmp.write_text(json.dumps(base, indent=2))
    return str(tmp)


def list_available_profiles():
    """List available machine, process, and filament profiles."""
    result = {
        "machines": sorted(f.stem for f in (PROFILES_DIR / "machine").glob("*.json")),
        "processes": sorted(f.stem for f in (PROFILES_DIR / "process").glob("*.json")),
        "filaments": sorted(f.stem for f in (PROFILES_DIR / "filament").glob("*.json")),
    }
    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 slicer.py input.stl [output.3mf] [printer_model] [filament_type]")
        sys.exit(1)

    stl = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    printer = sys.argv[3] if len(sys.argv) > 3 else "P2S"
    filament = sys.argv[4] if len(sys.argv) > 4 else "PLA"

    result = slice_stl(stl, out, printer_model=printer, filament_type=filament)
    print(f"Output: {result}")
