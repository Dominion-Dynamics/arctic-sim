#!/usr/bin/env python3
"""Water analysis and vessel course planning.

Lives apart from build_terrain.py because planning is *cheap* — it needs the DEM
that is already on disk, not a fresh download. Keeping it here lets
make_world.py re-plan a course in seconds when a seed changes, instead of
forcing a full terrain rebuild and re-fetching imagery.

Both modules import from here so the two paths cannot drift.
"""

from __future__ import annotations

import math


def pick_water(dem: str, grid: int, spacing: float, extent: float) -> dict:
    """The point of open water furthest from any shore.

    The terrain edge counts as shore: otherwise the answer is always the map
    boundary, where a vessel would sit half outside the world.
    """
    from collections import deque
    from osgeo import gdal
    import numpy as np
    gdal.UseExceptions()
    ds = gdal.Open(dem)                      # hold it: a temporary is freed
    z = ds.GetRasterBand(1).ReadAsArray().astype("float64")
    water = z <= 0.05
    if not water.any():
        return {"present": False}

    dist = np.full(z.shape, -1.0)
    dq = deque()
    for r in range(grid):
        for c in range(grid):
            if not water[r, c] or r in (0, grid - 1) or c in (0, grid - 1):
                dist[r, c] = 0.0
                dq.append((r, c))
    while dq:
        r, c = dq.popleft()
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            r2, c2 = r + dr, c + dc
            if 0 <= r2 < grid and 0 <= c2 < grid and dist[r2, c2] < 0:
                dist[r2, c2] = dist[r, c] + 1
                dq.append((r2, c2))
    dist[~water] = -1.0
    if dist.max() <= 0:
        return {"present": False}

    r, c = divmod(int(np.argmax(dist)), grid)
    half = extent / 2.0
    best_clear = float(dist[r, c] * spacing)

    # A pool of well-offshore spots, so a moored vessel can be placed randomly
    # without another DEM pass later. Thinned to keep terrain.json small.
    thresh = max(best_clear * 0.45, spacing * 4)
    rr, cc = np.nonzero(dist * spacing >= thresh)
    cand = []
    if len(rr):
        step = max(1, len(rr) // 400)
        for i in range(0, len(rr), step):
            cand.append([round(float(-half + cc[i] * spacing), 1),
                         round(float(half - rr[i] * spacing), 1),
                         round(float(dist[rr[i], cc[i]] * spacing), 1)])

    return {"present": True,
            "x": float(-half + c * spacing),
            "y": float(half - r * spacing),
            "clearance_m": best_clear,
            "water_fraction": float(water.mean()),
            "candidates": cand}


def plan_course(dem: str, grid: int, spacing: float, extent: float,
                seed: int, target_len: float, min_clear: float = 120.0,
                attempts: int = 12, start_xy=None) -> dict:
    # 0 means non-deterministic: a different course every build. Handy for
    # eyeballing variety, never for a competition where runs must match.
    if seed == 0:
        import secrets
        seed = secrets.randbelow(2 ** 31 - 1) + 1
    """Plan a course, retrying until one provably never crosses land.

    A single attempt is not guaranteed to succeed: the random walk can work
    itself into a bay where smoothing and repair cannot recover, and roughly one
    seed in ten grounds. Retrying with a derived seed keeps the user-facing seed
    deterministic while guaranteeing a usable course.
    """
    best = None
    for attempt in range(attempts):
        # Derived deterministically, so `seed` alone still fixes the outcome.
        c = _plan_course_once(dem, grid, spacing, extent,
                              seed if attempt == 0 else seed * 7919 + attempt,
                              target_len, min_clear, start_xy)
        # "Not aground" is too weak a bar: thinning waypoints lets straight
        # segments cut corners, and a 34 m vessel skimming 25 m off the rocks
        # looks wrong even though it never touches. Demand real sea room.
        want_clear = min_clear * 0.5
        if (c.get("present") and not c.get("grounded")
                and c.get("path_min_clearance_m", 0) >= want_clear):
            c["seed"] = seed
            c["attempts"] = attempt + 1
            return c
        if c.get("present") and not c.get("grounded") and best is None:
            best = c            # keep a land-free fallback in case none clear the bar
    if best is not None:
        best["seed"] = seed
        best["attempts"] = attempts
        best["relaxed"] = True
        return best
    return {"present": False, "seed": seed, "attempts": attempts}


def _plan_course_once(dem: str, grid: int, spacing: float, extent: float,
                      seed: int, target_len: float, min_clear: float = 120.0,
                      start_xy=None) -> dict:
    """Plot a navigable course across the water.

    A pure random walk looks like a drunk boat, so heading carries momentum and
    only turns gradually. Steering comes from the distance-to-shore field: when
    the vessel gets close to land it climbs the gradient back toward open water,
    which is roughly what a helm does. Seeded, so a given seed always yields the
    same course — competitors must get identical worlds.
    """
    from collections import deque
    from osgeo import gdal
    import numpy as np
    gdal.UseExceptions()
    ds = gdal.Open(dem)
    z = ds.GetRasterBand(1).ReadAsArray().astype("float64")
    water = z <= 0.05
    if not water.any():
        return {"present": False}

    # Distance to shore, in metres. Map edge counts as shore.
    dist = np.full(z.shape, -1.0)
    dq = deque()
    for r in range(grid):
        for c in range(grid):
            if not water[r, c] or r in (0, grid - 1) or c in (0, grid - 1):
                dist[r, c] = 0.0
                dq.append((r, c))
    while dq:
        r, c = dq.popleft()
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            r2, c2 = r + dr, c + dc
            if 0 <= r2 < grid and 0 <= c2 < grid and dist[r2, c2] < 0:
                dist[r2, c2] = dist[r, c] + 1
                dq.append((r2, c2))
    dist *= spacing
    if dist.max() < min_clear:
        min_clear = max(spacing * 2, dist.max() * 0.5)

    half = extent / 2.0

    def clear_at(x, y):
        c = int(round((x + half) / spacing))
        r = int(round((half - y) / spacing))
        if 0 <= r < grid and 0 <= c < grid:
            return dist[r, c]
        return -1.0

    rng = np.random.default_rng(seed)

    # Route, not ramble. A random walk loiters in whatever basin it starts in
    # and never traverses the channel, so the vessel was only ever seen in one
    # part of the strait. Instead: pick two navigable points far apart, then
    # find the cheapest path between them where cost falls with clearance —
    # a widest-path search, which naturally hugs the middle of the channel.
    import heapq

    nav = dist >= min_clear
    if nav.sum() < 50:
        nav = dist >= max(spacing * 2, dist.max() * 0.15)
    idx = np.argwhere(nav)
    if len(idx) < 2:
        return {"present": False}

    def to_world(r, c):
        return (-half + c * spacing, half - r * spacing)

    if start_xy is not None:
        # A requested position: snap to the nearest navigable cell so a point
        # picked slightly off the channel still works instead of failing.
        want_c = (start_xy[0] + half) / spacing
        want_r = (half - start_xy[1]) / spacing
        d0 = np.hypot(idx[:, 0] - want_r, idx[:, 1] - want_c)
        ra, ca = idx[int(np.argmin(d0))]
        snapped = float(d0.min() * spacing)
    else:
        # Otherwise draw uniformly from ALL navigable water, so the vessel can
        # begin anywhere in the channel rather than along one fixed line.
        ra, ca = idx[int(rng.integers(len(idx)))]
        snapped = 0.0
    a = (int(ra), int(ca))

    # The far end is the most distant navigable point within reach of the
    # requested course length, which pushes routes toward traversing the
    # channel instead of hopping across it.
    d = np.hypot((idx[:, 0] - ra) * spacing, (idx[:, 1] - ca) * spacing)
    reach = np.nonzero(d <= target_len)[0]
    if len(reach) < 2:
        reach = np.arange(len(idx))
    far = reach[np.argsort(d[reach])[-max(1, len(reach) // 20):]]
    rb, cb = idx[int(far[int(rng.integers(len(far)))])]
    b = (int(rb), int(cb))
    if a == b:
        return {"present": False}

    # Dijkstra over navigable water. Cost per step scales with distance and is
    # penalised near shore, so the route prefers open water without being
    # forbidden from narrows.
    ref = max(float(dist.max()), 1.0)
    INF = float("inf")
    cost = np.full(dist.shape, INF)
    prev = np.full(dist.shape, -1, dtype=np.int64)
    cost[a] = 0.0
    pq = [(0.0, a[0] * grid + a[1])]
    nbrs = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
            (-1, -1, 1.414), (-1, 1, 1.414), (1, -1, 1.414), (1, 1, 1.414)]
    while pq:
        d0, key = heapq.heappop(pq)
        r, c = divmod(key, grid)
        if d0 > cost[r, c]:
            continue
        if (r, c) == b:
            break
        for dr, dc, w in nbrs:
            r2, c2 = r + dr, c + dc
            if not (0 <= r2 < grid and 0 <= c2 < grid) or not nav[r2, c2]:
                continue
            # 1x in mid-channel, up to 4x hugging the bank.
            penalty = 1.0 + 3.0 * (1.0 - min(dist[r2, c2] / ref, 1.0))
            nd = d0 + w * spacing * penalty
            if nd < cost[r2, c2]:
                cost[r2, c2] = nd
                prev[r2, c2] = key
                heapq.heappush(pq, (nd, r2 * grid + c2))

    if cost[b] == INF:
        return {"present": False}

    chain = []
    cur = b[0] * grid + b[1]
    while cur >= 0:
        r, c = divmod(cur, grid)
        chain.append(to_world(r, c))
        if (r, c) == a:
            break
        cur = int(prev[r, c])
    chain.reverse()
    if len(chain) < 4:
        return {"present": False}

    # Thin to waypoints roughly a ship-length apart; the repair pass below
    # re-checks every segment anyway.
    stride = max(1, int(len(chain) / 120))
    pts = chain[::stride]
    if pts[-1] != chain[-1]:
        pts.append(chain[-1])

    arr = np.array(pts)

    # Smooth out the jitter. Note this can cut corners *across* land, so the
    # result is repaired and validated below rather than trusted.
    k = 5
    pad = np.vstack([arr[:1].repeat(k, 0), arr, arr[-1:].repeat(k, 0)])
    sm = np.stack([np.convolve(pad[:, i], np.ones(k) / k, "same") for i in range(2)], 1)
    sm = sm[k:-k]
    if len(sm) != len(arr):
        sm = arr.copy()

    # Any smoothed point that drifted shoreward reverts to the raw point, which
    # was valid by construction. Dropping it instead would join its neighbours
    # with a longer straight line - exactly the failure we are guarding against.
    for i in range(len(sm)):
        if clear_at(sm[i][0], sm[i][1]) < min_clear:
            sm[i] = arr[i]

    def segment_min(a, b):
        """Lowest clearance anywhere along a-b, sampled finer than a grid cell."""
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(2, int(d / (spacing * 0.5)) + 1)
        return min(clear_at(a[0] + (b[0] - a[0]) * t / n,
                            a[1] + (b[1] - a[1]) * t / n) for t in range(n + 1))

    # Repair: the vessel travels straight between waypoints, so a segment can
    # cross a headland even when both ends sit in open water. Nudge offending
    # points up the clearance gradient, then subdivide anything still bad.
    for _ in range(6):
        bad = [i for i in range(len(sm) - 1)
               if segment_min(sm[i], sm[i + 1]) < min_clear * 0.5]
        if not bad:
            break
        for i in bad:
            for j in (i, i + 1):
                best, bp = clear_at(sm[j][0], sm[j][1]), None
                for ang in np.linspace(0, 2 * math.pi, 16, endpoint=False):
                    for rad in (spacing, spacing * 2, spacing * 4):
                        cx = sm[j][0] + math.cos(ang) * rad
                        cy = sm[j][1] + math.sin(ang) * rad
                        cl = clear_at(cx, cy)
                        if cl > best:
                            best, bp = cl, (cx, cy)
                if bp:
                    sm[j] = bp

    # Anything still crossing land gets extra vertices pulled to open water.
    out = [sm[0]]
    for i in range(len(sm) - 1):
        if segment_min(sm[i], sm[i + 1]) < min_clear * 0.5:
            mx = (sm[i][0] + sm[i + 1][0]) / 2
            my = (sm[i][1] + sm[i + 1][1]) / 2
            best, bp = -1.0, None
            for ang in np.linspace(0, 2 * math.pi, 24, endpoint=False):
                for rad in (spacing * 2, spacing * 4, spacing * 8):
                    cx, cy = mx + math.cos(ang) * rad, my + math.sin(ang) * rad
                    cl = clear_at(cx, cy)
                    if cl > best:
                        best, bp = cl, (cx, cy)
            if bp and best >= min_clear:
                out.append(np.array(bp))
        out.append(sm[i + 1])
    sm = np.array(out)

    # Final verification along the travelled line, including the closing leg
    # since the plugin loops.
    legs = [(sm[i], sm[i + 1]) for i in range(len(sm) - 1)] + [(sm[-1], sm[0])]
    path_min = min(segment_min(a, b) for a, b in legs)
    length = float(np.sum(np.hypot(*np.diff(sm, axis=0).T)))

    return {"present": True, "seed": seed,
            "waypoints": [[round(float(a), 1), round(float(b), 1)] for a, b in sm],
            "length_m": round(length, 1),
            "min_clearance_m": round(float(min(clear_at(a, b) for a, b in sm)), 1),
            "path_min_clearance_m": round(float(path_min), 1),
            "snapped_m": round(snapped, 1),
            "grounded": bool(path_min <= 0.0)}


