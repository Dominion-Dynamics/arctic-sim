// Move a model along a polyline at constant speed.
//
// Written as a model plugin rather than a Gazebo <actor> trajectory because
// gzweb has no actor support at all — an actor would animate in Gazebo and sit
// frozen in the browser. Moving a *model* publishes on ~/pose/info, which is
// exactly what gzweb already consumes to animate the drone.
//
// The model must not be <static>: Gazebo does not publish pose updates for
// static models, so the vessel would move server-side and appear stuck in the
// browser. Use gravity 0 + kinematic instead.

#include <string>
#include <vector>

#include <gazebo/common/Plugin.hh>
#include <gazebo/common/Events.hh>
#include <gazebo/physics/physics.hh>
#include <ignition/math/Pose3.hh>
#include <ignition/math/Vector3.hh>

namespace gazebo
{
class VesselPathPlugin : public ModelPlugin
{
  public: void Load(physics::ModelPtr _model, sdf::ElementPtr _sdf) override
  {
    this->model = _model;

    if (_sdf->HasElement("speed"))
      this->speed = _sdf->Get<double>("speed");
    if (_sdf->HasElement("loop"))
      this->loop = _sdf->Get<bool>("loop");
    if (_sdf->HasElement("z"))
      this->z = _sdf->Get<double>("z");
    // Arc length along the course at t=0. Randomising this keeps the validated
    // track but stops the vessel always being in the same place at start.
    if (_sdf->HasElement("start"))
      this->startOffset = _sdf->Get<double>("start");

    // Waypoints as "<waypoint>x y</waypoint>", in world metres.
    if (_sdf->HasElement("waypoint"))
    {
      sdf::ElementPtr wp = _sdf->GetElement("waypoint");
      while (wp)
      {
        ignition::math::Vector2d p = wp->Get<ignition::math::Vector2d>();
        this->pts.push_back(p);
        wp = wp->GetNextElement("waypoint");
      }
    }

    if (this->pts.size() < 2)
    {
      gzerr << "[vessel_path] need at least 2 waypoints; plugin idle\n";
      return;
    }

    // Cumulative arc length, so travel is constant-speed regardless of how
    // unevenly the waypoints are spaced.
    this->cum.push_back(0.0);
    for (size_t i = 1; i < this->pts.size(); ++i)
      this->cum.push_back(this->cum.back() +
                          this->pts[i].Distance(this->pts[i - 1]));
    this->total = this->cum.back();

    this->s = ignition::math::clamp(this->startOffset, 0.0, this->total);

    gzmsg << "[vessel_path] start offset " << this->s << " m; "
          << this->pts.size() << " waypoints, "
          << this->total << " m course at " << this->speed << " m/s ("
          << (this->total / this->speed / 60.0) << " min per lap)\n";

    this->conn = event::Events::ConnectWorldUpdateBegin(
        std::bind(&VesselPathPlugin::OnUpdate, this, std::placeholders::_1));

    // Gazebo Classic does not reliably call ModelPlugin::Reset for model
    // plugins on `gz world -r`, so subscribe to the world-reset event directly.
    // Without this the vessel resumes from wherever it had got to.
    this->resetConn = event::Events::ConnectWorldReset(
        std::bind(&VesselPathPlugin::OnReset, this));
  }

  // Gazebo's world reset restores poses but not plugin state. Without this the
  // vessel snaps straight back to wherever it had got to along the course.
  public: void Reset() override { this->OnReset(); }

  private: void OnReset()
  {
    this->s = ignition::math::clamp(this->startOffset, 0.0, this->total);
    this->dir = 1.0;
    this->last = common::Time::Zero;
    gzmsg << "[vessel_path] reset to " << this->s << " m along the course\n";
  }

  private: void OnUpdate(const common::UpdateInfo &_info)
  {
    if (this->pts.size() < 2)
      return;

    if (this->last == common::Time::Zero)
    {
      this->last = _info.simTime;
      return;
    }
    double dt = (_info.simTime - this->last).Double();
    this->last = _info.simTime;
    if (dt <= 0.0)
      return;

    this->s += this->speed * this->dir * dt;
    if (this->loop)
    {
      while (this->s > this->total) this->s -= this->total;
      while (this->s < 0.0)         this->s += this->total;
    }
    else if (this->s > this->total || this->s < 0.0)
    {
      // Turn around at the ends rather than teleporting back to the start.
      this->dir = -this->dir;
      this->s = ignition::math::clamp(this->s, 0.0, this->total);
    }

    // Locate the segment containing arc length s.
    size_t i = 1;
    while (i < this->cum.size() - 1 && this->cum[i] < this->s) ++i;
    double seg = this->cum[i] - this->cum[i - 1];
    double f = seg > 1e-9 ? (this->s - this->cum[i - 1]) / seg : 0.0;

    ignition::math::Vector2d a = this->pts[i - 1];
    ignition::math::Vector2d b = this->pts[i];
    ignition::math::Vector2d p = a + (b - a) * f;

    // Heading follows the course, so the hull points where it is going.
    double yaw = std::atan2((b.Y() - a.Y()) * this->dir,
                            (b.X() - a.X()) * this->dir);

    this->model->SetWorldPose(ignition::math::Pose3d(
        p.X(), p.Y(), this->z, 0.0, 0.0, yaw));
  }

  private: physics::ModelPtr model;
  private: event::ConnectionPtr conn;
  private: event::ConnectionPtr resetConn;
  private: std::vector<ignition::math::Vector2d> pts;
  private: std::vector<double> cum;
  private: double total{0.0};
  private: double s{0.0};
  private: double dir{1.0};
  private: double startOffset{0.0};
  private: double speed{3.0};   // ~6 knots
  private: double z{0.0};
  private: bool loop{true};
  private: common::Time last{common::Time::Zero};
};

GZ_REGISTER_MODEL_PLUGIN(VesselPathPlugin)
}  // namespace gazebo
