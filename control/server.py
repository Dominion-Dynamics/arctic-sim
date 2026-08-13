#!/usr/bin/env python3
"""Control service for arctic-sim: reset, logs, and .env editing.

Serves a panel that is injected into the gzweb page, plus the API behind it.
Runs in its own container with the Docker socket mounted, because gzweb's own
server has no Docker access and no business gaining any.

Bound to 127.0.0.1 by compose. It can restart containers and rewrite .env, so it
must not be reachable off the machine.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GAZEBO_IP = os.environ.get("GAZEBO_IP", "10.23.0.5")
REPO = os.environ.get("REPO_DIR", "/repo")
PORT = int(os.environ.get("CONTROL_PORT", "8090"))
SIM = os.environ.get("SIM_CONTAINER", "arctic-sim")

# Only these may be edited from the browser. An allowlist keeps a typo in the
# editor from injecting arbitrary environment into the containers.
EDITABLE = {
    "SITE_NAME", "SITE_LAT", "SITE_LON", "SITE_EXTENT", "SITE_GRID",
    "IMAGERY_SOURCE", "IMAGERY_SIZE", "IMAGERY_MONTHS", "IMAGERY_CLOUD",
    "IMAGERY_DETAIL", "IMAGERY_DETAIL_STRENGTH", "TILE_ZOOM",
    "MAPBOX_TOKEN", "MAPTILER_KEY",
    "SHIP", "SHIP_MOVING", "SHIP_SPEED", "SHIP_MODEL",
    "SHIP_START", "SHIP_START_SEED", "SHIP_START_AT", 
    "COURSE_SEED", "COURSE_LENGTH",
    "FOG", "FOG_DENSITY", "FOG_COLOUR", "FOG_TYPE",
    "PHYSICS_RATE", "SPEEDUP", "VEHICLE", "FRAME", "VEHICLE_MODEL",
    "GZWEB_PORT", "CONTROL_PORT", "SITL_VEHICLES",
}

_busy = threading.Lock()
_status = {"state": "idle", "detail": ""}


def run(cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


LOG_LINE = re.compile(r"^(\S+)\s*\|\s*(\d{4}-\d\d-\d\dT[\d:.]+Z)\s?(.*)$")


def order_logs(raw: str) -> str:
    """Sort compose output by timestamp.

    `docker compose logs` emits one chunk per service in no fixed order, so each
    poll returned the same lines arranged differently and the panel appeared to
    flap. Sorting by the container timestamp gives a stable chronological view.
    Continuation lines carry the previous line's stamp so wrapped output stays
    attached to its parent.
    """
    entries, seq, last = [], 0, None
    for line in raw.splitlines():
        m = LOG_LINE.match(line)
        if m:
            svc, ts, msg = m.groups()
            last = ts
            entries.append((ts, seq, f"{svc:<14} {ts[11:19]}  {msg}"))
        elif line.strip():
            entries.append((last or "", seq, f"{'':<14} {'':<8}  {line}"))
        seq += 1
    entries.sort(key=lambda e: (e[0], e[1]))
    return "\n".join(e[2] for e in entries)


def read_env() -> str:
    path = os.path.join(REPO, ".env")
    return open(path).read() if os.path.exists(path) else ""


def existing_keys() -> set:
    keys = set()
    for line in read_env().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


def write_env(text: str) -> tuple[bool, str]:
    """Validate then write.

    Unknown keys are rejected, except ones already present in the file — those
    are kept, so the editor can round-trip an .env it did not author instead of
    refusing to save or silently deleting lines.
    """
    allowed = EDITABLE | existing_keys()
    out, bad = [], []
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        if "=" not in stripped:
            bad.append(f"not KEY=value: {stripped[:40]}")
            continue
        k, v = stripped.split("=", 1)
        k = k.strip()
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", k):
            bad.append(f"bad key name: {k[:40]}")
            continue
        # ASSET_N is open-ended by design — the roster grows as you add
        # vehicles and towers, so it cannot be a fixed allowlist entry.
        if k not in allowed and not re.fullmatch(r"ASSET_\d+", k):
            bad.append(f"not editable here: {k}")
            continue
        out.append(f"{k}={v.strip()}")
    if bad:
        return False, "; ".join(bad[:6])
    path = os.path.join(REPO, ".env")
    tmp = path + ".tmp"
    # Carry the old file's ownership and mode onto the replacement.
    #
    # os.replace swaps in a NEW inode, which belongs to whoever wrote it — root,
    # since this runs in the control container. Without the chown below, the
    # first save through the panel silently flips .env to root:root while every
    # other file in the repo stays with the login user. After that, editing it
    # over SSH needs sudo, and an rsync that does not exclude it fails outright
    # with a permission error that says nothing about the panel having been the
    # cause.
    #
    # Best effort on purpose. Docker Desktop's bind mounts synthesise ownership
    # and chown there is meaningless or fails; a container running as a non-root
    # user cannot chown at all. Neither is a reason to lose the save, and in
    # both of those cases the ownership flip does not happen anyway.
    try:
        st = os.stat(path)
    except FileNotFoundError:
        st = None
    with open(tmp, "w") as fh:
        fh.write("\n".join(out).rstrip() + "\n")
    if st is not None:
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
            os.chmod(tmp, st.st_mode & 0o7777)
        except OSError:
            pass
    os.replace(tmp, path)          # atomic: never leave a half-written .env
    return True, "saved"


def background(fn):
    def wrapper():
        with _busy:
            try:
                fn()
            finally:
                _status["state"] = "idle"
    threading.Thread(target=wrapper, daemon=True).start()


def asset_services():
    """Service names of the asset containers.

    Asked of compose rather than hardcoded here, so adding a role to
    docker-compose.yml cannot silently miss the reset/rebuild paths.
    """
    rc, out = run(["docker", "compose", "config", "--services"], timeout=60)
    if rc != 0:
        return []
    # terrain is a one-shot job; control is us, and restarting it would kill
    # the request being served before the caller hears the result.
    infra = {"terrain", "control"}
    return sorted(x for x in out.split() if x and x not in infra)


def mav_probe(host, port, timeout=1.5):
    """Is anything answering MAVLink at this address?

    The endpoints are MAVProxy `udpin` sockets — listeners — so a client has to
    transmit before it will receive anything. Sending a byte registers us as a
    peer; without it a perfectly healthy vehicle looks dead.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(b"\x00", (host, port))
        data, _ = s.recvfrom(2048)
        return bool(data) and data[0] in (0xFD, 0xFE)
    except Exception:
        return False
    finally:
        s.close()


