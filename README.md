# arctic-sim

Real arctic terrain, a moving vessel, fog, and ArduPilot SITL — in one
`docker compose up`, viewable in a browser.

Pick coordinates anywhere above 60°N. The stack downloads the elevation model and
satellite imagery for that spot, builds a Gazebo world from it, plots a course
for a target vessel across the water, boots ArduPilot SITL homed to the same
coordinates, and serves the whole thing through gzweb.

**No ROS.** The only Gazebo plugins in play are `libArduPilotPlugin.so`,
`libLiftDragPlugin.so`, and this project's own `libVesselPathPlugin.so`. SITL
talks to Gazebo over the plugin's own UDP channel; MAVLink is only for ground
stations.

## Quickstart

```bash
cp .env.example .env
# add MAPBOX_TOKEN=pk.… to .env  (free — see below)
docker compose up
```

| | |
|---|---|
| Browser view | <http://localhost:8080> |
| Control panel | injected into the browser view; API on `127.0.0.1:8090` |
| Fleet check | `./mavcheck` |

Each asset is its own container with its own IP, and answers MAVLink on
ArduPilot's stock ports at that address. The host-side ports are strided only
because host ports cannot collide:

| asset | container IP | host TCP | host UDP (GCS) |
|---|---|---|---|
| quadcopter | 10.23.0.100 | 5760 | 14550 / 14551 |
| fixed-wing | 10.23.0.101 | 5770 | 14560 / 14561 |
| boat | 10.23.0.102 | 5780 | 14570 / 14571 |
| tower-1 | 10.23.0.103 | 5790 | 14580 / 14581 |
| tower-2 | 10.23.0.104 | 5800 | 14590 / 14591 |

The endpoints are MAVProxy `udpin` sockets — listeners — so a client has to
transmit first. Use `udpout`, or set an explicit target host in a GCS:

```bash
mavproxy.py --master=udpout:localhost:14550     # quadcopter
mavproxy.py --master=udpout:localhost:14580     # tower-1
```

`udp:` (listen mode) leaves both ends waiting and looks like a dead vehicle.

First run downloads terrain and builds ArduPilot from source, so it takes a
while. Subsequent runs reuse both.

**Without a key it still works.** The imagery step falls back to Sentinel-2
(10 m, free, no account) and prints how to upgrade. Nothing hard-fails for a
missing token.

## Imagery

The default is **Mapbox**, which serves ~1.5 m imagery at these latitudes —
about **6.8× sharper** than the Sentinel-2 fallback.

| source | resolution over Bellot Strait | account |
|---|---|---|
| **mapbox** (default) | ~1.48 m/px | free token |
| maptiler | ~1.48 m/px | free key |
| sentinel2 | 10 m/px | none |

Mapbox and MapTiler resell the same Maxar basemap, so resolution is identical.
Mapbox is the default because its free tier is a documented number rather than a
judgement call.

### Getting a token

<https://account.mapbox.com/access-tokens/> — the Raster Tiles API includes
**750,000 tile requests/month free**. A full build of all three sites costs
**321 tiles**, so one person could rebuild everything ~2,300 times a month
inside the free tier. Volume will never be the constraint, and because the result
is baked into a texture, nothing touches the API during actual runs.

### Switching source

```bash
IMAGERY_SOURCE=mapbox MAPBOX_TOKEN=pk.… FORCE_TERRAIN=1 \
  docker compose run --rm terrain
GZWEB_REDEPLOY=1 docker compose up -d --force-recreate sim
```

`FORCE_TERRAIN=1` rebuilds **every site in `sites.conf`**, not just the active
one — `SITE_NAME` only selects which world loads.

### Verify a zoom before trusting it

A tile server will serve z22 anywhere; past the source resolution it is only
upscaling. Coverage tiers vary by location, so check each site:

```bash
MAPBOX_TOKEN=pk.… python3 terrain/check_tiles.py --name resolute --provider mapbox
```

It measures where sharpness stops climbing and reports the true metres/pixel.

## Sites

Two ways in; they share state.

### With docker compose

`sites.conf` is the list compose builds. Add a line, run `docker compose up`,
and only the new site downloads:

```
# name        lat          lon           extent_m   [grid]
fort_ross     71.991960   -94.822428    6500
pond_inlet    72.698900   -77.964700    5000
resolute      74.697222   -94.829722    2500
```

