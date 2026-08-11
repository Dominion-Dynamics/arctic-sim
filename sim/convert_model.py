#!/usr/bin/env python3
"""Convert a heavy downloaded OBJ into a sim-ready COLLADA model.

Runs INSIDE the GDAL container (needs numpy; GDAL reads the texture JPEGs).

    python3 /scripts/convert_model.py \
        --obj "/in/Fishing Vessel VII.obj" \
        --name fishing_vessel --length 33.6 --target-tris 40000

Downloaded vessel models are built for close-up renders: a million triangles and
dozens of 4K textures. Flown over at 100 m in a browser client, none of that
survives — but the download cost does. This reduces the mesh by vertex
clustering and replaces every texture with its average colour, which keeps the
silhouette and the paint scheme while dropping two orders of magnitude of size.

Textures become flat colours rather than an atlas because the target is seen
from the air: at that range a hull reads as "red hull, white wheelhouse", not as
plank detail.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
from osgeo import gdal

gdal.UseExceptions()


def parse_mtl(path: str) -> dict:
    """material -> (Kd, texture filename or None)."""
    mats, cur = {}, None
    if not os.path.exists(path):
        return mats
    for line in open(path, "r", errors="ignore"):
        t = line.split()
        if not t:
            continue
        if t[0] == "newmtl":
            cur = " ".join(t[1:])
            mats[cur] = {"kd": (0.7, 0.7, 0.7), "tex": None}
        elif cur and t[0] == "Kd" and len(t) >= 4:
            mats[cur]["kd"] = tuple(float(v) for v in t[1:4])
        elif cur and t[0] == "map_Kd":
            mats[cur]["tex"] = " ".join(t[1:])
    return mats


def texture_colour(path: str) -> tuple | None:
    """Average colour of a texture, so a flat material still looks right."""
    try:
        ds = gdal.Open(path)
        n = min(3, ds.RasterCount)
        # Decimated read: full-size JPEGs are pointless for an average.
        cols = []
        for i in range(n):
            a = ds.GetRasterBand(i + 1).ReadAsArray(
                buf_xsize=min(64, ds.RasterXSize),
                buf_ysize=min(64, ds.RasterYSize)).astype("float64")
            cols.append(a.mean() / 255.0)
        while len(cols) < 3:
            cols.append(cols[0])
        return tuple(cols)
    except Exception:
        return None


def load_obj(path: str):
    """Positions and per-material triangle lists. UVs and normals are dropped."""
    verts = []
    groups, cur = {}, "default"
    for line in open(path, "r", errors="ignore"):
        if line.startswith("v "):
            t = line.split()
            verts.append((float(t[1]), float(t[2]), float(t[3])))
        elif line.startswith("usemtl"):
            cur = line.strip().split(None, 1)[1] if len(line.split()) > 1 else "default"
            groups.setdefault(cur, [])
        elif line.startswith("f "):
            idx = []
            for tok in line.split()[1:]:
                v = tok.split("/")[0]
                if v:
                    i = int(v)
                    idx.append(i - 1 if i > 0 else len(verts) + i)
            for k in range(1, len(idx) - 1):       # fan-triangulate
                groups.setdefault(cur, []).append((idx[0], idx[k], idx[k + 1]))
    return np.array(verts, dtype="float64"), groups


def cluster_decimate(verts: np.ndarray, tris: np.ndarray, cells: int):
    """Vertex clustering: collapse each grid cell to one representative vertex.

    Cruder than quadric-error simplification, but dependency-free, deterministic,
    and it preserves the silhouette — which is all an aerial target needs.
    """
    lo, hi = verts.min(0), verts.max(0)
    span = np.maximum(hi - lo, 1e-9)
    ijk = np.floor((verts - lo) / span * cells).astype(np.int64)
    ijk = np.clip(ijk, 0, cells - 1)
    key = (ijk[:, 0] * cells + ijk[:, 1]) * cells + ijk[:, 2]

    uniq, inv = np.unique(key, return_inverse=True)
    new = np.zeros((len(uniq), 3))
    np.add.at(new, inv, verts)
    counts = np.bincount(inv, minlength=len(uniq))
    new /= counts[:, None]

    nt = inv[tris]
    keep = (nt[:, 0] != nt[:, 1]) & (nt[:, 1] != nt[:, 2]) & (nt[:, 0] != nt[:, 2])
    return new, nt[keep], keep


DAE_HEAD = """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>arctic-sim convert_model</authoring_tool></contributor>
    <unit meter="1" name="meter"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_images/>
  <library_effects>
{effects}  </library_effects>
  <library_materials>
{materials}  </library_materials>
  <library_geometries>
{geometries}  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
{nodes}    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
"""

EFFECT = """    <effect id="fx{i}">
      <profile_COMMON><technique sid="common"><lambert>
        <diffuse><color>{r:.4f} {g:.4f} {b:.4f} 1</color></diffuse>
        <ambient><color>{ar:.4f} {ag:.4f} {ab:.4f} 1</color></ambient>
      </lambert></technique></profile_COMMON>
    </effect>