def cam_probe(host, port, timeout=1.0):
    """Is a camera stream listening? A plain TCP connect — cheap, and enough to
    know whether the button should be live."""
    import socket
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except Exception:
        return False


def asset_roster():
    """Every declared role, whether it is rostered, and how to reach it."""
    import sys as _sys
    tdir = os.path.join(REPO, "terrain")
    if tdir not in _sys.path:
        _sys.path.insert(0, tdir)
    try:
        from fleet import SLOTS, address, parse_assets
    except Exception as e:
        return {"ok": False, "error": f"fleet.py unavailable: {e}"}

    env = {}
    for line in read_env().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    rostered = {a["name"]: a for a in parse_assets(env)}

    # Probe concurrently; serially this would take one timeout per role.
    import threading
    out, live = [], {}

    cams = {}

    def worker(role, ip, port):
        live[role] = mav_probe(ip, port)

    def camworker(role, port):
        # Cameras render in the sim container, so they answer there, not on the
        # asset's own IP — the per-asset relay does not exist yet.
        cams[role] = cam_probe(GAZEBO_IP, port)

    threads = []
    for role in SLOTS:
        a = address(SLOTS[role])
        # Reach each asset on the compose network at its own IP and stock port.
        # 127.0.0.1 here would be the control container itself; the host-side
        # published ports are not visible from in here.
        t = threading.Thread(target=worker,
                             args=(role, a["ip"], a["gcs_port"]))
        t.start()
        threads.append(t)
        c = threading.Thread(target=camworker, args=(role, a["cam_port"]))
        c.start()
        threads.append(c)
    for t in threads:
        t.join()

    for role, slot in SLOTS.items():
        a = address(slot)
        r = rostered.get(role)
        out.append({
            "name": role,
            "type": r["type"] if r else None,
            "rostered": bool(r),
            "ip": a["ip"],
            "tcp": a["host_tcp"],
            "udp": a["host_gcs"],
            "mavlink": live.get(role, False),
            "gcs": a["gcs_port"],
            "cam": a["cam_port"],
            "camera": cams.get(role, False),
        })
    return {"ok": True, "assets": out}


def do_reset():
    """Restart the simulation containers — a clean slate, not a world reset.

    `gz world -r` only resets poses and sim time; plugin state, SITL's EKF and
    gzweb's connection all survive it, so "reset" was never quite a reset.
    Restarting the containers puts everything back to boot state.

    `control` is deliberately excluded: restarting it would kill the request
    being served and the caller would never hear the result.
    """
    _status.update(state="working", detail="restarting sim and the fleet…")
    rc, out = run(["docker", "compose", "up", "-d", "--force-recreate"]
                  + asset_services(), timeout=900)
    _status["detail"] = ("restarted — gzweb takes ~1-2 min to come back, then "
                         "reload the page") if rc == 0 else f"restart failed: {out[-300:]}"


def do_rebuild():
    """Regenerate the world from .env, then restart the sim AND every asset.

    Anything in .env can move an asset: its coordinates, the roster itself, the
    site. All of it lands in the world file and in sim.env, and sim.env is only
    read at container boot — so a rebuild that restarts just the sim leaves the
    fleet believing the old layout.
    """
    _status.update(state="working", detail="regenerating terrain…")
    rc, out = run(["docker", "compose", "run", "--rm", "terrain"])
    if rc != 0:
        _status["detail"] = f"terrain failed: {out[-400:]}"
        return
    # The sim is recreated rather than restarted so gzweb reconverts any model
    # whose geometry changed.
    _status["detail"] = "restarting sim…"
    run(["docker", "compose", "rm", "-sf", "sim"], timeout=120)
    rc, out = run(["docker", "compose", "up", "-d", "sim"], timeout=300)
    if rc != 0:
        _status["detail"] = f"failed: {out[-300:]}"
        return

    # Every asset too, not just the sim. Each container reads its home position
    # and its FDM ports out of sim.env once, at boot. Move an asset's lat/lon in
    # .env and the world gets the new pose while SITL keeps the old EKF origin —
    # the vehicle then appears to fly off the map. Adding or removing an asset
    # likewise only takes effect when its container restarts.
    # --force-recreate, not restart. A restart re-runs the entrypoint but keeps
    # the container's environment as it was at creation, so .env edits like
    # PHYSICS_RATE never reach it; it also keeps SITL's eeprom.bin, and
    # --add-param-file only supplies defaults, which a stored value overrides.
    assets = [a for a in asset_services() if a != "sim"]
    if assets:
        _status["detail"] = f"recreating {len(assets)} asset(s)…"
        rc, out = run(["docker", "compose", "up", "-d", "--force-recreate"]
                      + assets, timeout=900)
    _status["detail"] = ("rebuilt — reload the page in ~2 min once gzweb has "
                         "reconverted assets") if rc == 0 else f"failed: {out[-300:]}"