`SITE_NAME` in `.env` picks which one loads. Editing a line rebuilds only what
changed:

```
[terrain] fort_ross: up to date, skipping
[terrain] resolute: extent changed since last build — rebuilding
```

That check matters — without it, changing coordinates while keeping the same
name leaves stale terrain in place and nothing tells you.

### With the CLI

```bash
./arctic add resolute 74.6972 -94.8297 --extent 2000
./arctic add resolute "74°41'50\"N" "94°49'47\"W" --extent 2000   # same thing
./arctic list
./arctic fly fort_ross      # start it and open the browser
./arctic use pond_inlet     # switch the running sim
./arctic rm resolute
```

Have a bounding box rather than a centre? Paste the corners:

```bash
./arctic add fort_ross --bbox "72°00'08.59\"N 94°54'35.12\"W,
                               72°00'57.85\"N 94°44'57.83\"W,
                               71°58'05.99\"N 94°54'17.78\"W,
                               71°58'51.79\"N 94°43'32.23\"W"
```

`./arctic` writes to the same `sites.conf`, so anything added this way is also
built by `docker compose up`.

**Grid is chosen for you** — the smallest 2ⁿ+1 grid sampling at least as finely
as ArcticDEM's 10 m posting: 500 m → 65², 6.5 km → 1025², 20 km → 2049².

Note `./arctic imagery <provider>` predates the vessel and fog, and forwards
only the imagery variables — use the two-command form above if `FOG`/`SHIP` are
not set in `.env`, or it will quietly rebuild without them.

## Placing your own assets

Right-click anywhere in the 3D view to read that spot's position. The menu
offers three formats:

| format | for |
|---|---|
| **SDF pose** | pasting straight into a `<model>` in a world file |
| **x y z** | world metres, relative to the site centre |
| **lat,lon** | `.env` settings, and anything else that speaks GPS |

Paste a `lat,lon` into `VEHICLE_START_AT` to move where the vehicle spawns:

```
VEHICLE_START_AT=71.996182,-94.862405
```

The vehicle is set down on the ground there, and SITL's home — the EKF origin
the autopilot navigates from — follows it, so the aircraft and its origin stay
together. Pick land: water is at sea level, and the vehicle will be placed on
it.

At 72 N a lat/lon pair is also a valid pair of world metres (`71, -94` is a
real point on a 6.5 km site), so the two cannot be told apart by range.
Bare numbers are read as lat/lon; prefix with `xy:` for world metres —
`xy:-527,1350`. Blank means the flat dry ground nearest the site centre.

Changing it regenerates the world only, in seconds — nothing is re-downloaded.

The target vessel is deliberately *not* settable from the right-click menu: it
is the thing teams have to find, so its start comes from `SHIP_START` below.

## The target vessel

A 33.6 m fishing vessel that plots its own course across the water.

```bash
SHIP=1 SHIP_MOVING=1 COURSE_SEED=1 SHIP_SPEED=3.0
```

```
course: 101 waypoints, 5481 m (seed 1)
clearance: 190 m minimum along the travelled line, verified end to end
vessel under way: 3.0 m/s (30.4 min per lap)
```

**It never crosses land, and that is verified rather than assumed.** Waypoints
sitting in water is not enough: the vessel travels straight lines between them,
so a segment can cut across a headland with both endpoints clear. The planner
samples every segment finer than a grid cell — including the closing leg, since
the course loops — repairs anything that grounds, and retries with a derived
seed until the whole path is provably clear. Across 15 seeds, one grounded on
its first attempt and succeeded on the second; all 15 end up land-free.

The course steers rather than wanders: heading carries momentum, and when
clearance drops the planner climbs the distance-to-shore gradient back toward
open water.

It is a **model plugin, not a Gazebo `<actor>`** — gzweb has no actor support, so
an actor would animate server-side and sit frozen in the browser. Moving a model
publishes on `~/pose/info`, which gzweb already consumes. For the same reason the
vessel is not `<static>`: Gazebo does not publish pose updates for static models.
It uses `gravity 0` + `kinematic`, which keeps it on the surface without a
buoyancy solver Classic does not have.

Drop `SHIP_MOVING` to moor it. `SHIP_MODEL=ship` swaps to a lightweight
procedural hull (64 KB vs 2.5 MB).

