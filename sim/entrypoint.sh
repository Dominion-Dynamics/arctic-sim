#!/usr/bin/env bash
# Boot Gazebo Classic headless, then gzweb in front of it.
#
# Models and worlds are bind-mounted, so gzweb's asset conversion runs here at
# startup rather than at image build time. That keeps the image independent of
# whichever terrain you generated.
# No -u: gazebo's own setup.sh dereferences LD_LIBRARY_PATH before setting it,
# and nvm.sh does the same. Defaults are supplied explicitly below instead.
set -eo pipefail

WORLD_NAME="${WORLD_NAME:-fort_ross}"
WORLD="/sim/worlds/${WORLD_NAME}.world"

export NVM_DIR="$HOME/.nvm"
# shellcheck disable=SC1090
. "$NVM_DIR/nvm.sh" && nvm use 10 >/dev/null

if [[ ! -f "${WORLD}" ]]; then
    echo "ERROR: ${WORLD} not found." >&2
    echo "Generate terrain first:  make terrain" >&2
    ls -1 /sim/worlds/ 2>/dev/null | sed 's/^/  available: /' >&2
    exit 1
fi

echo "[sim] world: ${WORLD}"
grep -o '<latitude_deg>[^<]*' "${WORLD}" | head -1 | sed 's/^/[sim] /' || true

# Gazebo needs a display even headless: the heightmap is built by the rendering
# stack, and without a display the terrain silently fails to load.
#
# Two ways to get one, and they are not equivalent. Xvfb is a software
# framebuffer — OGRE ends up on llvmpipe, which is fine when CAMERAS=off but is
# the difference between usable and unusable once camera sensors render every
# frame. USE_HOST_X borrows an Xorg server running on a real GPU outside this
# container (see deploy/aws/), which is the only way OGRE 1.9 reaches the card:
# it speaks GLX, so an EGL-only headless context is not an option.
if [[ "${USE_HOST_X:-0}" == "1" ]]; then
    export DISPLAY="${HOST_DISPLAY:-:0}"
    echo "[sim] using the host X server on ${DISPLAY}"
    for _ in $(seq 1 60); do
        xdpyinfo >/dev/null 2>&1 && break
        sleep 0.5
    done
    if ! xdpyinfo >/dev/null 2>&1; then
        echo "ERROR: no X server on ${DISPLAY}." >&2
        echo "  /tmp/.X11-unix must be bind-mounted and the host's X server up:" >&2
        echo "      systemctl status arctic-xorg" >&2
        exit 1
    fi
else
    export DISPLAY=:100
    rm -f /tmp/.X100-lock
    Xvfb :100 -screen 0 1600x1200x24 >/tmp/xvfb.log 2>&1 &
    for _ in $(seq 1 50); do
        xdpyinfo -display :100 >/dev/null 2>&1 && break
        sleep 0.2
    done
fi

# Which GL implementation won is invisible everywhere else — a GPU box that
# quietly fell back to software looks identical in every other log line, just
# slower. Say it once, at startup, where it can be read back later.
glxinfo -B 2>/dev/null | grep -E "OpenGL (vendor|renderer) string" \
    | sed 's/^/[sim] /' || echo "[sim] glxinfo unavailable; GL vendor unknown"

source /usr/share/gazebo/setup.sh

# Convert models/worlds into gzweb's client assets. Skipped when already done,
# since it is slow and the result is cached in the bind mount.
STAMP="/sim/gzweb/http/client/assets/.stamp-${WORLD_NAME}"
if [[ ! -f "${STAMP}" ]] || [[ "${GZWEB_REDEPLOY:-0}" == "1" ]]; then
    echo "[sim] converting models for gzweb (first run, ~1-2 min)..."
    cd /sim/gzweb
    ./deploy.sh -m local >/tmp/gzweb-deploy.log 2>&1 || {
        echo "[sim] WARNING: gzweb deploy reported errors; last lines:" >&2
        tail -20 /tmp/gzweb-deploy.log >&2
    }
    mkdir -p "$(dirname "${STAMP}")" && date > "${STAMP}"
