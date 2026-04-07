#!/usr/bin/env python3
"""
SnailPrint REST API server.
Exposes the print pipeline via HTTP endpoints.
"""
import json
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

from pipeline import pipeline

PORT = 7780


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class PrintHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/print/printers":
            self._handle_printers()
        elif path.startswith("/api/print/status/"):
            self._handle_status(path.split("/")[-1])
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path == "/api/print/start":
            self._handle_start()
        elif self.path == "/api/print/makerworld/search":
            self._handle_makerworld_search()
        elif self.path == "/api/print/slice":
            self._handle_slice()
        elif self.path.startswith("/api/print/cancel/"):
            self._handle_cancel(self.path.split("/")[-1])
        else:
            self._json({"error": "Not found"}, 404)

    def _handle_printers(self):
        try:
            printers = pipeline.get_printers()
            self._json({"success": True, "printers": printers})
        except Exception as e:
            self._json({"success": False, "error": str(e)}, 500)

    def _handle_status(self, job_id):
        job = pipeline.get_job(job_id)
        if job:
            self._json(job)
        else:
            self._json({"error": "Job not found"}, 404)

    def _handle_start(self):
        body = self._read_body()
        mode = body.get("mode", "generate")

        filament = body.get("filament", "PLA")
        color = body.get("color")
        scale_mm = body.get("scale_mm", 50)
        printer = body.get("printer", "auto")
        engine = body.get("engine", "spar3d")

        # Launch in background thread so we don't block
        def run(jid):
            try:
                real_id = None
                if mode == "generate":
                    real_id = pipeline.print_from_text(
                        prompt=body.get("prompt", ""),
                        filament=filament, color=color,
                        scale_mm=scale_mm, printer=printer, engine=engine,
                    )
                elif mode == "file":
                    real_id = pipeline.print_from_file(
                        file_path=body.get("file", ""),
                        filament=filament, color=color,
                        scale_mm=scale_mm, printer=printer,
                    )
                elif mode == "image":
                    real_id = pipeline.print_from_image(
                        image_path=body.get("image", ""),
                        filament=filament, color=color,
                        scale_mm=scale_mm, printer=printer, engine=engine,
                    )
                elif mode == "photos":
                    real_id = pipeline.print_from_photos(
                        image_paths=body.get("images", []),
                        filament=filament, color=color,
                        scale_mm=scale_mm, printer=printer,
                    )
                elif mode == "makerworld":
                    real_id = pipeline.print_from_makerworld(
                        query=body.get("query"),
                        model_id=body.get("makerworld_id"),
                        filament=filament, color=color, printer=printer,
                    )
                # Link the pre-job to the real pipeline job
                if real_id and real_id != jid:
                    real_job = pipeline.get_job(real_id)
                    if real_job:
                        pipeline._jobs[jid] = pipeline._jobs.get(real_id, {})
                        pipeline._jobs[jid]["job_id"] = jid
            except Exception as e:
                print(f"[PIPELINE] Error: {e}")
                import traceback; traceback.print_exc()
                pipeline._update_job(jid, status="failed", error=str(e))

        # Create job synchronously to get ID, then run pipeline in background
        import uuid as _uuid
        job_id = str(_uuid.uuid4())[:8]
        pipeline._update_job(job_id, status="starting", mode=mode,
                              prompt=body.get("prompt", ""),
                              file=body.get("file", ""),
                              query=body.get("query", ""))

        t = threading.Thread(target=run, args=(job_id,), daemon=True)
        t.start()

        self._json({"success": True, "job_id": job_id, "status": "starting"})

    def _handle_makerworld_search(self):
        body = self._read_body()
        query = body.get("query", "")
        limit = body.get("limit", 10)

        try:
            import makerworld
            results = makerworld.search(query, limit=limit)
            self._json({"success": True, "results": results})
        except Exception as e:
            self._json({"success": False, "error": str(e)}, 500)

    def _handle_slice(self):
        body = self._read_body()
        stl = body.get("stl", "")
        filament = body.get("filament", "PLA")
        supports = body.get("supports", "tree")

        try:
            from slicer import slice_stl
            result = slice_stl(stl, filament_type=filament,
                                tree_supports=(supports == "tree"))
            self._json({"success": True, "threemf": result})
        except Exception as e:
            self._json({"success": False, "error": str(e)}, 500)

    def _handle_cancel(self, job_id):
        # TODO: implement cancel via cloud API
        self._json({"success": True, "cancelled": True})

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            return json.loads(self.rfile.read(length))
        return {}

    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        if "/api/" in str(args[0]):
            print(f"  {args[0]}")


if __name__ == "__main__":
    print()
    print("  🖨️  SnailPrint")
    print(f"  http://localhost:{PORT}")
    print(f"  Endpoints: /api/print/start, /api/print/printers, /api/print/status/<id>")
    print()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), PrintHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