Clearance is horizontal only — there is no bathymetry, so the water is a flat
plane at sea level and the vessel cannot ground on a shoal.

### Bringing your own model

```bash
docker run --rm -v "<dir>:/in:ro" -v $PWD/sim/models/<name>:/model \
  -v $PWD/sim:/scripts:ro ghcr.io/osgeo/gdal:ubuntu-small-latest \
  python3 /scripts/convert_model.py --obj "/in/model.obj" \
    --name <name> --length 33.6 --target-tris 40000
```

Downloaded vessel models are built for close-up renders — the bundled one started
at 1.03 M triangles and 48 textures. The converter decimates by vertex clustering
and replaces each texture with its average colour: 42 k triangles, 2.5 MB. From
the air a hull reads as colour and silhouette, not plank detail.

## Fog

```bash
FOG=1 FOG_DENSITY=0.0008     # visibility ~3.75 km
```

Gazebo renders fog into **camera sensors**, so it genuinely degrades what a drone
sees, not just the human view. Visibility is roughly `3 / density` metres:

| density | visibility |
|---|---|
| 0.0004 | ~7.5 km |
| 0.0008 | ~3.75 km |
| 0.002 | ~1.5 km |
| 0.005 | ~600 m |

gzweb has no fog support at all, so it is patched in: `THREE.FogExp2` on the
scene, plus explicit fog in the heightmap's custom shader, which does not inherit
scene fog. The sim entrypoint reads `<density>` from the world file at startup
and bakes it into the gzweb bundle, so the browser view and the sensors cannot
drift apart.

## Georeferencing

Easy to get silently wrong, so it is measured per site.

**World axes are DEM grid axes, not true ENU.** ArcticDEM is polar stereographic
about 45°W, so grid north equals true north only on that meridian. At Fort Ross
the world's +Y axis bears **−49.80°** from true north. Left at zero, every
GPS-derived bearing is wrong — at 1 km that is a ~550 m error. Verified with
NavSat sensors at Pond Inlet:

| `heading_deg` | reported lon at world (0,1000) | error |
|---|---|---|
| `0` | −77.9647 | ~550 m |
| **`+32.95`** | −77.98109 | **~6 m** |

**Grid metres are not ground metres.** Polar stereographic is conformal, not
equidistant. At 72°N the point scale factor is ~0.992, so an uncorrected 6.5 km
window is really ~6.55 km of ground. `--true-scale` (on by default) corrects it.

**Vehicles spawn on land, vessels on water.** The geometric centre of a coastal
site is often sea — Fort Ross's centre is open water in Bellot Strait. The
pipeline finds dry flat ground for the aircraft and the water furthest from any
shore for the vessel, then homes SITL to the aircraft's point so the EKF origin
and the terrain agree.

## Vehicles

`VEHICLE`/`FRAME` are ArduPilot's; `VEHICLE_MODEL` is the Gazebo model.

| VEHICLE | FRAME | VEHICLE_MODEL | status |
|---|---|---|---|
| ArduCopter | `gazebo-iris` | `iris_with_ardupilot` | bundled |
| ArduRover | `gazebo-rover` | — | needs a model |
| ArduPlane | `gazebo-zephyr` | — | needs a model |

`PHYSICS_RATE` (default 250) drives both the world's step rate and SITL's
`SCHED_LOOP_RATE` from one place — they must match, or SITL reports
`Main loop slow` and refuses to arm.

## Running a competition

```bash
./arctic verify --write     # organiser, once — records the reference build
./arctic verify             # entrants — proves their world matches
```

Every build hashes its albedo and heightmap. `competition.lock` pins the expected
values; ship it with the rules. If a provider updates imagery mid-competition,
someone's `verify` fails loudly instead of them quietly competing on a different
map.

Pin `COURSE_SEED`, `FOG_DENSITY`, `SHIP_SPEED` and `IMAGERY_SOURCE` too, or
entrants can tune their own weather and target behaviour.

**On licensing:** Mapbox and MapTiler both permit only limited temporary caching,
and this pipeline bakes tiles into a stored texture. Requiring each entrant to
bring their own token means you never redistribute a tile, but it does not make
the caching compliant — it spreads the exposure. If the competition has prizes or
sponsors, describe exactly what you are doing to Mapbox and get an answer in
writing. `IMAGERY_SOURCE=sentinel2` is the fallback with no account and no
ambiguity: Copernicus data is freely redistributable, so you could ship one
identical pre-baked world to everyone.

