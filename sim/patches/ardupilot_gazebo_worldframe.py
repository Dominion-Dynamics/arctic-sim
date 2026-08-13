#!/usr/bin/env python3
"""Teach ArduPilotPlugin that a georeferenced world is not X-North/Y-West.

Applied to the ardupilot_gazebo clone at image build time. Anchored on source
text rather than line numbers, and exits non-zero if the anchor is missing, so
an upstream change breaks the build loudly instead of silently producing a
simulator whose GPS is rotated.

THE BUG
-------
The stock plugin hardcodes

    gazeboXYZToNED = Pose3d(0, 0, 0, IGN_PI, 0, 0)

a 180 degree roll, which converts world -> NED only if the world is
X-North / Y-West / Z-Up. arctic-sim's worlds are not: make_world.py builds them
on ArcticDEM's EPSG:3413 grid, so world +X is grid EAST and +Y is grid NORTH,
and the grid itself is rotated from true north by the grid convergence at that
longitude (about -49.8 deg at Fort Ross, and different at every site).

Measured on the running sim before this patch: commanding the quad to a lat/lon
put it 435 m away on a bearing rotated exactly 90.00 deg from the truth, and the
convergence error rode on top of that. ArduPilot was certain it had arrived --
GLOBAL_POSITION_INT matched the commanded point exactly -- because every
lat/lon it computes inherits the same rotation.

THE FIX
-------
    yaw = pi/2 - heading_offset

pi/2 turns X-North/Y-West into plain ENU; subtracting the world's own
heading_offset removes the grid's rotation from true north. Both position and
attitude come from `pose - gazeboXYZToNED` and velocity from the same rotation,
so correcting this one member fixes all three consistently -- which matters,
because fixing position alone would leave the compass wrong.

Verified against geodetic truth: world displacement (dx=52.77, dy=-431.97) with
heading 49.8048 deg yields NED (-238.5, +364.0); the true offset computed from
EPSG:3413 is (-238.7, +365.2).

WHY IT IS OPT-IN
----------------
Reading heading_offset unconditionally would change behaviour for every
existing model: a stock world has no <spherical_coordinates>, heading is 0, and
this would still apply the pi/2 that stock does not want. So it is keyed on an
explicit <worldFrame>ENU</worldFrame> in the plugin block. Absent, the stock
transform is untouched.

It is deliberately NOT an environment variable or a per-site constant. The
heading is read from the world at load time, so building a site at a new
longitude gets the right convergence with nothing to configure.
"""
import sys

PATH = "/sim/ardupilot_gazebo/src/ArduPilotPlugin.cc"

ANCHOR = """  this->gazeboXYZToNED = ignition::math::Pose3d(0, 0, 0, IGN_PI, 0, 0);
  if (_sdf->HasElement("gazeboXYZToNED"))
  {
    this->gazeboXYZToNED = _sdf->Get<ignition::math::Pose3d>("gazeboXYZToNED");
  }
"""

ADDITION = """
  // arctic-sim: <worldFrame>ENU</worldFrame> derives the transform from the
  // world's own <spherical_coordinates><heading_deg> instead of assuming
  // X-North/Y-West. Absent, everything above is left exactly as upstream.
  //
  // yaw = pi/2 - heading: the pi/2 turns X-North/Y-West into plain ENU, and
  // -heading removes the world's rotation from true north. A georeferenced
  // world declares that rotation itself, so a site built at a different
  // longitude -- with a different grid convergence -- is handled with nothing
  // to configure here or per site.
  //
  // An explicit <gazeboXYZToNED> above still wins, since it is set after the
  // default and this only runs when that element is absent.
  if (!_sdf->HasElement("gazeboXYZToNED") &&
      _sdf->HasElement("worldFrame") &&
      _sdf->Get<std::string>("worldFrame") == "ENU")
  {
    double headingRad = 0.0;
    gazebo::common::SphericalCoordinatesPtr sc =
        _model->GetWorld()->SphericalCoords();
    if (sc)
    {
      headingRad = sc->HeadingOffset().Radian();
    }
    const double yaw = IGN_PI / 2.0 - headingRad;
    this->gazeboXYZToNED = ignition::math::Pose3d(0, 0, 0, IGN_PI, 0, yaw);
    gzmsg << "[ArduPilotPlugin] worldFrame=ENU: world heading "
          << headingRad * 180.0 / IGN_PI << " deg -> gazeboXYZToNED yaw "
          << yaw * 180.0 / IGN_PI << " deg\\n";
  }
"""

src = open(PATH).read()

if "worldFrame" in src:
    print("[patch] already applied, nothing to do")
    sys.exit(0)

if ANCHOR not in src:
    sys.exit(
        "[patch] FAILED: anchor not found in %s.\n"
        "        Upstream ardupilot_gazebo changed the gazeboXYZToNED block.\n"
        "        Re-anchor this patch rather than skipping it: without it every\n"
        "        lat/lon the simulator reports is silently rotated." % PATH
    )

open(PATH, "w").write(src.replace(ANCHOR, ANCHOR + ADDITION, 1))
print("[patch] ArduPilotPlugin.cc: worldFrame=ENU support added")
