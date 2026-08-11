#!/usr/bin/env python3
"""Generate a ship model for Gazebo Classic, hull as COLLADA.

    python3 sim/gen_ship.py --length 42 --beam 9

Writes sim/models/ship/{model.config,model.sdf,meshes/hull.dae}.

The hull is a lofted mesh because a box does not read as a vessel from the air,
and this exists to be spotted from the air. Superstructure stays as SDF
primitives — they are boxes in reality, and primitives avoid a second mesh
conversion step in gzweb.

COLLADA rather than OBJ: gzweb's asset pipeline already converts the .dae meshes
the iris model ships with, so it is the proven format here.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

DAE = """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>arctic-sim gen_ship</authoring_tool></contributor>
    <unit meter="1" name="meter"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_effects>
    <effect id="hull-fx">
      <profile_COMMON><technique sid="common"><lambert>
        <diffuse><color>{r} {g} {b} 1</color></diffuse>
        <ambient><color>{ar} {ag} {ab} 1</color></ambient>
      </lambert></technique></profile_COMMON>
    </effect>
  </library_effects>
  <library_materials>
    <material id="hull-mat" name="hull"><instance_effect url="#hull-fx"/></material>
  </library_materials>
  <library_geometries>
    <geometry id="hull-geo" name="hull">
      <mesh>
        <source id="hull-pos">
          <float_array id="hull-pos-arr" count="{npos}">{positions}</float_array>
          <technique_common>
            <accessor source="#hull-pos-arr" count="{nvert}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="hull-nrm">
          <float_array id="hull-nrm-arr" count="{nnrm}">{normals}</float_array>
          <technique_common>
            <accessor source="#hull-nrm-arr" count="{nnormal}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="hull-vtx">
          <input semantic="POSITION" source="#hull-pos"/>
        </vertices>
        <triangles material="hull-sym" count="{ntri}">
          <input semantic="VERTEX" source="#hull-vtx" offset="0"/>
          <input semantic="NORMAL" source="#hull-nrm" offset="1"/>
          <p>{indices}</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="hull-node" name="hull" type="NODE">
        <instance_geometry url="#hull-geo">
          <bind_material><technique_common>
            <instance_material symbol="hull-sym" target="#hull-mat"/>
          </technique_common></bind_material>
        </instance_geometry>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
"""


def build_hull(length: float, beam: float, draft: float, freeboard: float,
               stations: int = 28, ring: int = 9):
    """Loft a hull: pointed bow, full midships, gently tapered transom stern."""
    depth = draft + freeboard
    verts, tris = [], []

    def half_beam(t: float) -> float:
        # t = 0 at bow, 1 at stern.
        if t < 0.30:
            return beam / 2 * math.sin(t / 0.30 * math.pi / 2) ** 0.85
        if t > 0.88:
            return beam / 2 * (1.0 - 0.35 * (t - 0.88) / 0.12)
        return beam / 2

    def section(t: float):
        """One cross-section ring, from keel up the starboard side and back."""
        hb = half_beam(t)
        # Deadrise: the hull narrows toward the keel.
        keel = -draft * (0.55 + 0.45 * math.sin(min(t, 1 - t) * math.pi))
        pts = []
        for i in range(ring):
            f = i / (ring - 1)                    # 0 keel -> 1 sheer
            y = hb * math.sin(f * math.pi / 2) ** 0.75
            z = keel + (depth) * f ** 1.15
            pts.append((y, z))
        return pts

    for s in range(stations + 1):
        t = s / stations
        x = length / 2 - t * length
        for y, z in section(t):
            verts.append((x, -y, z))          # port
        for y, z in reversed(section(t)):
            verts.append((x, y, z))           # starboard
    per = ring * 2

    for s in range(stations):
        a0 = s * per
        b0 = (s + 1) * per
        for i in range(per):
            j = (i + 1) % per
            tris.append((a0 + i, b0 + i, b0 + j))
            tris.append((a0 + i, b0 + j, a0 + j))

    # Cap the deck so the hull is closed from above.
    deck_start = len(verts)
    for s in range(stations + 1):
        t = s / stations
        x = length / 2 - t * length
        hb = half_beam(t)
        verts.append((x, -hb, depth - draft))
        verts.append((x, hb, depth - draft))
    for s in range(stations):
        a = deck_start + s * 2
        b = deck_start + (s + 1) * 2
        tris.append((a, b, b + 1))
        tris.append((a, b + 1, a + 1))
    return verts, tris


def face_normal(v, tri):
    ax, ay, az = v[tri[0]]
    bx, by, bz = v[tri[1]]
    cx, cy, cz = v[tri[2]]
    ux, uy, uz = bx - ax, by - ay, bz - az
    wx, wy, wz = cx - ax, cy - ay, cz - az
    nx, ny, nz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
    m = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    return nx / m, ny / m, nz / m


MODEL_SDF = """<?xml version="1.0" ?>
<!-- Static vessel. Gazebo Classic has no buoyancy solver, so the hull is pinned
     at the waterline rather than floated; for a detection target that is the
     behaviour you want anyway - it stays exactly where you put it. -->
