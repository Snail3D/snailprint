#!/usr/bin/env python3
"""
OrcaSlicer CLI slicer wrapper.
Slices STL files to Bambu-format 3MF with tree supports, printer profiles, filament settings.

OrcaSlicer 2.3.2 has a CLI bug where P2S dual-extruder profiles crash with SIGSEGV
in update_values_to_printer_extruders_for_multiple_filaments (NULL pointer deref).
Workaround: use an A1-based hybrid machine profile that carries P2S physical settings
(bed size, start/end gcode, retraction, max speeds) but avoids the dual-extruder
code path. The A1 process profile is compatible and produces correct P2S gcode.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

ORCA_CLI = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
PROFILES_DIR = Path("/Applications/OrcaSlicer.app/Contents/Resources/profiles/BBL")

# Patched profiles in ~/.snailprint/profiles/
PATCHED_PROFILES_DIR = Path(os.path.expanduser("~/.snailprint/profiles"))

# Machine profile mapping
# P2S uses a hybrid profile (A1 base + P2S physical settings) to avoid CLI segfault
MACHINE_PROFILES = {
    "P2S": "P2S_0.4_a1_hybrid.json",  # patched -- avoids dual-extruder CLI crash
    "P1S": "Bambu Lab P1S 0.4 nozzle.json",
    "X1C": "Bambu Lab X1 Carbon 0.4 nozzle.json",
    "A1": "Bambu Lab A1 0.4 nozzle.json",
}

# Process profile mapping
# P2S uses A1 process profile (compatible) because the P2S process profile
# has dual-extruder arrays that trigger the same CLI crash
PROCESS_PROFILES = {
    "P2S": "0.20mm Standard @BBL A1.json",  # A1 process -- compatible with hybrid machine
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

# P2S physical settings to overlay onto the A1 base machine profile
_P2S_OVERLAY_KEYS = [
    'printable_area', 'printable_height', 'bed_exclude_area',
    'machine_start_gcode', 'machine_end_gcode', 'before_layer_change_gcode',
    'layer_change_gcode', 'time_lapse_gcode', 'printer_model',
    'printer_variant', 'printer_structure', 'bed_mesh_min', 'bed_mesh_max',
    'bed_mesh_probe_distance', 'gcode_flavor', 'thumbnails',
    'nozzle_diameter', 'nozzle_height', 'nozzle_type', 'nozzle_volume',
    'retraction_length', 'retraction_speed', 'deretraction_speed',
    'retract_lift_above', 'retract_lift_below', 'z_hop', 'z_hop_types',
    'wipe', 'wipe_distance', 'retract_before_wipe',
    'machine_max_acceleration_x', 'machine_max_acceleration_y',
    'machine_max_acceleration_z', 'machine_max_acceleration_e',
    'machine_max_speed_x', 'machine_max_speed_y', 'machine_max_speed_z',
    'machine_max_speed_e', 'machine_max_jerk_x', 'machine_max_jerk_y',
    'machine_max_jerk_z', 'machine_max_jerk_e',
    'machine_max_acceleration_extruding', 'machine_max_acceleration_retracting',
    'machine_max_acceleration_travel',
]


def _ensure_p2s_hybrid_profile():
    """
    Generate the P2S hybrid machine profile if it doesn't exist or if source profiles
    have been updated. Uses A1 as base (works in CLI) with P2S physical settings overlaid.
    """
    hybrid_path = PATCHED_PROFILES_DIR / "P2S_0.4_a1_hybrid.json"
    a1_path = PROFILES_DIR / "machine" / "Bambu Lab A1 0.4 nozzle.json"
    p2s_path = PROFILES_DIR / "machine" / "Bambu Lab P2S 0.4 nozzle.json"

    if not a1_path.exists() or not p2s_path.exists():
        raise FileNotFoundError(
            f"OrcaSlicer profiles not found. Need: {a1_path} and {p2s_path}"
        )

    # Regenerate if missing or if source profiles are newer
    if hybrid_path.exists():
        hybrid_mtime = hybrid_path.stat().st_mtime
        if (a1_path.stat().st_mtime <= hybrid_mtime and
                p2s_path.stat().st_mtime <= hybrid_mtime):
            return hybrid_path

    import copy
    a1 = json.loads(a1_path.read_text())
    p2s = json.loads(p2s_path.read_text())

    hybrid = copy.deepcopy(a1)
    for key in _P2S_OVERLAY_KEYS:
        if key in p2s:
            val = p2s[key]
            # Trim dual-extruder arrays (2-element) to single to avoid CLI crash
            if isinstance(val, list) and len(val) == 2:
                hybrid[key] = [val[0]]
            else:
                hybrid[key] = val

    PATCHED_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    hybrid_path.write_text(json.dumps(hybrid, indent=2))
    print(f"[SLICER] Generated P2S hybrid profile: {hybrid_path}")
    return hybrid_path


def slice_stl(stl_path, output_path=None, printer_model="P2S",
              filament_type="PLA", tree_supports=True):
    """
    Slice an STL file using OrcaSlicer CLI.

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
    machine_name = MACHINE_PROFILES.get(printer_model, MACHINE_PROFILES["P2S"])

    # For P2S, ensure the hybrid profile exists
    if printer_model == "P2S":
        machine_file = _ensure_p2s_hybrid_profile()
    else:
        machine_file = PATCHED_PROFILES_DIR / machine_name
        if not machine_file.exists():
            machine_file = PROFILES_DIR / "machine" / machine_name

    process_file = PROFILES_DIR / "process" / PROCESS_PROFILES.get(
        printer_model, PROCESS_PROFILES["P2S"]
    )
    filament_file = PROFILES_DIR / "filament" / FILAMENT_PROFILES.get(
        filament_type.upper(), FILAMENT_PROFILES["PLA"]
    )

    if not machine_file.exists():
        raise FileNotFoundError(f"Machine profile not found: {machine_file}")
    if not process_file.exists():
        raise FileNotFoundError(f"Process profile not found: {process_file}")
    if not filament_file.exists():
        raise FileNotFoundError(f"Filament profile not found: {filament_file}")

    # Build command
    cmd = [
        ORCA_CLI,
        "--orient", "1",
        "--arrange", "1",
        "--load-settings", f"{machine_file};{process_file}",
        "--load-filaments", str(filament_file),
        "--slice", "0",
        "--export-3mf", str(output_path),
    ]

    # Tree supports via process settings override
    if tree_supports:
        override = _create_support_override(process_file, tree_supports=True)
        cmd[cmd.index("--load-settings") + 1] = f"{machine_file};{override}"

    cmd.append(stl_path)

    print(f"[SLICER] Slicing {Path(stl_path).name} for {printer_model} with {filament_type}")
    print(f"[SLICER] Using OrcaSlicer CLI")
    print(f"[SLICER] CMD: {' '.join(cmd[:6])}...")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
    )

    if result.returncode != 0:
        err = result.stderr[:500] if result.stderr else result.stdout[:500]
        raise RuntimeError(f"OrcaSlicer slice failed (rc={result.returncode}): {err}")

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