PANEL_CSS = """
#as-bar{position:fixed;top:10px;right:10px;z-index:99999;display:flex;gap:6px;
 font:12px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
#as-bar button{background:rgba(18,26,31,.88);color:#dfe8ec;border:1px solid #33474f;
 border-radius:3px;padding:6px 11px;cursor:pointer;backdrop-filter:blur(8px)}
#as-bar button:hover{border-color:#5bc8e0;color:#fff}
#as-bar button:disabled{opacity:.5;cursor:default}
.as-panel{position:fixed;z-index:99998;background:rgba(12,18,22,.96);color:#dfe8ec;
 border:1px solid #33474f;border-radius:4px;display:none;flex-direction:column;
 backdrop-filter:blur(10px);font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
.as-panel.open{display:flex}
.as-head{display:flex;justify-content:space-between;align-items:center;
 padding:7px 10px;border-bottom:1px solid #23343b;font-weight:600}
.as-head span{letter-spacing:.06em;text-transform:uppercase;font-size:10px;color:#8fa6ae}
.as-head button{background:none;border:none;color:#8fa6ae;cursor:pointer;font-size:14px}
#as-console{left:10px;right:10px;bottom:10px;height:46vh}
.as-tabs{display:flex;gap:4px;margin-left:12px}
.as-tabs button{background:none;border:1px solid transparent;color:#6f858d;
 border-radius:3px;padding:2px 9px;cursor:pointer;font:inherit;font-size:11px}
.as-tabs button.on{color:#bfe6f2;border-color:#2c5a68;background:#12303a}
#as-filter{background:#0b1216;border:1px solid #23343b;color:#dfe8ec;border-radius:3px;
 padding:3px 8px;font:11px ui-monospace,Menlo,monospace;width:150px;margin-right:8px}
#as-filter:focus{outline:none;border-color:#3d8ba3}
#as-pick{position:fixed;z-index:100000;background:rgba(12,18,22,.97);color:#dfe8ec;
 border:1px solid #33474f;border-radius:4px;display:none;padding:9px 11px;min-width:250px;
 font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;box-shadow:0 8px 28px rgba(0,0,0,.5)}
#as-pick.open{display:block}
#as-pick .r{display:flex;justify-content:space-between;gap:14px}
#as-pick .k{color:#6f858d}
#as-pick .btns{display:flex;gap:5px;margin-top:8px;border-top:1px solid #23343b;padding-top:8px}
#as-pick button{flex:1;background:#12303a;color:#bfe6f2;border:1px solid #2c5a68;
 border-radius:3px;padding:4px 6px;cursor:pointer;font:11px ui-monospace,Menlo,monospace}
#as-pick button:hover{border-color:#5bc8e0;color:#fff}
#as-pick .hint{color:#5d7883;font-size:10px;margin-top:6px}
.as-t{color:#5d7883}.as-lv-warn{color:#e8c98a}.as-lv-error{color:#ff9b8a}
#as-log{flex:1;overflow:auto;margin:0;padding:8px 10px;white-space:pre-wrap;
 font-size:11px;color:#b9c9d0}
#as-env{right:10px;top:52px;width:min(560px,92vw);max-height:78vh}
#as-envtext{flex:1;min-height:44vh;background:#080d10;color:#dfe8ec;border:none;
 border-bottom:1px solid #23343b;padding:10px;font:12px/1.55 ui-monospace,Menlo,monospace;
 resize:vertical}
.as-foot{display:flex;gap:6px;align-items:center;padding:8px 10px}
.as-foot button{background:#12303a;color:#bfe6f2;border:1px solid #2c5a68;
 border-radius:3px;padding:6px 12px;cursor:pointer}
.as-foot button.primary{background:#1d5568;border-color:#3d8ba3;color:#fff}
.as-msg{flex:1;color:#8fa6ae;font-size:11px}
.as-msg.err{color:#ff9b8a}.as-msg.ok{color:#8fdca8}

#as-assets{position:fixed;right:10px;bottom:10px;z-index:99997;min-width:246px;
 background:rgba(12,18,22,.94);color:#dfe8ec;border:1px solid #33474f;border-radius:4px;
 backdrop-filter:blur(10px);font:11px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
#as-assets .as-head{padding:6px 10px}
#as-assets.min .as-rows{display:none}
.as-rows{padding:4px 0 6px}
.as-row{display:flex;align-items:center;gap:8px;padding:3px 10px}
.as-row:hover{background:rgba(91,200,224,.08)}
.as-dot{width:7px;height:7px;border-radius:50%;flex:0 0 auto;background:#3d4c53}
.as-dot.live{background:#57d38c;box-shadow:0 0 5px rgba(87,211,140,.8)}
.as-dot.idle{background:#4a5a62}
.as-nm{flex:1 1 auto;white-space:nowrap}
.as-row.off .as-nm{color:#6f858d}
.as-ty{color:#7d949c;font-size:10px}
.as-eye{background:none;border:1px solid transparent;border-radius:3px;cursor:pointer;
 padding:1px 4px;color:#8fa6ae;line-height:1;flex:0 0 auto}
.as-eye:hover{color:#bfe6f2;border-color:#2c5a68;background:#12303a}
.as-eye:disabled{opacity:.28;cursor:default}
.as-fol{background:none;border:1px solid transparent;border-radius:3px;cursor:pointer;
 padding:1px 4px;color:#8fa6ae;line-height:1;flex:0 0 auto}
.as-fol:hover{color:#bfe6f2;border-color:#2c5a68;background:#12303a}
.as-fol:disabled{opacity:.28;cursor:default}
.as-fol.on{color:#7fe0a0;border-color:#2f6b46;background:#102d1e}
.as-fol.on:hover{color:#a8f0c4}
.as-cam{background:none;border:1px solid transparent;border-radius:3px;cursor:pointer;
 padding:1px 4px;color:#8fa6ae;line-height:1;flex:0 0 auto}
.as-cam:hover{color:#ffd9a0;border-color:#6b5330;background:#2b2113}
.as-cam:disabled{opacity:.28;cursor:default}
.as-cli{background:none;border:1px solid transparent;border-radius:3px;cursor:pointer;
 padding:1px 4px;color:#8fa6ae;line-height:1;flex:0 0 auto}
.as-cli:hover{color:#c9b6ff;border-color:#4a3d6b;background:#1d1830}
.as-cli.ok{color:#7fe0a0;border-color:#2f6b46;background:#102d1e}
.as-note{padding:2px 10px 0;color:#7d949c;font-size:10px;min-height:13px}
"""

