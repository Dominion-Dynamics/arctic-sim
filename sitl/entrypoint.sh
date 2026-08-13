#!/usr/bin/env bash
# Start the ONE ArduPilot SITL instance this container owns.
#
# Which asset that is comes from ASSET_NAME; everything about it comes from the
# FLEET roster in out/<world>/sim.env, written by terrain/make_world.py from the
# ASSET_N settings. The roster is never re-derived here, so the FDM ports baked
# into each Gazebo model cannot drift from the ports SITL actually uses.
#
# The asset always runs as ArduPilot instance 0, so inside this container it
# uses the stock ports: MAVLink TCP 5760, GCS UDP 14550. That is the point of
# one container per asset — no stride arithmetic to find your vehicle, just its
# address. Only the FDM ports have to be unique across the fleet, because they
# are all ports on the sim container, and those are set explicitly with
# --sim-port-in / --sim-port-out rather than by instance number.
#
# Home must match the world's <spherical_coordinates>, or the EKF origin and the
# terrain disagree and the vehicle appears to fly off the map.
set -uo pipefail

WORLD_NAME="${WORLD_NAME:-fort_ross}"
GAZEBO_HOST="${GAZEBO_HOST:-10.23.0.5}"
ASSET_NAME="${ASSET_NAME:-}"

if [[ -z "${ASSET_NAME}" ]]; then
    echo "[sitl] ERROR: ASSET_NAME is not set." >&2
    echo "  This image runs one asset per container; the service block in" >&2
    echo "  docker-compose.yml is what sets it. Use \`docker compose up\`." >&2
    exit 1
fi

SITE_ENV="/out/${WORLD_NAME}/sim.env"
if [[ ! -f "${SITE_ENV}" ]]; then
    echo "[${ASSET_NAME}] ERROR: ${SITE_ENV} missing; build the world first." >&2
    exit 1
fi
# shellcheck disable=SC1090
source "${SITE_ENV}"

# Find this container's row in the roster.
ROW=""
IFS=';' read -ra ENTRIES <<< "${FLEET:-}"
for e in "${ENTRIES[@]}"; do
    if [[ "${e%%,*}" == "${ASSET_NAME}" ]]; then ROW="${e}"; break; fi
done
if [[ -z "${ROW}" ]]; then
    # Every role has a service block, but only the ones listed in ASSET_N are
    # actually flying. Idle rather than exit: `restart: unless-stopped` would
    # turn a clean exit into a crash loop, and a loud loop is worse than a
    # quiet container that says exactly why it has nothing to do.
    echo "[${ASSET_NAME}] not in the roster for ${WORLD_NAME} — idle."
    echo "[${ASSET_NAME}] To fly it, add a line to .env and rebuild:"
    echo "[${ASSET_NAME}]     ASSET_n=<type>,${ASSET_NAME},<lat>,<lon>"
    echo "[${ASSET_NAME}] Currently rostered: ${FLEET:-<none>}"
    exec sleep infinity
fi

IFS=',' read -r name type vehicle frame sim_model slot \
    fdm_in fdm_out ip lat lon alt <<< "${ROW}"

