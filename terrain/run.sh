#!/usr/bin/env bash
# Terrain pipeline, run inside the GDAL container by the `terrain` compose
# service. Kept as a file rather than an inline compose command because YAML
# folded scalars silently preserve newlines on indented continuation lines,
# which turns arguments into separate commands.
#
# Two sources of sites, in priority order:
#   1. /sites.conf   — a list; every entry is built (existing ones skipped)
#   2. SITE_* env    — a single site, when no sites.conf is mounted
#
# Rebuild decisions are made per site, at three levels:
#   terrain    lat/lon/extent/grid changed   -> full rebuild, re-downloads
#   scenario also covers the vessel course: it is planned from the DEM that
#              is already on disk, so a new route costs seconds, not a re-download
#   scenario   ship/fog/physics changed      -> regenerate the world only, seconds
#
# Without the scenario level, editing FOG or SHIP_START in .env and running
# `docker compose up` reports "up to date, skipping" and silently keeps the old
# world — the settings reach the container but never reach the world file.
set -euo pipefail

# Explicit on/off. `${FOG:+--fog}` only tests for non-empty, so FOG=0 would
# still switch fog ON — a trap worth closing before anyone sets it.
on() { case "${1:-}" in 1|true|yes|on|TRUE|True) return 0;; *) return 1;; esac; }

# Everything that changes the generated world but not the downloaded data.
scenario_sig() {
    printf '%s|' \
        "${SHIP:-}" "${SHIP_MOVING:-}" "${SHIP_SPEED:-}" "${SHIP_MODEL:-}" \
        "${SHIP_START:-}" "${SHIP_START_SEED:-}" "${SHIP_START_AT:-}" \
        "${COURSE_SEED:-}" "${COURSE_LENGTH:-}" \
        "${FOG:-}" "${FOG_DENSITY:-}" "${FOG_COLOUR:-}" "${FOG_TYPE:-}" \
        "${PHYSICS_RATE:-}" "${SHADOWS:-}" "${CAMERAS:-}"
    # Every ASSET_N, sorted so the signature does not depend on
    # the order the environment happens to enumerate them in.
    { env | grep -E "^ASSET_[0-9]+=" || true; } | sort | tr "\n" "|"
    echo
}

make_world_only() {
    local name="$1"
    # A lat/lon is only meaningful for the site it was picked in. Applying it to
    # every entry in sites.conf sent the other worlds hundreds of km off-map.
    local start_at=""
    if [[ "${name}" == "${SITE_NAME:-fort_ross}" ]]; then
        start_at="${SHIP_START_AT:-}"
    fi
    local scenario=()
    on "${SHIP:-}"        && scenario+=(--ship)
    on "${SHIP_MOVING:-}" && scenario+=(--ship-moving)
    on "${FOG:-}"         && scenario+=(--fog)
    on "${SHADOWS:-}"     && scenario+=(--shadows)
    scenario+=(--cameras "${CAMERAS:-live}")

    # A lat/lon is only meaningful for the site it was picked in, so the
    # roster is hidden from every other entry in sites.conf. Done in a subshell:
    # unsetting in place would strip ASSET_* for every site built afterwards.
    (
    if [[ "${name}" != "${SITE_NAME:-fort_ross}" ]]; then
        while IFS= read -r v; do [[ -n "${v}" ]] && unset "${v}"; done \
            < <(env | grep -oE "^ASSET_[0-9]+" | sort -u || true)
    fi
    ARCTIC_OUT="/out/${name}" ARCTIC_OUT_ROOT="/out/${name}" \
        python3 /terrain/make_world.py \
            --name "${name}" \
            --physics-rate "${PHYSICS_RATE:-250}" \
            --ship-start "${SHIP_START:-fixed}" \
            --ship-start-seed "${SHIP_START_SEED:-0}" \
            --ship-start-at "${start_at}" \
            --course-seed "${COURSE_SEED:-0}" \
            --course-length "${COURSE_LENGTH:-9000}" \
            ${scenario[@]+"${scenario[@]}"}
    )
    scenario_sig > "/out/${name}/scenario.sig"
}

