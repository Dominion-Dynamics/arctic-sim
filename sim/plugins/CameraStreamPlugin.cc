// Serve a Gazebo camera sensor as MJPEG over HTTP.
//
// Chosen over RTSP deliberately. RTSP means H.264, which needs gstreamer in the
// image (there is none) and costs far more CPU than JPEG on a container that is
// already the bottleneck — the physics rate had to come down to 150 Hz to keep
// up. MJPEG trades bandwidth, which is free on a LAN or VPN, for CPU, which is
// not. It also opens in a browser and is one line in OpenCV:
//
//     cv2.VideoCapture("http://host:8630/stream")
//
// Endpoints:
//   /               a minimal HTML page wrapping the stream, for eyeballing
//   /stream         multipart/x-mixed-replace, the continuous feed
//   /snapshot.jpg   one frame — cheap enough for control-panel thumbnails
//
// Cost is dominated by the RENDER, not the JPEG: Gazebo renders an always_on
// camera whether or not anything consumes it. Two ways to control that:
//
//   resolution  the real lever. Halving both axes quarters the pixels, and the
//               render scales with pixels. Prefer this.
//   <sleep_when_idle>1</sleep_when_idle>
//               deactivates the sensor until a client connects. Cheapest of
//               all, but the feed is then not live — the first frame after
//               connecting lags, and nothing is observable while unwatched.
//               Off by default: a competition feed should be running whether
//               or not someone happens to be looking at it.

#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <jpeglib.h>

#include <gazebo/common/Plugin.hh>
#include <gazebo/plugins/CameraPlugin.hh>

namespace gazebo
{
class CameraStreamPlugin : public CameraPlugin
{
  public: ~CameraStreamPlugin() override
  {
    this->run = false;
    if (this->listenFd >= 0)
    {
      ::shutdown(this->listenFd, SHUT_RDWR);
      ::close(this->listenFd);
      this->listenFd = -1;
    }
    this->frameCv.notify_all();
    if (this->acceptor.joinable())
      this->acceptor.join();
  }

  public: void Load(sensors::SensorPtr _sensor, sdf::ElementPtr _sdf) override
  {
    CameraPlugin::Load(_sensor, _sdf);

    this->port    = _sdf->Get<int>("port", 8600).first;
    this->quality = _sdf->Get<int>("quality", 70).first;
    // Integer downsample. The sensor renders at its SDF resolution regardless;
    // this only shrinks what gets encoded and sent, which is where the cost is.
    this->shrink  = std::max(1, _sdf->Get<int>("shrink", 1).first);
    this->label   = _sdf->Get<std::string>("label", this->parentSensor->Name()).first;
    this->sleepIdle = _sdf->Get<bool>("sleep_when_idle", false).first;

    // port 0 means "disabled": bind nothing, serve nothing, and leave the
    // sensor inactive. The control panel probes the port, so the camera button
    // greys out by itself rather than offering a feed that cannot appear.
    if (this->port <= 0)
    {
      if (this->parentSensor)
        this->parentSensor->SetActive(false);
      gzmsg << "[camera_stream] " << this->label << ": disabled (port 0)\n";
      return;
    }

    if (this->sleepIdle && this->parentSensor)
      this->parentSensor->SetActive(false);

    this->run = true;
    this->acceptor = std::thread(&CameraStreamPlugin::Serve, this);

    gzmsg << "[camera_stream] " << this->label << " on :" << this->port
          << " (quality " << this->quality << ", shrink " << this->shrink
          << (this->sleepIdle ? ", sleeps when idle" : ", always live")
          << ")\n";
  }

  // Called by CameraPlugin for every rendered frame.
  public: void OnNewFrame(const unsigned char *_image,
                          unsigned int _w, unsigned int _h,
                          unsigned int _depth, const std::string &_format) override
  {
    if (this->clients.load() <= 0)
      return;                      // nobody watching: skip the encode entirely
    if (_depth < 3)
      return;

    std::vector<unsigned char> jpeg;
    if (!this->Encode(_image, _w, _h, _depth, jpeg))
      return;

    {
      std::lock_guard<std::mutex> lk(this->frameMu);
      this->frame.swap(jpeg);
      ++this->seq;
    }
    this->frameCv.notify_all();
  }

