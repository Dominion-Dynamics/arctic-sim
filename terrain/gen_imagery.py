#!/usr/bin/env python3
"""Drape real Sentinel-2 imagery over the terrain as the albedo texture.

Runs INSIDE the GDAL container. Replaces the procedural elevation-ramp albedo
with a true-colour satellite composite, reprojected onto the terrain footprint.

    IMAGERY_MONTHS=6,7,8 IMAGERY_CLOUD=5 python3 _gen_imagery.py

High-arctic caveat: usable optical imagery only exists in the polar day. Winter
scenes are unlit, so what you get is a summer, largely snow-free surface. If you
need a winter look, this is the wrong input - a snow shader over the DEM is.
"""

import json
import math
import os
import subprocess
import sys
import urllib.request

from osgeo import gdal, osr

gdal.UseExceptions()

OUT = os.environ.get("ARCTIC_OUT", "/out")
STAC = "https://earth-search.aws.element84.com/v1/search"
COLLECTION = "sentinel-2-l2a"

CLOUD = float(os.environ.get("IMAGERY_CLOUD", "5"))
MONTHS = {int(m) for m in os.environ.get("IMAGERY_MONTHS", "6,7,8").split(",")}
SIZE = int(os.environ.get("IMAGERY_SIZE", "2048"))
# Sentinel-2 tops out at 10 m, but ArcticDEM publishes a 2 m mosaic — five times
# finer. Shading the imagery with a 2 m hillshade puts real micro-relief into
# the texture instead of upsampled mush.
DETAIL = os.environ.get("IMAGERY_DETAIL", "1") == "1"
DETAIL_COLLECTION = "arcticdem-mosaics-v4.1-2m"
# ArcticDEM is on PGC's STAC, not Element84's Sentinel-2 one.
PGC_STAC = "https://stac.pgc.umn.edu/api/v1/collections/{c}/items"
DETAIL_STRENGTH = float(os.environ.get("IMAGERY_DETAIL_STRENGTH", "0.55"))
MAX_SCENES = int(os.environ.get("IMAGERY_SCENES", "4"))
START = os.environ.get("IMAGERY_START", "2019-01-01")
END = os.environ.get("IMAGERY_END", "2025-12-31")


def latlon_bbox(b: dict) -> list:
    src, dst = osr.SpatialReference(), osr.SpatialReference()
    src.ImportFromEPSG(3413); dst.ImportFromEPSG(4326)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)
    lons, lats = [], []
    for x in (b["xmin"], b["xmax"]):
        for y in (b["ymin"], b["ymax"]):
            lon, lat, *_ = tr.TransformPoint(x, y)
            lons.append(lon); lats.append(lat)
    pad = 0.02
    return [min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad]