fi

# Keep the browser fog in step with the world's <scene><fog>. gzweb has no fog
# support of its own, so the value is baked into its bundle at startup rather
# than read from the scene message.
FOG_D=$(grep -oE "<density>[0-9.]+</density>" "${WORLD}" | head -1 |
        grep -oE "[0-9.]+" || true)
BUNDLE=/sim/gzweb/http/client/gz3d.gui.js
if [[ -n "${FOG_D:-}" && -f "${BUNDLE}" ]]; then
    sed -i "s|[0-9.]* /\*ARCTIC_FOG_DEFAULT\*/|${FOG_D} /*ARCTIC_FOG_DEFAULT*/|" "${BUNDLE}"
    echo "[sim] browser fog density set to ${FOG_D} (from the world file)"
elif [[ -f "${BUNDLE}" ]]; then
    sed -i "s|[0-9.]* /\*ARCTIC_FOG_DEFAULT\*/|0 /*ARCTIC_FOG_DEFAULT*/|" "${BUNDLE}"
    echo "[sim] no fog in world; browser fog disabled"
fi

# Inject the control panel into gzweb's generated page. Done here rather than
# in the repo because index.html is produced by gzweb's deploy step.
INDEX=/sim/gzweb/http/client/index.html
if [[ -f "${INDEX}" ]] && ! grep -q "arctic-control-panel" "${INDEX}"; then
    # Console capture goes in <head>, ahead of gzweb's own scripts, or the
    # browser tab would only ever show what was logged after the panel loaded.
    python3 - "${INDEX}" <<PYEOF
import sys
p = sys.argv[1]
s = open(p).read()
cap = """<!-- arctic-control-panel -->\n<script>window.BROWSER=[];(function(){var L=['log','info','warn','error'];L.forEach(function(lv){var o=console[lv];console[lv]=function(){var d=new Date();function p(n){return (n<10?'0':'')+n;}try{window.BROWSER.push({t:p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds()),lv:lv,m:Array.prototype.map.call(arguments,function(a){try{return typeof a==='string'?a:JSON.stringify(a);}catch(e){return String(a);}}).join(' ')});if(window.BROWSER.length>800)window.BROWSER.shift();}catch(e){}o.apply(console,arguments);};});window.addEventListener('error',function(e){console.error(e.message+' ('+e.filename+':'+e.lineno+')');});})();</script>"""
# Both the API base and the script URL come from location.hostname. Hardcoding
# localhost for the src worked only because the browser and the sim were the
# same machine; over a tailnet it asks the *viewer's* laptop for panel.js and
# the 3D view loads with no panel at all.
tag = ("<script>window.__AS_API='http://'+location.hostname+':${CONTROL_PORT:-8090}';"
       "(function(){var s=document.createElement('script');"
       "s.src=window.__AS_API+'/panel.js';s.defer=true;"
       "document.head.appendChild(s);})();</script>")
s = s.replace("<head>", "<head>\n" + cap, 1)
s = s.replace("</body>", tag + "\n</body>", 1)
open(p, 'w').write(s)
PYEOF
    echo "[sim] control panel injected into gzweb page"
fi

echo "[sim] starting gzserver..."
cd /sim
gzserver --verbose "${WORLD}" &
GZ_PID=$!

# gzweb's bridge connects to gzserver's master; give it time to advertise.
sleep 8
if ! kill -0 ${GZ_PID} 2>/dev/null; then
    echo "ERROR: gzserver exited during startup" >&2
    exit 1
fi

echo "[sim] starting gzweb on :8080"
cd /sim/gzweb
npm start &

trap 'kill ${GZ_PID} 2>/dev/null; exit 0' TERM INT
wait ${GZ_PID}
