"""The asset roster: ASSET_1, ASSET_2, ... from the environment.

One line per thing you control:

    ASSET_1=copter,quad1,71.995670,-94.838931
    ASSET_2=tower,tower_n,71.995805,-94.838554

Fields are type, name, lat, lon and an optional heading. Position may be left
blank to auto-place, and `xy:` in front of the coordinates means world metres
instead of lat/lon.

The heading is either a true bearing in degrees, or `>lat,lon` to point the
asset at somewhere — useful for lining an aircraft up with a runway:

    ASSET_3=plane,fixed-wing,71.998195,-94.841967,>71.997790,-94.846245

The NAME must be one of the roles in SLOTS below, because each role is a real
service in docker-compose.yml. The role -- not the position in the roster --
fixes the address, so reordering or renumbering ASSET_N never moves an asset
onto a different IP than its container was given:

    IP            10.23.0.100 + slot     its own container on the compose net
    MAVLink       5760 / 14550           inside that container, always stock
    host ports    5760+10s / 14550+10s   because host ports cannot collide
    FDM           9002+10s / 9003+10s    ports on the *sim* container, so these
                                         must stay unique across the fleet

The target vessel is not an asset. It is what the teams have to find, so it
stays under SHIP_* and nobody gets a MAVLink endpoint to it.
"""
import re

# type -> what ArduPilot calls it, and what Gazebo model backs it.
#
# `frame` is passed to sim_vehicle.py -f. `model` is the SITL backend: frames
# marked external in ArduPilot's vehicleinfo already imply gazebo, but the
# tracker frame is not, so it needs --model gazebo spelled out.
TYPES = {
    "copter": {"vehicle": "ArduCopter", "frame": "gazebo-iris",
               "sim_model": None, "gz_model": "iris_with_ardupilot",
               "kind": "vehicle"},
    "plane":  {"vehicle": "ArduPlane", "frame": "gazebo-zephyr",
               "sim_model": None, "gz_model": "skywalker_x8",
               "kind": "vehicle"},
    # Tracked skid-steer ground rover. `rover-skid` rather than
    # `gazebo-rover` because the skid frame is what carries
    # ArduPilot's skid-steer defaults, and --model gazebo (below) is what
    # actually selects the FDM backend -- the frame's own model hint is
    # overridden either way.
    "rover":  {"vehicle": "Rover", "frame": "rover-skid",
               "sim_model": "gazebo", "gz_model": "rover_core",
               "kind": "vehicle"},
    # A boat is a Rover with FRAME_CLASS=2 (sitl/params/boat.parm); ArduPilot has
    # no boat vehicle. Declared so that param file is reachable at all -- the
    # entrypoint loads /params/<type>.parm, so with `boat` missing from this
    # table boat.parm could never load. The model is still not bundled: an asset
    # of this type gets the "model not bundled" warning from place_assets.
    "boat":   {"vehicle": "Rover", "frame": "rover-skid",
               "sim_model": "gazebo", "gz_model": "usv_camera_boat",
               "kind": "vehicle"},
    "tower":  {"vehicle": "AntennaTracker", "frame": "tracker",
               "sim_model": "gazebo", "gz_model": None,
               "kind": "tower"},
}

# Fixed address table, keyed by ROLE.
#
# These roles are declared as real services in docker-compose.yml, so plain
# `docker compose up --build` starts them with no generation step. That only
# stays true if the two agree, which is why the allocation is keyed by name
# rather than by position in the roster: reordering ASSET_N must not silently
# move an asset onto a different IP than the one its container was given.
#
# Adding a role means adding it here AND adding the matching service block.
# Slot 2 stays reserved for the `boat` role that .env.example and the README
# already publish at 10.23.0.102 — the ground rover took 5 rather than quietly
# moving an address someone may already have pointed a GCS at.
SLOTS = {
    "quadcopter": 0,
    "fixed-wing": 1,
    "tower-1":    3,
    "tower-2":    4,
    "rover":      5,
}

FDM_BASE = 9002       # port on the SIM container; must be unique per asset
IP_PREFIX = "10.23.0."
IP_BASE = 100
STRIDE = 10