PANEL_JS = r"""
(function(){
  if (window.__asPanel) return; window.__asPanel = true;
  var API = window.__AS_API || (location.protocol+'//'+location.hostname+':__PORT__');
  function el(t,a,h){var e=document.createElement(t);for(var k in (a||{}))e.setAttribute(k,a[k]);
    if(h!==undefined)e.innerHTML=h;return e;}
  function css(t){var s=document.createElement('style');s.textContent=t;document.head.appendChild(s);}
  css(__CSS__);

  var bar = el('div',{id:'as-bar'});
  var bReset = el('button',{},'Reset');
  var bLog   = el('button',{},'Console');
  var bEnv   = el('button',{},'Settings');
  bar.appendChild(bReset); bar.appendChild(bLog); bar.appendChild(bEnv);
  document.body.appendChild(bar);

  var con = el('div',{id:'as-console',class:'as-panel'});
  var head = el('div',{class:'as-head'},'<span>console</span>');
  var tabs = el('div',{class:'as-tabs'});
  var tDock = el('button',{class:'on'},'docker');
  var tBrow = el('button',{},'browser');
  tabs.appendChild(tDock); tabs.appendChild(tBrow);
  head.appendChild(tabs);
  var fBox = el('input',{id:'as-filter',placeholder:'filter…',spellcheck:'false'});
  fBox.oninput = function(){ render(); };
  head.appendChild(el('div',{style:'flex:1'}));
  head.appendChild(fBox);
  head.appendChild(el('button',{},'×')).onclick=function(){toggle(con,false);};
  con.appendChild(head);
  var log = el('pre',{id:'as-log'},'');
  con.appendChild(log);
  document.body.appendChild(con);

  var source='docker';
  function setTab(which){
    source=which;
    tDock.className = which==='docker'?'on':'';
    tBrow.className = which==='browser'?'on':'';
    render();
  }
  tDock.onclick=function(){setTab('docker');};
  tBrow.onclick=function(){setTab('browser');};

  var env = el('div',{id:'as-env',class:'as-panel'});
  env.appendChild(el('div',{class:'as-head'},'<span>.env</span>'));
  env.querySelector('.as-head').appendChild(el('button',{},'×')).onclick=function(){toggle(env,false);};
  var ta = el('textarea',{id:'as-envtext',spellcheck:'false'});
  env.appendChild(ta);
  var foot = el('div',{class:'as-foot'});
  var msg = el('div',{class:'as-msg'},'');
  var bSave = el('button',{},'Save');
  var bApply= el('button',{class:'primary'},'Save & rebuild');
  foot.appendChild(msg); foot.appendChild(bSave); foot.appendChild(bApply);
  env.appendChild(foot);
  document.body.appendChild(env);

  function toggle(p,on){ p.classList.toggle('open', on===undefined ? !p.classList.contains('open') : on); }
  function api(path,opts){ return fetch(API+path, opts||{}).then(function(r){return r.json();}); }

  var dockerText='', logTimer=null;

  function hhmmss(d){
    function p(n){return (n<10?'0':'')+n;}
    return p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
  }
  function render(){
    var atBottom = log.scrollTop+log.clientHeight >= log.scrollHeight-40;
    var filt = fBox.value.trim().toLowerCase();
    if(source==='docker'){
      var t = dockerText;
      if(filt) t = t.split('\n').filter(function(l){
        return l.toLowerCase().indexOf(filt) >= 0; }).join('\n');
      log.textContent = t || '(no matching output)';
    }
    else{
      log.innerHTML = BROWSER.length
        ? BROWSER.filter(function(e){
            return !filt || (e.m+e.lv).toLowerCase().indexOf(filt) >= 0; })
          .map(function(e){
            return '<span class="as-t">'+e.t+'</span>  <span class="as-lv-'+e.lv+'">'+
                   e.m.replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];})+
                   '</span>';
          }).join('\n')
        : '(nothing logged by the page yet)';
    }
    if(atBottom) log.scrollTop=log.scrollHeight;
  }
  bLog.onclick=function(){
    var open=!con.classList.contains('open'); toggle(con,open);
    if(open){ pull(); logTimer=setInterval(pull,3000); }
    else if(logTimer){ clearInterval(logTimer); logTimer=null; }
  };
  function pull(){
    if(source==='browser'){ render(); return; }
    api('/api/logs?tail=400').then(function(d){
      dockerText=d.log||''; render();
    }).catch(function(e){ dockerText='control service unreachable: '+e; render(); });
  }

  bEnv.onclick=function(){
    var open=!env.classList.contains('open'); toggle(env,open);
    if(open) api('/api/env').then(function(d){ ta.value=d.env||''; setMsg('',''); });
  };
  function setMsg(t,cls){ msg.textContent=t; msg.className='as-msg'+(cls?' '+cls:''); }
  function save(then){
    setMsg('saving…','');
    api('/api/env',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({env:ta.value})}).then(function(d){
      if(!d.ok){ setMsg(d.error||'save failed','err'); return; }
      setMsg('saved','ok'); if(then) then();
    }).catch(function(e){ setMsg(''+e,'err'); });
  }
  bSave.onclick=function(){ save(); };
  bApply.onclick=function(){ save(function(){
    setMsg('rebuilding — this takes a few minutes','');
    api('/api/rebuild',{method:'POST'}).then(poll);
  });};

  bReset.onclick=function(){
    bReset.disabled=true; bReset.textContent='Resetting…';
    api('/api/reset',{method:'POST'}).then(function(){ poll(); })
      .catch(function(){ bReset.disabled=false; bReset.textContent='Reset'; });
  };

  /* ---- right-click to pick coordinates ---------------------------------
     Raycast into gzweb's own scene (exposed as the page global `scene`) and
     turn the hit into world XYZ plus real lat/lon. The world is georeferenced,
     so a picked point is a usable spawn coordinate for any asset. */
  var SITE=null;
  api('/api/site').then(function(d){ if(d && d.ok) SITE=d; }).catch(function(){});

  var pick = el('div',{id:'as-pick'});
  document.body.appendChild(pick);

  var D2R=Math.PI/180, R2D=180/Math.PI;
  var PS={a:6378137.0, e:0.081819190842621, latTs:70*D2R, lon0:-45*D2R};
  function ps2ll(x,y){
    var tc=Math.tan(Math.PI/4-PS.latTs/2)/
           Math.pow((1-PS.e*Math.sin(PS.latTs))/(1+PS.e*Math.sin(PS.latTs)),PS.e/2);
    var mc=Math.cos(PS.latTs)/Math.sqrt(1-PS.e*PS.e*Math.pow(Math.sin(PS.latTs),2));
    var t=Math.hypot(x,y)*tc/(PS.a*mc);
    var chi=Math.PI/2-2*Math.atan(t);
    var e2=PS.e*PS.e,e4=e2*e2,e6=e4*e2,e8=e4*e4;
    var lat=chi+(e2/2+5*e4/24+e6/12+13*e8/360)*Math.sin(2*chi)
               +(7*e4/48+29*e6/240+811*e8/11520)*Math.sin(4*chi)
               +(7*e6/120+81*e8/1120)*Math.sin(6*chi)
               +(4279*e8/161280)*Math.sin(8*chi);
    var lon=PS.lon0+Math.atan2(x,-y);
    return [lat*R2D, ((lon*R2D+540)%360)-180];
  }

  var last=null;
  function showPick(ev, p){
    var lat=null, lon=null;
    if(SITE && SITE.bounds3413){
      var b=SITE.bounds3413;
      var cx=(b.xmin+b.xmax)/2, cy=(b.ymin+b.ymax)/2;
      var ll=ps2ll(cx+p.x, cy+p.y); lat=ll[0]; lon=ll[1];
    }
    last={x:p.x,y:p.y,z:p.z,lat:lat,lon:lon};
    function row(k,v){return '<div class="r"><span class="k">'+k+'</span><span>'+v+'</span></div>';}
    var html = row('x', p.x.toFixed(1)+' m') + row('y', p.y.toFixed(1)+' m') +
               row('z', p.z.toFixed(2)+' m');
    if(lat!==null) html += row('lat', lat.toFixed(6)) + row('lon', lon.toFixed(6));
    html += '<div class="btns">'+
            '<button data-c="pose">SDF pose</button>'+
            '<button data-c="xyz">x y z</button>'+
            (lat!==null?'<button data-c="ll">lat,lon</button>':'')+
            '</div><div class="hint">right-click terrain to pick · esc to close</div>';
    pick.innerHTML=html;
    pick.style.left=Math.min(ev.clientX+8, innerWidth-280)+'px';
    pick.style.top =Math.min(ev.clientY+8, innerHeight-190)+'px';
    pick.classList.add('open');
  }
  pick.addEventListener('click', function(ev){
    var b=ev.target.closest('button'); if(!b||!last) return;
    var t = b.dataset.c==='pose'
        ? '<pose>'+last.x.toFixed(2)+' '+last.y.toFixed(2)+' '+last.z.toFixed(2)+' 0 0 0</pose>'
        : b.dataset.c==='xyz'
        ? last.x.toFixed(2)+' '+last.y.toFixed(2)+' '+last.z.toFixed(2)
        : last.lat.toFixed(6)+','+last.lon.toFixed(6);
    (navigator.clipboard ? navigator.clipboard.writeText(t)
      : Promise.reject()).then(function(){ b.textContent='copied'; },
      function(){
        // Clipboard API needs a secure context; fall back to a temp textarea.
        var ta=document.createElement('textarea'); ta.value=t; document.body.appendChild(ta);
        ta.select(); try{document.execCommand('copy');}catch(e){}
        document.body.removeChild(ta); b.textContent='copied';
      });
    setTimeout(function(){ b.textContent = b.dataset.c==='pose'?'SDF pose':
      (b.dataset.c==='xyz'?'x y z':'lat,lon'); }, 1200);
  });
  document.addEventListener('keydown', function(e){
    if(e.key==='Escape') pick.classList.remove('open'); });
  document.addEventListener('mousedown', function(e){
    if(!pick.contains(e.target)) pick.classList.remove('open'); }, true);

  function tryPick(ev){
    var sc = window.scene;
    if(!sc || !sc.camera || !sc.scene || typeof THREE === 'undefined') return false;
    var cv = sc.getDomElement ? sc.getDomElement() : document.querySelector('canvas');
    if(!cv || (ev.target !== cv && !cv.contains(ev.target))) return false;
    var r = cv.getBoundingClientRect();
    var ndc = new THREE.Vector2(
        ((ev.clientX-r.left)/r.width)*2-1, -((ev.clientY-r.top)/r.height)*2+1);
    var rc = new THREE.Raycaster();
    rc.setFromCamera(ndc, sc.camera);
    var hits = rc.intersectObjects(sc.scene.children, true);
    for(var i=0;i<hits.length;i++){
      if(hits[i].object && hits[i].object.visible !== false && hits[i].point){
        showPick(ev, hits[i].point); return true;
      }
    }
    return false;
  }

  // gzweb opens its own model menu from `mousedown` with which===3, not from
  // `contextmenu` — so cancelling contextmenu alone let its menu through.
  // Intercept in the capture phase and stop it reaching gzweb's jQuery handler.
  document.addEventListener('mousedown', function(ev){
    if(ev.button !== 2) return;
    if(pick.contains(ev.target)) return;
    if(tryPick(ev)){
      ev.preventDefault();
      ev.stopPropagation();
      ev.stopImmediatePropagation();
    }
  }, true);
  // Belt and braces: keep the browser's own menu away too.
  document.addEventListener('contextmenu', function(ev){
    var sc = window.scene;
    var cv = sc && sc.getDomElement ? sc.getDomElement() : document.querySelector('canvas');
    if(cv && (ev.target === cv || cv.contains(ev.target))){
      ev.preventDefault(); ev.stopPropagation();
    }
  }, true);

  function poll(){
    api('/api/status').then(function(d){
      if(d.state==='working'){ setMsg(d.detail||'working…',''); setTimeout(poll,2000); }
      else{
        setMsg(d.detail||'done', /fail|error/i.test(d.detail||'')?'err':'ok');
        bReset.disabled=false; bReset.textContent='Reset';
      }
    }).catch(function(){ bReset.disabled=false; bReset.textContent='Reset'; });
  }

  // ---- asset panel -------------------------------------------------------
  // Lists every declared role, whether it is rostered and answering MAVLink,
  // and lets you fly the camera to it. Model lookup uses gzweb's own
  // scene.getByName, which is keyed on the Gazebo model name — the same string
  // as the asset's role, so the roster and the scene cannot disagree.
  var EYE = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" '
    + 'stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8'
    + '-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';

  // A crosshair, deliberately distinct from the eye: the eye is a one-shot
  // jump, this one keeps the camera locked on while the asset moves.
  var CROSS = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" '
    + 'stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="7"/>'
    + '<path d="M12 1v4M12 19v4M1 12h4M19 12h4"/><circle cx="12" cy="12" r="1.5" '
    + 'fill="currentColor" stroke="none"/></svg>';

  var CAM = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" '
    + 'stroke="currentColor" stroke-width="2"><path d="M23 7l-7 5 7 5V7z"/>'
    + '<rect x="1" y="5" width="15" height="14" rx="2"/></svg>';

  var CLI = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" '
    + 'stroke="currentColor" stroke-width="2"><path d="M4 17l6-5-6-5"/>'
    + '<path d="M12 19h8"/></svg>';

  // Whatever host served this page, plus the asset's HOST port. Same rule the
  // camera buttons use, and correct from every vantage point: localhost on the
  // Docker host, a WireGuard peer on 10.99.0.1, a tailnet node on the MagicDNS
  // name. If you can load this page, that address reaches the assets too.
  //
  // This used to branch on where the page was served from and hand remote
  // viewers the container IP and the stock in-container port
  // (10.23.0.100:14550), on the theory that anyone not on localhost was
  // sitting on the compose network. Nobody is. The `arctic` bridge is internal
  // to the Docker host and is not routed off it — a remote viewer got an
  // address unreachable from anywhere they could stand, which is why the
  // camera buttons worked and these did not. Cameras never had the branch.
  //
  // Note a.udp vs a.gcs: a.udp is host_gcs (14550 + 10*slot), unique per
  // asset, whereas a.gcs is the stock in-container 14550 that EVERY asset
  // shares — each one runs as ArduPilot instance 0 on its own IP. Publishing
  // is the only thing that tells them apart, so off-box the host port is the
  // one that carries any information.
  function mavCmd(a){
    return 'mavproxy.py --master=udpout:' + location.hostname + ':' + a.udp;
  }
  function copyText(t, btn){
    var done = function(){
      if(!btn) return;
      btn.classList.add('ok');
      setTimeout(function(){ btn.classList.remove('ok'); }, 1200);
    };
    if(navigator.clipboard && window.isSecureContext){
      navigator.clipboard.writeText(t).then(done, function(){ fallback(t, done); });
    } else { fallback(t, done); }
  }
  function fallback(t, done){
    // clipboard API needs a secure context; plain http://host:8080 is not one
    var ta = document.createElement('textarea');
    ta.value = t; ta.style.position='fixed'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); done(); } catch(e) {}
    document.body.removeChild(ta);
  }

  var followName = null, followRAF = null, followOffset = null;
  // Created lazily: panel.js is deferred, so THREE may not exist yet at load.
  function offsetVec(){
    if(!followOffset) followOffset = new THREE.Vector3();
    return followOffset;
  }

  function followTick(){
    if(!followName){ followRAF = null; return; }
    var sc = window.scene;
    var o = sc && (sc.getByName ? sc.getByName(followName)
                                : sc.scene && sc.scene.getObjectByName(followName));
    if(o && sc.camera){
      var p = new THREE.Vector3();
      o.getWorldPosition ? o.getWorldPosition(p) : p.copy(o.position);
      if(sc.controls && sc.controls.target){
        // Re-derive the offset from the CURRENT target every frame, so any
        // orbiting or zooming done since the last frame is preserved and the
        // camera does not fight the user's mouse.
        offsetVec().subVectors(sc.camera.position, sc.controls.target);
        sc.controls.target.copy(p);
      }
      sc.camera.position.copy(p).add(offsetVec());
      if(sc.controls && sc.controls.update) sc.controls.update();
    }
    followRAF = requestAnimationFrame(followTick);
  }

  function stopFollow(){
    followName = null;
    if(followRAF){ cancelAnimationFrame(followRAF); followRAF = null; }
  }

  function toggleFollow(name){
    var sc = window.scene;
    if(followName === name){ stopFollow(); anote('stopped following ' + name);
                             markFollow(); return; }
    if(!sc || !sc.camera || typeof THREE === 'undefined'){
      anote('viewer not ready'); return; }
    var o = sc.getByName ? sc.getByName(name)
                         : sc.scene && sc.scene.getObjectByName(name);
    if(!o){ anote(name + ' not in the scene yet'); return; }
    var p = new THREE.Vector3();
    o.getWorldPosition ? o.getWorldPosition(p) : p.copy(o.position);
    // Start from wherever the camera already is, so engaging follow does not
    // yank the view; if it is sitting on top of the asset, back off a little.
    offsetVec().subVectors(sc.camera.position, p);
    if(offsetVec().length() < 3){ offsetVec().set(10, 10, 6); }
    followName = name;
    anote('following ' + name);
    markFollow();
    if(!followRAF) followTick();
  }

  function markFollow(){
    apanel.querySelectorAll('.as-fol').forEach(function(b){
      b.classList.toggle('on', b.getAttribute('data-n') === followName);
    });
  }

  var apanel = el('div',{id:'as-assets'});
  apanel.innerHTML = '<div class="as-head"><span>Assets</span>'
    + '<button id="as-amin" title="collapse">–</button></div>'
    + '<div class="as-rows"><div class="as-note" id="as-anote">loading…</div></div>';
  document.body.appendChild(apanel);
  apanel.querySelector('#as-amin').onclick = function(){
    apanel.classList.toggle('min');
    this.textContent = apanel.classList.contains('min') ? '+' : '–';
  };

  function anote(t){ var n=document.getElementById('as-anote'); if(n) n.textContent=t||''; }

  function focusAsset(name){
    var sc = window.scene;
    if(!sc || !sc.scene || typeof THREE === 'undefined'){
      anote('viewer not ready'); return; }
    var o = sc.getByName ? sc.getByName(name) : sc.scene.getObjectByName(name);
    if(!o){ anote(name + ' not in the scene yet'); return; }
    var p = new THREE.Vector3();
    o.getWorldPosition ? o.getWorldPosition(p) : p.copy(o.position);

    // Keep the current viewing direction and just re-anchor it on the asset,
    // so the view does not spin to a new orientation on every click.
    var tgt = (sc.controls && sc.controls.target) ? sc.controls.target
                                                 : new THREE.Vector3();
    var dir = new THREE.Vector3().subVectors(sc.camera.position, tgt);
    if(dir.lengthSq() < 1e-6){ dir.set(1, 1, 0.7); }
    dir.normalize();

    // Frame the asset by its own size rather than a fixed standoff: a 2.5 m
    // tower and a 0.5 m quad want very different distances to fill the view.
    // Falls back to a fixed distance if the bounds come back degenerate, which
    // happens while a model's meshes are still loading.
    var dist = 10;
    try {
      var box = new THREE.Box3().setFromObject(o);
      if(box && isFinite(box.min.x) && !box.isEmpty()){
        var sz = box.getSize(new THREE.Vector3());
        var radius = Math.max(sz.x, sz.y, sz.z) * 0.5;
        if(radius > 0.01){ dist = Math.max(4, Math.min(radius * 4.5, 60)); }
      }
    } catch(e) { /* keep the default */ }
    sc.camera.position.copy(p).addScaledVector(dir, dist);
    if(sc.controls && sc.controls.target){
      sc.controls.target.copy(p);
      if(sc.controls.update) sc.controls.update();
    }
    sc.camera.lookAt(p);
    anote('moved to ' + name + '  (' + dist.toFixed(0) + ' m)');
  }

  function drawAssets(d){
    var rows = apanel.querySelector('.as-rows');
    if(!d || !d.ok){ rows.innerHTML = '<div class="as-note">'
      + ((d && d.error) || 'unavailable') + '</div>'; return; }
    var h = '';
    d.assets.forEach(function(a){
      var live = a.rostered && a.mavlink;
      h += '<div class="as-row' + (a.rostered ? '' : ' off') + '">'
         + '<span class="as-dot ' + (live ? 'live' : 'idle') + '" title="'
         + (a.rostered ? (a.mavlink ? 'MAVLink up' : 'rostered, no MAVLink')
                       : 'not in ASSET_N') + '"></span>'
         + '<span class="as-nm">' + a.name + '</span>'
         + '<span class="as-ty">' + (a.type || 'idle') + '</span>'
         + '<button class="as-eye" data-n="' + a.name + '"'
         + (a.rostered ? '' : ' disabled')
         + ' title="' + (a.rostered ? 'move camera to ' + a.name
                                    : a.name + ' is not in ASSET_N') + '">'
         + EYE + '</button>'
         + '<button class="as-fol" data-n="' + a.name + '"'
         + (a.rostered ? '' : ' disabled')
         + ' title="' + (a.rostered ? 'follow ' + a.name + ' (click again to stop)'
                                    : a.name + ' is not in ASSET_N') + '">'
         + CROSS + '</button>'
         + '<button class="as-cam" data-c="' + a.cam + '" data-n="' + a.name + '"'
         + (a.camera ? '' : ' disabled')
         + ' title="' + (a.camera ? 'open ' + a.name + ' camera (port ' + a.cam + ')'
                                  : 'no camera stream on port ' + a.cam) + '">'
         + CAM + '</button>'
         + '<button class="as-cli" data-n="' + a.name + '"'
         + (a.rostered ? '' : ' disabled')
         + ' title="copy MAVProxy command">' + CLI + '</button></div>';
    });
    h += '<div class="as-note" id="as-anote"></div>';
    rows.innerHTML = h;
    rows.querySelectorAll('.as-eye').forEach(function(b){
      b.onclick = function(){
        // A one-shot jump and a live follow would fight each other.
        if(followName){ stopFollow(); }
        focusAsset(b.getAttribute('data-n'));
        markFollow();
      };
    });
    rows.querySelectorAll('.as-fol').forEach(function(b){
      b.onclick = function(){ toggleFollow(b.getAttribute('data-n')); };
    });
    rows.querySelectorAll('.as-cli').forEach(function(b){
      var a = (d.assets || []).filter(function(x){
        return x.name === b.getAttribute('data-n'); })[0];
      if(!a) return;
      b.title = mavCmd(a);
      b.onclick = function(){ copyText(mavCmd(a), b); anote('copied: ' + mavCmd(a)); };
    });
    rows.querySelectorAll('.as-cam').forEach(function(b){
      b.onclick = function(){
        // Streams are published on the host, so reuse whatever host the page
        // was loaded from — works for localhost and for a remote box alike.
        var url = location.protocol + '//' + location.hostname + ':'
                + b.getAttribute('data-c') + '/';
        window.open(url, 'cam_' + b.getAttribute('data-n'));
      };
    });
    // The list is re-rendered on every poll, so re-apply the active highlight.
    markFollow();
  }

  function pollAssets(){
    fetch(API + '/api/assets').then(function(r){ return r.json(); })
      .then(drawAssets).catch(function(){ anote('control service unreachable'); });
  }
  pollAssets();
  setInterval(pollAssets, 6000);

})();
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # quiet; the sim log is noisy enough
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # The panel is served from :8090 but runs inside the page on :8080.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/panel.js":
            js = (PANEL_JS.replace("__PORT__", str(PORT))
                          .replace("__CSS__", json.dumps(PANEL_CSS)))
            return self._send(200, js, "application/javascript")
        if path == "/api/env":
            return self._send(200, json.dumps({"env": read_env()}))
        if path == "/api/site":
            # The page needs the EPSG:3413 bounds to turn a picked world point
            # into lat/lon. Read the active site straight from terrain.json.
            name = ""
            for line in read_env().splitlines():
                if line.strip().startswith("SITE_NAME="):
                    name = line.split("=", 1)[1].strip()
            meta_path = os.path.join(REPO, "out", name, "terrain.json")
            if not name or not os.path.exists(meta_path):
                return self._send(200, json.dumps({"ok": False}))
            m = json.load(open(meta_path))
            return self._send(200, json.dumps({
                "ok": True, "name": name,
                "bounds3413": m.get("bounds_3413"),
                "centre": m.get("location"),
                "extent_m": m.get("extent_m"),
                "convergence_deg": m.get("convergence_deg"),
            }))
        if path == "/api/assets":
            return self._send(200, json.dumps(asset_roster()))
        if path == "/api/status":
            return self._send(200, json.dumps(_status))
        if path == "/api/logs":
            m = re.search(r"tail=(\d+)", self.path)
            tail = m.group(1) if m else "300"
            # --timestamps so lines can be correlated with the browser console
            rc, out = run(["docker", "compose", "logs", "--no-color",
                           "--timestamps", "--tail", tail], timeout=60)
            return self._send(200, json.dumps({"log": order_logs(out)}))
        if path == "/":
            return self._send(200,
                "<h3>arctic-sim control</h3><p>The panel is injected into the "
                "gzweb page. Endpoints: /api/env /api/logs /api/reset "
                "/api/rebuild /api/status</p>", "text/html")
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}

        if path == "/api/env":
            ok, detail = write_env(payload.get("env", ""))
            return self._send(200 if ok else 400,
                              json.dumps({"ok": ok, "error": None if ok else detail}))
        if path == "/api/reset":
            if _busy.locked():
                return self._send(409, json.dumps({"ok": False, "error": "busy"}))
            background(do_reset)
            return self._send(202, json.dumps({"ok": True}))
        if path == "/api/rebuild":
            if _busy.locked():
                return self._send(409, json.dumps({"ok": False, "error": "busy"}))
            background(do_rebuild)
            return self._send(202, json.dumps({"ok": True}))
        self._send(404, json.dumps({"error": "not found"}))


if __name__ == "__main__":
    print(f"[control] listening on :{PORT}, repo={REPO}, sim={SIM}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
