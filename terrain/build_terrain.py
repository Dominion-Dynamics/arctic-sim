#!/usr/bin/env python3
"""Build a Gazebo heightmap from ArcticDEM for an arbitrary arctic footprint.

Runs on the host with only the standard library; all raster work is delegated to
a GDAL container, so there is nothing to install locally beyond Docker.

The default target is Pond Inlet (Mittimatalik), Nunavut.

    python3 pipeline/build_terrain.py --extent 500   --grid 65    # pilot
    python3 pipeline/build_terrain.py --extent 20000 --grid 2049  # full map
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))
from course import pick_water, plan_course  # noqa: E402

GDAL_IMAGE = "ghcr.io/osgeo/gdal:ubuntu-small-latest"
STAC_COLLECTION = "arcticdem-mosaics-v4.1-10m"
STAC_SEARCH = "https://stac.pgc.umn.edu/api/v1/collections/{c}/items"

# ArcticDEM mosaics are published in NSIDC Sea Ice Polar Stereographic North.
# Working natively in this CRS avoids any reprojection distortion at 72 deg N.
DEM_CRS = "EPSG:3413"

# Pond Inlet (Mittimatalik), Nunavut.
DEFAULT_LAT = 72.6989
DEFAULT_LON = -77.9647


def die(msg: str) -> "None":
    sys.exit(f"error: {msg}")


# This script runs INSIDE the GDAL container, with the output directory bind
# mounted at /out. Everything below therefore calls the gdal CLIs directly.
GDAL_ENV = dict(os.environ,
                GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                GDAL_HTTP_MAX_RETRY="5",
                GDAL_HTTP_RETRY_DELAY="2")


def docker(args: list[str], mounts=None) -> str:
    """Run a GDAL command and return stdout. `mounts` is ignored (kept so the
    call sites read the same as the host-side original)."""
    proc = subprocess.run(args, capture_output=True, text=True, env=GDAL_ENV)
    if proc.returncode != 0:
        die(f"{' '.join(args[:2])} failed:\n{proc.stderr.strip()}")
    return proc.stdout


VALID_GRIDS = [65, 129, 257, 513, 1025, 2049]


def choose_grid(extent_m: float) -> int:
    """Smallest valid grid sampling at least as finely as ArcticDEM's 10 m.

    Must match the rule in the `arctic` CLI: otherwise a site added through
    sites.conf silently gets a different resolution than the same site added
    through the CLI.
    """
    for g in VALID_GRIDS:
        if extent_m / (g - 1) <= 10.0:
            return g
    return VALID_GRIDS[-1]


def is_valid_grid(n: int) -> bool:
    """Gazebo heightmaps must be square with side 2^n + 1."""
    m = n - 1
    return n >= 3 and m > 0 and (m & (m - 1)) == 0


def to_dem_crs(lat: float, lon: float) -> tuple[float, float]:
    out = subprocess.run(
        ["gdaltransform", "-s_srs", "EPSG:4326", "-t_srs", DEM_CRS],
        input=f"{lon} {lat}\n", capture_output=True, text=True, env=GDAL_ENV,
    )
    if out.returncode != 0:
        die(f"coordinate transform failed:\n{out.stderr.strip()}")
    x, y, *_ = out.stdout.split()
    return float(x), float(y)


def from_dem_crs(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """EPSG:3413 (x, y) -> (lat, lon) for each point."""
    stdin = "".join(f"{x} {y}\n" for x, y in points)
    out = subprocess.run(
        ["gdaltransform", "-s_srs", DEM_CRS, "-t_srs", "EPSG:4326"],
        input=stdin, capture_output=True, text=True, env=GDAL_ENV,
    )
    if out.returncode != 0:
        die(f"inverse transform failed:\n{out.stderr.strip()}")
    res = []
    for line in out.stdout.strip().splitlines():
        lon, lat, *_ = line.split()
        res.append((float(lat), float(lon)))
    return res


def grid_convergence(cx: float, cy: float) -> float:
    """True bearing of the world +Y axis, in degrees (0 would be true north).

    ArcticDEM is polar stereographic about 45 deg W, so its grid north only
    coincides with true north on that meridian. At Pond Inlet (78 deg W) the two
    differ by about 33 deg, which matters for any heading-aware sensor.
    """
    (la1, lo1), (la2, lo2) = from_dem_crs([(cx, cy), (cx, cy + 1000.0)])
    p1, p2 = math.radians(la1), math.radians(la2)
    dl = math.radians(lo2 - lo1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    brg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return brg - 360.0 if brg > 180.0 else brg


def point_scale_factor(lat: float) -> float:
    """EPSG:3413 point scale factor: grid metres per ground metre.

    Polar stereographic is conformal but not equidistant - it is only true to
    scale on its standard parallel (70 deg N). At Pond Inlet k ~ 0.9923, so a
    20 km window measured in grid units spans about 20.16 km of real ground.
    """
    e = 0.081819190842621
    m = lambda p: math.cos(p) / math.sqrt(1 - e * e * math.sin(p) ** 2)
    t = lambda p: (math.tan(math.pi / 4 - p / 2) /
                   ((1 - e * math.sin(p)) / (1 + e * math.sin(p))) ** (e / 2))
    ts, la = math.radians(70.0), math.radians(lat)
    return (m(ts) * t(la)) / (m(la) * t(ts))


def geographic_bbox(lat: float, lon: float, extent_m: float) -> tuple[float, ...]:
    """Lat/lon bbox covering the footprint, with margin, for the STAC query."""
    half = extent_m / 2.0 * 1.25  # margin so tiles clipped by the edge still match
    dlat = half / 111_320.0
    dlon = half / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def find_dem_tiles(bbox: tuple[float, ...]) -> list[str]:
    """Ask PGC's STAC API which ArcticDEM mosaic tiles cover the footprint."""
    query = urllib.parse.urlencode({
        "bbox": ",".join(f"{v:.6f}" for v in bbox),
        "limit": 100,
    })
    url = f"{STAC_SEARCH.format(c=STAC_COLLECTION)}?{query}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        doc = json.load(resp)
    hrefs = []
    for feat in doc.get("features", []):
        href = feat.get("assets", {}).get("dem", {}).get("href")
        if href:
            hrefs.append(href)
    if not hrefs:
        die(f"no ArcticDEM tiles cover bbox {bbox}")
    return sorted(hrefs)