# Inside its own container every asset runs as ArduPilot instance 0, so it uses
# the stock ports -- MAVLink TCP 5760, GCS UDP 14550 -- on its own IP. Only the
# FDM ports must differ, because they are all ports on the sim container, and
# those are overridden with --sim-port-in/--sim-port-out. Host ports keep the
# stride only because host ports cannot collide.
TCP_PORT = 5760
GCS_PORT = 14550
HOST_TCP_BASE = 5760
HOST_GCS_BASE = 14550

# MJPEG camera feeds. These live on the SIM container (that is where the sensors
# render), so like the FDM ports they must be unique across the fleet.
CAM_BASE = 8600


def address(slot):
    """Every port and address an asset gets, derived from its role slot."""
    return {
        "slot": slot,
        "ip": f"{IP_PREFIX}{IP_BASE + slot}",
        "fdm_port": FDM_BASE + STRIDE * slot,
        "fdm_port_out": FDM_BASE + STRIDE * slot + 1,
        "tcp_port": TCP_PORT,
        "gcs_port": GCS_PORT,
        "host_tcp": HOST_TCP_BASE + STRIDE * slot,
        "host_gcs": HOST_GCS_BASE + STRIDE * slot,
        "cam_port": CAM_BASE + STRIDE * slot,
    }


def parse_assets(env):
    """Environment mapping -> ordered list of asset dicts.

    Indices need not be contiguous — ASSET_1, ASSET_2, ASSET_7 is fine and
    sorts numerically, so deleting a line does not silently renumber the ones
    above it into different ports.
    """
    found = []
    for key, val in env.items():
        m = re.fullmatch(r"ASSET_(\d+)", key.strip())
        if m and (val or "").strip():
            found.append((int(m.group(1)), val.strip()))
    # A repeated ASSET_N is invisible otherwise: the environment keeps only the
    # last assignment, so an earlier asset silently vanishes from the world.
    from collections import Counter
    for idx, count in Counter(i for i, _ in found).items():
        if count > 1:
            print(f"  WARNING: ASSET_{idx} appears {count} times; "
                  f"only the last is kept — renumber the others")
    found.sort(key=lambda kv: kv[0])

    assets, seen = [], set()
    for idx, spec in found:
        bits = [b.strip() for b in spec.split(",")]
        if len(bits) < 2:
            print(f"  WARNING: ASSET_{idx}={spec!r} needs at least 'type,name'; skipped")
            continue
        kind, name = bits[0].lower(), bits[1]
        if kind not in TYPES:
            print(f"  WARNING: ASSET_{idx}: unknown type {kind!r} "
                  f"(known: {', '.join(sorted(TYPES))}); skipped")
            continue
        if name not in SLOTS:
            print(f"  WARNING: ASSET_{idx}: {name!r} is not a declared role "
                  f"(known: {', '.join(SLOTS)}); skipped. Add a service block "
                  f"in docker-compose.yml and an entry in fleet.SLOTS first.")
            continue
        if name in seen:
            print(f"  WARNING: ASSET_{idx}: duplicate name {name!r}; skipped")
            continue
        seen.add(name)

        # 'xy:-31,698' arrives split across two fields because the separator is
        # the same comma. Position is always exactly two fields, so anything
        # after them is the heading.
        pos_bits = bits[2:4]
        head_bits = bits[4:]
        if pos_bits and not pos_bits[0]:
            # blank position: 'type,name,,<heading>'
            pos_bits, head_bits = [], [b for b in bits[3:] if b]
        point = ",".join(b for b in pos_bits if b)
        heading = ",".join(b for b in head_bits if b)
        spec_t = TYPES[kind]
        assets.append({
            "index": idx, "type": kind, "name": name, "point": point,
            "heading": heading,
            "container": f"arctic-sim-{name}",
            **address(SLOTS[name]), **spec_t,
        })
    return assets


def describe(a):
    return (f"ASSET_{a['index']} {a['type']:6s} {a['name']:12s} "
            f"{a['ip']:12s} fdm={a['fdm_port']} host tcp={a['host_tcp']} "
            f"udp={a['host_gcs']}")
