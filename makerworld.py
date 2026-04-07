#!/usr/bin/env python3
"""
MakerWorld search and download client.
Uses the actual MakerWorld API endpoints.
"""
import json
import os
import sys
from pathlib import Path

import requests

BASE_URL = "https://makerworld.com"
SEARCH_URL = "https://makerworld.com/api/v1/search-service/select/design2"
DETAIL_URL = "https://makerworld.com/api/v1/design-service/design/{design_id}?lang=en"
DOWNLOAD_URL = "https://makerworld.com/api/v1/design-service/instance/{instance_id}/f3mf?type=download"
TOKEN_PATH = Path(os.path.expanduser("~/.snailprint/token.json"))
DOWNLOAD_DIR = Path(os.path.expanduser("~/.snailprint/downloads"))

# devModelName -> human-readable printer name
PRINTER_NAMES = {
    "N7": "P2S",
    "C12": "P1S",
    "N2S": "A1",
    "N1": "A1 mini",
    "BL-P001": "X1C",
}

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://makerworld.com/",
    "x-bbl-client-name": "MakerWorld",
    "x-bbl-client-type": "web",
    "x-bbl-app-source": "makerworld",
    "x-bbl-client-version": "00.00.00.01",
}


class MakerWorld:
    def __init__(self, token=None):
        """Initialize client. Loads token from ~/.snailprint/token.json if not provided."""
        self.token = token
        if self.token is None and TOKEN_PATH.exists():
            try:
                data = json.loads(TOKEN_PATH.read_text())
                self.token = data.get("token") or data.get("access_token")
            except Exception:
                pass

    def _headers(self, auth=False):
        h = dict(BASE_HEADERS)
        if auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def search(self, query, limit=10, printer="N7"):
        """
        Search MakerWorld for 3D models.

        Args:
            query: search keywords
            limit: max results to return
            printer: devModelName filter (default N7 = P2S)

        Returns:
            list of {id, name, author, thumbnail, downloads, printable, url}
        """
        params = {
            "keyword": query,
            "limit": limit,
            "offset": 0,
            "orderBy": "score",
            "devModelName": printer,
        }

        resp = requests.get(SEARCH_URL, params=params, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("hits", [])[:limit]:
            results.append({
                "id": str(item.get("id", "")),
                "name": item.get("title", ""),
                "author": item.get("designCreator", {}).get("name", ""),
                "thumbnail": item.get("cover", ""),
                "downloads": item.get("downloadCount", 0),
                "printable": item.get("is_printable", False),
                "url": f"{BASE_URL}/en/models/{item.get('id', '')}",
            })

        return results

    def get_model(self, design_id):
        """
        Fetch full model detail including instances/profiles.

        Returns:
            dict with model info and parsed profiles list, each profile containing:
            {instance_id, title, printer, nozzle, filaments, plates, has_zip_stl}
        """
        url = DETAIL_URL.format(design_id=design_id)
        resp = requests.get(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()

        profiles = []
        for inst in data.get("instances", []):
            ext = inst.get("extention", {})
            model_info = ext.get("modelInfo", {})
            compat = model_info.get("compatibility", {})

            plates = []
            for plate in model_info.get("plates", []):
                plates.append({
                    "prediction_s": plate.get("prediction"),
                    "weight_g": plate.get("weight"),
                    "filaments": plate.get("filaments", []),
                })

            profiles.append({
                "instance_id": inst.get("id"),
                "title": inst.get("title", ""),
                "printer": compat.get("devModelName", ""),
                "printer_name": PRINTER_NAMES.get(compat.get("devModelName", ""), compat.get("devModelName", "")),
                "nozzle": compat.get("nozzleDiameter", 0.4),
                "other_compatibility": model_info.get("otherCompatibility", []),
                "plates": plates,
                "has_zip_stl": inst.get("hasZipStl", False),
            })

        return {
            "id": str(design_id),
            "title": data.get("title", ""),
            "author": data.get("designCreator", {}).get("name", ""),
            "default_instance_id": data.get("defaultInstanceId"),
            "profiles": profiles,
            "model_files": data.get("designExtension", {}).get("model_files", []),
            "raw": data,
        }

    def find_compatible_instance(self, design_id, printer="N7", nozzle=0.4, filament_type=None):
        """
        Find a pre-sliced instance matching the given printer, nozzle, and optionally filament.

        Returns:
            instance_id (int) or None
        """
        model = self.get_model(design_id)

        for profile in model["profiles"]:
            if profile["printer"] != printer:
                continue
            if abs(profile["nozzle"] - nozzle) > 0.01:
                continue
            if filament_type:
                # Check if any plate uses the requested filament type
                matched_filament = False
                for plate in profile["plates"]:
                    for fil in plate.get("filaments", []):
                        if filament_type.upper() in str(fil.get("type", "")).upper():
                            matched_filament = True
                            break
                    if matched_filament:
                        break
                if not matched_filament:
                    continue
            return profile["instance_id"]

        return None

    def _get_download_url(self, instance_id):
        """Resolve a signed download URL for a pre-sliced 3MF."""
        url = DOWNLOAD_URL.format(instance_id=instance_id)
        resp = requests.get(url, headers=self._headers(auth=True), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("url")

    def download(self, design_id, output_dir=None, printer="N7", nozzle=0.4, filament_type=None):
        """
        Smart download:
          1. Get model detail
          2. Find compatible pre-sliced 3MF if available
          3. Download it; fall back to raw model files if no profile matches

        Returns:
            (file_path: str, is_presliced: bool)
        """
        if output_dir is None:
            output_dir = DOWNLOAD_DIR / str(design_id)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        model = self.get_model(design_id)

        # Try to find a compatible pre-sliced instance
        instance_id = self.find_compatible_instance(
            design_id, printer=printer, nozzle=nozzle, filament_type=filament_type
        )

        # Fall back to default instance if no specific match
        if instance_id is None and model.get("default_instance_id"):
            instance_id = model["default_instance_id"]
            print(f"[MAKERWORLD] No exact profile match; using default instance {instance_id}")

        if instance_id is not None:
            try:
                signed_url = self._get_download_url(instance_id)
                if signed_url:
                    filename = f"{design_id}_instance{instance_id}.3mf"
                    file_path = output_dir / filename
                    _download_file(signed_url, file_path, headers=BASE_HEADERS)
                    return str(file_path), True
            except Exception as e:
                print(f"[MAKERWORLD] Pre-sliced download failed ({e}); falling back to raw files")

        # Fall back to raw model files
        model_files = model.get("model_files", [])
        for mf in model_files:
            url = mf.get("url", mf.get("downloadUrl", ""))
            name = mf.get("name", mf.get("fileName", ""))
            if url and name and (name.endswith(".stl") or name.endswith(".3mf")):
                file_path = output_dir / name
                _download_file(url, file_path, headers=BASE_HEADERS)
                return str(file_path), False

        raise RuntimeError(f"No downloadable file found for design {design_id}")


# ── module-level convenience wrappers ────────────────────────────────────────

def search(query, limit=10, printer="N7"):
    """Module-level search wrapper."""
    return MakerWorld().search(query, limit=limit, printer=printer)


def download(design_id, output_dir=None, printer="N7", nozzle=0.4, filament_type=None):
    """Module-level download wrapper."""
    return MakerWorld().download(
        design_id,
        output_dir=output_dir,
        printer=printer,
        nozzle=nozzle,
        filament_type=filament_type,
    )


def _download_file(url, path, headers=None):
    """Download a file from URL to local path."""
    print(f"[MAKERWORLD] Downloading {Path(path).name}...")
    resp = requests.get(url, stream=True, timeout=60, headers=headers or {})
    resp.raise_for_status()
    with open(path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    print(f"[MAKERWORLD] Saved: {path} ({Path(path).stat().st_size / 1024:.0f} KB)")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 makerworld.py search <query> [--printer N7] [--limit 10]")
        print("       python3 makerworld.py download <design_id> [--printer N7] [--nozzle 0.4]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "search":
        # Parse simple flags
        args = sys.argv[2:]
        printer = "N7"
        limit = 10
        query_parts = []
        i = 0
        while i < len(args):
            if args[i] == "--printer" and i + 1 < len(args):
                printer = args[i + 1]; i += 2
            elif args[i] == "--limit" and i + 1 < len(args):
                limit = int(args[i + 1]); i += 2
            else:
                query_parts.append(args[i]); i += 1
        query = " ".join(query_parts)
        if not query:
            print("Error: search query required")
            sys.exit(1)

        printer_label = PRINTER_NAMES.get(printer, printer)
        print(f"Searching MakerWorld for '{query}' (printer={printer_label})...\n")
        results = search(query, limit=limit, printer=printer)
        if not results:
            print("No results found.")
        for r in results:
            printable = "[printable]" if r["printable"] else ""
            print(f"  [{r['id']}] {r['name']} — by {r['author'] or 'unknown'} "
                  f"({r['downloads']} downloads) {printable}")
            print(f"           {r['url']}")

    elif cmd == "download":
        if len(sys.argv) < 3:
            print("Error: design_id required")
            sys.exit(1)
        args = sys.argv[2:]
        design_id = args[0]
        printer = "N7"
        nozzle = 0.4
        filament_type = None
        i = 1
        while i < len(args):
            if args[i] == "--printer" and i + 1 < len(args):
                printer = args[i + 1]; i += 2
            elif args[i] == "--nozzle" and i + 1 < len(args):
                nozzle = float(args[i + 1]); i += 2
            elif args[i] == "--filament" and i + 1 < len(args):
                filament_type = args[i + 1]; i += 2
            else:
                i += 1

        printer_label = PRINTER_NAMES.get(printer, printer)
        print(f"Downloading design {design_id} (printer={printer_label}, nozzle={nozzle}mm)...")
        file_path, is_presliced = download(
            design_id, printer=printer, nozzle=nozzle, filament_type=filament_type
        )
        kind = "pre-sliced 3MF" if is_presliced else "raw model file"
        print(f"Downloaded ({kind}): {file_path}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
