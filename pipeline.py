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
                        scale_mm=100, printer=None, engine="spar3d"):
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

    def print_from_file(self, file_path, filament="PLA", color=None,
                        scale_mm=100, printer=None):
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

        # Download model
        if model_id is None:
            results = makerworld.search(query, limit=1)
            if not results:
                self._update_job(job_id, status="failed", error=f"No MakerWorld results for '{query}'")
                return job_id
            model_id = results[0]["id"]
            self._update_job(job_id, makerworld_model=results[0])

        self._update_job(job_id, status="downloading")
        file_path = makerworld.download(model_id, output_dir=str(job_dir))

        # If it's a pre-sliced 3MF, use it directly
        if file_path.endswith(".3mf"):
            return self._submit_print(job_id, file_path, filament, color, printer,
                                       f"MakerWorld #{model_id}")

        return self._finish_pipeline(job_id, job_dir, file_path, filament, color,
                                      100, printer, f"MakerWorld #{model_id}")

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
        """Find printer, submit job, start monitor."""
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

        # Submit to cloud
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
