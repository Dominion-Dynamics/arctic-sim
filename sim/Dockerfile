# SIM CONTAINER — Gazebo Classic 11 + gzweb. No ROS.
#
# Focal is not nostalgia: OSRF only publishes Gazebo Classic 11 for focal, and
# only focal has arm64 binaries. Jammy and noble have the apt metadata but no
# gazebo11 packages for either architecture. Staying on focal is what makes this
# run natively on Apple Silicon instead of under emulation.
#
# ROS is deliberately absent. The only Gazebo plugins in play are
# libArduPilotPlugin.so and libLiftDragPlugin.so, both pure Gazebo Classic, so
# nothing here needs a ROS distribution. SITL talks to Gazebo over the plugin's
# own UDP channel (9002/9003), never through ROS.
FROM ubuntu:20.04

EXPOSE 8080 9002/udp 9003/udp 11345

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        lsb-release wget curl gnupg2 ca-certificates git \
        cmake build-essential rapidjson-dev libjansson-dev imagemagick \
        libboost-dev libtinyxml-dev python3 xvfb dos2unix net-tools \
        # x11-utils is xdpyinfo, which the entrypoint waits on to know the
        # display is live. Without it that loop can only ever time out, and the
        # wait degrades into a fixed sleep that happens to be long enough.
        x11-utils \
        # glxinfo, to report which GL implementation actually won at startup.
        mesa-utils \
        # libglvnd dispatches GL to whichever vendor the X server advertises.
        # gazebo11 pulls these in anyway; naming them here is what makes the
        # GPU path's requirement legible, since nvidia-container-toolkit mounts
        # libGLX_nvidia in alongside them and glvnd picks it over mesa.
        libglvnd0 libgl1 libglx0 libegl1 \
    && rm -rf /var/lib/apt/lists/*

# --- Gazebo Classic 11 (focal channel is multi-arch: amd64 + arm64) ---------
RUN wget -q https://packages.osrfoundation.org/gazebo.gpg \
        -O /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] http://packages.osrfoundation.org/gazebo/ubuntu-stable $(lsb_release -cs) main" \
        > /etc/apt/sources.list.d/gazebo-stable.list && \
    apt-get update && apt-get install -y --no-install-recommends \
        gazebo11 libgazebo11-dev \
    && rm -rf /var/lib/apt/lists/*

# --- ArduPilot Gazebo plugin ------------------------------------------------
WORKDIR /sim
RUN git clone --depth 1 https://github.com/nicholasaleks/ardupilot_gazebo.git && \
    cd ardupilot_gazebo && mkdir build && cd build && \
    cmake .. && make -j"$(nproc)"

# --- arctic-sim plugins -----------------------------------------------------
COPY sim/plugins /sim/plugins
RUN cd /sim/plugins && mkdir -p build && cd build && \
    cmake .. && make -j"$(nproc)"

ENV GAZEBO_MODEL_PATH=/sim/models:/usr/share/gazebo-11/models \
    GAZEBO_RESOURCE_PATH=/sim/worlds:/usr/share/gazebo-11 \
    GAZEBO_PLUGIN_PATH=/sim/plugins/build:/sim/ardupilot_gazebo/build:/usr/lib/aarch64-linux-gnu/gazebo-11/plugins:/usr/lib/x86_64-linux-gnu/gazebo-11/plugins \
    GAZEBO_MODEL_DATABASE_URI='' \
    DISPLAY=:100

# --- gzweb ------------------------------------------------------------------
# gzweb is a Node 10 / npm 6 era codebase; newer toolchains fail to build its
# native gzbridge addon.
RUN curl -fsSL -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.35.3/install.sh | bash \
    && export NVM_DIR="$HOME/.nvm" \
    && . "$NVM_DIR/nvm.sh" \
    && nvm install 10 && nvm use 10 && npm install -g npm@6

# gzweb's webify scripts have `#!/usr/bin/python` (Python 2) shebangs. Focal has
# no bare `python`, and without it deploy.sh silently skips asset generation and
# the world renders as wireframe-on-grid.
RUN ln -sf /usr/bin/python2.7 /usr/bin/python || true
RUN apt-get update && apt-get install -y --no-install-recommends python2 \
    && ln -sf /usr/bin/python2 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*

COPY sim/gzweb /sim/gzweb
WORKDIR /sim/gzweb
RUN find . -type f -exec dos2unix -q {} \; && chmod +x deploy.sh && \
    bash -lc 'export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh" && nvm use 10 && \
              npm install v8flags grunt-cli && npm install'

COPY sim/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN dos2unix -q /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /sim
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
