#!/usr/bin/env python3
"""
Mesh preparation for 3D printing — flat base cut, watertight repair, scaling.
"""
import os
from pathlib import Path


def prepare_for_print(input_path, output_path=None, scale_mm=100, cut_percent=0.05):
    """
    Prepare a mesh for 3D printing.

    1. Load mesh (GLB, OBJ, STL, PLY)
    2. Cut bottom N% to create flat base
    3. Cap the cut (watertight)
    4. Repair normals
    5. Scale to target size in mm
    6. Place base at Z=0
    7. Export as STL

    Returns path to output STL.
    """
    import trimesh
    import numpy as np

    mesh = trimesh.load(str(input_path), force="mesh")

    if len(mesh.vertices) == 0:
        raise ValueError("Empty mesh — no vertices")

    # Cut bottom to create flat base
    z_min = mesh.bounds[0][2]
    z_range = mesh.extents[2]
    cut_z = z_min + (z_range * cut_percent)

    sliced = mesh.slice_plane(
        plane_origin=[0, 0, cut_z],
        plane_normal=[0, 0, 1],  # keep above
        cap=True,
    )

    if len(sliced.vertices) < 10:
        # If slicing lost too much, skip the cut
        print(f"Warning: slice lost too many vertices ({len(sliced.vertices)}), using original mesh")
        sliced = mesh

    # Repair
    trimesh.repair.fill_holes(sliced)
    trimesh.repair.fix_normals(sliced)

    # Translate so flat base sits at Z=0
    sliced.vertices[:, 2] -= sliced.bounds[0][2]

    # Scale to target size
    max_ext = max(sliced.extents)
    if max_ext > 0:
        sliced.apply_scale(scale_mm / max_ext)

    # Export
    if output_path is None:
        stem = Path(input_path).stem
        output_path = str(Path(input_path).parent / f"{stem}_print.stl")

    sliced.export(str(output_path), file_type="stl")

    return str(output_path)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 mesh_prep.py input.glb [output.stl] [scale_mm]")
        sys.exit(1)

    inp = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    scale = int(sys.argv[3]) if len(sys.argv) > 3 else 100

    result = prepare_for_print(inp, out, scale_mm=scale)
    print(f"Output: {result}")