"""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--obj", required=True)
    p.add_argument("--mtl", default=None)
    p.add_argument("--name", default="vessel")
    p.add_argument("--out", default="/model")
    p.add_argument("--target-tris", type=int, default=40000)
    p.add_argument("--length", type=float, default=None,
                   help="rescale so the longest axis is this many metres")
    p.add_argument("--axis-order", default="zxy",
                   help="model axes mapped to world XYZ (default zxy: model Z is length)")
    a = p.parse_args()

    print(f"  reading {os.path.basename(a.obj)}")
    verts, groups = load_obj(a.obj)
    tri_total = sum(len(v) for v in groups.values())
    print(f"  {len(verts):,} verts, {tri_total:,} tris, {len(groups)} material(s)")

    mtl = a.mtl or os.path.splitext(a.obj)[0] + ".mtl"
    if not os.path.exists(mtl):
        base = os.path.dirname(a.obj)
        cands = [f for f in os.listdir(base) if f.lower().endswith(".mtl")]
        mtl = os.path.join(base, cands[0]) if cands else mtl
    mats = parse_mtl(mtl)
    print(f"  materials from {os.path.basename(mtl)}: {len(mats)}")

    # Reorient: most downloaded models are Y-up with length along Z; Gazebo is
    # Z-up with X forward.
    order = {"x": 0, "y": 1, "z": 2}
    perm = [order[c] for c in a.axis_order.lower()]
    verts = verts[:, perm]

    lo, hi = verts.min(0), verts.max(0)
    if a.length:
        scale = a.length / max(hi - lo)
        verts *= scale
        print(f"  scaled x{scale:.4f} -> {a.length:g} m long")
    verts -= (verts.min(0) + verts.max(0)) / 2.0      # centre on origin
    verts[:, 2] -= verts[:, 2].min()                  # keel to z=0

    all_tris = np.concatenate([np.array(v, dtype=np.int64)
                               for v in groups.values() if v])
    owner = np.concatenate([np.full(len(v), i, dtype=np.int64)
                            for i, v in enumerate(groups.values()) if v])

    # Search the clustering resolution that lands nearest the triangle budget.
    cells, best = 32, None
    for _ in range(14):
        nv, nt, keep = cluster_decimate(verts, all_tris, cells)
        if best is None or abs(len(nt) - a.target_tris) < abs(best[1] - a.target_tris):
            best = (cells, len(nt), nv, nt, keep)
        if len(nt) < a.target_tris * 0.9:
            cells = int(cells * 1.35) + 1
        elif len(nt) > a.target_tris * 1.1:
            cells = max(8, int(cells / 1.25))
        else:
            break
    cells, ntris, nv, nt, keep = best
    print(f"  decimated {tri_total:,} -> {ntris:,} tris "
          f"({ntris/tri_total*100:.1f}%, grid {cells}^3)")

    names = list(groups.keys())
    owner_kept = owner[keep]

    effects, materials, geometries, nodes = "", "", "", ""
    written = 0
    for mi, mname in enumerate(names):
        sel = nt[owner_kept == mi]
        if len(sel) == 0:
            continue
        info = mats.get(mname, {"kd": (0.7, 0.7, 0.7), "tex": None})
        col = None
        if info["tex"]:
            tp = os.path.join(os.path.dirname(a.obj), info["tex"])
            col = texture_colour(tp)
        if col is None:
            col = info["kd"]
        r, g, b = col
        effects += EFFECT.format(i=mi, r=r, g=g, b=b,
                                 ar=r * 0.35, ag=g * 0.35, ab=b * 0.35)
        materials += (f'    <material id="mat{mi}" name="m{mi}">'
                      f'<instance_effect url="#fx{mi}"/></material>\n')

        used, remap = np.unique(sel, return_inverse=True)
        sub = nv[used]
        faces = remap.reshape(-1, 3)
        # Flat normals, one per triangle.
        e1 = sub[faces[:, 1]] - sub[faces[:, 0]]
        e2 = sub[faces[:, 2]] - sub[faces[:, 0]]
        nrm = np.cross(e1, e2)
        ln = np.linalg.norm(nrm, axis=1, keepdims=True)
        nrm = nrm / np.where(ln == 0, 1, ln)

        idx = np.empty(len(faces) * 6, dtype=np.int64)
        idx[0::2] = faces.ravel()
        idx[1::2] = np.repeat(np.arange(len(faces)), 3)

        geometries += f"""    <geometry id="g{mi}" name="g{mi}"><mesh>
      <source id="g{mi}-p"><float_array id="g{mi}-pa" count="{sub.size}">{' '.join(f'{v:.4f}' for v in sub.ravel())}</float_array>
        <technique_common><accessor source="#g{mi}-pa" count="{len(sub)}" stride="3">
          <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
        </accessor></technique_common></source>
      <source id="g{mi}-n"><float_array id="g{mi}-na" count="{nrm.size}">{' '.join(f'{v:.4f}' for v in nrm.ravel())}</float_array>
        <technique_common><accessor source="#g{mi}-na" count="{len(nrm)}" stride="3">
          <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
        </accessor></technique_common></source>
      <vertices id="g{mi}-v"><input semantic="POSITION" source="#g{mi}-p"/></vertices>
      <triangles material="s{mi}" count="{len(faces)}">
        <input semantic="VERTEX" source="#g{mi}-v" offset="0"/>
        <input semantic="NORMAL" source="#g{mi}-n" offset="1"/>
        <p>{' '.join(str(v) for v in idx)}</p>
      </triangles>
    </mesh></geometry>\n"""
        nodes += (f'      <node id="n{mi}" name="n{mi}" type="NODE">'
                  f'<instance_geometry url="#g{mi}">'
                  f'<bind_material><technique_common>'
                  f'<instance_material symbol="s{mi}" target="#mat{mi}"/>'
                  f'</technique_common></bind_material></instance_geometry></node>\n')
        written += 1

    mdir = os.path.join(a.out, "meshes")
    os.makedirs(mdir, exist_ok=True)
    dae = os.path.join(mdir, f"{a.name}.dae")
    open(dae, "w").write(DAE_HEAD.format(effects=effects, materials=materials,
                                         geometries=geometries, nodes=nodes))
    size = os.path.getsize(dae) / 1e6
    ext = nv.max(0) - nv.min(0)
    print(f"  wrote {a.name}.dae — {written} material group(s), {size:.1f} MB")
    print(f"  bounds: {ext[0]:.1f} x {ext[1]:.1f} x {ext[2]:.1f} m (L x W x H)")


if __name__ == "__main__":
    main()
