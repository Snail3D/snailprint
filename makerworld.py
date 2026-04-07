#!/usr/bin/env python3
"""
MakerWorld search and download client.
Scrapes makerworld.com for 3D model search and download.
"""
import json
import os
import re
import sys
from pathlib import Path

import requests

BASE_URL = "https://makerworld.com"
SEARCH_URL = "https://makerworld.com/api/v1/design"
DOWNLOAD_DIR = Path(os.path.expanduser("~/.snailprint/downloads"))


def search(query, limit=10):
    """
    Search MakerWorld for 3D models.

    Returns list of dicts with: id, name, author, thumbnail, rating, downloads
    """
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # MakerWorld uses an internal API
    params = {
        "keyword": query,
        "limit": limit,
        "offset": 0,
    }
    headers = {
        "User-Agent": "SnailPrint/1.0",
        "Accept": "application/json",
    }

    try:
        resp = requests.get(SEARCH_URL, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        # Fallback: scrape the HTML search page
        return _scrape_search(query, limit)

    results = []
    for item in data.get("hits", data.get("designs", data.get("items", [])))[:limit]:
        results.append({
            "id": str(item.get("id", item.get("designId", ""))),
            "name": item.get("title", item.get("name", "")),
            "author": item.get("designCreator", {}).get("name", ""),
            "thumbnail": item.get("cover", item.get("thumbnail", "")),
            "rating": item.get("rating", 0),
            "downloads": item.get("downloadCount", 0),
            "url": f"{BASE_URL}/en/models/{item.get('id', '')}",
        })

    return results


def _scrape_search(query, limit=10):
    """Fallback: scrape the HTML search results page."""
    url = f"{BASE_URL}/en/search/models?keyword={requests.utils.quote(query)}"
    headers = {"User-Agent": "SnailPrint/1.0"}

    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    html = resp.text

    # Extract model cards from HTML using regex
    results = []
    pattern = r'href="/en/models/(\d+)[^"]*"[^>]*>.*?<[^>]*>([^<]+)'
    matches = re.findall(pattern, html, re.DOTALL)

    seen_ids = set()
    for model_id, name in matches[:limit * 2]:
        if model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        name = name.strip()
        if not name or len(name) < 3:
            continue
        results.append({
            "id": model_id,
            "name": name,
            "author": "",
            "thumbnail": "",
            "rating": 0,
            "downloads": 0,
            "url": f"{BASE_URL}/en/models/{model_id}",
        })
        if len(results) >= limit:
            break

    return results


def download(model_id, output_dir=None):
    """
    Download a model's STL/3MF file from MakerWorld.

    Returns path to downloaded file.
    """
    if output_dir is None:
        output_dir = DOWNLOAD_DIR / str(model_id)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Get model detail page to find download links
    detail_url = f"{BASE_URL}/api/v1/design/{model_id}"
    headers = {"User-Agent": "SnailPrint/1.0", "Accept": "application/json"}

    try:
        resp = requests.get(detail_url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        # Try scraping the model page
        return _scrape_download(model_id, output_dir)

    # Find downloadable files
    files = data.get("files", data.get("modelFiles", []))
    for f in files:
        url = f.get("url", f.get("downloadUrl", ""))
        name = f.get("name", f.get("fileName", "model.stl"))
        if url and (name.endswith(".stl") or name.endswith(".3mf")):
            file_path = output_dir / name
            _download_file(url, file_path)
            return str(file_path)

    raise RuntimeError(f"No downloadable STL/3MF found for model {model_id}")


def _scrape_download(model_id, output_dir):
    """Fallback download via page scraping."""
    page_url = f"{BASE_URL}/en/models/{model_id}"
    headers = {"User-Agent": "SnailPrint/1.0"}
    resp = requests.get(page_url, headers=headers, timeout=15)

    # Look for download URLs in the page
    urls = re.findall(r'https://[^"]*\.(?:stl|3mf)[^"]*', resp.text)
    if urls:
        url = urls[0]
        name = url.split("/")[-1].split("?")[0]
        file_path = output_dir / name
        _download_file(url, file_path)
        return str(file_path)

    raise RuntimeError(f"Could not find download URL for model {model_id}")


def _download_file(url, path):
    """Download a file from URL to local path."""
    print(f"[MAKERWORLD] Downloading {Path(path).name}...")
    resp = requests.get(url, stream=True, timeout=60,
                        headers={"User-Agent": "SnailPrint/1.0"})
    resp.raise_for_status()
    with open(path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    print(f"[MAKERWORLD] Saved: {path} ({Path(path).stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 makerworld.py search <query>")
        print("       python3 makerworld.py download <model_id>")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "search":
        query = " ".join(sys.argv[2:])
        results = search(query)
        for r in results:
            print(f"  [{r['id']}] {r['name']} — {r['url']}")
    elif cmd == "download":
        model_id = sys.argv[2]
        path = download(model_id)
        print(f"Downloaded: {path}")
