#!/usr/bin/env python3
"""Generate a Gazebo Classic 11 world with real terrain from a built DEM.

    python3 terrain/make_world.py --name fort_ross

Reads out/<name>/terrain.json and writes:
    sim/models/arctic_terrain/{model.config,model.sdf}
    sim/models/arctic_terrain/materials/textures/{heightmap.png,albedo.png}
    sim/worlds/<name>.world
    out/<name>/sim.env          home position for the SITL container

Gazebo Classic differs from gz-sim in two ways that matter here:
  * Classic (OGRE1) uses the heightmap image at its native 2^n+1 size, so
    <size> is the true footprint. gz-sim (OGRE2) crops the last row/column and
    spans size*(N-1)/N instead.
  * Classic supports <use_terrain_paging>, which gz-sim has no equivalent for.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
from pathlib import Path

# Bind-mounted by compose: /out holds generated terrain, /sim the Gazebo tree.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from course import pick_water, plan_course
from tower import write_tower
from fleet import parse_assets, describe

OUT_ROOT = Path(os.environ.get("ARCTIC_OUT_ROOT", "/out"))
SIM_ROOT = Path(os.environ.get("ARCTIC_SIM_ROOT", "/sim"))

MODEL_CONFIG = """<?xml version="1.0"?>
<model>
  <name>{model_name}</name>
  <version>1.0</version>
  <sdf version="1.6">model.sdf</sdf>
  <author><name>arctic-sim</name></author>
  <description>
    {extent:g} m square terrain at {lat:.6f}, {lon:.6f}, from {dataset}.
    {grid}x{grid} samples at {spacing:.2f} m; elevation {zmin:.1f} to {zmax:.1f} m.
  </description>
</model>
"""

MODEL_SDF = """<?xml version="1.0" ?>
<sdf version="1.6">
  <model name="{model_name}">
    <static>true</static>
    <link name="link">

      <collision name="collision">
        <geometry>
          <heightmap>
            <uri>model://{model_name}/materials/textures/heightmap.png</uri>
            <size>{ex:.3f} {ex:.3f} {ez:.3f}</size>
            <pos>0 0 {pz:.3f}</pos>
          </heightmap>
        </geometry>
        <surface>
          <friction>
            <ode><mu>{mu}</mu><mu2>{mu}</mu2></ode>
          </friction>
        </surface>
      </collision>

      <visual name="visual">
        <geometry>
          <heightmap>
            <uri>model://{model_name}/materials/textures/heightmap.png</uri>
            <size>{ex:.3f} {ex:.3f} {ez:.3f}</size>
            <pos>0 0 {pz:.3f}</pos>
            <!-- Three identical textures, not one. gzweb's heightmap shader
                 binds texture0/1/2 and blends[0..1] unconditionally; supplying
                 fewer leaves those samplers undefined, which renders as an
                 iridescent chrome surface. Making all three the satellite image
                 turns the blend into a no-op and the imagery shows correctly.
                 Tile size equals the footprint so it maps 1:1 without tiling. -->
            <texture>
              <diffuse>model://{model_name}/materials/textures/albedo.png</diffuse>
              <normal>file://media/materials/textures/flat_normal.png</normal>
              <size>{ex:.3f}</size>
            </texture>
            <texture>
              <diffuse>model://{model_name}/materials/textures/albedo.png</diffuse>
              <normal>file://media/materials/textures/flat_normal.png</normal>
              <size>{ex:.3f}</size>
            </texture>
            <!-- texture2 is a tiling detail map, not a third terrain layer:
                 the patched gzweb shader multiplies it in so the ground stays
                 readable up close, where 10 m imagery is magnified ~200x.
                 fade_dist 0 on the first blend disables the height mix, leaving
                 the satellite image as the base colour everywhere. -->
            <texture>
              <diffuse>model://{model_name}/materials/textures/detail.png</diffuse>
              <normal>file://media/materials/textures/flat_normal.png</normal>
              <size>{detail_m:.2f}</size>
            </texture>
            <blend>
              <min_height>{b1:.2f}</min_height>
              <fade_dist>0</fade_dist>
            </blend>
            <blend>
              <min_height>{b2:.2f}</min_height>
              <fade_dist>0</fade_dist>
            </blend>
            <sampling>{sampling}</sampling>
            <use_terrain_paging>{paging}</use_terrain_paging>
          </heightmap>
        </geometry>
      </visual>

    </link>
  </model>