  private: bool Encode(const unsigned char *_src, unsigned int _w,
                       unsigned int _h, unsigned int _depth,
                       std::vector<unsigned char> &_out)
  {
    const unsigned int s = this->shrink;
    const unsigned int w = _w / s, h = _h / s;
    if (w == 0 || h == 0)
      return false;

    jpeg_compress_struct cinfo;
    jpeg_error_mgr jerr;
    cinfo.err = jpeg_std_error(&jerr);
    jpeg_create_compress(&cinfo);

    unsigned char *buf = nullptr;
    unsigned long size = 0;
    jpeg_mem_dest(&cinfo, &buf, &size);

    cinfo.image_width = w;
    cinfo.image_height = h;
    cinfo.input_components = 3;
    cinfo.in_color_space = JCS_RGB;
    jpeg_set_defaults(&cinfo);
    jpeg_set_quality(&cinfo, this->quality, TRUE);
    jpeg_start_compress(&cinfo, TRUE);

    std::vector<unsigned char> row(w * 3);
    while (cinfo.next_scanline < h)
    {
      const unsigned char *src = _src + (cinfo.next_scanline * s) * _w * _depth;
      if (s == 1)
      {
        std::memcpy(row.data(), src, w * 3);
      }
      else
      {
        // Nearest-neighbour. Box-filtering would look better but this runs on
        // the render thread, and the point of shrinking is to spend less here.
        for (unsigned int x = 0; x < w; ++x)
          std::memcpy(&row[x * 3], src + (x * s) * _depth, 3);
      }
      unsigned char *rp = row.data();
      jpeg_write_scanlines(&cinfo, &rp, 1);
    }

    jpeg_finish_compress(&cinfo);
    _out.assign(buf, buf + size);
    jpeg_destroy_compress(&cinfo);
    if (buf)
      free(buf);
    return true;
  }