def sample_aligned_extent(cx: float, cy: float, span: float, grid: int) -> tuple[float, ...]:
    """Extent whose cell *centres* span exactly `span` metres.

    GDAL treats raster cells as areas; Gazebo treats heightmap pixels as point
    samples on a grid. A grid of N samples spans N-1 intervals, so requesting a
    plain `span`-wide window and N columns yields a spacing of span/N and a
    terrain stretched by N/(N-1). Widening the request by that same factor puts
    the outermost cell centres exactly `span` apart.
    """
    half = span / 2.0 * grid / (grid - 1)
    return (cx - half, cy - half, cx + half, cy + half)


def pick_spawn(dem: str, grid: int, spacing: float, extent: float,
               min_elev: float = 5.0, max_slope_deg: float = 6.0) -> dict:
    """Nearest point to the centre that is dry land and reasonably flat.

    The geometric centre of a coastal site is often water — Fort Ross sits on
    Bellot Strait, so its centre is open sea. Spawning a vehicle there drops it
    in the ocean, which is a poor first impression of an "out of the box" sim.
    """
    from osgeo import gdal
    import numpy as np
    gdal.UseExceptions()
    ds = gdal.Open(dem)
    z = ds.GetRasterBand(1).ReadAsArray().astype("float64")
    gy, gx = np.gradient(z, spacing)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))

    half = extent / 2.0
    ok = (z >= min_elev) & (slope <= max_slope_deg)
    if not ok.any():  # relax rather than fail outright
        ok = z >= min_elev
    if not ok.any():
        mid = grid // 2
        return {"x": 0.0, "y": 0.0, "z": float(z[mid, mid]), "on_land": False}

    rows, cols = np.nonzero(ok)
    xs = -half + cols * spacing
    ys = half - rows * spacing
    i = int(np.argmin(np.hypot(xs, ys)))
    return {"x": float(xs[i]), "y": float(ys[i]),
            "z": float(z[rows[i], cols[i]]),
            "slope_deg": float(slope[rows[i], cols[i]]),
            "offset_m": float(np.hypot(xs[i], ys[i])), "on_land": True}


def band_stats(path_in_container: str, mounts: dict[Path, str]) -> dict:
    doc = json.loads(docker(
        ["gdalinfo", "-stats", "-json", path_in_container], mounts))
    band = doc["bands"][0]
    return {
        "min": float(band["minimum"]),
        "max": float(band["maximum"]),
        "mean": float(band["mean"]),
        "stddev": float(band["stdDev"]),
        "nodata": band.get("noDataValue"),
    }