# --- Geodetic reference: the WORLD ORIGIN, not this asset's spawn point ------
#
# ArduPilotPlugin reports the model's pose in world coordinates, measured from
# the world origin. SITL takes that FDM position as an offset from --home. So
# --home must be the lat/lon OF THE WORLD ORIGIN, or every reported position is
# out by however far the asset spawned from it.
#
# Passing the asset's own lat/lon here — which is the obvious thing to do, and
# what this did — puts that spawn offset into every coordinate twice. Measured
# on the quad: it spawned 719 m from the world origin and its reported position
# was 719 m adrift, while ArduPilot rated its own GPS healthy throughout.
#
# The world origin is the site centre, which the terrain build already records.
# Read it rather than deriving it, so a new site needs nothing added here.
#
# Altitude is 0 for the same reason: world z is measured from sea level (see the
# <elevation> comment in make_world.py) and the plugin sends it straight
# through, so a non-zero home altitude is added to a height that already
# includes it. That is what produced "Field Elevation Set: 152m" on terrain
# 76 m high.
ORIGIN_JSON="/out/${WORLD_NAME}/terrain.json"
if [[ -f "${ORIGIN_JSON}" ]]; then
    read -r home_lat home_lon < <(python3 -c "
import json
loc = json.load(open('${ORIGIN_JSON}')).get('location') or {}
print(loc.get('lat',''), loc.get('lon',''))
")
fi
if [[ -z "${home_lat:-}" || -z "${home_lon:-}" ]]; then
    # Falling back to the spawn point reintroduces the offset, so say so rather
    # than letting it look like a GPS or tuning problem later.
    echo "[${name}] WARNING: no location in ${ORIGIN_JSON}; falling back to this"
    echo "[${name}]          asset's spawn point as the geodetic reference."
    echo "[${name}]          Reported positions will be offset by the distance"
    echo "[${name}]          from the world origin to the spawn point."
    home_lat="${lat}"; home_lon="${lon}"; home_alt="${alt}"
else
    home_alt=0
fi

echo "[${name}] ${vehicle} (${type}) at ${ip}"
echo "[${name}] spawn=${lat},${lon},${alt}  home(world origin)=${home_lat},${home_lon},${home_alt}"
echo "[${name}] world=${WORLD_NAME} gazebo=${GAZEBO_HOST}"
echo "[${name}] MAVLink tcp:5760  GCS udp:14550  FDM ${fdm_in}/${fdm_out}"

# Wait for this asset's own ArduPilotPlugin to be listening before SITL probes.
for i in $(seq 1 90); do
    if (echo > /dev/udp/${GAZEBO_HOST}/${fdm_in}) >/dev/null 2>&1; then break; fi
    sleep 2
done

# If SCHED_LOOP_RATE exceeds what SITL can actually achieve, every PreArm check
# fails with "Main loop slow" and the vehicle will not arm.
#
# Not lockstep, despite what this comment used to claim: this fork of
# ardupilot_gazebo has no lock_step support at all (grep the plugin source).
# Gazebo free-runs its own clock and SITL keeps up as best it can, which is why
# the achieved rate is a fraction of the world rate rather than equal to it.
#
# It must NOT simply match the world, which is what this used to do. The FDM
# exchange is a synchronous round trip — SITL sends servo output and then blocks
# until the plugin answers, and the plugin only answers on its next world step.
# Measured on the wire inside the container, servo out and FDM in are exactly
# 1:1, so the loop rate IS the round-trip rate. Each iteration therefore pays
# the fraction of a step it spends waiting for the next step boundary (~half a
# step on average) plus ~1 ms of bridge networking, and SITL lands strictly
# below the world rate no matter how idle the host is.
#
# Measured on g4dn.2xlarge: a 250 Hz world sustains ~205 Hz of round trips.
# Setting the two equal is therefore self-defeating — it guarantees
# "PreArm: Main loop slow (205Hz < 250Hz)" and no amount of CPU or GPU fixes it.
# Chasing it by changing PHYSICS_RATE moves both numbers and never converges,
# because the achieved rate is a fraction of whatever you ask for.
#
# So run the world FASTER than the loop. LOOP_RATE sets SCHED_LOOP_RATE
# independently; leave it unset and the old coupled behaviour applies, which is
# fine only while the loop rate is comfortably under what the world sustains
# (150 in a 250 Hz world never complained for exactly this reason).
RATE="${LOOP_RATE:-${PHYSICS_RATE:-250}}"
echo "SCHED_LOOP_RATE ${RATE}" > /tmp/rate.parm

# Every asset runs as instance 0 in its own container, so they would all take
# ArduPilot's default system ID and collide — a GCS attached to several would
# see one vehicle flickering between them. The roster slot is already unique,
# so use it. slot+1 keeps the first asset at the conventional sysid 1.
SYSID=$((slot + 1))
echo "SYSID_THISMAV ${SYSID}" >> /tmp/rate.parm
echo "[${name}] SCHED_LOOP_RATE=${RATE}  SYSID_THISMAV=${SYSID}"

cd "$HOME/ardupilot"

ARGS=(
    -v "${vehicle}"
    -f "${frame}"
    -I 0
    --sim-address="${GAZEBO_HOST}"
    --custom-location="${home_lat},${home_lon},${home_alt},${HOME_DIR:-0}"
    --speedup="${SPEEDUP:-1}"
    --no-rebuild
    # Instance 0 would default to FDM 9002/9003. Override so this asset talks to
    # its own plugin: SITL sends servo to Gazebo's fdm_port_in, and binds the
    # port Gazebo sends state back on.
    -A "--sim-port-out ${fdm_in} --sim-port-in ${fdm_out}"
)

# The stock `tracker` frame is not marked external, so on its own SITL would
# simulate the mount internally and never talk to Gazebo. --model gazebo swaps
# in the FDM backend; the model table in SITL_cmdline.cpp is shared by every
# vehicle binary, so this works even though ArduPilot ships no gazebo-tracker.
[[ -n "${sim_model}" ]] && ARGS+=(--model "${sim_model}")
# Per asset TYPE, not per vehicle: boat and rover are both ArduRover but want
# different parameters, and the type is what the roster actually carries.
[[ -f "/params/${type}.parm" ]] && ARGS+=(--add-param-file="/params/${type}.parm")

# LAST, so it wins. copter.parm ships SCHED_LOOP_RATE 400; loaded after this it
# silently overrode the rate derived from PHYSICS_RATE, and every PreArm failed
# with "Main loop slow (150Hz < 400Hz)" no matter what .env said.
ARGS+=(--add-param-file=/tmp/rate.parm)

# MAVProxy is not optional: SITL's SERIAL0 blocks on "Waiting for connection ...."
# until a client attaches, so an instance with nothing connected never steps and
# never sends an FDM packet to Gazebo.
#
# --no-extra-ports disables MAVProxy's built-in 14550/14551, which send *to*
# 127.0.0.1 and are therefore unreachable from outside the container. `udpin`
# makes MAVProxy listen instead.
ARGS+=(--no-extra-ports --no-rcin
       --out=udpin:0.0.0.0:14550
       --out=udpin:0.0.0.0:14551
       -m "--non-interactive")

# Real exec, with no pipe: SITL becomes PID 1, so if it dies the container
# dies with it and `restart: unless-stopped` can do its job. Piping to sed
# would leave bash as PID 1 and the container alive but doing nothing --
# and Compose already prefixes every log line with the service name.
exec sim_vehicle.py "${ARGS[@]}" 2>&1
