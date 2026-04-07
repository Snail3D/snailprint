#!/usr/bin/env python3
"""
SnailPrint pipeline orchestrator.
Ties together: 3D generation, mesh prep, slicing, cloud upload, monitoring.
"""
import json
import os
import time
import uuid
from pathlib import Path

import requests

from bambu_cloud import BambuCloud
from mesh_prep import prepare_for_print
from slicer import slice_stl
from monitor import PrintMonitor
import makerworld

SNAILSTUDIO_URL = "http://localhost:7777"
GENERATED_DIR = Path(os.path.expanduser("~/.snailprint/generated"))
GENERATED_DIR.mkdir(parents=True, exist_ok=True)


class PrintPipeline:
    def __init__(self):
        self.cloud = BambuCloud()
        self._jobs = {}

    def print_from_text(self, prompt, filament="PLA", color=None,
                        scale_mm=50, printer=None, engine="spar3d"):
        """Full pipeline: text prompt → 3D model → slice → print."""
        job_id = str(uuid.uuid4())[:8]
        job_dir = GENERATED_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        self._update_job(job_id, status="generating", prompt=prompt)

        # Step 1: Generate 3D model via SnailStudio
        resp = requests.post(f"{SNAILSTUDIO_URL}/api/3d/generate", json={
            "mode": "text",
            "prompt": prompt,
            "options": {
                "engine": engine,
                "format": "stl",
                "scale_mm": scale_mm,
                "texture": False,
                "watertight": True,
                "generate_reference": True,
            },
        }, timeout=10)
        data = resp.json()
        gen_job_id = data.get("job_id")

        if not gen_job_id:
            self._update_job(job_id, status="failed", error="3D generation failed to start")
            return job_id

        # Poll for generation completion
        self._update_job(job_id, status="generating_3d", gen_job_id=gen_job_id)
        mesh_path = self._wait_for_generation(gen_job_id, job_dir)

        if mesh_path is None:
            self._update_job(job_id, status="failed", error="3D generation failed")
            return job_id

        # Continue with the common pipeline
        return self._finish_pipeline(job_id, job_dir, mesh_path, filament, color,
                                      scale_mm, printer, prompt)

    def print_from_photos(self, image_paths, filament="PLA", color=None,
                          scale_mm=50, printer=None):
        """Pipeline: multiple reference photos → multi-view 3D reconstruction → slice → print.
        Great for busts, figurines of real people/objects from photos."""
        job_id = str(uuid.uuid4())[:8]
        job_dir = GENERATED_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        self._update_job(job_id, status="reconstructing", images=image_paths)

        # Read images and convert to base64
        import base64
        images_b64 = []
        for p in image_paths:
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
                ext = Path(p).suffix.lower().strip(".")
                mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png"}.get(ext, "jpeg")
                images_b64.append(f"data:image/{mime};base64,{b64}")

        resp = requests.post(f"{SNAILSTUDIO_URL}/api/3d/generate", json={
            "mode": "multiview",
            "images": images_b64,
            "options": {
                "format": "stl",
                "scale_mm": scale_mm,
                "texture": False,
                "watertight": True,
            },
        }, timeout=10)
        data = resp.json()
        gen_job_id = data.get("job_id")

        if not gen_job_id:
            self._update_job(job_id, status="failed", error="Multi-view reconstruction failed to start")
            return job_id

        self._update_job(job_id, status="reconstructing_3d", gen_job_id=gen_job_id)
        mesh_path = self._wait_for_generation(gen_job_id, job_dir)

        if mesh_path is None:
            self._update_job(job_id, status="failed", error="Multi-view reconstruction failed")
            return job_id

        return self._finish_pipeline(job_id, job_dir, mesh_path, filament, color,
                                      scale_mm, printer, "photo_reconstruction")

    def print_from_image(self, image_path, filament="PLA", color=None,
                         scale_mm=50, printer=None, engine="spar3d"):
        """Pipeline: single reference image → 3D model → slice → print."""
        job_id = str(uuid.uuid4())[:8]
        job_dir = GENERATED_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        self._update_job(job_id, status="generating", image=image_path)

        import base64
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
            ext = Path(image_path).suffix.lower().strip(".")
            mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png"}.get(ext, "jpeg")
            image_b64 = f"data:image/{mime};base64,{b64}"

        resp = requests.post(f"{SNAILSTUDIO_URL}/api/3d/generate", json={
            "mode": "image",
            "image": image_b64,
            "options": {
                "engine": engine,
                "format": "stl",
                "scale_mm": scale_mm,
                "texture": False,
                "watertight": True,
            },
        }, timeout=10)
        data = resp.json()
        gen_job_id = data.get("job_id")

        if not gen_job_id:
            self._update_job(job_id, status="failed", error="Image-to-3D failed to start")
            return job_id

        self._update_job(job_id, status="generating_3d", gen_job_id=gen_job_id)
        mesh_path = self._wait_for_generation(gen_job_id, job_dir)

        if mesh_path is None:
            self._update_job(job_id, status="failed", error="Image-to-3D generation failed")
            return job_id

        return self._finish_pipeline(job_id, job_dir, mesh_path, filament, color,
                                      scale_mm, printer, Path(image_path).stem)

    def print_from_file(self, file_path, filament="PLA", color=None,
                        scale_mm=50, printer=None):
        """Pipeline: existing file → prep → slice → print."""
        job_id = str(uuid.uuid4())[:8]
        job_dir = GENERATED_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        self._update_job(job_id, status="preparing", file=file_path)

        # If already a 3MF, skip to printing
        if file_path.lower().endswith(".3mf"):
            return self._submit_print(job_id, file_path, filament, color, printer,
                                       Path(file_path).stem)

        return self._finish_pipeline(job_id, job_dir, file_path, filament, color,
                                      scale_mm, printer, Path(file_path).stem)

    def print_from_makerworld(self, query=None, model_id=None, filament="PLA",
                               color=None, printer=None):
        """Pipeline: MakerWorld search/download → prep → slice → print."""
        job_id = str(uuid.uuid4())[:8]
        job_dir = GENERATED_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        self._update_job(job_id, status="searching_makerworld", query=query)

        # Search MakerWorld
        from makerworld import MakerWorld
        mw = MakerWorld()

        if model_id is None:
            results = mw.search(query, limit=5, printer="N7")
            if not results:
                self._update_job(job_id, status="failed", error=f"No MakerWorld results for '{query}'")
                return job_id
            model_id = results[0]["id"]
            self._update_job(job_id, makerworld_model=results[0])

        # Smart download: try pre-sliced 3MF for P2S first
        self._update_job(job_id, status="downloading")
        file_path, is_presliced = mw.download(
            model_id, output_dir=str(job_dir),
            printer="N7", nozzle=0.4,
            filament_type=filament,
        )

        job_name = f"MakerWorld #{model_id}"

        if is_presliced:
            # Pre-sliced 3MF matches our printer — send straight to print
            self._update_job(job_id, status="presliced_match",
                             detail="Using pre-sliced profile from MakerWorld")
            return self._submit_print(job_id, file_path, filament, color, printer, job_name)

        # No matching profile — slice it ourselves
        return self._finish_pipeline(job_id, job_dir, file_path, filament, color,
                                      100, printer, job_name)

    def _finish_pipeline(self, job_id, job_dir, mesh_path, filament, color,
                          scale_mm, printer, job_name):
        """Common pipeline: mesh prep → slice → print."""
        # Step: Prepare mesh for printing
        self._update_job(job_id, status="preparing_mesh")
        stl_path = str(job_dir / "model_print.stl")
        try:
            stl_path = prepare_for_print(mesh_path, stl_path, scale_mm=scale_mm)
        except Exception as e:
            self._update_job(job_id, status="failed", error=f"Mesh prep failed: {e}")
            return job_id

        # Step: Slice
        self._update_job(job_id, status="slicing")
        threemf_path = str(job_dir / "model_sliced.3mf")
        try:
            threemf_path = slice_stl(stl_path, threemf_path,
                                      printer_model="P2S", filament_type=filament)
        except Exception as e:
            self._update_job(job_id, status="failed", error=f"Slicing failed: {e}")
            return job_id

        return self._submit_print(job_id, threemf_path, filament, color, printer, job_name)

    def _submit_print(self, job_id, threemf_path, filament, color, printer, job_name):
        """Find printer, run safety checks, submit job, start monitor."""
        self._update_job(job_id, status="finding_printer")

        # Find best printer
        if printer and printer != "auto":
            serial = printer
        else:
            serial, slot = self.cloud.find_filament(filament, color)
            if serial is None:
                self._update_job(job_id, status="failed",
                                  error=f"No printer found with {color or ''} {filament}")
                return job_id

        # === PRE-PRINT SAFETY CHECK ===
        self._update_job(job_id, status="safety_check", printer=serial)
        checks = self._run_safety_checks(serial, filament)
        self._update_job(job_id, safety_checks=checks)

        blockers = [c for c in checks if c["level"] == "blocker"]
        warnings = [c for c in checks if c["level"] == "warning"]

        if blockers:
            reasons = "; ".join(c["message"] for c in blockers)
            self._update_job(job_id, status="blocked",
                              error=f"Cannot print: {reasons}")
            return job_id

        if warnings:
            self._update_job(job_id, status="printing_with_warnings",
                              warnings=[c["message"] for c in warnings])

        # Submit print (cloud API or local LAN fallback)
        self._update_job(job_id, status="uploading", printer=serial)
        try:
            result = self.cloud.submit_print(serial, threemf_path)
            self._update_job(job_id, status="printing", cloud_result=result)
        except Exception as e:
            self._update_job(job_id, status="failed", error=f"Print submission failed: {e}")
            return job_id

        # Start monitor
        monitor = PrintMonitor(self.cloud, serial, job_name=job_name)
        monitor.start()
        self._update_job(job_id, monitor=monitor)

        return job_id

    def _run_safety_checks(self, serial, filament):
        """
        Pre-print safety checks via MQTT telemetry.
        Returns list of {level: 'blocker'|'warning'|'ok', check: str, message: str}
        """
        checks = []
        mqtt_data = self.cloud._get_ams_mqtt(serial)

        if mqtt_data is None:
            checks.append({"level": "blocker", "check": "connectivity",
                           "message": "Cannot reach printer"})
            return checks

        state = mqtt_data.get("gcode_state", "").upper()
        subtask = mqtt_data.get("subtask_name", "")

        # Bed clear check
        if state in ("RUNNING", "PREPARE"):
            checks.append({"level": "blocker", "check": "bed_clear",
                           "message": f"Printer is currently printing: '{subtask}'"})
        elif state == "PAUSE":
            checks.append({"level": "blocker", "check": "bed_clear",
                           "message": f"Printer has a paused print: '{subtask}'"})
        elif state == "FINISH" and subtask:
            checks.append({"level": "warning", "check": "bed_clear",
                           "message": f"Previous print '{subtask}' may still be on bed"})
        else:
            checks.append({"level": "ok", "check": "bed_clear",
                           "message": "Bed appears clear"})

        # Nozzle check
        nozzle = mqtt_data.get("nozzle_diameter", "")
        if nozzle and float(nozzle) != 0.4:
            checks.append({"level": "warning", "check": "nozzle",
                           "message": f"Nozzle is {nozzle}mm (expected 0.4mm)"})
        else:
            checks.append({"level": "ok", "check": "nozzle",
                           "message": f"Nozzle: {nozzle}mm"})

        # Filament check
        ams_slots = mqtt_data.get("ams", [])
        has_filament = any(
            filament.upper() in slot.get("type", "").upper()
            for slot in ams_slots
        )
        if not has_filament:
            loaded = [f"{s['type']} ({s['remaining']}%)" for s in ams_slots if s.get("type")]
            checks.append({"level": "warning", "check": "filament",
                           "message": f"No {filament} found in AMS. Loaded: {', '.join(loaded)}"})
        else:
            matching = [s for s in ams_slots if filament.upper() in s.get("type", "").upper()]
            low = [s for s in matching if s.get("remaining", 0) < 10]
            if low:
                checks.append({"level": "warning", "check": "filament",
                               "message": f"{filament} is low ({low[0]['remaining']}% remaining)"})
            else:
                checks.append({"level": "ok", "check": "filament",
                               "message": f"{filament} loaded and ready"})

        # Camera snapshot of bed
        snap_path = str(job_dir / "bed_check.jpg") if hasattr(self, '_current_job_dir') else "/tmp/snailprint_bed_check.jpg"
        import tempfile
        snap_path = tempfile.mktemp(suffix=".jpg", prefix="bed_check_")
        has_image = self.cloud.get_camera_snapshot(serial, snap_path)
        if has_image:
            checks.append({"level": "ok", "check": "camera",
                           "message": f"Bed snapshot saved: {snap_path}",
                           "image": snap_path})
        else:
            checks.append({"level": "warning", "check": "camera",
                           "message": "Could not get camera snapshot (LAN mode required)"})

        # HMS health warnings
        hms = mqtt_data.get("hms", [])
        if hms:
            for h in hms[:3]:
                checks.append({"level": "warning", "check": "hms",
                               "message": f"Printer warning: {h}"})

        # Print error check
        err = mqtt_data.get("mc_print_error_code", "0")
        if str(err) != "0":
            checks.append({"level": "warning", "check": "error",
                           "message": f"Printer error code: {err}"})

        return checks

    def _wait_for_generation(self, gen_job_id, job_dir, timeout=600):
        """Poll SnailStudio for 3D generation completion."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                resp = requests.get(
                    f"{SNAILSTUDIO_URL}/api/3d/status/{gen_job_id}", timeout=5
                )
                data = resp.json()
                status = data.get("status", "")

                if status == "complete":
                    result = data.get("result", {})
                    # Download the mesh file
                    mesh_url = result.get("preview") or result.get("mesh")
                    if mesh_url:
                        mesh_resp = requests.get(
                            f"{SNAILSTUDIO_URL}{mesh_url}", timeout=30
                        )
                        ext = ".glb" if "glb" in mesh_url else ".stl"
                        local_path = job_dir / f"model{ext}"
                        local_path.write_bytes(mesh_resp.content)
                        return str(local_path)

                elif status == "failed":
                    return None

            except Exception:
                pass

            time.sleep(5)

        return None  # timeout

    def _update_job(self, job_id, **kwargs):
        """Update job state."""
        if job_id not in self._jobs:
            self._jobs[job_id] = {"job_id": job_id, "created": time.time()}
        # Don't store non-serializable objects in the main state
        monitor = kwargs.pop("monitor", None)
        self._jobs[job_id].update(kwargs)
        if monitor:
            self._jobs[job_id]["_monitor"] = monitor

    def get_job(self, job_id):
        """Get job state (without internal objects)."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def get_printers(self):
        """Get all printers with AMS info."""
        return self.cloud.list_printers()


pipeline = PrintPipeline()