def search(bbox: list) -> list:
    body = json.dumps({
        "collections": [COLLECTION],
        "bbox": bbox,
        "datetime": f"{START}T00:00:00Z/{END}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": CLOUD}},
        "limit": 100,
        "sortby": [{"field": "properties.eo:cloud_cover", "direction": "asc"}],
    }).encode()
    req = urllib.request.Request(STAC, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        doc = json.load(r)

    feats = []
    for f in doc.get("features", []):
        month = int(f["properties"]["datetime"][5:7])
        if month not in MONTHS:
            continue
        a = f.get("assets", {})
        # TCI is ESA's 8-bit display product: it hard-clips snow and ice to pure
        # white, which is most of an arctic scene. The 16-bit reflectance bands
        # keep that detail, so we tone-map them ourselves.
        if all(k in a for k in ("red", "green", "blue")):
            href = (a["red"]["href"], a["green"]["href"], a["blue"]["href"])
        elif "visual" in a:
            href = a["visual"]["href"]
        else:
            continue
        feats.append((f["properties"]["eo:cloud_cover"],
                      f["properties"]["datetime"][:10], f["id"], href))
    if not feats:
        sys.exit(f"error: no Sentinel-2 scene under {CLOUD}% cloud in months "
                 f"{sorted(MONTHS)} between {START} and {END}")
    return feats[:MAX_SCENES]


def detail_shading(bbox: list, b: dict, env: dict) -> "object":
    """Hillshade from the 2 m ArcticDEM mosaic, resampled to the texture grid.

    Returns a multiplier array centred on 1.0, or None if 2 m coverage is
    missing (it is not global — some areas only have 10 m).
    """
    import numpy as np
    import urllib.parse
    q = urllib.parse.urlencode({"bbox": ",".join(f"{v:.6f}" for v in bbox),
                                "limit": 50})
    url = f"{PGC_STAC.format(c=DETAIL_COLLECTION)}?{q}"
    try:
        with urllib.request.urlopen(url, timeout=90) as r:
            doc = json.load(r)
    except Exception as e:
        print(f"  detail: STAC query failed ({e}); skipping")
        return None

    hrefs = [f["assets"]["dem"]["href"] for f in doc.get("features", [])
             if "dem" in f.get("assets", {})]
    if not hrefs:
        print("  detail: no 2 m coverage here; imagery left unshaded")
        return None
    print(f"  detail: {len(hrefs)} ArcticDEM 2 m tile(s)")

    dem = "/tmp/detail_2m.tif"
    cmd = ["gdalwarp", "-q", "-overwrite", "-t_srs", "EPSG:3413",
           "-te", str(b["xmin"]), str(b["ymin"]), str(b["xmax"]), str(b["ymax"]),
           "-ts", str(SIZE), str(SIZE), "-r", "cubic", "-ot", "Float32",
           "-of", "GTiff"] + [f"/vsicurl/{h}" for h in hrefs] + [dem]
    if subprocess.run(cmd, capture_output=True, text=True, env=env).returncode != 0:
        print("  detail: warp failed; imagery left unshaded")
        return None

    hs = "/tmp/detail_hs.tif"
    # -z exaggerates relief so subtle arctic micro-topography actually reads.
    if subprocess.run(["gdaldem", "hillshade", "-q", "-z", "2.5", "-compute_edges",
                       dem, hs], capture_output=True, text=True,
                      env=env).returncode != 0:
        print("  detail: hillshade failed; imagery left unshaded")
        return None

    ds = gdal.Open(hs)
    a = ds.GetRasterBand(1).ReadAsArray().astype("float32")
    valid = a > 0
    if not valid.any():
        return None

    # Use only the HIGH-FREQUENCY part of the hillshade. The satellite image
    # already carries broad-scale shading from its own sun angle; multiplying by
    # the full hillshade shades it twice, which saturates bright slopes into
    # flat grey and lifts the darks. Subtracting a blurred copy leaves just the
    # 2 m micro-relief that 10 m imagery genuinely cannot resolve.
    small = max(8, SIZE // 16)
    mem = gdal.GetDriverByName("MEM").Create("", SIZE, SIZE, 1, gdal.GDT_Float32)
    mem.GetRasterBand(1).WriteArray(a)
    down = gdal.Translate("", mem, format="MEM", width=small, height=small,
                          resampleAlg="average")
    # Hold the dataset: chaining .GetRasterBand() off the Translate() result
    # frees it before the read and GDAL throws a bare TypeError.
    up = gdal.Translate("", down, format="MEM", width=SIZE, height=SIZE,
                        resampleAlg="cubicspline")
    low = up.GetRasterBand(1).ReadAsArray()

    high = (a - low) / 255.0
    mult = 1.0 + DETAIL_STRENGTH * high * 2.0
    mult[~valid] = 1.0
    return np.clip(mult, 0.72, 1.34)


def write_detail_tile(path: str, size: int = 512) -> None:
    """Seamless grey noise, mean ~0.5, used as a close-up detail multiplier.

    Luminance only: it modulates brightness without tinting the satellite
    colours. Wrapped at the edges so tiling leaves no visible seams.
    """
    import numpy as np
    rng = np.random.default_rng(11)

    def octave(freq):
        c = rng.random((freq + 1, freq + 1))
        c[-1, :] = c[0, :]
        c[:, -1] = c[:, 0]
        n = c.shape[0] - 1
        t = np.linspace(0, n, size)
        i0 = np.clip(np.floor(t).astype(int), 0, n - 1)
        f = t - i0
        f = f * f * (3 - 2 * f)
        rows = c[i0, :] * (1 - f)[:, None] + c[i0 + 1, :] * f[:, None]
        return rows[:, i0] * (1 - f)[None, :] + rows[:, i0 + 1] * f[None, :]

    acc, amp, norm = np.zeros((size, size)), 1.0, 0.0
    for o in range(6):
        acc += amp * octave(2 ** (o + 1))
        norm += amp
        amp *= 0.55
    acc /= norm
    acc = (acc - acc.mean()) * 0.9 + 0.5           # centre on 0.5
    acc += (rng.random((size, size)) - 0.5) * 0.05  # grain
    g = np.clip(acc * 255.0, 0, 255).astype("uint8")

    mem = gdal.GetDriverByName("MEM").Create("", size, size, 3, gdal.GDT_Byte)
    for i in range(3):
        mem.GetRasterBand(i + 1).WriteArray(g)
    gdal.GetDriverByName("PNG").CreateCopy(path, mem, strict=0)
    for junk in (path + ".aux.xml",):
        if os.path.exists(junk):
            os.remove(junk)
    print(f"  detail tile {size}x{size} (mean {g.mean()/255:.2f})")


MERC_ORIGIN = 20037508.342789244


TILE_PROVIDERS = {
    "maptiler": {
        "env": "MAPTILER_KEY",
        "style": "satellite-v4",
        "url": "https://api.maptiler.com/maps/{style}/{z}/{x}/{y}.jpg?key={key}",
        "credit": "(c) MapTiler (c) OpenStreetMap contributors",
    },
    "mapbox": {
        "env": "MAPBOX_TOKEN",
        "style": "mapbox.satellite",
        "url": "https://api.mapbox.com/v4/{style}/{z}/{x}/{y}@2x.jpg90?access_token={key}",
        "credit": "(c) Mapbox (c) OpenStreetMap (c) Maxar",
    },
}


def fetch_tiles(provider: str, b: dict, env: dict):
    """Mosaic raster tiles from MapTiler or Mapbox over the site footprint.

    Tiles are Web Mercator (EPSG:3857); each is georeferenced from its z/x/y and
    the set is warped into the site's EPSG:3413 bounds like any other source.

    Zoom matters: the server happily serves z22, but past the source resolution
    it is only upscaling. terrain/check_maptiler.py measures where real detail
    stops — at Bellot Strait that is z14 (~1.5 m/px).
    """
    import numpy as np
    prov = TILE_PROVIDERS[provider]
    key = os.environ.get(prov["env"], "").strip()
    if not key:
        sys.exit(
            f"\n  IMAGERY_SOURCE={provider} needs {prov['env']}.\n"
            f"  Get a free key at https://cloud.maptiler.com/account/keys/ "
            f"(or https://account.mapbox.com/access-tokens/)\n"
            f"  then either export it or add it to .env:\n"
            f"      {prov['env']}=your_key_here\n"
            f"  Or run without a key using the open Sentinel-2 source:\n"
            f"      IMAGERY_SOURCE=sentinel2 docker compose up\n")
    z = int(os.environ.get("TILE_ZOOM", os.environ.get("MAPTILER_ZOOM", "14")))
    style = os.environ.get("TILE_STYLE", prov["style"])

    # Site bounds -> WGS84 -> tile range.
    src, dst = osr.SpatialReference(), osr.SpatialReference()
    src.ImportFromEPSG(3413); dst.ImportFromEPSG(4326)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)
    lons, lats = [], []
    for x in (b["xmin"], b["xmax"]):
        for y in (b["ymin"], b["ymax"]):
            lo, la, *_ = tr.TransformPoint(x, y)
            lons.append(lo); lats.append(la)

    def deg2tile(lat, lon):
        n = 2 ** z
        tx = (lon + 180.0) / 360.0 * n
        ty = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
        return int(tx), int(ty)

    x0, y1 = deg2tile(min(lats), min(lons))
    x1, y0 = deg2tile(max(lats), max(lons))
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    n_tiles = (x1 - x0 + 1) * (y1 - y0 + 1)
    print(f"  {provider}: {style} z{z}, {n_tiles} tile(s)")
    if n_tiles > 900:
        sys.exit(f"error: {n_tiles} tiles is too many — lower TILE_ZOOM")

    span = 2 * MERC_ORIGIN / (2 ** z)
    tmp = "/tmp/mt"
    os.makedirs(tmp, exist_ok=True)
    paths, failed = [], 0
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            url = prov["url"].format(style=style, z=z, x=tx, y=ty, key=key)
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "arctic-sim/1.0"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = r.read()
            except Exception:
                failed += 1
                continue
            raw = f"{tmp}/{z}_{tx}_{ty}.jpg"
            open(raw, "wb").write(data)
            ds = gdal.Open(raw)
            geo = f"{tmp}/{z}_{tx}_{ty}.tif"
            out = gdal.GetDriverByName("GTiff").CreateCopy(geo, ds)
            out.SetGeoTransform([-MERC_ORIGIN + tx * span, span / ds.RasterXSize, 0,
                                 MERC_ORIGIN - ty * span, 0, -span / ds.RasterYSize])
            sr = osr.SpatialReference(); sr.ImportFromEPSG(3857)
            out.SetProjection(sr.ExportToWkt())
            out = None
            paths.append(geo)
    if not paths:
        sys.exit(f"error: no {provider} tiles fetched — check {prov['env']}")
    if failed:
        print(f"  {provider}: {failed} tile(s) failed, continuing")

    vrt = "/tmp/mt.vrt"
    gdal.BuildVRT(vrt, paths)
    merged = "/tmp/mt_3413.tif"
    r = subprocess.run(["gdalwarp", "-q", "-overwrite", "-t_srs", "EPSG:3413",
                        "-te", str(b["xmin"]), str(b["ymin"]),
                        str(b["xmax"]), str(b["ymax"]),
                        "-ts", str(SIZE), str(SIZE), "-r", "cubic",
                        "-of", "GTiff", vrt, merged],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        sys.exit(f"error: warp failed:\n{r.stderr.strip()}")
    ds = gdal.Open(merged)
    arr = np.stack([ds.GetRasterBand(i + 1).ReadAsArray()
                    for i in range(min(3, ds.RasterCount))], -1).astype("float32")
    # Tiles are already display-ready 8-bit; no tone curve needed.
    return np.clip(arr, 0, 255), float((arr.sum(-1) == 0).mean())


def use_local(path: str, b: dict, env: dict):
    """Reproject a purchased scene onto the terrain footprint.

    Commercial deliveries arrive in their own UTM zone and bit depth. gdalwarp
    handles the reprojection; anything already 8-bit is passed through, while
    higher bit depths get the same tone curve as the Sentinel-2 path.
    """
    import numpy as np
    out = "/tmp/local_imagery.tif"
    cmd = ["gdalwarp", "-q", "-overwrite", "-t_srs", "EPSG:3413",
           "-te", str(b["xmin"]), str(b["ymin"]), str(b["xmax"]), str(b["ymax"]),
           "-ts", str(SIZE), str(SIZE), "-r", "cubic", "-of", "GTiff", path, out]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        sys.exit(f"error: could not warp {path}:\n{r.stderr.strip()}")

    ds = gdal.Open(out)
    n = min(3, ds.RasterCount)
    arr = np.stack([ds.GetRasterBand(i + 1).ReadAsArray() for i in range(n)], -1)
    if n < 3:                       # panchromatic (WorldView-1) -> greyscale RGB
        arr = np.repeat(arr[..., :1], 3, axis=-1)
    arr = arr.astype("float32")
    if arr.max() > 255:             # 11/16-bit delivery
        hi = float(np.percentile(arr[arr > 0], 99.5)) or arr.max()
        arr = np.power(np.clip(arr / hi, 0, 1), 0.45) * 248.0
    gap = float((arr.sum(-1) == 0).mean())
    print(f"  local imagery: {path} ({ds.RasterXSize}x{ds.RasterYSize} src, "
          f"{n} band(s), {gap*100:.2f}% uncovered)")
    return np.clip(arr, 0, 255), gap


def main() -> None:
    meta = json.loads(open(f"{OUT}/terrain.json").read())
    b = meta["bounds_3413"]

    source = os.environ.get("IMAGERY_SOURCE", "sentinel2").strip().lower()
    local = os.environ.get("IMAGERY_FILE", "").strip()
    if source in TILE_PROVIDERS:
        # No key is not a fatal error. Falling back to the open Sentinel-2
        # source means `docker compose up` still produces a working world for
        # someone who has not signed up yet — just a blurrier one.
        need = TILE_PROVIDERS[source]["env"]
        if not os.environ.get(need, "").strip():
            print(f"\n  {need} not set — falling back to Sentinel-2 (10 m).")
            print(f"  For the sharp imagery (~1.5 m), get a free key and re-run:")
            print(f"      {need}=<key> FORCE_TERRAIN=1 docker compose run --rm terrain")
            print(f"      mapbox:   https://account.mapbox.com/access-tokens/")
            print(f"      maptiler: https://cloud.maptiler.com/account/keys/\n")
            source = "sentinel2"

    if source in TILE_PROVIDERS:
        z = os.environ.get("TILE_ZOOM", os.environ.get("MAPTILER_ZOOM", "14"))
        scenes = [(0.0, source, f"{source} z{z}", "")]
    if source in TILE_PROVIDERS:
        pass
    elif local:
        if not os.path.exists(local):
            sys.exit(f"error: IMAGERY_FILE={local} not found")
        scenes = [(0.0, "local", os.path.basename(local), local)]
    else:
        scenes = search(latlon_bbox(b))

    if not local and source == "sentinel2":
        print(f"  {len(scenes)} scene(s), best first:")
        for cc, date, sid, _ in scenes:
            print(f"    {sid}  {date}  cloud={cc:.1f}%")


    import numpy as np

    env = dict(os.environ, GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
               CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
               GDAL_HTTP_MAX_RETRY="5", GDAL_HTTP_RETRY_DELAY="2")
    tif = "/tmp/imagery.tif"

    def warp_to(srcs, out, dtype):
        cmd = ["gdalwarp", "-q", "-overwrite", "-t_srs", "EPSG:3413",
               "-te", str(b["xmin"]), str(b["ymin"]), str(b["xmax"]), str(b["ymax"]),
               "-ts", str(SIZE), str(SIZE), "-r", "cubic", "-ot", dtype,
               "-of", "GTiff"] + srcs + [out]
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            sys.exit(f"error: gdalwarp failed:\n{r.stderr.strip()}")
        ds = gdal.Open(out)
        return ds.GetRasterBand(1).ReadAsArray(), ds

    def warp(subset):
        # gdalwarp paints sources in order, so the best scene goes last, on top.
        use_bands = isinstance(subset[0][3], tuple)
        if use_bands:
            chans = []
            for band_idx in range(3):
                srcs = [f"/vsicurl/{h[band_idx]}" for _, _, _, h in reversed(subset)]
                a, _ = warp_to(srcs, f"/tmp/band{band_idx}.tif", "UInt16")
                chans.append(a.astype("float32"))
            refl = np.stack(chans, -1)
            gap = float((refl.sum(-1) == 0).mean())
            # L2A surface reflectance is scaled by 10000. A plain linear stretch
            # to ~0.3 (the usual true-colour recipe) saturates snow at 0.8-0.9;
            # a gamma curve over the full range keeps highlight texture while
            # still lifting the dark tundra.
            x = np.clip(refl / 10000.0, 0.0, 1.0)
            arr = np.power(x, 0.45) * 248.0
            return np.clip(arr, 0, 255), gap

        srcs = [f"/vsicurl/{h}" for _, _, _, h in reversed(subset)]
        cmd = ["gdalwarp", "-q", "-overwrite", "-t_srs", "EPSG:3413",
               "-te", str(b["xmin"]), str(b["ymin"]), str(b["xmax"]), str(b["ymax"]),
               "-ts", str(SIZE), str(SIZE), "-r", "cubic", "-ot", "Byte",
               "-of", "GTiff"] + srcs + [tif]
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            sys.exit(f"error: gdalwarp failed:\n{r.stderr.strip()}")
        ds = gdal.Open(tif)
        n = min(3, ds.RasterCount)
        arr = np.stack([ds.GetRasterBand(i + 1).ReadAsArray() for i in range(n)], -1)
        if n < 3:
            arr = np.repeat(arr[..., :1], 3, axis=-1)
        return arr.astype("float32"), float((arr.sum(-1) == 0).mean())

    # Blending scenes from different dates mixes drifting sea ice and each
    # scene's own defective pixels, which shows up as colour speckle over water.
    # Use the single best scene, and only add more if it leaves real gaps.
    if source in TILE_PROVIDERS:
        used = scenes
        stack, gap = fetch_tiles(source, b, env)
    elif local:
        used = scenes
        stack, gap = use_local(local, b, env)
    else:
        used = scenes[:1]
        stack, gap = warp(used)
    while source == "sentinel2" and not local and gap > 0.02 and len(used) < len(scenes):
        used = scenes[:len(used) + 1]
        print(f"    {gap*100:.1f}% uncovered -> adding scene {len(used)}")
        stack, gap = warp(used)
    print(f"  using {len(used)} scene(s), {gap*100:.2f}% uncovered")

    # TCI is ESA's already-stretched true-colour product; restretching it
    # amplifies dark-water noise for no gain. Opt in only if you need it.
    if os.environ.get("IMAGERY_STRETCH", "0") == "1":  # legacy TCI path only
        f = stack.astype("float32")
        valid = f.sum(-1) > 0
        lo, hi = np.percentile(f[valid], [2.0, 98.0])
        if hi > lo:
            f = np.clip((f - lo) / (hi - lo), 0, 1) * 255.0
        stack = f
    if DETAIL:
        mult = detail_shading(latlon_bbox(b), b, env)
        if mult is not None:
            stack = np.clip(stack.astype("float32") * mult[..., None], 0, 255)
            print(f"  detail: 2 m hillshade applied (strength {DETAIL_STRENGTH})")

    stack = np.clip(stack, 0, 255).astype("uint8")
    nb = 3

    mem = gdal.GetDriverByName("MEM").Create("", SIZE, SIZE, 3, gdal.GDT_Byte)
    for i in range(3):
        mem.GetRasterBand(i + 1).WriteArray(stack[..., min(i, nb - 1)])
    gdal.GetDriverByName("PNG").CreateCopy(f"{OUT}/albedo.png", mem, strict=0)
    for junk in (f"{OUT}/albedo.png.aux.xml",):
        if os.path.exists(junk):
            os.remove(junk)

    write_detail_tile(f"{OUT}/detail.png")

    import hashlib
    digest = hashlib.sha256(open(f"{OUT}/albedo.png", "rb").read()).hexdigest()

    meta["imagery"] = {
        "albedo_sha256": digest,
        "source": source if source != "sentinel2" else COLLECTION,
        "scenes": [{"id": s, "date": d, "cloud": c} for c, d, s, _ in used],
        "size": SIZE,
        "credit": TILE_PROVIDERS.get(source, {}).get("credit", ""),
        "native_px": round(meta["extent_m"] / 10.0),
        "detail_2m": bool(DETAIL),
        "detail_px": round(meta["extent_m"] / 2.0) if DETAIL else 0,
    }
    open(f"{OUT}/terrain.json", "w").write(json.dumps(meta, indent=2) + "\n")
    print(f"  albedo sha256 {digest[:16]}…  (identical builds produce identical hashes)")
    if source in TILE_PROVIDERS:
        z = int(os.environ.get("TILE_ZOOM", os.environ.get("MAPTILER_ZOOM", "14")))
        gsd = 156543.03392 * math.cos(math.radians(meta["location"]["lat"])) / (2**z) / 2
        print(f"  albedo {SIZE}x{SIZE} from {source} z{z} "
              f"(~{gsd:.2f} m/px native, {meta['extent_m']/gsd:.0f} px of real detail)")
    elif local:
        print(f"  albedo {SIZE}x{SIZE} from {os.path.basename(local)}")
    else:
        print(f"  albedo {SIZE}x{SIZE} from Sentinel-2 "
              f"(native detail ~{meta['imagery']['native_px']} px at 10 m)")


if __name__ == "__main__":
    main()