build_one() {
    local name="$1" lat="$2" lon="$3" extent="$4" grid="${5:-}"
    local world="/sim/worlds/${name}.world"
    local meta="/out/${name}/terrain.json"
    local sig="/out/${name}/scenario.sig"

    if [[ -f "${world}" && -f "${meta}" && -z "${FORCE_TERRAIN:-}" ]]; then
        local why
        why=$(python3 - "${meta}" "${lat}" "${lon}" "${extent}" "${grid}" <<'PY'
import json, sys
meta, lat, lon, extent, grid = sys.argv[1:6]
m = json.load(open(meta))
d = []
if abs(m["location"]["lat"] - float(lat)) > 1e-6: d.append("latitude")
if abs(m["location"]["lon"] - float(lon)) > 1e-6: d.append("longitude")
if abs(m["extent_m"] - float(extent)) > 0.5:      d.append("extent")
if grid and int(grid) != m["grid"]:               d.append("grid")
print(", ".join(d))
PY
)
        if [[ -z "${why}" ]]; then
            # Terrain is current. Has the scenario moved underneath it?
            # An unpinned random start has to be re-rolled each run, or the
            # signature matches, the world is reused, and "random" silently
            # means "whatever was generated the first time".
            local unpinned=0
            if [[ "${SHIP_START:-fixed}" == "random" && \
                  ( -z "${SHIP_START_SEED:-}" || "${SHIP_START_SEED}" == "0" ) ]]; then
                unpinned=1
            fi
            if [[ -z "${COURSE_SEED:-}" || "${COURSE_SEED}" == "0" ]]; then
                unpinned=1
            fi
            # An explicit position is deterministic by definition.
            [[ -n "${SHIP_START_AT:-}" ]] && unpinned=0
            if [[ ${unpinned} -eq 0 ]] && [[ -f "${sig}" ]] \
               && [[ "$(cat "${sig}")" == "$(scenario_sig)" ]]; then
                echo "[terrain] ${name}: up to date, skipping"
                return 0
            fi
            echo "[terrain] ${name}: scenario changed — regenerating world only"
            make_world_only "${name}"
            return 0
        fi
        echo "[terrain] ${name}: ${why} changed since last build — rebuilding"
    fi

    echo "[terrain] building ${name} at ${lat}, ${lon} (${extent} m)"
    local gridarg=()
    [[ -n "${grid}" ]] && gridarg=(--grid "${grid}")

    ARCTIC_OUT="/out/${name}" ARCTIC_OUT_ROOT="/out/${name}" \
    python3 /terrain/build_terrain.py \
        --name "${name}" \
        --lat "${lat}" --lon "${lon}" \
        --extent "${extent}" ${gridarg[@]+"${gridarg[@]}"} \
        --true-scale --sea-level-floor 0

    ARCTIC_OUT="/out/${name}" ARCTIC_OUT_ROOT="/out/${name}" \
        python3 /terrain/gen_imagery.py

    make_world_only "${name}"
}

if [[ -f /sites.conf ]]; then
    echo "[terrain] reading /sites.conf"
    while read -r name lat lon extent grid _rest; do
        [[ -z "${name:-}" || "${name}" == \#* ]] && continue
        build_one "${name}" "${lat}" "${lon}" "${extent}" "${grid:-}"
    done < <(sed 's/#.*//' /sites.conf)
else
    build_one "${SITE_NAME:-fort_ross}" "${SITE_LAT}" "${SITE_LON}" \
              "${SITE_EXTENT:-6500}" "${SITE_GRID:-}"
fi

ACTIVE="${SITE_NAME:-fort_ross}"
if [[ ! -f "/sim/worlds/${ACTIVE}.world" ]]; then
    echo "[terrain] ERROR: SITE_NAME=${ACTIVE} is not in sites.conf" >&2
    echo "[terrain] built worlds:" >&2
    ls -1 /sim/worlds/*.world 2>/dev/null | xargs -n1 basename 2>/dev/null | sed 's/^/  /' >&2
    exit 1
fi
echo "[terrain] active site: ${ACTIVE}"