Both providers require visible attribution — Mapbox additionally requires
crediting Maxar. That belongs in the simulator, not just this file.

## Why Gazebo Classic

Deliberate, not inertia:

- **gzweb only exists for Classic.** Modern Gazebo has no web client, which is
  the whole reason this runs in a browser rather than a VNC session.
- **Classic has `<use_terrain_paging>`** — heightmap LOD that gz-sim has no
  equivalent for.
- **Focal has arm64 builds.** OSRF publishes `gazebo11` for focal on both amd64
  and arm64; jammy and noble carry the apt metadata but no binaries. That is what
  lets this run natively on Apple Silicon rather than under emulation.

The cost is real: Classic reached end of life in January 2025, and its last ROS
pairing was Humble. Since this stack has no ROS at all, that second point costs
nothing here.

### Patches to vendored gzweb

gzweb is vendored under `sim/gzweb/`, with four deliberate changes:

| what | why |
|---|---|
| camera far plane 1000 → 50000 | stock gzweb clips a multi-km terrain at a hard grey line |
| heightmap mesh cap 256 → 512 | 256 segments over 6.5 km is ~25 m/vertex, visibly faceted |
| `texture2` repurposed as a detail map | 10 m imagery magnified ~200× up close needs high-frequency detail |
| fog added | stock gzweb has no fog of any kind |

Tunable at runtime with `window.GZ3D_MAX_HEIGHTMAP`, `window.GZ3D_DETAIL_AMOUNT`,
`window.GZ3D_FOG`.

## Networking

The compose network is `10.23.0.0/24`, deliberately clear of Damn Vulnerable
Drone's `10.13.0.0/24` so both can run side by side.

| service | address | role |
|---|---|---|
| `sim` | 10.23.0.5 | Gazebo + gzweb |
| `sitl` | 10.23.0.2 | ArduPilot |

`iris_with_ardupilot` ships with the ArduPilotPlugin endpoints hardcoded in its
SDF, so `make_world.py` rewrites `listen_addr`/`fdm_addr` to match `GAZEBO_IP`/
`SITL_IP`. Change the subnet freely.

## Running it on a GPU host

**The browser view does not need a server GPU.** gzweb renders in the viewer's
own browser, so the whole 3D scene costs the host nothing but message fanout.
A GPU earns its place only once `CAMERAS` is on, because Gazebo renders camera
sensors server-side through OGRE. Without a card that rasterising happens on the
CPU, competing with exactly the cores ArduPilot needs to hold lockstep at
`PHYSICS_RATE` — which surfaces as `PreArm: Main loop slow` and reads like a
parameter problem.

Getting OGRE onto the card takes more than attaching a GPU. OGRE 1.9 speaks
**GLX**, not EGL, so it needs a real X server *running on the GPU* — Xvfb is a
software framebuffer and yields llvmpipe no matter what hardware is present.
Run Xorg on the host with the `nvidia` driver, then:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
```

That overlay bind-mounts the host's X socket into the sim container, sets
`USE_HOST_X=1` so the entrypoint skips its own Xvfb, and — the part everyone
misses — sets `NVIDIA_DRIVER_CAPABILITIES` to include `graphics`. The default
capability set is compute-only: without it the container gets CUDA and no GLX
vendor at all, and silently renders on the CPU.

Check which one you got. It is the only symptom, and it is easy to miss:

```bash
docker compose logs sim | grep 'OpenGL renderer'
```

`Tesla T4/PCIe/SSE2` is right. `llvmpipe` means you are paying for a GPU and
rendering on the CPU anyway.

## Credits and attribution

- **Terrain** — [ArcticDEM](https://www.pgc.umn.edu/data/arcticdem/) v4.1, Polar
  Geospatial Center, University of Minnesota, funded by the NSF. CC-BY-4.0.
- **Imagery** — © Mapbox © OpenStreetMap © Maxar, or © MapTiler © OpenStreetMap
  contributors, or Copernicus Sentinel-2 (ESA).
- **Simulator arrangement** — Gazebo Classic + gzweb + ArduPilot, extracted from
  [Damn Vulnerable Drone](https://github.com/nicholasaleks/Damn-Vulnerable-Drone)
  with ROS removed.