  private: void Serve()
  {
    this->listenFd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (this->listenFd < 0)
    {
      gzerr << "[camera_stream] " << this->label << ": socket() failed\n";
      return;
    }
    int one = 1;
    ::setsockopt(this->listenFd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(this->port);
    if (::bind(this->listenFd, (sockaddr *)&addr, sizeof(addr)) < 0 ||
        ::listen(this->listenFd, 8) < 0)
    {
      gzerr << "[camera_stream] " << this->label << ": cannot bind :"
            << this->port << "\n";
      ::close(this->listenFd);
      this->listenFd = -1;
      return;
    }

    while (this->run)
    {
      int fd = ::accept(this->listenFd, nullptr, nullptr);
      if (fd < 0)
        break;
      std::thread(&CameraStreamPlugin::Client, this, fd).detach();
    }
  }

  private: static bool WriteAll(int _fd, const char *_p, size_t _n)
  {
    while (_n)
    {
      ssize_t k = ::send(_fd, _p, _n, MSG_NOSIGNAL);
      if (k <= 0)
        return false;
      _p += k;
      _n -= k;
    }
    return true;
  }

  private: void Client(int _fd)
  {
    int one = 1;
    ::setsockopt(_fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    char req[1024] = {0};
    ssize_t n = ::recv(_fd, req, sizeof(req) - 1, 0);
    if (n <= 0)
    {
      ::close(_fd);
      return;
    }
    std::string path = "/";
    {
      std::string r(req, n);
      size_t a = r.find(' ');
      size_t b = r.find(' ', a + 1);
      if (a != std::string::npos && b != std::string::npos)
        path = r.substr(a + 1, b - a - 1);
    }

    if (++this->clients == 1 && this->sleepIdle && this->parentSensor)
      this->parentSensor->SetActive(true);

    if (path.rfind("/snapshot", 0) == 0)
      this->SendSnapshot(_fd);
    else if (path == "/stream" || path.rfind("/stream", 0) == 0)
      this->SendStream(_fd);
    else
      this->SendIndex(_fd);

    if (--this->clients == 0 && this->sleepIdle && this->parentSensor)
      this->parentSensor->SetActive(false);
    ::close(_fd);
  }

  private: bool WaitFrame(unsigned long &_seen, std::vector<unsigned char> &_copy)
  {
    std::unique_lock<std::mutex> lk(this->frameMu);
    if (!this->frameCv.wait_for(lk, std::chrono::seconds(5),
          [&] { return !this->run || this->seq != _seen; }))
      return false;
    if (!this->run)
      return false;
    _seen = this->seq;
    _copy = this->frame;
    return !_copy.empty();
  }

  private: void SendSnapshot(int _fd)
  {
    unsigned long seen = 0;
    std::vector<unsigned char> jpg;
    if (!this->WaitFrame(seen, jpg))
    {
      const char *e = "HTTP/1.0 503 Service Unavailable\r\n"
                      "Content-Length: 0\r\n\r\n";
      WriteAll(_fd, e, std::strlen(e));
      return;
    }
    char hdr[256];
    int k = std::snprintf(hdr, sizeof(hdr),
        "HTTP/1.0 200 OK\r\nContent-Type: image/jpeg\r\n"
        "Cache-Control: no-store\r\nContent-Length: %zu\r\n"
        "Access-Control-Allow-Origin: *\r\n\r\n", jpg.size());
    if (WriteAll(_fd, hdr, k))
      WriteAll(_fd, (const char *)jpg.data(), jpg.size());
  }

  private: void SendStream(int _fd)
  {
    const char *hdr =
        "HTTP/1.0 200 OK\r\n"
        "Content-Type: multipart/x-mixed-replace; boundary=arcticframe\r\n"
        "Cache-Control: no-store\r\n"
        "Access-Control-Allow-Origin: *\r\n\r\n";
    if (!WriteAll(_fd, hdr, std::strlen(hdr)))
      return;

    unsigned long seen = 0;
    std::vector<unsigned char> jpg;
    while (this->run)
    {
      if (!this->WaitFrame(seen, jpg))
        continue;
      char part[192];
      int k = std::snprintf(part, sizeof(part),
          "--arcticframe\r\nContent-Type: image/jpeg\r\n"
          "Content-Length: %zu\r\n\r\n", jpg.size());
      if (!WriteAll(_fd, part, k))
        return;
      if (!WriteAll(_fd, (const char *)jpg.data(), jpg.size()))
        return;
      if (!WriteAll(_fd, "\r\n", 2))
        return;
    }
  }

  private: void SendIndex(int _fd)
  {
    std::string body =
        "<!doctype html><meta charset=utf-8><title>" + this->label +
        "</title><style>body{margin:0;background:#0b1216;color:#dfe8ec;"
        "font:13px ui-monospace,Menlo,monospace}h1{font-size:13px;padding:8px}"
        "img{display:block;max-width:100%;height:auto}</style><h1>" +
        this->label + "</h1><img src=\"/stream\">";
    char hdr[256];
    int k = std::snprintf(hdr, sizeof(hdr),
        "HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n"
        "Content-Length: %zu\r\n\r\n", body.size());
    if (WriteAll(_fd, hdr, k))
      WriteAll(_fd, body.data(), body.size());
  }

  private: int port{8600};
  private: int quality{70};
  private: unsigned int shrink{1};
  private: std::string label;
  private: bool sleepIdle{false};

  private: int listenFd{-1};
  private: std::atomic<bool> run{false};
  private: std::atomic<int> clients{0};
  private: std::thread acceptor;

  private: std::mutex frameMu;
  private: std::condition_variable frameCv;
  private: std::vector<unsigned char> frame;
  private: unsigned long seq{0};
};

GZ_REGISTER_SENSOR_PLUGIN(CameraStreamPlugin)
}  // namespace gazebo