</sdf>
"""

WORLD = """<?xml version="1.0" ?>
<sdf version="1.6">
  <world name="{name}">

    <!-- Real-world anchor. The world axes are ArcticDEM grid axes (EPSG:3413),
         which are rotated from true north by the grid convergence at this
         longitude, so heading_deg carries that rotation. Without it every
         GPS-derived bearing is wrong. -->
    <spherical_coordinates>
      <surface_model>EARTH_WGS84</surface_model>
      <latitude_deg>{lat:.7f}</latitude_deg>
      <longitude_deg>{lon:.7f}</longitude_deg>
      <!-- 0, not the spawn height: world z is already measured from sea
           level (the heightmap's base is at zmin=0), so any non-zero value
           here is counted twice and every vehicle reports double its
           true altitude. -->
        <elevation>{elev:.2f}</elevation>
      <heading_deg>{heading:.4f}</heading_deg>
    </spherical_coordinates>

    <!-- Must match SITL's SCHED_LOOP_RATE. ArduPilot runs lockstep with the
         FDM, so if its loop cannot keep up it raises "Main loop slow" and
         refuses to arm. 250 Hz is comfortable on a laptop; raise both together
         if you have the headroom. -->
    <physics type="ode">
      <max_step_size>{step:.6f}</max_step_size>
      <real_time_factor>1</real_time_factor>
      <real_time_update_rate>{rate}</real_time_update_rate>
    </physics>

    <scene>{fog}
      <!-- Kept low: gzweb's heightmap shader ADDS ambient to diffuse and
           multiplies the texture by the sum, so a high ambient blows the
           imagery out to white. -->
      <ambient>0.35 0.36 0.40 1</ambient>
      <background>0.7 0.76 0.84 1</background>
      <!-- Off: with no GPU, Ogre's shadow pass re-renders the scene per
           light per frame in llvmpipe, and the terrain is the bulk of it.
           Set SHADOWS=1 in .env if you want them back. -->
      <shadows>{shadows}</shadows>
      <grid>false</grid>
    </scene>

    <include><uri>model://sun</uri></include>

    <include>
      <uri>model://{terrain_model}</uri>
      <pose>0 0 0 0 0 0</pose>
    </include>

{ship}{assets}

  </world>
</sdf>
"""


FOG_SDF = """
      <!-- Gazebo renders this into camera sensors, so it genuinely degrades
           what a drone sees — not just the human view. exp2 falls off with the
           square of distance, which matches how arctic haze actually behaves.
           gzweb has no fog support at all; the browser view is fogged by a
           separate patch in gz3d. -->
      <fog>
        <type>{type}</type>
        <color>{r} {g} {b} 1</color>
        <density>{density}</density>
        <start>{start:.0f}</start>
        <end>{end:.0f}</end>
      </fog>"""

MOVING_SHIP_SDF = """
    <!-- Vessel under way. NOT static: Gazebo does not publish ~/pose/info for
         static models, so it would move server-side and sit frozen in gzweb.
         Gravity off + kinematic keeps it on the surface without a buoyancy
         solver, which Gazebo Classic does not have. -->
    <model name="{name}">
      <pose>{x:.1f} {y:.1f} {z:.2f} 0 0 0</pose>
      <link name="hull">
        <gravity>0</gravity>
        <kinematic>1</kinematic>
        <visual name="visual">
          <pose>0 0 -{draft:.2f} 0 0 0</pose>
          <geometry>
            <mesh><uri>model://{model}/meshes/{model}.dae</uri></mesh>
          </geometry>
        </visual>
        <collision name="collision">
          <pose>0 0 -0.6 0 0 0</pose>
          <geometry><box><size>33.6 8.2 5.0</size></box></geometry>
        </collision>
      </link>
      <plugin name="vessel_path" filename="libVesselPathPlugin.so">
        <speed>{speed}</speed>
        <!-- false: the route is an open transit across the strait, not a
             circuit. The plugin reverses at each end, which is what a vessel
             working a channel actually does. loop=true would teleport her from
             one end back to the other. -->
        <loop>false</loop>
        <z>{z:.2f}</z>
        <start>{start:.1f}</start>
{waypoints}      </plugin>
    </model>
"""

SHIP_SDF = """
    <!-- Detection target. Pinned at the waterline: Gazebo Classic has no
         buoyancy solver, and for a find-and-track task a target that stays
         exactly where you put it is what you want. -->
    <include>
      <uri>model://{model}</uri>
      <name>{name}</name>
      <pose>{x:.1f} {y:.1f} 0 0 0 {yaw:.3f}</pose>
    </include>
"""


ASSET_SDF = {
    "vehicle": """
    <!-- {type} '{name}' in container arctic-sim-{name} at {ip}
         (fdm {fdm_port}, MAVLink {ip}:{tcp_port}).
         Spawned just clear of the surface. -->
    <model name="{name}">
      <pose>{x:.2f} {y:.2f} {z:.3f} 0 0 {yaw:.5f}</pose>
      <include>
        <uri>model://{gz_model}</uri>
      </include>
    </model>""",
    "tower": """
    <!-- {type} '{name}', an AntennaTracker in arctic-sim-{name} at {ip}
         (fdm {fdm_port}, MAVLink {ip}:{tcp_port}). Pinned to the world by a
         fixed joint inside the model, so <pose> is where it stands. -->
    <include>
      <uri>model://{gz_model}</uri>
      <pose>{x:.2f} {y:.2f} {z:.3f} 0 0 {yaw:.5f}</pose>
      <name>{name}</name>
    </include>""",
}


def place_assets(meta, dem, models_root, default_xy, spawn_offset,
                 listen_addr="0.0.0.0", cameras="live", env=None):
    """Place every ASSET_N; returns (world SDF, roster).

    A blank position falls back to the auto-chosen spawn, so a roster can name
    assets before anyone has picked coordinates for them.
    """
    import os
    assets = parse_assets(env if env is not None else os.environ)
    if not assets:
        print("  no ASSET_N entries — world will have no vehicles or towers")
        return "", []

    blocks, roster = [], []
    auto_n = 0
    for a in assets:
        xy = resolve_point(a["point"], meta) if a["point"] else None
        if a["point"] and not xy:
            print(f"  WARNING: {a['name']}: could not resolve "
                  f"{a['point']!r}; using auto placement")
        if not xy:
            # Auto-placed assets fan out on a golden-angle spiral instead of all
            # landing on the one spawn point. .env.example ships every asset with
            # a blank position, so without this the quad spawns inside the tower
            # and tumbles off it.
            k = auto_n
            auto_n += 1
            ang = k * 2.39996          # golden angle, so no two ever line up
            rad = 0.0 if k == 0 else 14.0 + 7.0 * k
            xy = (default_xy[0] + math.cos(ang) * rad,
                  default_xy[1] + math.sin(ang) * rad)
        gz = ground_at(dem, meta, *xy) if dem else 0.0

        if a["kind"] == "tower":
            model = a["name"] if a["name"].startswith("tower") else f"tower_{a['name']}"
            write_tower(models_root, model, fdm_addr=a["ip"],
                        fdm_port=a["fdm_port"], listen_addr=listen_addr,
                        cam_port=(0 if cameras == "off" else a["cam_port"]),
                        cameras=cameras)
            z = gz
        else:
            model = a["gz_model"]
            z = gz + spawn_offset

        yaw = resolve_heading(a.get("heading"), meta, xy)
        blocks.append(ASSET_SDF[a["kind"]].format(
            **{**a, "x": xy[0], "y": xy[1], "z": z, "gz_model": model,
               "yaw": yaw}))
        roster.append({**a, "gz_model": model, "x": xy[0], "y": xy[1],
                       "ground_m": gz, "z": z, "yaw": yaw})
        hdg = ""
        if a.get("heading"):
            # Report the true bearing whichever way it was specified.
            conv = meta.get("convergence_deg", 0.0)
            hdg = f"  heading {(conv + 90.0 - math.degrees(yaw)) % 360:.1f} deg true"
        print(f"  {describe(a)} at ({xy[0]:.0f}, {xy[1]:.0f}) ground {gz:.1f} m{hdg}")

    missing = sorted({r["gz_model"] for r in roster
                      if r["kind"] != "tower"
                      and not (models_root / r["gz_model"]).exists()})
    if missing:
        print(f"  WARNING: model(s) not bundled: {', '.join(missing)} — "
              f"those assets will fail to load")
    return "".join(blocks), roster


def world_to_lonlat(meta):
    """Return f(x, y) -> (lat, lon), or None if the projection is unavailable.

    Both SITL home and every tracker instance need this; deriving it twice
    invites the two disagreeing about where the world actually is.
    """
    try:
        from osgeo import osr
        s3413, s4326 = osr.SpatialReference(), osr.SpatialReference()
        s3413.ImportFromEPSG(3413); s4326.ImportFromEPSG(4326)
        s3413.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        s4326.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tr = osr.CoordinateTransformation(s3413, s4326)
        b = meta["bounds_3413"]
        cx = (b["xmin"] + b["xmax"]) / 2.0
        cy = (b["ymin"] + b["ymax"]) / 2.0

        def f(x, y):
            lo, la, *_ = tr.TransformPoint(cx + x, cy + y)
            return (la, lo)
        return f
    except Exception as e:
        print(f"  WARNING: projection unavailable ({e})")
        return None


def resolve_heading(text, meta, xy):
    """Heading spec -> world yaw in radians (CCW from world +X).

    Two forms:
      '>lat,lon'  point the asset at somewhere. Resolved in world metres and
                  measured directly, so grid convergence never enters into it.
      '<degrees>' a true compass bearing. World +Y bears `convergence` from
                  true north, so a world vector at yaw t has true bearing
                  convergence + 90 - t; invert that.
    """
    text = (text or "").strip()
    if not text:
        return 0.0
    if text.startswith((">", "@", "face:")):
        target = text.lstrip(">@").replace("face:", "", 1)
        txy = resolve_point(target, meta)
        if not txy:
            print(f"  WARNING: could not resolve heading target {target!r}; using 0")
            return 0.0
        dx, dy = txy[0] - xy[0], txy[1] - xy[1]
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            print("  WARNING: heading target is the asset's own position; using 0")
            return 0.0
        return math.atan2(dy, dx)
    try:
        bearing = float(text)
    except ValueError:
        print(f"  WARNING: bad heading {text!r}; using 0")
        return 0.0
    conv = meta.get("convergence_deg", 0.0)
    return math.radians(conv + 90.0 - bearing)


def parse_towers(spec):
    """'north@71.99,-94.81; south@71.98,-94.83' -> [(name, 'lat,lon')].

    A bare 'lat,lon' with no name gets one from its position in the list, so a
    quick single-tower test does not need naming ceremony.
    """
    out = []
    for i, chunk in enumerate([c for c in (spec or "").split(";") if c.strip()]):
        name, _, rest = chunk.strip().rpartition("@")
        out.append(((name or f"tower_{i+1}").strip(), rest.strip()))
    return out


def place_towers(spec, meta, dem, models_root, fdm_addr, fdm_base=9012):
    """Write a model per tower and return (world SDF, [placed]).

    Each tower gets its own FDM port so its own AntennaTracker instance can
    drive it: instance N listens on fdm_base + 10*(N-1), matching ArduPilot's
    own -I<n> port stride. Instance 0 and port 9002 belong to the aircraft.
    """
    towers = parse_towers(spec)
    if not towers:
        return "", []
    blocks, placed = [], []
    for i, (name, point) in enumerate(towers):
        xy = resolve_point(point, meta)
        if not xy:
            print(f"  WARNING: tower {name}: could not resolve {point!r}; skipped")
            continue
        gz = ground_at(dem, meta, *xy) if dem else 0.0
        model = name if name.startswith("tower") else f"tower_{name}"
        port = fdm_base + 10 * i
        write_tower(models_root, model, fdm_addr=fdm_addr, fdm_port=port)
        blocks.append(TOWER_SDF.format(model=model, name=name,
                                       x=xy[0], y=xy[1], z=gz, yaw=0.0))
        placed.append({"name": name, "model": model, "instance": i + 1,
                       "fdm_port": port, "x": xy[0], "y": xy[1], "ground_m": gz})
        print(f"  tower {name}: ({xy[0]:.0f}, {xy[1]:.0f}) ground {gz:.1f} m, "
              f"camera at {gz + 2.7:.1f} m, tracker -I{i+1} fdm {port}")
    return "".join(blocks), placed


def resolve_point(text: str, meta: dict):
    """'lat,lon' or 'x,y' (world metres) -> world (x, y), or None.

    Latitude is bounded by 90 and world coordinates by half the extent, so the
    two forms are distinguishable without the caller having to say which.
    """
    text = (text or "").strip()
    if not text:
        return None
    # At 72 N, "71.99,-94.86" is a valid lat/lon *and* a valid pair of world
    # metres, so the two cannot be told apart by range. Default to lat/lon —
    # that is what the picker emits — and require an explicit xy: prefix for
    # world coordinates.
    as_xy = False
    if text.lower().startswith(("xy:", "world:")):
        as_xy = True
        text = text.split(":", 1)[1]
    try:
        v = [float(t) for t in text.replace(" ", "").split(",")[:2]]
    except ValueError:
        print(f"  WARNING: could not parse {text!r}; ignoring")
        return None
    if not as_xy and abs(v[0]) <= 90 and abs(v[1]) <= 180:
        from osgeo import osr
        src, dst = osr.SpatialReference(), osr.SpatialReference()
        src.ImportFromEPSG(4326); dst.ImportFromEPSG(3413)
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tr = osr.CoordinateTransformation(src, dst)
        px, py, *_ = tr.TransformPoint(v[1], v[0])
        b = meta["bounds_3413"]
        return (px - (b["xmin"] + b["xmax"]) / 2, py - (b["ymin"] + b["ymax"]) / 2)
    if not as_xy:
        print(f"  WARNING: {v} is outside lat/lon range; treating as world metres")
    return (v[0], v[1])


def ground_at(dem: str, meta: dict, x: float, y: float) -> float:
    from osgeo import gdal
    gdal.UseExceptions()
    ds = gdal.Open(dem)                 # held: a temporary would be freed
    z = ds.GetRasterBand(1).ReadAsArray()
    g, sp, half = meta["grid"], meta["spacing_m"], meta["extent_m"] / 2.0
    c = max(0, min(g - 1, int(round((x + half) / sp))))
    r = max(0, min(g - 1, int(round((half - y) / sp))))
    return float(z[r, c])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    p.add_argument("--vehicle-model", default="iris_with_ardupilot")
    p.add_argument("--friction", type=float, default=1.0)
    p.add_argument("--cameras", default="live",
                   choices=["live", "ondemand", "off"],
                   help="live: always rendering. ondemand: asleep until a "
                        "feed is opened. off: no stream at all.")
    p.add_argument("--shadows", action="store_true",
                   help="enable scene shadows (costly without a GPU)")
    p.add_argument("--heightmap-sampling", type=int, default=1,
                   help="vertices per heightmap datum; Gazebo defaults to 2")
    p.add_argument("--paging", action="store_true",
                   help="enable Classic terrain paging (large maps only)")
    p.add_argument("--spawn-offset", type=float, default=0.30,
                   help="metres above the surface to spawn the vehicle")
    p.add_argument("--ship", action="store_true",
                   help="place a vessel on the largest patch of open water")
    p.add_argument("--fog", action="store_true",
                   help="add atmospheric fog (also visible to camera sensors)")
    p.add_argument("--fog-density", type=float,
                   default=float(os.environ.get("FOG_DENSITY", "0.0008")))
    p.add_argument("--fog-colour", default=os.environ.get("FOG_COLOUR", "0.80 0.84 0.88"))
    p.add_argument("--fog-type", default=os.environ.get("FOG_TYPE", "exp2"),
                   choices=["linear", "exp", "exp2"])
    p.add_argument("--ship-model", default=os.environ.get("SHIP_MODEL", "fishing_vessel"),
                   help="Gazebo model name to place on the water")
    p.add_argument("--vehicle-start-at",
                   default=os.environ.get("VEHICLE_START_AT", ""),
                   help="spawn the aircraft here: 'lat,lon' from the right-click "
                        "picker, or 'x,y' in world metres. Also sets SITL home.")
    p.add_argument("--ship-start-at", default=os.environ.get("SHIP_START_AT", ""),
                   help="place the vessel here: 'lat,lon' (as copied from the "
                        "right-click picker) or 'x,y' in world metres. "
                        "Overrides SHIP_START when set.")
    p.add_argument("--course-seed", type=int,
                   default=int(os.environ.get("COURSE_SEED") or 0),
                   help="0 or unset = a new route every build")
    p.add_argument("--course-length", type=float,
                   default=float(os.environ.get("COURSE_LENGTH") or 9000),
                   help="target route length in metres")
    p.add_argument("--ship-start", default=os.environ.get("SHIP_START", "fixed"),
                   choices=["fixed", "random"],
                   help="fixed: always the same start. random: seeded random "
                        "position (moored) or point along the course (moving)")
    p.add_argument("--ship-start-seed", type=int,
                   default=int(os.environ.get("SHIP_START_SEED") or 0),
                   help="0 or unset = a different start every build; "
                        "any other value is reproducible")
    p.add_argument("--ship-moving", action="store_true",
                   help="follow the planned course instead of sitting still")
    p.add_argument("--ship-speed", type=float,
                   default=float(os.environ.get("SHIP_SPEED", "3.0")),
                   help="m/s (3.0 is about 6 knots)")
    p.add_argument("--ship-yaw", type=float, default=None,
                   help="heading in radians (default: along the waterway)")
    p.add_argument("--physics-rate", type=int,
                   default=int(os.environ.get("PHYSICS_RATE", "250")))
    p.add_argument("--gazebo-ip", default=os.environ.get("GAZEBO_IP", "10.23.0.5"))
    p.add_argument("--sitl-ip", default=os.environ.get("SITL_IP", "10.23.0.2"))
    args = p.parse_args()

    meta_path = OUT_ROOT / "terrain.json"
    if not meta_path.exists():
        sys.exit(f"error: {meta_path} not found — run build_terrain.py first")
    meta = json.loads(meta_path.read_text())

    grid = meta["grid"]
    if (grid - 1) & (grid - 2) != 0 and ((grid - 1) & (grid - 2)) != 0:
        pass  # validated upstream

    # One model per site. A fixed name would mean generating a second site
    # silently overwrote the first one's heightmap and texture.
    model_name = f"terrain_{args.name}"
    model_dir = SIM_ROOT / "models" / model_name
    tex = model_dir / "materials" / "textures"
    tex.mkdir(parents=True, exist_ok=True)

    src = meta_path.parent / "heightmap.png"
    if not src.exists():
        sys.exit(f"error: {src} not found")
    shutil.copy2(src, tex / "heightmap.png")

    albedo = OUT_ROOT / "albedo.png"
    if albedo.exists():
        shutil.copy2(albedo, tex / "albedo.png")
    detail = OUT_ROOT / "detail.png"
    if detail.exists():
        shutil.copy2(detail, tex / "detail.png")
    elif not (tex / "albedo.png").exists():
        sys.exit("error: no albedo.png — run gen_imagery.py first")

    dem_any = next((str(OUT_ROOT / c) for c in
                    ("dem_clamped.tif", "dem_filled.tif", "dem_raw.tif")
                    if (OUT_ROOT / c).exists()), None)

    elev = meta["elevation_m"]
    zmin, zmax, zrange = elev["min"], elev["max"], elev["range"]
    extent = meta["extent_m"]
    centre_z = meta.get("centre_elevation_m", (zmin + zmax) / 2)
    spawn = dict(meta.get("spawn", {"x": 0.0, "y": 0.0, "z": centre_z}))

    (model_dir / "model.config").write_text(MODEL_CONFIG.format(
        model_name=model_name, extent=extent, lat=meta["location"]["lat"], lon=meta["location"]["lon"],
        dataset=meta["source"]["dataset"], grid=grid,
        spacing=meta["spacing_m"], zmin=zmin, zmax=zmax))

    (model_dir / "model.sdf").write_text(MODEL_SDF.format(
        model_name=model_name, ex=extent, ez=zrange, pz=zmin, mu=args.friction,
        b1=zmin + 0.33 * zrange, b2=zmin + 0.66 * zrange,
        detail_m=float(os.environ.get("DETAIL_TILE_M", "12")),
        paging="true" if args.paging else "false",
        sampling=args.heightmap_sampling))

    ship_block = ""
    water = meta.get("water", {})
    if args.ship:
        if not water.get("present"):
            print("  --ship requested but this site has no open water; skipping")
        else:
            yaw = args.ship_yaw if args.ship_yaw is not None else 0.785
            import random as _random
            # 0/unset means "surprise me": asking for a random start and then
            # getting the same spot every time is not what anyone expects.
            # Pin a non-zero seed when runs must be comparable.
            seed_used = args.ship_start_seed
            if seed_used == 0:
                import secrets
                seed_used = secrets.randbelow(2 ** 31 - 1) + 1
            rng = _random.Random(seed_used)

            # Planned here, not during the DEM pass: it only needs the DEM
            # already on disk, so a new route costs seconds instead of a full
            # re-download. That is what makes re-rolling the start practical.
            dem = next((str(OUT_ROOT / c) for c in
                        ("dem_clamped.tif", "dem_filled.tif", "dem_raw.tif")
                        if (OUT_ROOT / c).exists()), None)
            cseed = args.course_seed
            if cseed == 0:
                import secrets
                cseed = secrets.randbelow(2 ** 31 - 1) + 1
            # A requested start position. Latitude here is ~72 and world
            # coordinates are within +/-half the extent, so the two are never
            # ambiguous — but be explicit rather than clever about it.
            forced = resolve_point(args.ship_start_at, meta)
            if forced:
                print(f"  vessel start requested at world "
                      f"({forced[0]:.0f}, {forced[1]:.0f})")

            course = {}
            if dem and args.ship_moving:
                course = plan_course(dem, meta["grid"], meta["spacing_m"],
                                     meta["extent_m"], cseed, args.course_length,
                                     start_xy=forced)
                if course.get("present"):
                    print(f"  route: {len(course['waypoints'])} waypoints, "
                          f"{course['length_m']:.0f} m, min clearance "
                          f"{course['path_min_clearance_m']:.0f} m "
                          f"(course seed {cseed}"
                          + (", unpinned" if args.course_seed == 0 else "") + ")")
            if args.ship_moving and course.get("present"):
                wps = course["waypoints"]
                wp_xml = "".join(f"        <waypoint>{a} {b}</waypoint>\n"
                                 for a, b in wps)
                # Random start is a phase offset along the already-validated
                # course, not a new position: the track stays land-free by
                # construction, only the vessel's place on it changes.
                # A requested position means start there: offset 0 is the top
                # of the route, which the planner anchored to that point.
                start = 0.0 if forced else (
                    rng.uniform(0.0, course["length_m"])
                    if args.ship_start == "random" else 0.0)
                ship_block = MOVING_SHIP_SDF.format(
                    name="target_vessel", model=args.ship_model,
                    x=wps[0][0], y=wps[0][1], z=0.0, draft=2.6,
                    speed=args.ship_speed, start=start, waypoints=wp_xml)
                lap = course["length_m"] / args.ship_speed / 60.0
                import math as _m
                sx, sy = wps[0]
                acc = 0.0
                for _a, _b in zip(wps, wps[1:]):
                    d = _m.hypot(_b[0] - _a[0], _b[1] - _a[1])
                    if acc + d >= start:
                        f = (start - acc) / max(d, 1e-9)
                        sx, sy = _a[0] + (_b[0] - _a[0]) * f, _a[1] + (_b[1] - _a[1]) * f
                        break
                    acc += d
                if forced:
                    snap = course.get("snapped_m", 0.0)
                    where = (f"starts at requested ({sx:.0f}, {sy:.0f})"
                             + (f", snapped {snap:.0f} m to navigable water"
                                if snap > 1 else ""))
                elif args.ship_start == "random":
                    where = f"random start {start:.0f} m along, at ({sx:.0f}, {sy:.0f})"
                else:
                    where = f"starts at ({wps[0][0]:.0f}, {wps[0][1]:.0f})"
                print(f"  vessel under way at {args.ship_speed} m/s "
                      f"({lap:.1f} min end to end)")
                print(f"    {where}")
            else:
                sx, sy, sc = water["x"], water["y"], water["clearance_m"]
                if forced:
                    sx, sy = forced
                    sc = 0.0
                elif args.ship_start == "random" and water.get("candidates"):
                    sx, sy, sc = rng.choice(water["candidates"])
                    yaw = rng.uniform(0.0, 2 * 3.14159265)
                ship_block = SHIP_SDF.format(name="target_vessel", model=args.ship_model,
                                             x=sx, y=sy, yaw=yaw)
                how = ("requested position" if forced else
                       f"random of {len(water.get('candidates', []))} spots "
                       f"(seed {seed_used}"
                       + (", unpinned" if args.ship_start_seed == 0 else "") + ")"
                       if args.ship_start == "random" else "furthest from shore")
                print(f"  ship moored at ({sx:.0f}, {sy:.0f}), "
                      f"{sc:.0f} m from shore — {how}")

    fog_block = ""
    if args.fog:
        fr, fg, fb = args.fog_colour.split()
        # Visibility ~ 3/density for exp2; report it so the number means something.
        vis = 3.0 / max(args.fog_density, 1e-9)
        fog_block = FOG_SDF.format(type=args.fog_type, r=fr, g=fg, b=fb,
                                   density=args.fog_density,
                                   start=min(200.0, vis * 0.1), end=vis)
        print(f"  fog {args.fog_type} density {args.fog_density} "
              f"-> visibility ~{vis:.0f} m")

    asset_block, roster = place_assets(
        meta, dem_any, SIM_ROOT / "models",
        (spawn["x"], spawn["y"]), args.spawn_offset, args.gazebo_ip,
        cameras=args.cameras)

    # The world is anchored on the first flying asset, so its EKF origin and
    # the terrain agree. With no vehicles, fall back to the auto spawn.
    first = next((r for r in roster if r["kind"] == "vehicle"), None)
    if first:
        spawn["x"], spawn["y"], spawn["z"] = first["x"], first["y"], first["ground_m"]

    conv = meta.get("convergence_deg", 0.0)
    worlds = SIM_ROOT / "worlds"
    worlds.mkdir(parents=True, exist_ok=True)
    (worlds / f"{args.name}.world").write_text(WORLD.format(
        name=args.name, terrain_model=model_name,
        lat=meta["location"]["lat"], lon=meta["location"]["lon"],
        elev=0.0, heading=-conv,
        sx=spawn["x"], sy=spawn["y"], sz=spawn["z"] + args.spawn_offset,
        rate=args.physics_rate, step=1.0 / args.physics_rate,
        ship=ship_block, fog=fog_block, assets=asset_block,
        shadows="true" if args.shadows else "false"))

    # SITL must boot at the same place the world is anchored, otherwise the
    # EKF origin and the terrain disagree and the vehicle flies off the map.
    if course.get("present"):
        meta["course"] = course
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")

    to_ll = world_to_lonlat(meta)
    home_lat, home_lon = meta["location"]["lat"], meta["location"]["lon"]
    if to_ll:
        home_lat, home_lon = to_ll(spawn["x"], spawn["y"])
    else:
        print("  WARNING: could not derive home from spawn; using site centre")

    (OUT_ROOT / "sim.env").write_text(
        f"HOME_LAT={home_lat:.7f}\n"
        f"HOME_LON={home_lon:.7f}\n"
        f"HOME_ALT={spawn['z']:.2f}\n"
        f"HOME_DIR=0\n"
        f"WORLD_NAME={args.name}\n"
        # The whole roster. sitl/entrypoint.sh reads this
        # rather than re-parsing TOWERS, so the ports can only be assigned once.
        # The full roster, so sitl/entrypoint.sh never re-parses ASSET_N and
        # the port assignment can only be made in one place. QUOTED: this file
        # is `source`d, and an unquoted ';' would end the assignment and leave
        # bash trying to run the second asset as a command.
        + "FLEET=\"" + ";".join(
            "{name},{type},{vehicle},{frame},{sim_model},{slot},"
            "{fdm_port},{fdm_port_out},{ip},{lat:.7f},{lon:.7f},{alt:.2f}".format(
                **{**r, "sim_model": r["sim_model"] or ""},
                **dict(zip(("lat", "lon"),
                           to_ll(r["x"], r["y"]) if to_ll else (0.0, 0.0))),
                alt=r["ground_m"])
            for r in roster) + "\""
        + "\n")

    # The stock vehicle models hardcode their ArduPilotPlugin endpoints.
    # Rewrite them so the compose subnet can change without editing SDF by hand
    # (DVD already occupies 10.13.0.0/24 on most dev machines), and so each
    # asset talks to its own SITL instance.
    by_model = {}
    for r in roster:
        if r["kind"] == "tower":
            continue          # tower SDF is generated with its port already in
        by_model.setdefault(r["gz_model"], []).append(r)
    # Nested models carry cameras too — the quadcopter's gimbal lives inside
    # iris_with_ardupilot, so keying only on the roster's gz_model missed it.
    for r in list(roster):
        if r["kind"] == "tower":
            continue
        for sub in (SIM_ROOT / "models").glob("*/model.sdf"):
            if "camera_stream" not in sub.read_text():
                continue
            if sub.parent.name in by_model:
                continue
            by_model.setdefault(sub.parent.name, []).append(r)

    for model, users in by_model.items():
        if len(users) > 1:
            print(f"  WARNING: {len(users)} assets share model {model!r}; they "
                  f"cannot have separate FDM ports. Give each its own model dir.")
        sdf = SIM_ROOT / "models" / model / "model.sdf"
        if not sdf.exists():
            continue
        txt = sdf.read_text()
        new_txt = re.sub(r"<listen_addr>[^<]*</listen_addr>",
                         f"<listen_addr>{args.gazebo_ip}</listen_addr>", txt)
        new_txt = re.sub(r"<fdm_addr>[^<]*</fdm_addr>",
                         f"<fdm_addr>{users[0]['ip']}</fdm_addr>", new_txt)
        new_txt = re.sub(r"<fdm_port_in>[^<]*</fdm_port_in>",
                         f"<fdm_port_in>{users[0]['fdm_port']}</fdm_port_in>",
                         new_txt)
        # SITL instance I listens on 9003+10I; the plugin must reply there or
        # the instance hangs in lockstep.
        out_port = users[0]["fdm_port"] + 1
        if "<fdm_port_out>" in new_txt:
            new_txt = re.sub(r"<fdm_port_out>[^<]*</fdm_port_out>",
                             f"<fdm_port_out>{out_port}</fdm_port_out>", new_txt)
        else:
            new_txt = new_txt.replace(
                f"<fdm_port_in>{users[0]['fdm_port']}</fdm_port_in>",
                f"<fdm_port_in>{users[0]['fdm_port']}</fdm_port_in>\n"
                f"      <fdm_port_out>{out_port}</fdm_port_out>", 1)
        # The MJPEG port lives on the sim container, so it has to be unique
        # per asset just like the FDM ports.
        if "camera_stream" in new_txt:
            mode = args.cameras
            on = "1" if mode == "live" else "0"
            sleep = "1" if mode == "ondemand" else "0"
            new_txt = re.sub(r"<always_on>[01]</always_on>",
                             f"<always_on>{on}</always_on>", new_txt)
            if "<sleep_when_idle>" in new_txt:
                new_txt = re.sub(r"<sleep_when_idle>[01]</sleep_when_idle>",
                                 f"<sleep_when_idle>{sleep}</sleep_when_idle>", new_txt)
            else:
                new_txt = new_txt.replace("<shrink>1</shrink>",
                    f"<shrink>1</shrink>\n          "
                    f"<sleep_when_idle>{sleep}</sleep_when_idle>", 1)
            new_txt = re.sub(r"(<plugin name=\"camera_stream\"[\s\S]*?)<port>\d+</port>",
                             lambda m: m.group(1) + "<port>%d</port>" % (
                                 0 if args.cameras == "off" else users[0]["cam_port"]),
                             new_txt)
        if new_txt != txt:
            sdf.write_text(new_txt)
            print(f"  patched {model}: gazebo={args.gazebo_ip} "
                  f"asset={users[0]['ip']} fdm={users[0]['fdm_port']} "
                  f"cam={users[0]['cam_port']}")

    print(f"wrote sim/worlds/{args.name}.world and sim/models/{model_name}/")
    print(f"  heightmap {grid}x{grid}, footprint {extent:g} m, "
          f"elevation {zmin:.1f}–{zmax:.1f} m")
    print(f"  {len(roster)} asset(s); world anchored at "
          f"({spawn['x']:.0f}, {spawn['y']:.0f}) ground {spawn['z']:.2f} m")
    print(f"  physics {args.physics_rate} Hz (SITL SCHED_LOOP_RATE must match)")
    print(f"  home {meta['location']['lat']:.5f}, {meta['location']['lon']:.5f}"
          f"  heading offset {-conv:+.2f}°")


if __name__ == "__main__":
    main()