<sdf version="1.6">
  <model name="ship">
    <static>{static}</static>
    <link name="hull">

      <visual name="hull_visual">
        <geometry>
          <mesh>
            <uri>model://ship/meshes/hull.dae</uri>
            <scale>1 1 1</scale>
          </mesh>
        </geometry>
      </visual>

      <!-- Collision is a plain box: the lofted hull would be needless work for
           the physics engine and nothing needs to touch it precisely. -->
      <collision name="hull_collision">
        <pose>0 0 {hull_cz:.2f} 0 0 0</pose>
        <geometry><box><size>{length:.2f} {beam:.2f} {depth:.2f}</size></box></geometry>
      </collision>
{blocks}
    </link>
  </model>
</sdf>
"""

BOX = """
      <visual name="{name}_visual">
        <pose>{x:.2f} {y:.2f} {z:.2f} 0 0 0</pose>
        <geometry><box><size>{sx:.2f} {sy:.2f} {sz:.2f}</size></box></geometry>
        <material>
          <ambient>{amb}</ambient><diffuse>{dif}</diffuse>
        </material>
      </visual>
      <collision name="{name}_collision">
        <pose>{x:.2f} {y:.2f} {z:.2f} 0 0 0</pose>
        <geometry><box><size>{sx:.2f} {sy:.2f} {sz:.2f}</size></box></geometry>
      </collision>
"""

CYL = """
      <visual name="{name}_visual">
        <pose>{x:.2f} {y:.2f} {z:.2f} 0 0 0</pose>
        <geometry><cylinder><radius>{r:.2f}</radius><length>{h:.2f}</length></cylinder></geometry>
        <material>
          <ambient>{amb}</ambient><diffuse>{dif}</diffuse>
        </material>
      </visual>
"""

CONFIG = """<?xml version="1.0"?>
<model>
  <name>ship</name>
  <version>1.0</version>
  <sdf version="1.6">model.sdf</sdf>
  <author><name>arctic-sim</name></author>
  <description>
    {length:g} m arctic supply vessel. Lofted COLLADA hull with primitive
    superstructure. Intended as an aerial detection and tracking target.
  </description>
</model>
"""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--length", type=float, default=42.0)
    p.add_argument("--beam", type=float, default=9.0)
    p.add_argument("--draft", type=float, default=2.6)
    p.add_argument("--freeboard", type=float, default=2.4)
    p.add_argument("--hull-colour", default="0.72 0.12 0.10",
                   help="RGB 0-1; red reads clearly against dark arctic water")
    p.add_argument("--static", default="true", choices=["true", "false"])
    a = p.parse_args()

    verts, tris = build_hull(a.length, a.beam, a.draft, a.freeboard)
    normals, idx = [], []
    for t in tris:
        n = face_normal(verts, t)
        normals.append(n)
        ni = len(normals) - 1
        for vi in t:
            idx += [vi, ni]

    r, g, b = [float(v) for v in a.hull_colour.split()]
    dae = DAE.format(
        r=r, g=g, b=b, ar=r * 0.35, ag=g * 0.35, ab=b * 0.35,
        npos=len(verts) * 3, nvert=len(verts),
        positions=" ".join(f"{c:.4f}" for v in verts for c in v),
        nnrm=len(normals) * 3, nnormal=len(normals),
        normals=" ".join(f"{c:.4f}" for n in normals for c in n),
        ntri=len(tris), indices=" ".join(str(i) for i in idx))

    mdir = REPO / "sim" / "models" / "ship"
    (mdir / "meshes").mkdir(parents=True, exist_ok=True)
    (mdir / "meshes" / "hull.dae").write_text(dae)

    deck = a.freeboard
    white = ("0.86 0.87 0.88 1", "0.90 0.91 0.92 1")
    dark = ("0.15 0.16 0.18 1", "0.20 0.21 0.24 1")
    blocks = ""
    # Deckhouse aft of midships, bridge on top, funnel and mast above that.
    blocks += BOX.format(name="deckhouse", x=-a.length * 0.16, y=0,
                         z=deck + 2.6, sx=a.length * 0.30, sy=a.beam * 0.78,
                         sz=5.2, amb=white[0], dif=white[1])
    blocks += BOX.format(name="bridge", x=-a.length * 0.10, y=0,
                         z=deck + 6.6, sx=a.length * 0.14, sy=a.beam * 0.86,
                         sz=2.8, amb=white[0], dif=white[1])
    blocks += BOX.format(name="hatch", x=a.length * 0.20, y=0, z=deck + 0.5,
                         sx=a.length * 0.34, sy=a.beam * 0.60, sz=1.0,
                         amb=dark[0], dif=dark[1])
    blocks += CYL.format(name="funnel", x=-a.length * 0.24, y=0, z=deck + 8.4,
                         r=1.15, h=4.2, amb=dark[0], dif=dark[1])
    blocks += CYL.format(name="mast", x=-a.length * 0.10, y=0, z=deck + 11.0,
                         r=0.18, h=6.0, amb=white[0], dif=white[1])

    (mdir / "model.sdf").write_text(MODEL_SDF.format(
        static=a.static, length=a.length, beam=a.beam,
        depth=a.draft + a.freeboard,
        hull_cz=(a.freeboard - a.draft) / 2, blocks=blocks))
    (mdir / "model.config").write_text(CONFIG.format(length=a.length))

    print(f"  ship: {a.length:g} x {a.beam:g} m, {len(verts)} verts, {len(tris)} tris")
    print(f"  wrote sim/models/ship/")


if __name__ == "__main__":
    main()