def build(args: argparse.Namespace) -> dict:
    if args.grid is None:
        args.grid = choose_grid(args.extent)
        print(f"       grid not specified -> {args.grid}^2 "
              f"({args.extent / (args.grid - 1):.2f} m/sample)")
    if not is_valid_grid(args.grid):
        die(f"--grid must be 2^n+1 (65, 129, 257, 513, 1025, 2049); got {args.grid}")
    if shutil.which("gdalwarp") is None:
        die("gdalwarp not found — run this inside the GDAL container")

    out_dir = Path(os.environ.get("ARCTIC_OUT", "/out"))
    out_dir.mkdir(parents=True, exist_ok=True)
    mounts = None

    # --true-scale shrinks the grid window so the footprint measures `extent`
    # metres on the *ground* rather than in distorted grid units.
    k = point_scale_factor(args.lat)
    grid_span = args.extent * k if args.true_scale else args.extent
    ground_span = grid_span / k
    spacing = args.extent / (args.grid - 1)
    print(f"[1/6] {args.name}: {args.extent:g} m across {args.grid}x{args.grid} "
          f"samples ({spacing:.3f} m/sample)")
    print(f"       EPSG:3413 scale factor k={k:.6f}; footprint is "
          f"{ground_span:.1f} m on the ground"
          + ("  [corrected]" if args.true_scale else "  [uncorrected]"))

    cx, cy = to_dem_crs(args.lat, args.lon)
    print(f"[2/6] centre {args.lat}, {args.lon} -> {DEM_CRS} ({cx:.2f}, {cy:.2f})")

    tiles = find_dem_tiles(geographic_bbox(args.lat, args.lon, args.extent))
    print(f"[3/6] {len(tiles)} ArcticDEM tile(s):")
    for t in tiles:
        print(f"       {t.rsplit('/', 1)[-1]}")

    xmin, ymin, xmax, ymax = sample_aligned_extent(cx, cy, grid_span, args.grid)

    raw = str(out_dir / "dem_raw.tif")
    print(f"[4/6] windowed read via /vsicurl (COG range requests)")
    docker([
        "gdalwarp", "-q",
        "-te", f"{xmin:.6f}", f"{ymin:.6f}", f"{xmax:.6f}", f"{ymax:.6f}",
        "-ts", str(args.grid), str(args.grid),
        "-r", args.resample, "-ot", "Float32",
        "-of", "GTiff", "-co", "COMPRESS=DEFLATE", "-overwrite",
    ] + [f"/vsicurl/{t}" for t in tiles] + [raw], mounts)

    stats = band_stats(raw, mounts)
    dem = raw

    # ArcticDEM leaves voids (-9999) over water and steep shadow. Any void that
    # survives into the heightmap becomes a spike, so fill before scaling.
    if stats["min"] <= -9000:
        print("       nodata voids present -> interpolating")
        docker(["gdal_fillnodata", "-q", "-md", "50", raw, str(out_dir / "dem_filled.tif")], mounts)
        dem = str(out_dir / "dem_filled.tif")
        stats = band_stats(dem, mounts)

    if args.sea_level_floor is not None:
        print(f"       clamping elevations below {args.sea_level_floor} m")
        # gdal_calc has --quiet but no -q; passing -q swallows the next token.
        docker([
            "gdal_calc", "--quiet", "-A", dem,
            f"--calc=maximum(A,{args.sea_level_floor})",
            f"--outfile={out_dir}/dem_clamped.tif", "--type=Float32", "--overwrite",
        ], mounts)
        dem = str(out_dir / "dem_clamped.tif")
        stats = band_stats(dem, mounts)

    zmin, zmax = stats["min"], stats["max"]
    zrange = zmax - zmin
    if zrange <= 0:
        die("terrain is perfectly flat; cannot build a heightmap")

    # Gazebo Classic's image loader cannot read 16-bit greyscale PNG — it
    # reports zero height and every pixel read fails "Coordinates out of range".
    # 8-bit is the working format there; 16-bit is kept for engines that do
    # support it, at ~1/256 of the relief per step instead of 1/65536.
    bits = int(args.heightmap_bits)
    dtype, top = ("Byte", 255) if bits == 8 else ("UInt16", 65535)
    step = zrange / top
    print(f"[5/6] elevation {zmin:.2f} .. {zmax:.2f} m (relief {zrange:.2f} m)")
    print(f"       heightmap {bits}-bit -> {step:.3f} m vertical step")
    if bits == 8 and step > 0.5:
        print(f"       NOTE: {step:.2f} m steps will terrace visibly at low "
              f"altitude; use --heightmap-bits 16 on engines that support it")
    docker([
        "gdal_translate", "-q", "-of", "PNG", "-ot", dtype,
        "-scale", f"{zmin}", f"{zmax}", "0", str(top),
        dem, str(out_dir / "heightmap.png"),
    ], mounts)

    conv = grid_convergence((xmin + xmax) / 2, (ymin + ymax) / 2)
    print(f"       world +Y (grid north) bears {conv:+.2f} deg from true north")

    # Elevation at the exact centre: the vehicle spawns here and SITL's home
    # altitude must match, or the EKF origin and the terrain disagree.
    mid = args.grid // 2
    centre_z = float(docker(
        ["gdallocationinfo", "-valonly", dem, str(mid), str(mid)], mounts).strip())

    spawn = pick_spawn(dem, args.grid, spacing, args.extent)
    if spawn["on_land"]:
        print(f"       spawn {spawn['offset_m']:.0f} m from centre at "
              f"{spawn['z']:.1f} m ({spawn['slope_deg']:.1f}° slope)")
    else:
        print("       WARNING: no dry flat ground found; spawning at centre")

    water = pick_water(dem, args.grid, spacing, args.extent)
    if water.get("present"):
        print(f"       open water at ({water['x']:.0f}, {water['y']:.0f}), "
              f"{water['clearance_m']:.0f} m clear "
              f"({water['water_fraction']*100:.0f}% of site is water)")

    course = {"present": False}
    if water.get("present"):
        course = plan_course(dem, args.grid, spacing, args.extent,
                             args.course_seed, args.course_length)
        if course.get("present"):
            print(f"       course: {len(course['waypoints'])} waypoints, "
                  f"{course['length_m']:.0f} m (seed {args.course_seed})")
            extra = (f", {course['attempts']} attempts"
                     if course.get("attempts", 1) > 1 else "")
            print(f"       clearance: {course['path_min_clearance_m']:.0f} m minimum "
                  f"along the travelled line, verified end to end{extra}")
        else:
            print("       WARNING: no land-free course found; vessel will be moored")

    meta = {
        "name": args.name,
        "centre_elevation_m": centre_z,
        "spawn": spawn,
        "water": water,
        "course": course,
        "location": {"lat": args.lat, "lon": args.lon},
        # World axes are DEM grid axes, NOT true ENU. See grid_convergence().
        "convergence_deg": conv,
        "source": {
            "dataset": STAC_COLLECTION,
            "crs": DEM_CRS,
            "tiles": [t.rsplit("/", 1)[-1] for t in tiles],
            "resample": args.resample,
        },
        "grid": args.grid,
        "extent_m": args.extent,
        "spacing_m": spacing,
        "scale_factor": k,
        "true_scale": bool(args.true_scale),
        "ground_span_m": ground_span,
        "bounds_3413": {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax},
        "elevation_m": {
            "min": zmin, "max": zmax, "range": zrange,
            "mean": stats["mean"], "stddev": stats["stddev"],
        },
        # Gazebo maps pixel 0 -> pos.z and pixel 65535 -> pos.z + size.z.
        "heightmap_bits": bits,
        "gazebo": {
            "size": [args.extent, args.extent, zrange],
            "pos": [0.0, 0.0, zmin],
            "quantisation_m": step,
        },
    }
    (out_dir / "terrain.json").write_text(json.dumps(meta, indent=2) + "\n")

    # GDAL emits .aux.xml sidecars next to each raster; Gazebo needs none.
    for junk in out_dir.glob("*.aux.xml"):
        junk.unlink()

    print(f"[6/6] wrote {out_dir}/heightmap.png + terrain.json"
          f"  (centre elevation {centre_z:.2f} m)")
    return meta


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", default="pilot_500m", help="output subdirectory name")
    p.add_argument("--lat", type=float, default=DEFAULT_LAT)
    p.add_argument("--lon", type=float, default=DEFAULT_LON)
    p.add_argument("--extent", type=float, default=500.0,
                   help="footprint width in metres (square)")
    p.add_argument("--grid", type=int, default=None,
                   help="heightmap samples per side (2^n+1); "
                        "default: matched to the source resolution")
    p.add_argument("--resample", default="cubic",
                   choices=["near", "bilinear", "cubic", "cubicspline", "lanczos"])
    p.add_argument("--heightmap-bits", type=int, default=8, choices=[8, 16],
                   help="8 for Gazebo Classic (its loader rejects 16-bit)")
    p.add_argument("--true-scale", action="store_true",
                   help="correct for EPSG:3413 scale distortion so --extent is "
                        "ground metres (~0.77%% at this latitude)")
    p.add_argument("--course-seed", type=int,
                   default=int(os.environ.get("COURSE_SEED", "1")),
                   help="seed for the vessel course; same seed = same course")
    p.add_argument("--course-length", type=float,
                   default=float(os.environ.get("COURSE_LENGTH", "6000")),
                   help="target course length in metres")
    p.add_argument("--sea-level-floor", type=float, default=None,
                   help="clamp elevations below this value, e.g. 0 to flatten sea")
    build(p.parse_args())


if __name__ == "__main__":
    main()
