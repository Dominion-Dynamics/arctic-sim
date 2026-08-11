#!/usr/bin/env python3
"""Measure the TRUE resolution of a tile provider's imagery over a site.

    MAPTILER_KEY=xxx python3 terrain/check_tiles.py --name fort_ross
    MAPBOX_TOKEN=pk.xxx python3 terrain/check_tiles.py --name fort_ross --provider mapbox

A tile server will happily serve z18 everywhere. That does not mean z18 carries
z18 detail — beyond the source resolution it is just upscaling. This fetches a
tile at each zoom and measures high-frequency energy: while the source has real
detail, sharpness holds roughly steady as you zoom; once you pass it, each level
is an interpolation of the last and sharpness falls off a cliff.

Requires only an API key from the free tier — evaluation is what it is for.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GDAL = "ghcr.io/osgeo/gdal:ubuntu-small-latest"
# MapTiler: 512px is the default and has NO size segment; 256px inserts one.
# Mapbox: the v4 raster API uses @2x for 512px tiles.
PROVIDERS = {
    "maptiler": {
        "env": "MAPTILER_KEY",
        "style": "satellite-v4",
        "tile": {512: "https://api.maptiler.com/maps/{style}/{z}/{x}/{y}.jpg?key={key}",
                 256: "https://api.maptiler.com/maps/{style}/256/{z}/{x}/{y}.jpg?key={key}"},
        "tilejson": "https://api.maptiler.com/maps/{style}/tiles.json?key={key}",
    },
    "mapbox": {
        "env": "MAPBOX_TOKEN",
        "style": "mapbox.satellite",
        "tile": {512: "https://api.mapbox.com/v4/{style}/{z}/{x}/{y}@2x.jpg90?access_token={key}",
                 256: "https://api.mapbox.com/v4/{style}/{z}/{x}/{y}.jpg90?access_token={key}"},
        "tilejson": "https://api.mapbox.com/v4/{style}.json?access_token={key}",
    },
}


def deg2tile(lat: float, lon: float, z: int) -> tuple:
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    r = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * n
    return int(x), int(y)


def ground_res(lat: float, z: int, tile_px: int) -> float:
    """Metres per pixel at this latitude, accounting for tile size."""
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** z) * (256.0 / tile_px)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--name", default="fort_ross")
    p.add_argument("--provider", default="maptiler", choices=sorted(PROVIDERS))
    p.add_argument("--style", default=None)
    p.add_argument("--tile-size", type=int, default=512, choices=[256, 512])
    p.add_argument("--zooms", default="11,12,13,14,15,16,17")
    p.add_argument("--keep", action="store_true", help="keep the fetched tiles")
    a = p.parse_args()

    prov = PROVIDERS[a.provider]
    key = os.environ.get(prov["env"], "").strip()
    if not key:
        sys.exit(f"error: set {prov['env']} (free tier is fine)")
    if a.style is None:
        a.style = prov["style"]

    meta = json.loads((REPO / "out" / a.name / "terrain.json").read_text())
    lat, lon = meta["location"]["lat"], meta["location"]["lon"]
    out = REPO / "out" / a.name / f"tiles_{a.provider}"
    out.mkdir(parents=True, exist_ok=True)

    print(f"  {a.name} at {lat:.5f}, {lon:.5f}   {a.provider}/{a.style}  {a.tile_size}px")
    # TileJSON states the served zoom range outright — worth reading first.
    try:
        req = urllib.request.Request(prov["tilejson"].format(style=a.style, key=key),
                                     headers={"User-Agent": "arctic-sim/1.0"})
        with urllib.request.urlopen(req, timeout=45) as r:
            tj = json.load(r)
        print(f"  TileJSON: zoom {tj.get('minzoom')}..{tj.get('maxzoom')}  "
              f"{tj.get('name','')}")
        if tj.get("attribution"):
            import re as _re
            print(f"  attribution: {_re.sub('<[^>]+>', '', tj['attribution'])[:80]}")
    except Exception as e:
        print(f"  TileJSON unavailable: {e}")
    print()
    got = []
    for z in [int(v) for v in a.zooms.split(",")]:
        x, y = deg2tile(lat, lon, z)
        tmpl = prov["tile"][a.tile_size]
        url = tmpl.format(style=a.style, z=z, x=x, y=y, key=key)
        dest = out / f"z{z:02d}.jpg"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "arctic-sim/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            dest.write_bytes(data)
            got.append((z, dest, ground_res(lat, z, a.tile_size), len(data)))
        except Exception as e:
            print(f"    z{z}: FAILED {e}")

    if not got:
        sys.exit("no tiles fetched — check the key and style name")

    # Sharpness per tile. Comparing like for like: every tile is the same pixel
    # size, so a genuine detail increase shows as sustained gradient energy.
    script = """
from osgeo import gdal
import numpy as np, sys, json
gdal.UseExceptions()
res = []
for line in sys.stdin.read().strip().splitlines():
    z, path, gsd = line.split("|")
    ds = gdal.Open(path)
    a = np.stack([ds.GetRasterBand(i+1).ReadAsArray() for i in range(min(3, ds.RasterCount))], -1)
    g = a.mean(-1).astype("float64")
    gy, gx = np.gradient(g)
    res.append((int(z), float(gsd), float(np.hypot(gx, gy).mean()), float(g.std())))
print(json.dumps(res))
"""
    payload = "\n".join(f"{z}|/t/{d.name}|{g:.3f}" for z, d, g, _ in got)
    r = subprocess.run(
        ["docker", "run", "--rm", "-i", "-v", f"{out}:/t", GDAL, "python3", "-c", script],
        input=payload, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"analysis failed:\n{r.stderr.strip()}")
    stats = json.loads(r.stdout.strip().splitlines()[-1])

    print(f"    {'zoom':<6}{'m/px':>8}{'sharpness':>12}{'contrast':>10}   verdict")
    prev = None
    native_z = None
    for z, gsd, sharp, sd in stats:
        note = ""
        if prev is not None:
            ratio = sharp / prev if prev > 0 else 0
            # Real added detail keeps sharpness up; pure upscaling roughly halves it.
            if ratio < 0.62 and native_z is None:
                native_z = z - 1
                note = "  <- upscaling starts here"
        print(f"    z{z:<5}{gsd:8.2f}{sharp:12.3f}{sd:10.1f}{note}")
        prev = sharp

    print()
    if native_z:
        best = ground_res(lat, native_z, a.tile_size)
        print(f"  native detail holds to about z{native_z} ~ {best:.2f} m/px")
        print(f"  that is {10.0/best:.1f}x better than Sentinel-2's 10 m")
        px = meta["extent_m"] / best
        print(f"  -> {px:.0f} px across the {meta['extent_m']:.0f} m site "
              f"(Sentinel-2 gives {meta['extent_m']/10:.0f})")
    else:
        print("  sharpness held across every zoom tested — try higher --zooms")

    print(f"\n  tiles in {out}  (open them to eyeball it too)")
    if not a.keep:
        print("  pass --keep to retain them for comparison")


if __name__ == "__main__":
    main()
