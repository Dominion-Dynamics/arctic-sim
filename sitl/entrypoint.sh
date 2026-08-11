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

echo "[${name}] ${vehicle} (${type}) at ${ip}"
echo "[${name}] home=${lat},${lon},${alt} world=${WORLD_NAME} gazebo=${GAZEBO_HOST}"
echo "[${name}] MAVLink tcp:5760  GCS udp:14550  FDM ${fdm_in}/${fdm_out}"

# Wait for this asset's own ArduPilotPlugin to be listening before SITL probes.
for i in $(seq 1 90); do
    if (echo > /dev/udp/${GAZEBO_HOST}/${fdm_in}) >/dev/null 2>&1; then break; fi
    sleep 2
done

# ArduPilot runs lockstep with Gazebo. If SCHED_LOOP_RATE exceeds what the host
# can actually step, every PreArm check fails with "Main loop slow" and the
# vehicle will not arm. Generated here so it always matches the world.
RATE="${PHYSICS_RATE:-250}"
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
    --custom-location="${lat},${lon},${alt},${HOME_DIR:-0}"
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
